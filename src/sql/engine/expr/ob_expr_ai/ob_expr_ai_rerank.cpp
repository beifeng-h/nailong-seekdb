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

#define USING_LOG_PREFIX SQL_ENG
#include "ob_expr_ai_rerank.h"
#include "query/ai/ob_ai_endpoint_resolver.h"
#include "share/rc/ob_server_runtime.h"
#include "common/json_type/ob_json_common.h"
#include <algorithm>
#include <cmath>

using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace oceanbase
{
namespace sql
{
namespace
{
constexpr int64_t RERANK_BATCH_SIZE = 20;

struct RerankHit
{
  int64_t index_;
  double score_;
  ObJsonNode *score_node_;
};

int collect_rerank_batch(ObJsonArray *response, const int64_t batch_size,
                         const int64_t offset, ObArray<RerankHit> &hits)
{
  int ret = OB_SUCCESS;
  bool seen[RERANK_BATCH_SIZE] = {};
  if (OB_ISNULL(response) || response->element_count() != batch_size) {
    ret = OB_INVALID_DATA;
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < batch_size; ++i) {
    ObJsonNode *node = response->get_value(i);
    if (OB_ISNULL(node) || node->json_type() != ObJsonNodeType::J_OBJECT) {
      ret = OB_INVALID_DATA;
    } else {
      ObJsonObject *obj = static_cast<ObJsonObject *>(node);
      ObJsonNode *index_node = obj->get_value("index");
      ObJsonNode *score_node = obj->get_value("relevance_score");
      int64_t index = -1;
      double score = 0;
      if (OB_ISNULL(index_node) || OB_ISNULL(score_node)) {
        ret = OB_INVALID_DATA;
      } else if (index_node->json_type() == ObJsonNodeType::J_INT) {
        index = index_node->get_int();
      } else if (index_node->json_type() == ObJsonNodeType::J_UINT
                 && index_node->get_uint() < static_cast<uint64_t>(batch_size)) {
        index = static_cast<int64_t>(index_node->get_uint());
      } else {
        ret = OB_INVALID_DATA;
      }
      if (OB_FAIL(ret)) {
      } else if (index < 0 || index >= batch_size || seen[index]) {
        ret = OB_INVALID_DATA;
      } else if (score_node->json_type() != ObJsonNodeType::J_INT
                 && score_node->json_type() != ObJsonNodeType::J_UINT
                 && score_node->json_type() != ObJsonNodeType::J_DOUBLE
                 && score_node->json_type() != ObJsonNodeType::J_DECIMAL) {
        ret = OB_INVALID_DATA;
      } else if (OB_FAIL(score_node->to_double(score))) {
        ret = OB_INVALID_DATA;
      } else if (!std::isfinite(score)) {
        ret = OB_INVALID_DATA;
      } else {
        seen[index] = true;
        RerankHit hit = {offset + index, score, score_node};
        if (OB_FAIL(hits.push_back(hit))) {
        }
      }
    }
  }
  return ret;
}

int rerank_documents(ObIAllocator &allocator, const ObAIFuncExprInfo &info,
                       const share::ObAiModelEndpointInfo &endpoint,
                       ObString &query, ObJsonArray *documents,
                       const bool object_documents, ObString &doc_key,
                       ObJsonArray *&result)
{
  int ret = OB_SUCCESS;
  ObArray<RerankHit> hits;
  const int64_t count = documents->element_count();
  // Validate all inputs before performing any external request.
  for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
    ObJsonNode *node = documents->get_value(i);
    if (OB_ISNULL(node)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (object_documents) {
      if (node->json_type() != ObJsonNodeType::J_OBJECT) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        ObJsonNode *text = static_cast<ObJsonObject *>(node)->get_value(doc_key);
        if (OB_ISNULL(text) || text->json_type() != ObJsonNodeType::J_STRING) {
          ret = OB_INVALID_ARGUMENT;
        }
      }
    } else if (node->json_type() != ObJsonNodeType::J_STRING) {
      ret = OB_INVALID_ARGUMENT;
    }
  }
  for (int64_t offset = 0; OB_SUCC(ret) && offset < count; offset += RERANK_BATCH_SIZE) {
    const int64_t size = std::min(RERANK_BATCH_SIZE, count - offset);
    ObJsonArray *batch = nullptr;
    ObJsonArray *response = nullptr;
    if (OB_FAIL(ObAIFuncJsonUtils::get_json_array(allocator, batch))) {
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
      ObJsonNode *node = documents->get_value(offset + i);
      if (object_documents) {
        node = static_cast<ObJsonObject *>(node)->get_value(doc_key);
      }
      ObJsonNode *text = node->clone(&allocator, true);
      if (OB_ISNULL(text)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (OB_FAIL(batch->append(text))) {
      }
    }
    if (OB_FAIL(ret)) {
    } else {
      ObAIFuncModel model(allocator, info, endpoint);
      if (OB_FAIL(model.call_rerank(query, batch, response))) {
      } else if (OB_ISNULL(response) || response->json_type() != ObJsonNodeType::J_ARRAY) {
        ret = OB_INVALID_DATA;
      } else if (OB_FAIL(collect_rerank_batch(response, size, offset, hits))) {
      }
    }
  }
  if (OB_SUCC(ret)) {
    std::sort(hits.begin(), hits.end(), [](const RerankHit &lhs, const RerankHit &rhs) {
      return lhs.score_ != rhs.score_ ? lhs.score_ > rhs.score_ : lhs.index_ < rhs.index_;
    });
    ret = ObAIFuncJsonUtils::get_json_array(allocator, result);
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < hits.count(); ++i) {
    const RerankHit &hit = hits.at(i);
    ObJsonObject *item = nullptr;
    if (object_documents) {
      ObJsonNode *original = documents->get_value(hit.index_);
      item = static_cast<ObJsonObject *>(original->clone(&allocator, true));
      if (OB_ISNULL(item)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (OB_FAIL(item->add("_model_score", hit.score_node_))) {
      }
    } else {
      ObJsonInt *index_node = nullptr;
      if (OB_FAIL(ObAIFuncJsonUtils::get_json_object(allocator, item))) {
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(allocator, hit.index_, index_node))) {
      } else if (OB_FAIL(item->add("index", index_node))) {
      } else if (OB_FAIL(item->add("relevance_score", hit.score_node_))) {
      }
    }
    if (OB_SUCC(ret) && OB_FAIL(result->append(item))) {
    }
  }
  return ret;
}
} // namespace

ObExprAIRerank::ObExprAIRerank(ObIAllocator &alloc)
    : ObFuncExprOperator(alloc, T_FUN_SYS_AI_RERANK, N_AI_RERANK,
                         MORE_THAN_ZERO, NOT_VALID_FOR_GENERATED_COL, NOT_ROW_DIMENSION)
{
}

ObExprAIRerank::~ObExprAIRerank()
{
}

int ObExprAIRerank::calc_result_typeN(ObExprResType &type, ObExprResType *types,
                                      int64_t param_num, ObExprTypeCtx &type_ctx) const
{
  UNUSED(type_ctx);
  int ret = OB_SUCCESS;
  if (param_num < 3 || param_num > 4) {
    ret = OB_ERR_PARAM_SIZE;
    ObString name(get_name());
    LOG_USER_ERROR(OB_ERR_PARAM_SIZE, name.length(), name.ptr());
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < 2; ++i) {
      if (!ob_is_string_type(types[i].get_type()) && types[i].get_type() != ObNullType) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        types[i].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
      }
    }
    if (OB_SUCC(ret)) {
      ret = ObJsonExprHelper::is_valid_for_json(types, DOCUMENTS_IDX, N_AI_RERANK);
    }
    if (OB_SUCC(ret) && param_num == 4) {
      if (types[DOC_KEY_IDX].get_type() != ObNullType
          && !ob_is_string_type(types[DOC_KEY_IDX].get_type())) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        types[DOC_KEY_IDX].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
      }
    }
    if (OB_SUCC(ret)) {
      type.set_json();
      type.set_length(ObAccuracy::DDL_DEFAULT_ACCURACY[ObJsonType].get_length());
    }
  }
  return ret;
}

int ObExprAIRerank::eval_ai_rerank(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res)
{
  int ret = OB_SUCCESS;
  ObDatum *model_datum = nullptr;
  ObDatum *query_datum = nullptr;
  ObDatum *documents_datum = nullptr;
  ObDatum *key_datum = nullptr;
  if (OB_FAIL(expr.eval_param_value(ctx, model_datum, query_datum, documents_datum, key_datum))) {
  } else if (model_datum->is_null() || query_datum->is_null() || documents_datum->is_null()) {
    ret = OB_INVALID_ARGUMENT;
    res.set_null();
  } else {
    ObEvalCtx::TempAllocGuard guard(ctx);
    MultimodeAlloctor allocator(guard.get_allocator());
    lib::ObMallocHookAttrGuard malloc_guard(lib::ObMemAttr(N_AI_RERANK));
    ObString model_id;
    ObString query;
    ObString doc_key;
    const bool object_documents = OB_NOT_NULL(key_datum) && !key_datum->is_null();
    ObIJsonBase *json = nullptr;
    ObJsonArray *result = nullptr;
    bool is_null = false;
    ObAIFuncExprInfo *info = nullptr;
    share::ObAiModelEndpointInfo endpoint;
    query::ObIAiEndpointResolver *resolver =
        ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
    if (OB_FAIL(ObTextStringHelper::read_real_string_data(ctx.exec_ctx_, allocator, *model_datum,
          expr.args_[MODEL_IDX]->datum_meta_, expr.args_[MODEL_IDX]->obj_meta_.has_lob_header(), model_id))) {
    } else if (OB_FAIL(ObTextStringHelper::read_real_string_data(ctx.exec_ctx_, allocator, *query_datum,
          expr.args_[QUERY_IDX]->datum_meta_, expr.args_[QUERY_IDX]->obj_meta_.has_lob_header(), query))) {
    } else if (object_documents && OB_FAIL(ObTextStringHelper::read_real_string_data(
          ctx.exec_ctx_, allocator, *key_datum, expr.args_[DOC_KEY_IDX]->datum_meta_,
          expr.args_[DOC_KEY_IDX]->obj_meta_.has_lob_header(), doc_key))) {
    } else if (model_id.empty() || query.empty() || (object_documents && doc_key.empty())) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_FAIL(ObJsonExprHelper::get_json_doc(expr, ctx, allocator, DOCUMENTS_IDX, json, is_null))) {
    } else if (OB_ISNULL(json) || json->json_type() != ObJsonNodeType::J_ARRAY) {
      ret = OB_INVALID_ARGUMENT;
    } else if (json->element_count() == 0) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(allocator, model_id, info))) {
    } else if (OB_ISNULL(info) || OB_ISNULL(resolver)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(ObAIFuncUtils::check_info_type_rerank(info))) {
    } else if (OB_FAIL(resolver->resolve_by_model_name(model_id, allocator, endpoint))) {
    } else if (OB_FAIL(rerank_documents(allocator, *info, endpoint, query,
          static_cast<ObJsonArray *>(json), object_documents, doc_key, result))) {
    } else {
      ObString binary;
      if (OB_FAIL(result->get_raw_binary(binary, &allocator))) {
      } else if (OB_FAIL(ObJsonExprHelper::pack_json_str_res(expr, ctx, res, binary))) {
      }
    }
  }
  return ret;
}

int ObExprAIRerank::cg_expr(ObExprCGCtx &expr_cg_ctx, const ObRawExpr &raw_expr,
                            ObExpr &rt_expr) const
{
  UNUSED(expr_cg_ctx);
  UNUSED(raw_expr);
  rt_expr.eval_func_ = ObExprAIRerank::eval_ai_rerank;
  return OB_SUCCESS;
}
} // namespace sql
} // namespace oceanbase
