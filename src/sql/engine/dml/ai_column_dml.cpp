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
#include "ai_column_dml.h"
#include "sql/ob_sql_context.h"
#include "sql/engine/dml/ob_dml_service.h"
#include "sql/engine/expr/ob_expr_json_func_helper.h"
#include "sql/engine/expr/ob_expr_uuid.h"
#include "share/ai_service/ai_column_context.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "query/vector/ob_vector_index_util.h"
#include "lib/string/ob_sql_string.h"

namespace oceanbase
{
namespace sql
{
using namespace common;
using namespace share::schema;
namespace
{
int schema_for(const ObDMLBaseCtDef &ctdef, ObTableModifyOp &op, const ObTableSchema *&table)
{
  int ret = OB_SUCCESS;
  auto *sql_ctx = op.get_exec_ctx().get_sql_ctx();
  if (OB_ISNULL(sql_ctx) || OB_ISNULL(sql_ctx->schema_guard_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(sql_ctx->schema_guard_->get_table_schema(ctdef.das_base_ctdef_.index_tid_, table))) {
  } else if (OB_ISNULL(table)) {
    ret = OB_SCHEMA_ERROR;
  }
  return ret;
}

int column_index(const ObDMLBaseCtDef &ctdef, uint64_t id, int64_t &index)
{
  int ret = OB_SUCCESS;
  index = OB_INVALID_INDEX;
  for (int64_t i = 0; i < ctdef.column_ids_.count() && index == OB_INVALID_INDEX; ++i) {
    if (ctdef.column_ids_.at(i) == id) {
      index = i;
    }
  }
  if (index == OB_INVALID_INDEX) {
    ret = OB_ERR_UNEXPECTED;
  }
  return ret;
}

int changed(const ObDMLBaseCtDef &ctdef, ObEvalCtx &ctx, uint64_t id, bool &different)
{
  int ret = OB_SUCCESS;
  int64_t index = 0;
  ObDatum *old_value = nullptr;
  ObDatum *new_value = nullptr;
  different = false;
  if (OB_FAIL(column_index(ctdef, id, index))) {
  } else if (index >= ctdef.old_row_.count() || index >= ctdef.new_row_.count()) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ctdef.old_row_.at(index)->eval(ctx, old_value))) {
  } else if (OB_FAIL(ctdef.new_row_.at(index)->eval(ctx, new_value))) {
  } else if (old_value->is_null() || new_value->is_null()) {
    different = old_value->is_null() != new_value->is_null();
  } else if (is_lob_storage(ctdef.old_row_.at(index)->datum_meta_.type_)
             && is_lob_storage(ctdef.new_row_.at(index)->datum_meta_.type_)) {
    int64_t comparison = 0;
    if (OB_FAIL(ObDMLService::check_lob_column_changed(ctx, *ctdef.old_row_.at(index), *old_value,
                                                     *ctdef.new_row_.at(index), *new_value, comparison))) {
    } else {
      different = comparison != 0;
    }
  } else {
    different = !ObDatum::binary_equal(*old_value, *new_value);
  }
  return ret;
}

int set_value(const ObDMLBaseCtDef &ctdef, ObTableModifyOp &op, uint64_t id, const ObString *text)
{
  ObEvalCtx &ctx = op.get_eval_ctx();
  int ret = OB_SUCCESS;
  int64_t index = 0;
  if (OB_FAIL(column_index(ctdef, id, index))) {
  } else if (index >= ctdef.new_row_.count()) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    ObExpr &expr = *ctdef.new_row_.at(index);
    ObDatum &datum = expr.locate_datum_for_write(ctx);
    if (OB_ISNULL(text)) {
      datum.set_null();
    } else {
      char *copy = expr.get_str_res_mem(ctx, text->length());
      if (OB_ISNULL(copy)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        MEMCPY(copy, text->ptr(), text->length());
        datum.set_string(copy, text->length());
      }
    }
    if (OB_SUCC(ret)) {
      expr.set_evaluated_flag(ctx);
      op.clear_dml_evaluated_flag(expr.parent_cnt_, expr.parents_);
    }
  }
  return ret;
}

int identifier(ObSqlString &sql, const ObString &value)
{
  int ret = sql.append("`");
  for (int64_t i = 0; OB_SUCC(ret) && i < value.length(); ++i) {
    if (value.ptr()[i] == '`' && OB_FAIL(sql.append("`"))) {
    } else {
      ret = sql.append(value.ptr() + i, 1);
    }
  }
  if (OB_SUCC(ret)) {
    ret = sql.append("`");
  }
  return ret;
}

// Hex literals preserve quotes, backslashes, NUL and Unicode under any SQL mode.
int literal(ObSqlString &sql, const ObString &value, bool utf8 = true)
{
  static const char hex[] = "0123456789abcdef";
  int ret = sql.append(utf8 ? "CONVERT(X'" : "X'");
  for (int64_t i = 0; OB_SUCC(ret) && i < value.length(); ++i) {
    unsigned char byte = static_cast<unsigned char>(value.ptr()[i]);
    char encoded[] = {hex[byte >> 4], hex[byte & 15]};
    ret = sql.append(encoded, 2);
  }
  if (OB_SUCC(ret)) {
    ret = sql.append(utf8 ? "' USING utf8mb4)" : "'");
  }
  return ret;
}

int predicate(const ObDMLBaseCtDef &ctdef, const ObTableSchema &table, ObTableModifyOp &op,
               bool old, ObSqlString &sql)
{
  int ret = OB_SUCCESS;
  const auto &keys = table.get_rowkey_info();
  const auto &row = old ? ctdef.old_row_ : ctdef.new_row_;
  if (keys.get_size() == 0) {
    ret = OB_ERR_UNEXPECTED;
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < keys.get_size(); ++i) {
    uint64_t id = OB_INVALID_ID;
    int64_t index = 0;
    const ObColumnSchemaV2 *column = nullptr;
    ObDatum *datum = nullptr;
    if (OB_FAIL(keys.get_column_id(i, id))) {
    } else if (OB_ISNULL(column = table.get_column_schema(id))) {
      ret = OB_SCHEMA_ERROR;
    } else if (OB_FAIL(column_index(ctdef, id, index))) {
    } else if (index >= row.count()) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(row.at(index)->eval(op.get_eval_ctx(), datum))) {
    } else if (i > 0 && OB_FAIL(sql.append(" AND "))) {
    } else if (OB_FAIL(identifier(sql, column->get_column_name_str()))) {
    } else if (OB_FAIL(sql.append(" = "))) {
    } else if (datum->is_null()) {
      ret = sql.append("NULL");
    } else if (ob_is_string_type(row.at(index)->datum_meta_.type_)) {
      ret = literal(sql, datum->get_string(), false);
    } else {
      ObObj object;
      char *buffer = nullptr;
      int64_t length = 0;
      int64_t position = 0;
      if (OB_FAIL(datum->to_obj(object, row.at(index)->obj_meta_))) {
      } else if (OB_FAIL(object.print_sql_literal(buffer, length, position,
                      op.get_exec_ctx().get_allocator(), op.get_obj_print_params()))) {
      } else {
        ret = sql.append(buffer, position);
      }
    }
  }
  return ret;
}

int result_spec(const ObDMLBaseCtDef &ctdef, const ObColumnSchemaV2 &column,
                 ObTableModifyOp &op, MultimodeAlloctor &allocator, ObString &json)
{
  int ret = OB_SUCCESS;
  int64_t index = 0;
  ObString binary;
  bool is_null = false;
  ObIJsonBase *value = nullptr;
  if (OB_FAIL(column_index(ctdef, column.get_ai_result_spec_column_id(), index))) {
  } else if (index >= ctdef.new_row_.count()) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ObJsonExprHelper::get_json_or_str_data(
                  ctdef.new_row_.at(index), op.get_eval_ctx(), allocator, binary, is_null))) {
  } else if (is_null) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ObJsonBaseFactory::get_json_base(&allocator, binary,
                  ObJsonExprHelper::get_json_internal_type(ctdef.new_row_.at(index)->datum_meta_.type_),
                  ObJsonInType::JSON_TREE, value))) {
  } else if (OB_ISNULL(value) || value->json_type() != ObJsonNodeType::J_OBJECT) {
    ret = OB_INVALID_DATA;
  } else {
    ObJsonBuffer buffer(&allocator);
    if (OB_FAIL(value->print(buffer, 0))) {
    } else {
      ret = buffer.get_result_string(json);
    }
  }
  return ret;
}

int target_type(const ObColumnSchemaV2 &column, ObSqlString &type)
{
  int ret = OB_SUCCESS;
  if (column.is_json()) {
    ret = type.append("json");
  } else if (ob_is_integer_type(column.get_data_type())) {
    ret = type.append(ob_is_unsigned_type(column.get_data_type()) ? "unsigned" : "signed");
  } else if (column.is_collection()) {
    int64_t dimension = 0;
    if (OB_FAIL(share::ObVectorIndexUtil::get_vector_dim_from_extend_type_info(column.get_extended_type_info(), dimension))) {
    } else {
      ret = type.append_fmt("vector(%ld)", dimension);
    }
  } else {
    ret = type.append("char");
  }
  return ret;
}

int execute(ObTableModifyOp &op, const ObSqlString &sql)
{
  int ret = op.open_inner_conn();
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(op.begin_nested_session(false))) {
  } else {
    ret = op.execute_write(sql.ptr());
    int end_ret = op.end_nested_session();
    if (OB_SUCC(ret)) {
      ret = end_ret;
    }
  }
  return ret;
}

int delete_tasks(const ObDMLBaseCtDef &ctdef, ObTableModifyOp &op,
                   const ObString &row_predicate, uint64_t column_id = OB_INVALID_ID)
{
  ObSqlString sql;
  int ret = sql.append_fmt("DELETE FROM oceanbase.__all_ai_column_task WHERE table_id=%lu AND rowkey_predicate=",
                           ctdef.das_base_ctdef_.index_tid_);
  if (OB_SUCC(ret)) {
    ret = literal(sql, row_predicate);
  }
  if (OB_SUCC(ret) && column_id != OB_INVALID_ID) {
    ret = sql.append_fmt(" AND column_id=%lu", column_id);
  }
  if (OB_SUCC(ret)) {
    ret = execute(op, sql);
  }
  return ret;
}
} // namespace

int AiColumnDml::prepare(const ObDMLBaseCtDef &ctdef, ObTableModifyOp &op, bool update)
{
  int ret = OB_SUCCESS;
  if (!ctdef.has_ai_columns_ || share::AiColumnContext::is_worker_sql()) {
    return ret;
  }
  const ObTableSchema *table = nullptr;
  ObSEArray<const ObColumnSchemaV2 *, 4> affected;
  bool key_changed = false;
  if (OB_FAIL(schema_for(ctdef, op, table))) {
  }
  if (OB_SUCC(ret) && update) {
    const auto &keys = table->get_rowkey_info();
    for (int64_t i = 0; OB_SUCC(ret) && !key_changed && i < keys.get_size(); ++i) {
      uint64_t id = OB_INVALID_ID;
      if (OB_FAIL(keys.get_column_id(i, id))) {
      } else {
        ret = changed(ctdef, op.get_eval_ctx(), id, key_changed);
      }
    }
  }
  if (OB_SUCC(ret)) {
    for (auto it = table->column_begin(); OB_SUCC(ret) && it != table->column_end(); ++it) {
      const ObColumnSchemaV2 *column = *it;
      if (OB_ISNULL(column) || !column->is_ai_column()) {
        continue;
      }
      bool refresh = !update || key_changed || ctdef.ai_retry_column_id_ == column->get_column_id();
      const auto &dependencies = column->get_ai_dependency_column_ids();
      for (int64_t i = 0; OB_SUCC(ret) && !refresh && i < dependencies.count(); ++i) {
        ret = changed(ctdef, op.get_eval_ctx(), dependencies.at(i), refresh);
      }
      if (OB_SUCC(ret) && refresh) {
        ret = affected.push_back(column);
      }
    }
  }
  if (OB_SUCC(ret) && !affected.empty()) {
    char uuid[UuidCommon::LENGTH_UUID];
    ObString request(sizeof(uuid), uuid);
    ObString pending("PENDING");
    if (OB_FAIL(ObExprUuid::gen_server_uuid(uuid, sizeof(uuid)))) {
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < affected.count(); ++i) {
      const ObColumnSchemaV2 &column = *affected.at(i);
      if (OB_FAIL(set_value(ctdef, op, column.get_column_id(), nullptr))) {
      } else if (OB_FAIL(set_value(ctdef, op, column.get_ai_state_column_id(), &pending))) {
      } else if (OB_FAIL(set_value(ctdef, op, column.get_ai_request_column_id(), &request))) {
      }
    }
  }
  return ret;
}

int AiColumnDml::after_row(const ObDMLBaseCtDef &ctdef, ObTableModifyOp &op,
                           bool insert, bool remove)
{
  int ret = OB_SUCCESS;
  if (!ctdef.has_ai_columns_ || share::AiColumnContext::is_worker_sql()) {
    return ret;
  }
  const ObTableSchema *table = nullptr;
  const ObDatabaseSchema *database = nullptr;
  ObSqlString old_key;
  ObSqlString new_key;
  ObEvalCtx::TempAllocGuard guard(op.get_eval_ctx());
  MultimodeAlloctor allocator(guard.get_allocator());
  if (OB_FAIL(schema_for(ctdef, op, table))) {
  } else if (!insert && OB_FAIL(predicate(ctdef, *table, op, true, old_key))) {
  } else if (remove) {
    ret = delete_tasks(ctdef, op, old_key.string());
  } else if (OB_FAIL(predicate(ctdef, *table, op, false, new_key))) {
  } else if (OB_FAIL(op.get_exec_ctx().get_sql_ctx()->schema_guard_->get_database_schema(table->get_database_id(), database))) {
  } else if (OB_ISNULL(database)) {
    ret = OB_SCHEMA_ERROR;
  }
  if (OB_SUCC(ret) && !remove) {
    for (auto it = table->column_begin(); OB_SUCC(ret) && it != table->column_end(); ++it) {
      const ObColumnSchemaV2 *column = *it;
      if (OB_ISNULL(column) || !column->is_ai_column()) {
        continue;
      }
      bool refresh = insert;
      if (!insert && OB_FAIL(changed(ctdef, op.get_eval_ctx(), column->get_ai_request_column_id(), refresh))) {
      } else if (!refresh) {
        continue;
      } else {
        int64_t request_index = 0;
        ObDatum *request = nullptr;
        ObString spec;
        ObSqlString type;
        ObSqlString sql;
        if (OB_FAIL(column_index(ctdef, column->get_ai_request_column_id(), request_index))) {
        } else if (OB_FAIL(ctdef.new_row_.at(request_index)->eval(op.get_eval_ctx(), request))) {
        } else if (request->is_null()) {
          ret = OB_ERR_UNEXPECTED;
        } else if (OB_FAIL(result_spec(ctdef, *column, op, allocator, spec))) {
        } else if (OB_FAIL(target_type(*column, type))) {
        } else if (!insert && OB_FAIL(delete_tasks(ctdef, op, old_key.string(), column->get_column_id()))) {
        } else if (OB_FAIL(sql.append_fmt("INSERT INTO oceanbase.__all_ai_column_task "
            "(table_id,column_id,request_id,database_name,table_name,column_name,rowkey_predicate,"
            "input_json,target_type,definition_version,max_retries,state,retry_count,eligible_ts,"
            "lease_owner,lease_until,result_value) VALUES(%lu,%lu,",
            table->get_table_id(), column->get_column_id()))) {
        } else if (OB_FAIL(literal(sql, request->get_string()))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, database->get_database_name_str()))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, table->get_table_name_str()))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, column->get_column_name_str()))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, new_key.string()))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, spec))) {
        } else if (OB_FAIL(sql.append(","))) {
        } else if (OB_FAIL(literal(sql, type.string()))) {
        } else if (OB_FAIL(sql.append_fmt(",%ld,%ld,0,0,NOW(6),'',NULL,NULL) "
               "ON DUPLICATE KEY UPDATE request_id=VALUES(request_id)",
               column->get_ai_definition_version(), column->get_ai_max_retries()))) {
        } else {
          ret = execute(op, sql);
        }
      }
    }
  }
  return ret;
}
} // namespace sql
} // namespace oceanbase
