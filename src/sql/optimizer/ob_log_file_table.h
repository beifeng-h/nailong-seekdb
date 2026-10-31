/**
 * Copyright (c) 2026 OceanBase.
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

/**
 * The logical operator of a table that reads a local file.
 *
 * The operator holds the description of the file, the columns of the table
 * are the columns of the file.
 */

#ifndef _OB_LOG_FILE_TABLE_H
#define _OB_LOG_FILE_TABLE_H
#include "sql/optimizer/ob_logical_operator.h"
#include "sql/resolver/dml/ob_dml_stmt.h"

namespace oceanbase
{
namespace sql
{
class ObLogFileTable : public ObLogicalOperator
{
public:
  ObLogFileTable(ObLogPlan &plan)
      : ObLogicalOperator(plan),
        table_id_(OB_INVALID_ID),
        table_name_(),
        access_exprs_(),
        file_table_def_(NULL)
  { }
  virtual ~ObLogFileTable() {}
  virtual uint64_t hash(uint64_t seed) const override;
  virtual int get_op_exprs(ObIArray<ObRawExpr*> &all_exprs) override;
  virtual int allocate_expr_post(ObAllocExprContext &ctx) override;
  virtual int get_plan_item_info(PlanText &plan_text, ObSqlPlanItem &plan_item) override;
  int generate_access_exprs();
  void set_table_id(uint64_t table_id) { table_id_ = table_id; }
  uint64_t get_table_id() const { return table_id_; }
  void set_table_name(const common::ObString &table_name) { table_name_ = table_name; }
  const common::ObString &get_table_name() const { return table_name_; }
  void set_file_table_def(const ObFileTableDef *file_table_def) { file_table_def_ = file_table_def; }
  const ObFileTableDef *get_file_table_def() const { return file_table_def_; }
  ObIArray<ObRawExpr*> &get_access_exprs() { return access_exprs_; }

private:
  uint64_t table_id_;
  common::ObString table_name_;
  common::ObSEArray<ObRawExpr*, 8, common::ModulePageAllocator, true> access_exprs_;
  const ObFileTableDef *file_table_def_;
  DISALLOW_COPY_AND_ASSIGN(ObLogFileTable);
};
}
}
#endif