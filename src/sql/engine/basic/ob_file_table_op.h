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
 * The physical operator of a table that reads a local file.
 *
 * The operator opens the file when it is opened, it reads the rows of the
 * file one at a time and it restarts at the first row when it is rescanned.
 * The file is never held in memory, only the row that is produced is.
 */

#ifndef OCEANBASE_BASIC_OB_FILE_TABLE_OP_H_
#define OCEANBASE_BASIC_OB_FILE_TABLE_OP_H_

#include "sql/engine/ob_operator.h"
#include "lib/charset/ob_charset.h"
#include "sql/engine/file_table/ob_file_table_reader.h"

namespace oceanbase
{
namespace sql
{

class ObExpr;

/**
 * The specification of the operator that reads a local file.
 */
class ObFileTableSpec : public ObOpSpec
{
  OB_UNIS_VERSION_V(1);
public:
  ObFileTableSpec(common::ObIAllocator &alloc, const ObPhyOperatorType type)
    : ObOpSpec(alloc, type),
      column_exprs_(alloc),
      path_(),
      format_(0),
      delimiter_(","),
      quote_("\""),
      escape_("\\"),
      null_string_(),
      has_header_(true),
      sample_size_(-1),
      column_types_(alloc)
  {}
  /** The expressions that hold the columns of the table. */
  common::ObFixedArray<ObExpr*, common::ObIAllocator> column_exprs_;
  /** The path of the file. */
  common::ObString path_;
  /** The format of the file. */
  int32_t format_;
  common::ObString delimiter_;
  common::ObString quote_;
  common::ObString escape_;
  common::ObString null_string_;
  bool has_header_;
  int64_t sample_size_;
  /** The type of every column of the table. */
  common::ObFixedArray<int32_t, common::ObIAllocator> column_types_;
};

/**
 * The operator that reads the rows of a local file.
 */
class ObFileTableOp : public ObOperator
{
public:
  ObFileTableOp(ObExecContext &exec_ctx, const ObOpSpec &spec, ObOpInput *input)
    : ObOperator(exec_ctx, spec, input),
      reader_(),
      is_opened_(false),
      is_glob_(false),
      glob_index_(0),
      glob_files_()
  {
  }
  virtual ~ObFileTableOp() {}
  virtual int inner_open() override;
  virtual int inner_rescan() override;
  virtual int inner_get_next_row() override;
  virtual int inner_close() override;
  virtual void destroy() override;

private:
  int open_file();
  int fill_datum(int64_t index, const file_table::Value &value);

private:
  std::unique_ptr<file_table::FileTableReader> reader_;
  bool is_opened_;
  bool is_glob_;
  int64_t glob_index_;
  std::vector<std::string> glob_files_;
};

} // end namespace sql
} // end namespace oceanbase

#endif // OCEANBASE_BASIC_OB_FILE_TABLE_OP_H_
