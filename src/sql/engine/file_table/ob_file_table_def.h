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
 * Shared definitions of the file table engine.
 *
 * The engine reads local, read-only data files (CSV, JSON Lines and Parquet)
 * and exposes them to the SQL layer as a table with a dynamically inferred
 * schema.  The engine itself is deliberately free of any dependency on the
 * SQL runtime so that it can be unit tested on its own.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_DEF_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_DEF_H_

#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{
namespace file_table
{

/** The file formats understood by the file table engine. */
enum class FileFormat
{
  CSV = 0,
  JSONL = 1,
  PARQUET = 2,
};

/**
 * The logical type of a column after deterministic inference over the whole
 * file.  The lattice is intentionally small: NULL widens into any other type,
 * BOOLEAN widens into BIGINT, BIGINT widens into DOUBLE and every numeric or
 * temporal type widens into VARCHAR.
 */
enum class InferredType
{
  NULL_TYPE = 0,
  BOOLEAN = 1,
  BIGINT = 2,
  DOUBLE = 3,
  VARCHAR = 4,
  DATE = 5,
  DATETIME = 6,
};

const char *to_string(InferredType type);

/** A single inferred column of a file table. */
struct ColumnDef
{
  std::string name;
  InferredType type = InferredType::VARCHAR;
  bool nullable = true;
};

/** A single cell of a file table row. */
struct Value
{
  bool is_null = true;
  InferredType type = InferredType::NULL_TYPE;
  int64_t i64 = 0;
  double f64 = 0.0;
  bool b = false;
  std::string str;

  static Value make_null(InferredType type)
  {
    Value value;
    value.is_null = true;
    value.type = type;
    return value;
  }
  static Value make_bool(bool v)
  {
    Value value;
    value.is_null = false;
    value.type = InferredType::BOOLEAN;
    value.b = v;
    return value;
  }
  static Value make_int(int64_t v)
  {
    Value value;
    value.is_null = false;
    value.type = InferredType::BIGINT;
    value.i64 = v;
    return value;
  }
  static Value make_double(double v)
  {
    Value value;
    value.is_null = false;
    value.type = InferredType::DOUBLE;
    value.f64 = v;
    return value;
  }
  static Value make_string(const std::string &v)
  {
    Value value;
    value.is_null = false;
    value.type = InferredType::VARCHAR;
    value.str = v;
    return value;
  }
};

/** The knobs that control how a file is parsed. */
struct ReadOptions
{
  /** Field separator of the CSV format. */
  char delimiter = ',';
  /** The character that quotes a CSV field. */
  char quote = '"';
  /** The escape character of a quoted CSV field. */
  char escape = '\\';
  /** The text that denotes a NULL value.  An empty string means "empty field". */
  std::string null_string;
  /** Whether the first line holds the column names. */
  bool has_header = true;
  /** The sample size.  Only the full scan (-1) is supported. */
  long sample_size = -1;
  /** The maximum number of rows to produce, -1 means unlimited. */
  long max_rows = -1;
  /** Whether a malformed record is reported or skipped. */
  bool ignore_errors = false;
};

/** A diagnostic produced while a file is read. */
struct FileError
{
  int code = 0;
  std::string message;
  /** The 1-based physical line the error was detected on, 0 when unknown. */
  long line = 0;
  /** The 1-based column the error was detected on, 0 when unknown. */
  long column = 0;

  bool empty() const { return 0 == code && message.empty(); }
  std::string to_string() const;
};

/** The default resource limits applied to a single file table. */
struct ResourceLimits
{
  /** The largest file the engine accepts, in bytes.  0 means unlimited. */
  long long max_file_size = 1024LL * 1024LL * 1024LL;
  /** The largest number of rows a single file table may produce. */
  long long max_rows = 50000000LL;
};

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_DEF_H_