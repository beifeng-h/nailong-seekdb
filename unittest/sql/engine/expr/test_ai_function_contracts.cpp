/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */


#include <gtest/gtest.h>
#include <limits>
#include <string>
#include "lib/allocator/ob_allocator.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"

using namespace oceanbase::common;

namespace oceanbase
{
namespace sql
{
namespace
{
class AIFunctionContracts : public ::testing::Test
{
protected:
  int object(const char *json, ObJsonObject *&value)
  {
    ObString text(json);
    return ObAIFuncJsonUtils::get_json_object_form_str(allocator_, text, value);
  }
  void expect_nonempty_completion(ObAIFuncIComplete &provider,
                                 const char *empty_json, const char *space_json)
  {
    ObJsonObject *response = nullptr;
    ObIJsonBase *result = nullptr;
    ASSERT_EQ(OB_SUCCESS, object(empty_json, response));
    EXPECT_EQ(OB_INVALID_DATA, provider.parse_output(allocator_, response, result));

    response = nullptr;
    result = nullptr;
    ASSERT_EQ(OB_SUCCESS, object(space_json, response));
    ASSERT_EQ(OB_SUCCESS, provider.parse_output(allocator_, response, result));
    ASSERT_NE(nullptr, result);
    ASSERT_EQ(ObJsonNodeType::J_STRING, result->json_type());
    EXPECT_EQ(ObString(" "), static_cast<ObJsonString *>(result)->get_str());
  }
  ObArenaAllocator allocator_;
};

TEST_F(AIFunctionContracts, repeated_long_placeholder_does_not_underallocate)
{
  std::string large(8192, 'x');
  ObString argument(large.size(), large.data());
  ObString pattern("{0}/{0}/{0}");
  ObJsonArray *args = nullptr;
  ObJsonString *value = nullptr;
  ObJsonObject *prompt = nullptr;
  ObString rendered;
  ASSERT_EQ(OB_SUCCESS, ObAIFuncJsonUtils::get_json_array(allocator_, args));
  ASSERT_EQ(OB_SUCCESS, ObAIFuncJsonUtils::get_json_string(allocator_, argument, value));
  ASSERT_EQ(OB_SUCCESS, args->append(value));
  ASSERT_EQ(OB_SUCCESS, ObAIFuncPromptObjectUtils::construct_prompt_object(allocator_, pattern, args, prompt));
  ASSERT_EQ(OB_SUCCESS, ObAIFuncPromptObjectUtils::replace_all_str_args_in_template(allocator_, prompt, rendered));
  EXPECT_EQ(large + "/" + large + "/" + large, std::string(rendered.ptr(), rendered.length()));
}

TEST_F(AIFunctionContracts, nested_prompt_preserves_unicode_and_json)
{
  ObJsonObject *prompt = nullptr;
  ASSERT_EQ(OB_SUCCESS, object(
      R"json({"template":"外层[{0}]/{1}","args":[{"template":"内层({0})","args":["你好"]},{"k":true}]})json", prompt));
  ObString rendered;
  ASSERT_EQ(OB_SUCCESS, ObAIFuncPromptObjectUtils::replace_all_str_args_in_template(allocator_, prompt, rendered));
  EXPECT_EQ("外层[内层(你好)]/{\"k\": true}", std::string(rendered.ptr(), rendered.length()));
}

TEST_F(AIFunctionContracts, overflowing_numeric_placeholder_is_rejected)
{
  ObJsonObject *prompt = nullptr;
  ASSERT_EQ(OB_SUCCESS, object(
      R"json({"template":"{184467440737095516160}","args":["x"]})json", prompt));
  ObString rendered;
  EXPECT_EQ(OB_INVALID_ARGUMENT,
      ObAIFuncPromptObjectUtils::replace_all_str_args_in_template(allocator_, prompt, rendered));
}

TEST_F(AIFunctionContracts, embedding_response_index_restores_input_order)
{
  ObJsonObject *response = nullptr;
  ASSERT_EQ(OB_SUCCESS, object(
      R"json({"data":[{"index":1,"embedding":[3,4]},{"index":0,"embedding":[1,2]}]})json", response));
  ObOpenAIUtils::ObOpenAIEmbed provider;
  ObIJsonBase *result = nullptr;
  ASSERT_EQ(OB_SUCCESS, provider.parse_output(allocator_, response, result));
  ASSERT_NE(nullptr, result);
  ASSERT_EQ(ObJsonNodeType::J_ARRAY, result->json_type());
  auto *vectors = static_cast<ObJsonArray *>(result);
  ASSERT_EQ(2, vectors->element_count());
  EXPECT_EQ(1, static_cast<ObJsonArray *>(vectors->get_value(0))->get_value(0)->get_int());
  EXPECT_EQ(3, static_cast<ObJsonArray *>(vectors->get_value(1))->get_value(0)->get_int());
}

TEST_F(AIFunctionContracts, malformed_embedding_identity_and_values_fail)
{
  const char *invalid[] = {
    R"json({"data":{}})json",
    R"json({"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]})json",
    R"json({"data":[{"index":1,"embedding":[1]}]})json",
    R"json({"data":[{"index":0,"embedding":[true]}]})json",
    R"json({"data":[{"index":0,"embedding":[]}]})json",
    R"json({"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[1,2]}]})json"
  };
  ObOpenAIUtils::ObOpenAIEmbed provider;
  for (const char *json : invalid) {
    ObJsonObject *response = nullptr;
    ASSERT_EQ(OB_SUCCESS, object(json, response));
    ObIJsonBase *result = nullptr;
    EXPECT_EQ(OB_INVALID_DATA, provider.parse_output(allocator_, response, result)) << json;
  }
  ObJsonObject *response = nullptr;
  ASSERT_EQ(OB_SUCCESS, object(R"json({"data":[{"index":0,"embedding":[1.0]}]})json", response));
  auto *data = static_cast<ObJsonArray *>(response->get_value("data"));
  auto *row = static_cast<ObJsonObject *>(data->get_value(0));
  auto *vector = static_cast<ObJsonArray *>(row->get_value("embedding"));
  ObJsonDouble *infinite = OB_NEWx(ObJsonDouble, &allocator_, std::numeric_limits<double>::infinity());
  ASSERT_NE(nullptr, infinite);
  ASSERT_EQ(OB_SUCCESS, vector->append(infinite));
  ObIJsonBase *result = nullptr;
  EXPECT_EQ(OB_INVALID_DATA, provider.parse_output(allocator_, response, result));
}

TEST_F(AIFunctionContracts, ollama_generation_is_native_non_streaming_without_authentication)
{
  ObOllamaUtils::ObOllamaComplete provider;
  ObJsonObject *config = nullptr;
  ASSERT_EQ(OB_SUCCESS, object(R"json({"stream":true,"options":{"temperature":0.2}})json", config));
  ObString model("model");
  ObString system;
  ObString input("hello");
  ObString empty_key;
  ObArray<ObString> headers;
  ASSERT_EQ(OB_SUCCESS, provider.get_header(allocator_, empty_key, headers));
  ASSERT_EQ(1, headers.count());
  EXPECT_EQ(ObString("Content-Type: application/json"), headers.at(0));
  ObJsonObject *body = nullptr;
  ASSERT_EQ(OB_SUCCESS, provider.get_body(allocator_, model, system, input, config, body));
  ASSERT_NE(nullptr, body);
  ASSERT_NE(nullptr, body->get_value("stream"));
  EXPECT_FALSE(body->get_value("stream")->get_boolean());
  ASSERT_NE(nullptr, body->get_value("prompt"));
  EXPECT_EQ(input, static_cast<ObJsonString *>(body->get_value("prompt"))->get_str());
  EXPECT_NE(nullptr, body->get_value("options"));
}

TEST_F(AIFunctionContracts, openai_completion_rejects_empty_and_preserves_space)
{
  ObOpenAIUtils::ObOpenAIComplete provider;
  expect_nonempty_completion(provider,
      R"json({"choices":[{"message":{"content":""}}]})json",
      R"json({"choices":[{"message":{"content":" "}}]})json");
}

TEST_F(AIFunctionContracts, ollama_completion_rejects_empty_and_preserves_space)
{
  ObOllamaUtils::ObOllamaComplete provider;
  expect_nonempty_completion(provider, R"json({"response":""})json", R"json({"response":" "})json");
}

TEST_F(AIFunctionContracts, dashscope_completion_rejects_empty_and_preserves_space)
{
  ObDashscopeUtils::ObDashscopeComplete provider;
  expect_nonempty_completion(provider,
      R"json({"output":{"choices":[{"message":{"content":""}}]}})json",
      R"json({"output":{"choices":[{"message":{"content":" "}}]}})json");
}
} // namespace
} // namespace sql
} // namespace oceanbase
