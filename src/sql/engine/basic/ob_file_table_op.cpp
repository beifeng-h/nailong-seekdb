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

#define USING_LOG_PREFIX SQL_ENG

#include "sql/engine/basic/ob_file_table_op.h"
#include "sql/engine/ob_exec_context.h"
#include "common/timezone/ob_time_convert.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

OB_SERIALIZE_MEMBER((ObFileTableSpec, ObOpSpec),
                     column_exprs_,
                     path_,
                     format_,
                     delimiter_,
                     quote_,
                     escape_,
                     null_string_,
                     has_header_,
                     sample_size_,
                     column_types_);

int ObFileTableOp::inner_open()
{
  int ret = OB_SUCCESS;
  if (OB_SUCC(ret) && OB_FAIL(open_file())) {
    ret = OB_ERR_UNEXPECTED;
  }
  return ret;
}

int ObFileTableOp::open_file()
{
  int ret = OB_SUCCESS;
  file_table::ReadOptions options;
  options.delimiter = MY_SPEC.delimiter_.empty() ? ',' : MY_SPEC.delimiter_.ptr()[0];
  options.quote = MY_SPEC.quote_.empty() ? '"' : MY_SPEC.quote_.ptr()[0];
  options.escape = MY_SPEC.escape_.empty() ? '\\' : MY_SPEC.escape_.ptr()[0];
  options.null_string = MY_SPEC.null_string_.empty()
                            ? std::string()
                            : std::string(MY_SPEC.null_string_.ptr(), MY_SPEC.null_string_.length());
  options.has_header = MY_SPEC.has_header_;
  options.sample_size = MY_SPEC.sample_size_;
  file_table::FileError error;
  const std::string path(MY_SPEC.path_.ptr(), MY_SPEC.path_.length());
  is_glob_ = false;
  glob_index_ = 0;
  glob_files_.clear();
  fprintf(stderr, "[file table] the operator opens the file %s\n", path.c_str());
  if (3 == MY_SPEC.format_) {
    is_glob_ = true;
    glob_index_ = 0;
    glob_files_.clear();
    if (OB_FAIL(file_table::glob_files(path, glob_files_, error))) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("cannot list the files of the glob pattern", K(path.c_str()), K(error.to_string().c_str()));
    } else {
      fprintf(stderr, "[file table] the operator listed %d files\n", static_cast<int>(glob_files_.size()));
    }
  } else if (OB_FAIL(file_table::create_file_reader(static_cast<file_table::FileFormat>(MY_SPEC.format_),
                                                    path,
                                                    options,
                                                    reader_,
                                                    error))) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    std::vector<file_table::ColumnDef> schema;
    if (OB_FAIL(reader_->infer_schema(schema, error))) {
      ret = OB_ERR_UNEXPECTED;
      LOG_USER_ERROR(OB_ERR_UNEXPECTED, "cannot read the file %s: %s", path.c_str(), error.to_string().c_str());
      fprintf(stderr, "[file table] the operator could not read the file: %s\n", error.to_string().c_str());
    } else if (OB_FAIL(reader_->rescan(error))) {
      ret = OB_ERR_UNEXPECTED;
      LOG_USER_ERROR(OB_ERR_UNEXPECTED, "cannot scan the file %s: %s", path.c_str(), error.to_string().c_str());
      fprintf(stderr, "[file table] the operator could not restart the file: %s\n", error.to_string().c_str());
    } else {
      fprintf(stderr, "[file table] the operator read the schema, columns=%d\n", (int)schema.size());
    }
  }
  return ret;
}

int ObFileTableOp::inner_rescan()
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObOperator::inner_rescan())) {
  } else if (is_glob_) {
    glob_index_ = 0;
  } else if (OB_ISNULL(reader_.get())) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    file_table::FileError error;
    if (OB_FAIL(reader_->rescan(error))) {
      ret = OB_ERR_UNEXPECTED;
      LOG_USER_ERROR(OB_ERR_UNEXPECTED, "cannot read the file again: %s", error.to_string().c_str());
    }
  }
  return ret;
}

int ObFileTableOp::inner_close()
{
  reader_.reset();
  glob_files_.clear();
  glob_index_ = 0;
  is_glob_ = false;
  is_opened_ = false;
  return OB_SUCCESS;
}

void ObFileTableOp::destroy()
{
  reader_.reset();
  ObOperator::destroy();
}

int ObFileTableOp::fill_datum(int64_t index, const file_table::Value &value)
{
  int ret = OB_SUCCESS;
  ObExpr *expr = MY_SPEC.column_exprs_.at(index);
  ObDatum &datum = expr->locate_datum_for_write(eval_ctx_);
  if (value.is_null) {
    datum.set_null();
  } else {
    const file_table::InferredType column_type =
        index < MY_SPEC.column_types_.count()
            ? static_cast<file_table::InferredType>(MY_SPEC.column_types_.at(index))
            : value.type;
    switch (column_type) {
    case file_table::InferredType::BOOLEAN:
      datum.set_bool(value.type == file_table::InferredType::BOOLEAN
                         ? value.b
                         : (0 != value.i64));
      break;
    case file_table::InferredType::BIGINT:
      datum.set_int(value.i64);
      break;
    case file_table::InferredType::DOUBLE:
      datum.set_double(value.f64);
      break;
    case file_table::InferredType::DATE: {
      if (value.type == file_table::InferredType::DATE) {
        ObString text;
        text.assign_ptr(value.str.data(), static_cast<int32_t>(value.str.length()));
        int32_t date_value = 0;
        if (OB_FAIL(ObTimeConverter::str_to_date(text, date_value))) {
          ret = OB_INVALID_DATE_VALUE;
        } else {
          datum.set_date(date_value);
        }
      } else {
        datum.set_date(static_cast<int32_t>(value.i64));
      }
      break;
    }
    case file_table::InferredType::DATETIME: {
      if (value.type == file_table::InferredType::DATETIME) {
        ObString text;
        text.assign_ptr(value.str.data(), static_cast<int32_t>(value.str.length()));
        int64_t datetime_value = 0;
        ObTimeConvertCtx convert_ctx(nullptr, false);
        if (OB_FAIL(ObTimeConverter::str_to_datetime(text, convert_ctx, datetime_value))) {
          ret = OB_INVALID_DATE_VALUE;
        } else {
          datum.set_datetime(datetime_value);
        }
      } else {
        datum.set_datetime(value.i64);
      }
      break;
    }
    default: {
      char *buf = nullptr;
      if (value.str.length() > 0
          && OB_ISNULL(buf = expr->get_str_res_mem(eval_ctx_, value.str.length()))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (value.str.length() > 0) {
        MEMCPY(buf, value.str.data(), value.str.length());
      }
      datum.set_string(buf, static_cast<uint32_t>(value.str.length()));
      break;
    }
    }
  }
  expr->set_evaluated_projected(eval_ctx_);
  return ret;
}

int ObFileTableOp::inner_get_next_row()
{
  int ret = OB_SUCCESS;
  std::vector<file_table::Value> row;
  file_table::FileError error;
  int read_ret = 0;
  if (is_glob_) {
    if (glob_index_ >= static_cast<int64_t>(glob_files_.size())) {
      read_ret = 1;
    } else {
      row.push_back(file_table::Value::make_string(glob_files_.at(glob_index_++)));
    }
  } else {
    read_ret = reader_->next_row(row, error);
  }
  if (1 == read_ret) {
    ret = OB_ITER_END;
  } else if (0 != read_ret) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("cannot read the file", K(error.to_string().c_str()));
  } else {
    const int64_t count = MY_SPEC.column_exprs_.count();
    for (int64_t i = 0; OB_SUCC(ret) && i < count && i < static_cast<int64_t>(row.size()); ++i) {
      if (OB_FAIL(fill_datum(i, row.at(i)))) {
        ret = OB_ERR_UNEXPECTED;
      }
    }
  }
  return ret;
}

}
}




