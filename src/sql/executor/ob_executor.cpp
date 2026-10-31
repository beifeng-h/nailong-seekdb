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

#define USING_LOG_PREFIX SQL_EXE

#include "ob_executor.h"
#include "query/session/ob_inner_sql_connection_access.h"
#include "share/ai_service/ai_column_context.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "sql/engine/ob_exec_context.h"
#include "sql/engine/ob_physical_plan.h"
#include "sql/ob_sql.h"
#include "sql/ob_result_set.h"
#include "sql/session/ob_inner_sql_connection.h"
#include "lib/string/ob_sql_string.h"
using namespace oceanbase::common;
using namespace oceanbase::sql;

namespace
{
thread_local bool checking_ai_column_state = false;

class AiColumnReadExecutor final : public sqlclient::ObIExecutor
{
public:
  AiColumnReadExecutor(const ObString &sql, const oceanbase::transaction::ObTxReadSnapshot &snapshot,
      bool &unready) : sql_(sql), snapshot_(snapshot), unready_(unready) {}

  int execute(ObSql &engine, ObSqlCtx &ctx, ObResultSet &result) override
  {
    int ret = OB_SUCCESS;
    ObString sql;
    if (!snapshot_.is_valid() || snapshot_.is_none_read()) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(ob_write_string(result.get_mem_pool(), sql_, sql, true))) {
    } else {
      result.get_session().store_query_string(sql);
      if (OB_FAIL(engine.stmt_query(sql, ctx, result))) {
      } else {
        // Preparation may clear execution context fields, so install this only
        // after preparation and before the inner connection opens the result.
        ctx.ai_column_read_snapshot_ = &snapshot_;
      }
    }
    return ret;
  }

  int process_result(ObResultSet &result) override
  {
    // InnerSQLResult::open has already prefetched the first SELECT row. The
    // probe has LIMIT 1; fetching again would discard the only matching row.
    unready_ = result.get_return_rows() > 0;
    return OB_SUCCESS;
  }

private:
  ObString sql_;
  const oceanbase::transaction::ObTxReadSnapshot &snapshot_;
  bool &unready_;
};

int append_ai_identifier(ObSqlString &sql, const ObString &identifier)
{
  int ret = sql.append("`");
  for (int64_t i = 0; OB_SUCC(ret) && i < identifier.length(); ++i) {
    if (identifier.ptr()[i] == '`' && OB_FAIL(sql.append("`"))) {
    } else if (OB_SUCC(ret) && OB_FAIL(sql.append(identifier.ptr() + i, 1))) {
    }
  }
  if (OB_SUCC(ret)) {
    ret = sql.append("`");
  }
  return ret;
}

int has_unready_ai_rows(ObExecContext &ctx, const char *sql, bool &unready)
{
  int ret = OB_SUCCESS;
  unready = false;
  ObSQLSessionInfo *session = ctx.get_my_session();
  oceanbase::common::sqlclient::ObISQLConnectionGuard guard;
  ObIInnerSQLConnection *connection = nullptr;
  bool owns_connection = false;
  if (OB_ISNULL(session)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (session->get_inner_conn() == nullptr) {
    if (OB_FAIL(oceanbase::query::ObInnerSQLConnectionAccess::
        create_connection_with_external_session(session, guard))) {
    } else if (guard.get_ptr() == nullptr) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      session->set_inner_conn(guard.get_ptr());
      owns_connection = true;
    }
  }
  if (OB_SUCC(ret) && OB_ISNULL(connection = as_inner_sql_connection(session->get_inner_conn()))) {
    ret = OB_ERR_UNEXPECTED;
  }
  if (OB_SUCC(ret)) {
    SMART_VAR(ObSQLSessionInfo::StmtSavedValue, saved_session) {
      ObIInnerSQLConnection::SavedValue saved_connection;
      if (OB_FAIL(connection->begin_nested_session(saved_session, saved_connection, true))) {
      } else {
        checking_ai_column_state = true;
        AiColumnReadExecutor executor(ObString::make_string(sql), ctx.get_das_ctx().get_snapshot(), unready);
        ret = connection->execute(executor);
        const int restore_ret = connection->end_nested_session(saved_session, saved_connection);
        checking_ai_column_state = false;
        if (OB_SUCC(ret)) {
          ret = restore_ret;
        }
      }
    }
  }
  if (owns_connection) {
    session->set_inner_conn(nullptr);
    guard.reset();
  }
  return ret;
}

int check_ai_column_strict_read(ObExecContext &ctx, const ObPhysicalPlan &plan)
{
  int ret = OB_SUCCESS;
  bool strict = false;
  const ObIArray<uint64_t> &references = plan.get_ai_column_references();
  if (checking_ai_column_state || oceanbase::share::AiColumnContext::is_worker_sql()
      || (ctx.get_my_session() != nullptr && ctx.get_my_session()->is_inner()
          && ctx.get_my_session()->get_ddl_info().is_ddl())
      || plan.get_stmt_type() == oceanbase::sql::stmt::T_EXPLAIN
      || references.empty()) {
  } else if (ctx.get_my_session() == nullptr || ctx.get_sql_ctx() == nullptr
      || ctx.get_sql_ctx()->schema_guard_ == nullptr) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ctx.get_my_session()->get_sys_variable(
      oceanbase::share::SYS_VAR_AI_COLUMN_STRICT_READ, strict))) {
  } else if (strict) {
    using namespace oceanbase::share::schema;
    ObSchemaGetterGuard &schema = *ctx.get_sql_ctx()->schema_guard_;
    if (references.count() % 2 != 0) {
      ret = OB_ERR_UNEXPECTED;
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < references.count(); i += 2) {
      const uint64_t table_id = references.at(i);
      const uint64_t column_id = references.at(i + 1);
      const ObTableSchema *table = nullptr;
      const ObDatabaseSchema *database = nullptr;
      const ObColumnSchemaV2 *column = nullptr;
      const ObColumnSchemaV2 *state = nullptr;
      ObSqlString sql;
      bool unready = false;
      if (OB_FAIL(schema.get_table_schema(table_id, table))) {
      } else if (OB_ISNULL(table)) {
        ret = OB_SCHEMA_ERROR;
      } else if (OB_ISNULL(column = table->get_column_schema(column_id))) {
        ret = OB_SCHEMA_ERROR;
      } else if (!column->is_ai_column()) {
      } else if (OB_ISNULL(state = table->get_column_schema(column->get_ai_state_column_id()))) {
        ret = OB_SCHEMA_ERROR;
      } else if (OB_FAIL(schema.get_database_schema(table->get_database_id(), database))) {
      } else if (OB_ISNULL(database)) {
        ret = OB_SCHEMA_ERROR;
      } else if (OB_FAIL(sql.append("SELECT 1 FROM "))
          || OB_FAIL(append_ai_identifier(sql, database->get_database_name_str()))
          || OB_FAIL(sql.append("."))
          || OB_FAIL(append_ai_identifier(sql, table->get_table_name_str()))
          || OB_FAIL(sql.append(" WHERE "))
          || OB_FAIL(append_ai_identifier(sql, state->get_column_name_str()))
          || OB_FAIL(sql.append(" IS NULL OR "))
          || OB_FAIL(append_ai_identifier(sql, state->get_column_name_str()))
          || OB_FAIL(sql.append(" <> 'READY' LIMIT 1"))) {
      } else if (OB_FAIL(has_unready_ai_rows(ctx, sql.ptr(), unready))) {
      } else if (unready) {
        ret = OB_AI_COLUMN_NOT_READY;
        LOG_USER_ERROR(OB_AI_COLUMN_NOT_READY, table_id, column_id);
      }
    }
  }
  return ret;
}
}

int ObExecutor::init(ObPhysicalPlan *plan)
{
  int ret = OB_SUCCESS;
  if (true == inited_) {
    ret = OB_INIT_TWICE;
  } else if (OB_ISNULL(plan)) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    phy_plan_ = plan;
    inited_ = true;
  }
  return ret;
}

void ObExecutor::reset()
{
  inited_ = false;
  phy_plan_ = NULL;
  execution_id_ = OB_INVALID_ID;
}

int ObExecutor::execute_plan(ObExecContext &ctx)
{
  NG_TRACE(exec_plan_begin);
  int ret = OB_SUCCESS;
  ObSqlExecutorCtx &task_exec_ctx = ctx.get_sql_exec_ctx();
  ObExecuteResult &exec_result = task_exec_ctx.get_execute_result();
  ObSQLSessionInfo *session_info = ctx.get_my_session();
  ObPhysicalPlanCtx *plan_ctx = ctx.get_physical_plan_ctx();
  int64_t batched_stmt_cnt = ctx.get_sql_ctx()->multi_stmt_item_.get_batched_stmt_cnt();
  ctx.set_use_temp_expr_ctx_cache(true);
  // If the batch execution is rewritten by insert multi values, there is no need to repack multiple times
  if (ctx.get_sql_ctx()->is_do_insert_batch_opt()) {
    batched_stmt_cnt = 0;
  }
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else if (OB_ISNULL(session_info)) {
    ret = OB_NOT_INIT;
  } else if (OB_ISNULL(phy_plan_)) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(check_ai_column_strict_read(ctx, *phy_plan_))) {
  } else if (OB_FAIL(session_info->set_cur_phy_plan(phy_plan_))) {
  } else if (OB_FAIL(phy_plan_->get_expr_frame_info()
                                 .pre_alloc_exec_memory(ctx))) {
  } else if (batched_stmt_cnt > 0
      && OB_FAIL(plan_ctx->create_implicit_cursor_infos(batched_stmt_cnt))) {
  } else {
    ObPhyPlanType execute_type = phy_plan_->get_plan_type();
    // Some parallel plans do not require the PX scheduler.
    if (execute_type != OB_PHY_PLAN_LOCAL && phy_plan_->is_require_local_execution()) {
      execute_type = OB_PHY_PLAN_LOCAL;
      LOG_TRACE("change the plan execution type",
          "fact", execute_type, K(phy_plan_->get_plan_type()));
    }

    switch (execute_type) {
      case OB_PHY_PLAN_LOCAL: {
        ObOperator *op = NULL;
        if (OB_FAIL(phy_plan_->get_root_op_spec()->create_operator(ctx, op))) {
        } else if (OB_ISNULL(op)) {
          ret = OB_ERR_UNEXPECTED;
        } else {
          exec_result.set_static_engine_root(op);
        }
        break;
      }
      case OB_PHY_PLAN_DISTRIBUTED:
        // PX special path
        // PX mode, scheduling work is handled by the ObPxCoord operator
        ret = execute_static_cg_px_plan(ctx);
        break;
      default:
        ret = OB_ERR_UNEXPECTED;
        break;
    }
  }
  NG_TRACE(exec_plan_end);
  return ret;
}

int ObExecutor::execute_static_cg_px_plan(ObExecContext &ctx)
{
  int ret = OB_SUCCESS;
  ObOperator *op = NULL;
  if (OB_FAIL(phy_plan_->get_root_op_spec()->create_op_input(ctx))) {
  } else if (OB_FAIL(phy_plan_->get_root_op_spec()->create_operator(ctx, op))) {
  } else if (OB_ISNULL(op)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    ctx.get_sql_executor_ctx()
        ->get_execute_result()
        .set_static_engine_root(op);
  }
  return ret;
}

int ObExecutor::close(ObExecContext &ctx)
{
  // close function should be designed to be callable at any time, therefore regardless of the value of inited_
  int ret = OB_SUCCESS;
  ObSQLSessionInfo *session_info = ctx.get_my_session();
  if (OB_LIKELY(NULL != session_info)) {
    // Reset cur_phy_plan_ in session to NULL
    session_info->reset_cur_phy_plan_to_null();
  }
  return ret;
}
