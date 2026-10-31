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

#define USING_LOG_PREFIX SERVER_OMT
#include "observer/ai_column/ai_column_task_service.h"
#include "observer/ai_service/ob_ai_service_executor.h"
#include "common/mysqlclient/ob_mysql_proxy.h"
#include "common/mysqlclient/ob_mysql_transaction.h"
#include "lib/thread/ob_thread_name.h"
#include "lib/time/ob_time_utility.h"
#include "lib/utility/utility.h"
#include "share/ob_server_struct.h"
#include "share/ai_service/ai_column_context.h"
#include "share/object/ob_obj_cast.h"
#include "share/schema/ob_multi_version_schema_service.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "share/rc/ob_server_runtime.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"
#include <cerrno>
#include <cmath>
#include <cstdlib>

namespace oceanbase
{
namespace observer
{
using namespace common;
using namespace sqlclient;

namespace
{
const char *const TASK_TABLE = "oceanbase.__all_ai_column_task";
const int64_t LEASE_SECONDS = 60;

int append_identifier(ObSqlString &sql, const ObString &name)
{
  int ret = sql.append("`");
  for (int64_t i = 0; OB_SUCC(ret) && i < name.length(); ++i) {
    if ('`' == name.ptr()[i] && OB_FAIL(sql.append("`"))) {
    } else {
      ret = sql.append(name.ptr() + i, 1);
    }
  }
  if (OB_SUCC(ret)) {
    ret = sql.append("`");
  }
  return ret;
}

int copy_string(ObIAllocator &allocator, ObMySQLResult &result, const char *name,
                ObString &value, bool nullable = false, bool *is_null = nullptr)
{
  int ret = OB_SUCCESS;
  ObObj object;
  ObString tmp;
  if (nullptr != is_null) {
    *is_null = false;
  }
  if (OB_FAIL(result.get_obj(name, object))) {
  } else if (object.is_null()) {
    if (!nullable) {
      ret = OB_ERR_NULL_VALUE;
    } else {
      value.reset();
      if (nullptr != is_null) {
        *is_null = true;
      }
    }
  } else if (OB_FAIL(result.get_varchar(name, tmp))) {
  } else {
    ret = ob_write_string(allocator, tmp, value, true);
  }
  return ret;
}

template <typename TaskT>
int append_group(ObSqlString &sql, const TaskT &task)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(sql.append_fmt("table_id = %ld AND request_id = ",
                            task.table_id_))) {
  } else {
    ret = sql_append_hex_escape_str(task.request_id_, sql);
  }
  return ret;
}

template <typename TaskT>
int check_published_table(const TaskT &task)
{
  int ret = OB_SUCCESS;
  share::schema::ObSchemaGetterGuard guard;
  const share::schema::ObTableSchema *table = nullptr;
  const share::schema::ObDatabaseSchema *database = nullptr;
  if (OB_ISNULL(GCTX.schema_service_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(GCTX.schema_service_->get_runtime_schema_guard(guard))) {
  } else if (OB_FAIL(guard.get_table_schema(task.table_id_, table))) {
  } else if (OB_ISNULL(table) || table->is_user_hidden_table()
             || table->get_table_name_str() != task.table_name_) {
    ret = OB_EAGAIN;
  } else if (OB_FAIL(guard.get_database_schema(table->get_database_id(), database))) {
  } else if (OB_ISNULL(database) || database->get_database_name_str() != task.database_name_) {
    ret = OB_EAGAIN;
  }
  // A swap commits its outbox before publishing its schema. Wait for the
  // public name to resolve to this table id before calling a model or checking
  // row tokens. DROP removes its tasks in the metadata DDL transaction.
  return ret;
}

template <typename TaskT>
int load_tasks(ObISQLClient &client, ObIAllocator &allocator, const ObSqlString &sql,
               ObArray<TaskT> &tasks)
{
  int ret = OB_SUCCESS;
  SMART_VAR(ObMySQLProxy::MySQLResult, res) {
    ObMySQLResult *result = nullptr;
    if (OB_FAIL(client.read(res, sql.ptr()))) {
    } else if (OB_ISNULL(result = res.get_result())) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      while (OB_SUCC(ret) && OB_SUCC(ret = result->next())) {
        TaskT task;
        if (OB_FAIL(result->get_int("table_id", task.table_id_))) {
        } else if (OB_FAIL(result->get_int("column_id", task.column_id_))) {
        } else if (OB_FAIL(result->get_int("definition_version", task.definition_version_))) {
        } else if (OB_FAIL(result->get_int("max_retries", task.max_retries_))) {
        } else if (OB_FAIL(result->get_int("retry_count", task.retry_count_))) {
        } else if (OB_FAIL(result->get_int("attempt_count", task.attempt_count_))) {
        } else if (OB_FAIL(result->get_int("state", task.state_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "request_id", task.request_id_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "database_name", task.database_name_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "table_name", task.table_name_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "column_name", task.column_name_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "rowkey_predicate", task.rowkey_predicate_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "input_json", task.input_json_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "target_type", task.target_type_))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "result_value", task.result_value_,
                                      true, &task.result_null_))) {
        } else {
          ret = tasks.push_back(task);
        }
      }
      if (OB_ITER_END == ret) {
        ret = OB_SUCCESS;
      }
    }
  }
  return ret;
}

int get_json_string(ObJsonObject &object, const char *key, ObString &value, bool optional = false)
{
  int ret = OB_SUCCESS;
  ObJsonNode *node = object.get_value(key);
  if (optional && (OB_ISNULL(node) || node->json_type() == ObJsonNodeType::J_NULL)) {
    value.reset();
  } else if (OB_ISNULL(node) || node->json_type() != ObJsonNodeType::J_STRING) {
    ret = OB_INVALID_DATA;
  } else {
    value = static_cast<ObJsonString *>(node)->get_str();
  }
  return ret;
}

int validate_value(ObIAllocator &allocator, const ObString &type, const ObString &value,
                   int64_t dimension)
{
  int ret = OB_SUCCESS;
  const bool is_json = type.case_compare("json") == 0;
  const bool is_vector = type.length() >= 6 && ObString(6, type.ptr()).case_compare("vector") == 0;
  if (is_json || is_vector) {
    ObIJsonBase *parsed = nullptr;
    if (OB_FAIL(ObJsonBaseFactory::get_json_base(&allocator, value,
                ObJsonInType::JSON_TREE, ObJsonInType::JSON_TREE, parsed))) {
    } else if (OB_ISNULL(parsed)) {
      ret = OB_INVALID_DATA;
    } else if (is_vector) {
      if (parsed->json_type() != ObJsonNodeType::J_ARRAY) {
        ret = OB_INVALID_DATA;
      } else {
        ObJsonArray *array = static_cast<ObJsonArray *>(parsed);
        if (dimension <= 0 || array->element_count() != dimension) {
          ret = OB_INVALID_DATA;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < array->element_count(); ++i) {
          ObJsonNode *number = array->get_value(i);
          double number_value = 0;
          if (OB_ISNULL(number) || !number->is_number()
              || OB_FAIL(number->to_double(number_value)) || !std::isfinite(number_value)
              || !std::isfinite(static_cast<float>(number_value))) {
            ret = OB_INVALID_DATA;
          }
        }
      }
    }
  } else if (type.case_compare("signed") == 0 || type.case_compare("unsigned") == 0) {
    ObString terminated;
    if (OB_FAIL(ob_write_string(allocator, value, terminated, true))) {
    } else {
      const char *start = terminated.ptr();
      char *end = nullptr;
      errno = 0;
      if (type.case_compare("unsigned") == 0) {
        while (*start == ' ' || *start == '\n' || *start == '\t' || *start == '\r') { ++start; }
        if (*start == '-') {
          ret = OB_INVALID_DATA;
        } else {
          IGNORE_RETURN std::strtoull(start, &end, 10);
        }
      } else {
        IGNORE_RETURN std::strtoll(start, &end, 10);
      }
      if (OB_SUCC(ret)) {
        if (ERANGE == errno || nullptr == end || end == start) {
          ret = OB_INVALID_DATA;
        } else {
          while (*end == ' ' || *end == '\n' || *end == '\t' || *end == '\r') { ++end; }
          if (*end != '\0') {
            ret = OB_INVALID_DATA;
          }
        }
      }
    }
  }
  return ret;
}

template <typename TaskT>
int validate_column_value(ObIAllocator &allocator, const TaskT &task)
{
  int ret = OB_SUCCESS;
  share::schema::ObSchemaGetterGuard guard;
  const share::schema::ObTableSchema *table = nullptr;
  const share::schema::ObColumnSchemaV2 *column = nullptr;
  if (OB_ISNULL(GCTX.schema_service_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(GCTX.schema_service_->get_runtime_schema_guard(guard))) {
  } else if (OB_FAIL(guard.get_table_schema(task.table_id_, table))) {
  } else if (OB_ISNULL(table) || OB_ISNULL(column = table->get_column_schema(task.column_id_))) {
    ret = OB_TABLE_NOT_EXIST;
  } else if (column->get_data_type() != ObCollectionSQLType) {
    ObObj input;
    ObObj converted;
    ObObj checked_buffer;
    const ObObj *converted_value = nullptr;
    const ObObj *checked = nullptr;
    input.set_varchar(task.result_value_);
    input.set_collation_type(CS_TYPE_UTF8MB4_BIN);
    ObAccuracy accuracy = column->get_accuracy();
    ObCastCtx context(&allocator, nullptr, CM_NONE, column->get_collation_type(), &accuracy);
    ObExpectType target(column->get_data_type(), column->get_collation_type());
    target.set_type_infos(&column->get_extended_type_info());
    if (OB_FAIL(ObObjCaster::to_type(target, context, input, converted, converted_value))) {
    } else if (OB_ISNULL(converted_value)) {
      ret = OB_INVALID_DATA;
    } else if (FALSE_IT(checked = converted_value)) {
    } else if (OB_FAIL(obj_accuracy_check(context, accuracy,
                   column->get_collation_type(), *converted_value, checked_buffer, checked))) {
    } else if (context.warning_ != OB_SUCCESS || OB_ISNULL(checked) || checked->is_null()) {
      ret = OB_INVALID_DATA;
    } else if ((checked->is_float() && !std::isfinite(checked->get_float()))
               || (checked->is_double() && !std::isfinite(checked->get_double()))) {
      ret = OB_INVALID_DATA;
    }
  }
  return ret;
}
} // namespace

struct AiColumnTaskService::Task
{
  int64_t table_id_ = 0;
  int64_t column_id_ = 0;
  int64_t definition_version_ = 0;
  int64_t max_retries_ = 0;
  int64_t retry_count_ = 0;
  int64_t attempt_count_ = 0;
  int64_t state_ = 0;
  bool result_null_ = true;
  ObString request_id_;
  ObString database_name_;
  ObString table_name_;
  ObString column_name_;
  ObString rowkey_predicate_;
  ObString input_json_;
  ObString target_type_;
  ObString result_value_;
};

AiColumnTaskService::AiColumnTaskService() : share::ObThreadPool(4), is_inited_(false) {}
AiColumnTaskService::~AiColumnTaskService() { destroy(); }

int AiColumnTaskService::init()
{
  int ret = OB_SUCCESS;
  if (is_inited_) {
    ret = OB_INIT_TWICE;
  } else {
    set_run_wrapper(share::server_runtime());
    if (OB_SUCC(ret = share::ObThreadPool::init())) {
      is_inited_ = true;
    }
  }
  return ret;
}

int AiColumnTaskService::start()
{
  return is_inited_ ? share::ObThreadPool::start() : OB_NOT_INIT;
}
void AiColumnTaskService::stop() { share::ObThreadPool::stop(); }
void AiColumnTaskService::wait() { share::ObThreadPool::wait(); }
void AiColumnTaskService::destroy()
{
  if (is_inited_) {
    stop();
    wait();
    share::ObThreadPool::destroy();
    is_inited_ = false;
  }
}

void AiColumnTaskService::run1()
{
  lib::set_thread_name("AIColumn");
  share::AiColumnContext::WorkerScope guard;
  ObSqlString owner;
  int ret = owner.assign_fmt("%s-%ld-%lu", to_cstring(GCTX.self_addr()),
                            ObTimeUtility::current_time(), get_thread_idx());
  while (OB_SUCC(ret) && !has_set_stop()) {
    bool found = false;
    if (OB_ISNULL(GCTX.sql_proxy_) || GCTX.in_bootstrap_ || GCTX.start_service_time_ <= 0
        || OB_ISNULL(GCTX.schema_service_)) {
      usleep(100000);
    } else {
      THIS_WORKER.set_timeout_ts(ObTimeUtility::current_time() + 120 * 1000000L);
      int tmp_ret = process_one_group(owner.string(), found);
      if (OB_SUCCESS != tmp_ret && REACH_TIME_INTERVAL(10000000)) {
        LOG_WARN("AI column task iteration failed", K(tmp_ret));
      }
      if (OB_SUCCESS != tmp_ret || !found) {
        usleep(100000);
      }
    }
  }
}

int AiColumnTaskService::claim_group(ObIAllocator &allocator, const ObString &owner,
                                     ObArray<Task> &tasks, bool &found)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  ObArray<Task> candidates;
  found = false;
  if (OB_FAIL(sql.assign_fmt("SELECT t.* FROM %s t "
      "WHERE (t.eligible_ts IS NULL OR t.eligible_ts <= NOW(6)) "
      "AND (t.lease_until IS NULL OR t.lease_until < NOW(6)) "
      "AND NOT EXISTS (SELECT 1 FROM %s waiting WHERE waiting.table_id = t.table_id AND waiting.request_id = t.request_id "
      "AND waiting.state = 3 AND waiting.eligible_ts > NOW(6)) "
      "ORDER BY t.eligible_ts, t.request_id, t.column_id LIMIT 1", TASK_TABLE, TASK_TABLE))) {
  } else if (OB_FAIL(load_tasks(*GCTX.sql_proxy_, allocator, sql, candidates))) {
  } else if (!candidates.empty()) {
    const Task &first = candidates.at(0);
    int64_t affected_rows = 0;
    sql.reuse();
    if (OB_FAIL(check_published_table(first))) {
    } else if (OB_FAIL(sql.append_fmt("UPDATE %s SET lease_owner = ", TASK_TABLE))) {
    } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
    } else if (OB_FAIL(sql.append_fmt(
        ", lease_until = DATE_ADD(NOW(6), INTERVAL %ld SECOND), "
        "state = CASE WHEN state IN (0,1,3) THEN 1 ELSE state END WHERE ", LEASE_SECONDS))) {
    } else if (OB_FAIL(append_group(sql, first))) {
    } else if (OB_FAIL(sql.append(" AND (lease_until IS NULL OR lease_until < NOW(6))"))) {
    } else if (OB_FAIL(GCTX.sql_proxy_->write(sql.ptr(), affected_rows))) {
    } else if (affected_rows > 0) {
      sql.reuse();
      if (OB_FAIL(sql.append_fmt("SELECT * FROM %s WHERE ", TASK_TABLE))) {
      } else if (OB_FAIL(append_group(sql, first))) {
      } else if (OB_FAIL(sql.append(" AND lease_owner = "))) {
      } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
      } else if (OB_FAIL(sql.append(" ORDER BY column_id"))) {
      } else if (OB_FAIL(load_tasks(*GCTX.sql_proxy_, allocator, sql, tasks))) {
      } else {
        found = !tasks.empty();
      }
    }
  }
  return ret;
}

int AiColumnTaskService::execute_task(ObArenaAllocator &allocator, Task &task)
{
  int ret = OB_SUCCESS;
  ObJsonObject *spec = nullptr;
  ObString kind;
  ObString model_name;
  ObString input;
  ObString system;
  ObJsonObject *config = nullptr;
  ObAIFuncExprInfo *info = nullptr;
  share::ObAiModelEndpointInfo endpoint;
  int64_t dimension = 0;
  if (OB_FAIL(ObAIFuncJsonUtils::get_json_object_form_str(allocator, task.input_json_, spec))) {
  } else if (OB_FAIL(get_json_string(*spec, "kind", kind))) {
  } else if (OB_FAIL(get_json_string(*spec, "model", model_name))) {
  } else if (OB_FAIL(get_json_string(*spec, "system", system, true))) {
  } else {
    ObJsonNode *input_node = spec->get_value("input");
    ObJsonNode *config_node = spec->get_value("config");
    ObJsonNode *dimension_node = spec->get_value("dimension");
    if (OB_ISNULL(input_node) || input_node->json_type() == ObJsonNodeType::J_NULL) {
      task.result_null_ = true;
      return OB_SUCCESS;
    } else if (input_node->json_type() == ObJsonNodeType::J_STRING) {
      input = static_cast<ObJsonString *>(input_node)->get_str();
    } else if (kind.case_compare("completion") == 0
               && input_node->json_type() == ObJsonNodeType::J_OBJECT
               && ObAIFuncPromptObjectUtils::is_valid_prompt_object(static_cast<ObJsonObject *>(input_node))) {
      ret = ObAIFuncPromptObjectUtils::replace_all_str_args_in_template(
          allocator, static_cast<ObJsonObject *>(input_node), input);
    } else {
      ret = OB_INVALID_DATA;
    }
    if (OB_SUCC(ret) && OB_NOT_NULL(config_node) && config_node->json_type() != ObJsonNodeType::J_NULL) {
      if (config_node->json_type() != ObJsonNodeType::J_OBJECT) {
        ret = OB_INVALID_DATA;
      } else {
        config = static_cast<ObJsonObject *>(config_node);
      }
    }
    if (OB_SUCC(ret) && OB_NOT_NULL(dimension_node) && dimension_node->json_type() != ObJsonNodeType::J_NULL) {
      if ((dimension_node->json_type() != ObJsonNodeType::J_INT
           && dimension_node->json_type() != ObJsonNodeType::J_UINT)
          || OB_FAIL(dimension_node->to_int(dimension, true)) || dimension <= 0) {
        ret = OB_INVALID_DATA;
      }
    }
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(allocator, model_name, info))) {
  } else if (OB_FAIL(share::ObAiServiceExecutor::read_ai_endpoint_by_ai_model_name(
               allocator, model_name, endpoint))) {
  } else {
    ObAIFuncModel model(allocator, *info, endpoint);
    if (kind.case_compare("completion") == 0) {
      if (OB_FAIL(ObAIFuncUtils::check_info_type_completion(info))) {
      } else if (OB_FAIL(model.call_completion_with_system(system, input, config, task.result_value_))) {
      } else if (task.result_value_.empty()) {
        ret = OB_INVALID_DATA;
      } else {
        task.result_null_ = false;
      }
    } else if (kind.case_compare("embedding") == 0) {
      ObJsonInt *dim = nullptr;
      if (OB_FAIL(ObAIFuncUtils::check_info_type_dense_embedding(info))) {
      } else if (dimension <= 0) {
        ret = OB_INVALID_DATA;
      } else if (OB_ISNULL(config) && OB_FAIL(ObAIFuncJsonUtils::get_json_object(allocator, config))) {
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(allocator, dimension, dim))) {
      } else if (OB_FAIL(config->add("dimensions", dim))) {
      } else if (OB_FAIL(model.call_dense_embedding(input, config, task.result_value_))) {
      } else {
        task.result_null_ = false;
      }
    } else {
      ret = OB_INVALID_DATA;
    }
  }
  if (OB_SUCC(ret) && !task.result_null_) {
    ret = validate_value(allocator, task.target_type_, task.result_value_, dimension);
  }
  if (OB_SUCC(ret) && !task.result_null_) {
    ret = validate_column_value(allocator, task);
  }
  return ret;
}

int AiColumnTaskService::save_task_result(const Task &task, const ObString &owner, int execution_ret)
{
  int ret = OB_SUCCESS;
  const bool success = OB_SUCCESS == execution_ret;
  const bool retry = !success && task.attempt_count_ <= task.max_retries_;
  ObSqlString sql;
  int64_t affected_rows = 0;
  if (OB_FAIL(sql.append_fmt("UPDATE %s SET state = %d, retry_count = %ld, last_error = %d, "
      "eligible_ts = DATE_ADD(NOW(6), INTERVAL %d SECOND), result_value = ", TASK_TABLE,
      success ? 2 : (retry ? 3 : 4), task.retry_count_ + (retry ? 1 : 0), execution_ret,
      retry ? 1 : 0))) {
  } else if (success && !task.result_null_) {
    ret = sql_append_hex_escape_str(task.result_value_, sql);
  } else {
    ret = sql.append("NULL");
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(sql.append(" WHERE "))) {
  } else if (OB_FAIL(append_group(sql, task))) {
  } else if (OB_FAIL(sql.append_fmt(" AND column_id = %ld AND state = 1 AND lease_until > NOW(6) "
                                  "AND lease_owner = ", task.column_id_))) {
  } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
  } else if (OB_FAIL(GCTX.sql_proxy_->write(sql.ptr(), affected_rows))) {
  } else if (affected_rows != 1) {
    ret = OB_EAGAIN;
  }
  return ret;
}

int AiColumnTaskService::process_one_group(const ObString &owner, bool &found)
{
  int ret = OB_SUCCESS;
  ObArenaAllocator allocator("AIColumnTask");
  ObArray<Task> tasks;
  if (OB_FAIL(claim_group(allocator, owner, tasks, found))) {
  } else if (found) {
    for (int64_t i = 0; OB_SUCC(ret) && !has_set_stop() && i < tasks.count(); ++i) {
      Task &task = tasks.at(i);
      if (OB_FAIL(check_published_table(task))) {
      } else if (task.state_ == 1) {
        THIS_WORKER.set_timeout_ts(ObTimeUtility::current_time() + LEASE_SECONDS * 1000000L);
        ObSqlString renew;
        int64_t affected_rows = 0;
        if (OB_FAIL(renew.append_fmt("UPDATE %s SET lease_until = DATE_ADD(NOW(6), "
                                    "INTERVAL %ld SECOND) WHERE ", TASK_TABLE, LEASE_SECONDS))) {
        } else if (OB_FAIL(append_group(renew, task))) {
        } else if (OB_FAIL(renew.append(" AND lease_until > NOW(6) AND lease_owner = "))) {
        } else if (OB_FAIL(sql_append_hex_escape_str(owner, renew))) {
        } else if (OB_FAIL(GCTX.sql_proxy_->write(renew.ptr(), affected_rows))) {
        } else if (affected_rows == 0) {
          ret = OB_EAGAIN;
        } else if (task.attempt_count_ > task.max_retries_) {
          // An interrupted attempt may have reached the model. Its durable
          // budget cannot be silently reset by process recovery.
          ret = save_task_result(task, owner, OB_TIMEOUT);
        } else {
          renew.reuse();
          if (OB_FAIL(renew.append_fmt("UPDATE %s SET attempt_count = attempt_count + 1 WHERE ",
                                      TASK_TABLE))) {
          } else if (OB_FAIL(append_group(renew, task))) {
          } else if (OB_FAIL(renew.append_fmt(" AND column_id = %ld AND state = 1 "
              "AND attempt_count <= max_retries AND lease_until > NOW(6) AND lease_owner = ", task.column_id_))) {
          } else if (OB_FAIL(sql_append_hex_escape_str(owner, renew))) {
          } else if (OB_FAIL(GCTX.sql_proxy_->write(renew.ptr(), affected_rows))) {
          } else if (affected_rows != 1) {
            ret = OB_EAGAIN;
          } else {
            ++task.attempt_count_;
            const int execution_ret = execute_task(allocator, task);
            ret = save_task_result(task, owner, execution_ret);
          }
        }
      }
    }
    if (OB_SUCC(ret) && !has_set_stop()) {
      THIS_WORKER.set_timeout_ts(ObTimeUtility::current_time() + LEASE_SECONDS * 1000000L);
      ret = publish_group(allocator, tasks, owner);
    }
  }
  return ret;
}

int AiColumnTaskService::publish_group(ObIAllocator &allocator, const ObArray<Task> &original_tasks,
                                       const ObString &owner)
{
  int ret = OB_SUCCESS;
  if (original_tasks.empty()) {
    return ret;
  }
  ObMySQLTransaction trans;
  ObArray<Task> tasks;
  ObSqlString sql;
  const Task &first = original_tasks.at(0);
  bool terminal = true;
  bool obsolete = false;
  if (OB_FAIL(check_published_table(first))) {
  } else if (OB_FAIL(trans.start(GCTX.sql_proxy_))) {
  } else if (OB_FAIL(sql.append_fmt("SELECT * FROM %s WHERE ", TASK_TABLE))) {
  } else if (OB_FAIL(append_group(sql, first))) {
  } else if (OB_FAIL(sql.append(" AND lease_until > NOW(6) AND lease_owner = "))) {
  } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
  } else if (OB_FAIL(sql.append(" ORDER BY column_id"))) {
  } else if (OB_FAIL(load_tasks(trans, allocator, sql, tasks))) {
  } else if (tasks.count() != original_tasks.count()) {
    ret = OB_EAGAIN;
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
    terminal = terminal && (tasks.at(i).state_ == 2 || tasks.at(i).state_ == 4);
  }
  ObArray<ObString> state_names;
  ObArray<ObString> request_names;
  for (int64_t i = 0; OB_SUCC(ret) && terminal && i < tasks.count(); ++i) {
    const Task &task = tasks.at(i);
    sql.reuse();
    if (OB_FAIL(sql.append_fmt("SELECT a.definition_version, s.column_name AS state_name, "
        "r.column_name AS request_name FROM oceanbase.__all_ai_column a "
        "JOIN oceanbase.__all_column s ON s.table_id = a.table_id AND s.column_id = a.state_column_id "
        "JOIN oceanbase.__all_column r ON r.table_id = a.table_id AND r.column_id = a.request_column_id "
        "WHERE a.table_id = %ld AND a.column_id = %ld",
        task.table_id_, task.column_id_))) {
    } else {
      SMART_VAR(ObMySQLProxy::MySQLResult, res) {
        ObMySQLResult *result = nullptr;
        ObString state_name;
        ObString request_name;
        int64_t version = 0;
        if (OB_FAIL(trans.read(res, sql.ptr()))) {
        } else if (OB_ISNULL(result = res.get_result())) {
          ret = OB_ERR_UNEXPECTED;
        } else if (OB_FAIL(result->next())) {
          if (OB_ITER_END == ret) {
            ret = OB_SUCCESS;
            obsolete = true;
          }
        } else if (OB_FAIL(result->get_int("definition_version", version))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "state_name", state_name))) {
        } else if (OB_FAIL(copy_string(allocator, *result, "request_name", request_name))) {
        } else if (OB_FAIL(state_names.push_back(state_name))) {
        } else if (OB_FAIL(request_names.push_back(request_name))) {
        } else if (version != task.definition_version_) {
          obsolete = true;
        }
      }
    }
  }
  // Lock the business row, then compare every group token before publication.
  if (OB_SUCC(ret) && terminal && !obsolete) {
    sql.reuse();
    if (OB_FAIL(sql.append("SELECT "))) {
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < request_names.count(); ++i) {
      if (i > 0 && OB_FAIL(sql.append(","))) {
      } else {
        ret = append_identifier(sql, request_names.at(i));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(sql.append(" FROM "))) {
    } else if (OB_FAIL(append_identifier(sql, first.database_name_))) {
    } else if (OB_FAIL(sql.append("."))) {
    } else if (OB_FAIL(append_identifier(sql, first.table_name_))) {
    } else if (OB_FAIL(sql.append_fmt(" WHERE %.*s FOR UPDATE", first.rowkey_predicate_.length(),
                                    first.rowkey_predicate_.ptr()))) {
    } else {
      SMART_VAR(ObMySQLProxy::MySQLResult, res) {
        ObMySQLResult *result = nullptr;
        if (OB_FAIL(trans.read(res, sql.ptr()))) {
          if (ret == OB_TABLE_NOT_EXIST || ret == OB_ERR_BAD_DATABASE) {
            ret = OB_SUCCESS;
            obsolete = true;
          }
        } else if (OB_ISNULL(result = res.get_result())) {
          ret = OB_ERR_UNEXPECTED;
        } else if (OB_FAIL(result->next())) {
          if (OB_ITER_END == ret) {
            ret = OB_SUCCESS;
            obsolete = true;
          }
        } else {
          for (int64_t i = 0; OB_SUCC(ret) && !obsolete && i < request_names.count(); ++i) {
            ObString token;
            const int get_ret = result->get_varchar(i, token);
            if (get_ret == OB_ERR_NULL_VALUE) {
              obsolete = true;
            } else if (OB_SUCCESS != get_ret) {
              ret = get_ret;
            } else if (token != first.request_id_) {
              obsolete = true;
            }
          }
        }
      }
    }
  }
  if (OB_SUCC(ret) && terminal && !obsolete) {
    // Follow the business DML lock order: target row first, outbox rows second.
    // Recheck task ownership and definition under locks after the row was locked.
    ObArray<Task> locked_tasks;
    sql.reuse();
    if (OB_FAIL(sql.append_fmt("SELECT * FROM %s WHERE ", TASK_TABLE))) {
    } else if (OB_FAIL(append_group(sql, first))) {
    } else if (OB_FAIL(sql.append(" AND lease_until > NOW(6) AND lease_owner = "))) {
    } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
    } else if (OB_FAIL(sql.append(" ORDER BY column_id FOR UPDATE"))) {
    } else if (OB_FAIL(load_tasks(trans, allocator, sql, locked_tasks))) {
    } else if (locked_tasks.count() != tasks.count()) {
      ret = OB_EAGAIN;
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < locked_tasks.count(); ++i) {
      const Task &task = locked_tasks.at(i);
      if (task.state_ != 2 && task.state_ != 4) {
        ret = OB_EAGAIN;
      } else {
        sql.reuse();
        if (OB_FAIL(sql.append_fmt("SELECT definition_version FROM oceanbase.__all_ai_column "
            "WHERE table_id = %ld AND column_id = %ld FOR UPDATE",
            task.table_id_, task.column_id_))) {
        } else {
          SMART_VAR(ObMySQLProxy::MySQLResult, res) {
            ObMySQLResult *result = nullptr;
            int64_t version = 0;
            if (OB_FAIL(trans.read(res, sql.ptr()))) {
            } else if (OB_ISNULL(result = res.get_result())) {
              ret = OB_ERR_UNEXPECTED;
            } else if (OB_FAIL(result->next())) {
              if (OB_ITER_END == ret) {
                ret = OB_SUCCESS;
                obsolete = true;
              }
            } else if (OB_FAIL(result->get_int("definition_version", version))) {
            } else if (version != task.definition_version_) {
              obsolete = true;
            }
          }
        }
      }
    }
  }
  if (OB_SUCC(ret) && terminal && !obsolete) {
    sql.reuse();
    if (OB_FAIL(sql.append("UPDATE "))) {
    } else if (OB_FAIL(append_identifier(sql, first.database_name_))) {
    } else if (OB_FAIL(sql.append("."))) {
    } else if (OB_FAIL(append_identifier(sql, first.table_name_))) {
    } else if (OB_FAIL(sql.append(" SET "))) {
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
      const Task &task = tasks.at(i);
      if (i > 0 && OB_FAIL(sql.append(","))) {
      } else if (OB_FAIL(append_identifier(sql, task.column_name_))) {
      } else if (OB_FAIL(sql.append(" = "))) {
      } else if (task.state_ == 4 || task.result_null_) {
        ret = sql.append("NULL");
      } else if (OB_FAIL(sql.append("CAST("))) {
      } else if (OB_FAIL(sql_append_hex_escape_str(task.result_value_, sql))) {
      } else {
        ret = sql.append_fmt(" AS %.*s)", task.target_type_.length(), task.target_type_.ptr());
      }
      if (OB_FAIL(ret)) {
      } else if (OB_FAIL(sql.append(","))) {
      } else if (OB_FAIL(append_identifier(sql, state_names.at(i)))) {
      } else {
        ret = sql.append_fmt(" = '%s'", task.state_ == 2 ? "READY" : "FAILED");
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(sql.append_fmt(" WHERE %.*s", first.rowkey_predicate_.length(),
                                    first.rowkey_predicate_.ptr()))) {
    }
    int64_t affected_rows = 0;
    if (OB_SUCC(ret)) {
      ret = trans.write(sql.ptr(), affected_rows);
    }
  }
  if (OB_SUCC(ret)) {
    sql.reuse();
    if (OB_FAIL(sql.append_fmt(terminal ? "DELETE FROM %s WHERE " :
        "UPDATE %s SET lease_owner = '', lease_until = NULL WHERE ", TASK_TABLE))) {
    } else if (OB_FAIL(append_group(sql, first))) {
    } else if (OB_FAIL(sql.append(" AND lease_owner = "))) {
    } else if (OB_FAIL(sql_append_hex_escape_str(owner, sql))) {
    } else {
      int64_t affected_rows = 0;
      ret = trans.write(sql.ptr(), affected_rows);
    }
  }
  if (trans.is_started()) {
    const int end_ret = trans.end(OB_SUCCESS == ret);
    if (OB_SUCC(ret)) {
      ret = end_ret;
    }
  }
  return ret;
}

} // namespace observer
} // namespace oceanbase
