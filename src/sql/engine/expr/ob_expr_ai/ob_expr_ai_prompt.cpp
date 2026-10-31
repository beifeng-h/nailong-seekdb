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
#include "ob_expr_ai_prompt.h"
#include "ob_ai_func.h"
#include "lib/utility/utility.h"
#include "common/json_type/ob_json_common.h"


using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace oceanbase 
{
namespace sql 
{

ObExprAIPrompt::ObExprAIPrompt(common::ObIAllocator &alloc)
    : ObFuncExprOperator(alloc, 
                    T_FUN_SYS_AI_PROMPT, 
                    N_AI_PROMPT, 
                    MORE_THAN_ZERO,
                    VALID_FOR_GENERATED_COL, 
                    NOT_ROW_DIMENSION) 
{
}

ObExprAIPrompt::~ObExprAIPrompt() 
{
}

int ObExprAIPrompt::calc_result_typeN(ObExprResType &type,
                                    ObExprResType *types_stack,
                                    int64_t param_num,
                                    common::ObExprTypeCtx &type_ctx) const
{
  UNUSED(type_ctx);
  int ret = param_num > 0 ? OB_SUCCESS : OB_ERR_PARAM_SIZE;
  for (int64_t i = 0; OB_SUCC(ret) && i < param_num; ++i) {
    if (ob_is_string_type(types_stack[i].get_type())) {
      types_stack[i].set_calc_collation_type(CS_TYPE_UTF8MB4_BIN);
    } else if (i > 0 && ob_is_json(types_stack[i].get_type())) {
    } else {
      ret = OB_ERR_INVALID_TYPE_FOR_OP;
    }
  }
  if (OB_SUCC(ret)) {
    type.set_json();
    type.set_length(ObAccuracy::DDL_DEFAULT_ACCURACY[ObJsonType].get_length());
  }
  return ret;
}

int ObExprAIPrompt::eval_ai_prompt(const ObExpr &expr, ObEvalCtx &ctx, ObDatum &res)
{
  INIT_SUCC(ret);
  ObEvalCtx::TempAllocGuard guard(ctx);
  MultimodeAlloctor allocator(guard.get_allocator());
  ObDatum *datum = nullptr;
  ObString template_str;
  ObJsonArray *args = nullptr;
  ObJsonObject *prompt = nullptr;
  if (OB_FAIL(expr.args_[0]->eval(ctx, datum))) {
  } else if (datum->is_null()) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ObTextStringHelper::read_real_string_data(ctx.exec_ctx_, allocator, *datum,
                 expr.args_[0]->datum_meta_, expr.args_[0]->obj_meta_.has_lob_header(), template_str))) {
  } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_array(allocator, args))) {
  }
  for (int64_t i = 1; OB_SUCC(ret) && i < expr.arg_cnt_; ++i) {
    ObExpr &arg = *expr.args_[i];
    if (OB_FAIL(arg.eval(ctx, datum))) {
    } else if (datum->is_null()) {
      ret = OB_INVALID_ARGUMENT;
    } else if (arg.datum_meta_.type_ == ObJsonType) {
      ObIJsonBase *value = nullptr;
      bool is_null = false;
      if (OB_FAIL(ObJsonExprHelper::get_json_doc(expr, ctx, allocator, i, value, is_null))) {
      } else if (OB_ISNULL(value)) {
        ret = OB_INVALID_ARGUMENT;
      } else if (OB_FAIL(args->append(static_cast<ObJsonNode *>(value)))) {
      }
    } else {
      ObString value;
      ObJsonString *string_node = nullptr;
      if (OB_FAIL(ObTextStringHelper::read_real_string_data(ctx.exec_ctx_, allocator, *datum,
                     arg.datum_meta_, arg.obj_meta_.has_lob_header(), value))) {
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_string(allocator, value, string_node))) {
      } else if (OB_FAIL(args->append(string_node))) {
      }
    }
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(ObAIFuncPromptObjectUtils::construct_prompt_object(allocator, template_str, args, prompt))) {
  } else {
    ObString binary;
    if (OB_FAIL(prompt->get_raw_binary(binary, &allocator))) {
    } else if (OB_FAIL(ObJsonExprHelper::pack_json_str_res(expr, ctx, res, binary))) {
    }
  }
  return ret;
}



int ObExprAIPrompt::cg_expr(ObExprCGCtx &expr_cg_ctx, 
                        const ObRawExpr &raw_expr,
                        ObExpr &rt_expr) const 
{
  UNUSED(expr_cg_ctx);
  UNUSED(raw_expr);
  int ret = OB_SUCCESS;
  rt_expr.eval_func_ = ObExprAIPrompt::eval_ai_prompt;
  return ret;
}

} // namespace sql
} // namespace oceanbase
