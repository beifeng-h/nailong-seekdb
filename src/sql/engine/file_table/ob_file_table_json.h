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
 * The streaming JSON Lines reader of the file table engine.
 *
 * Every line of the file holds one JSON object.  The engine supports flat
 * objects only: a nested object or an array is reported as an error.  The
 * columns of the table are the union of the keys of all objects, a key that
 * is missing in an object contributes a NULL value.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_JSON_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_JSON_H_

#include <cstdio>
#include <string>
#include <vector>

#include "sql/engine/file_table/ob_file_table_def.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

/** The value kinds a JSON object member may have. */
enum class JsonValueKind
{
  NULL_VALUE = 0,
  BOOLEAN_VALUE = 1,
  NUMBER_VALUE = 2,
  STRING_VALUE = 3,
};

/** One member of a JSON object. */
struct JsonMember
{
  std::string name;
  JsonValueKind kind = JsonValueKind::NULL_VALUE;
  /** The text of a number or a string value. */
  std::string text;
  bool boolean_value = false;
  bool is_integer = false;
  int64_t integer_value = 0;
  double double_value = 0.0;
};

/** One object of a JSON Lines file. */
struct JsonObject
{
  std::vector<JsonMember> members;
  long long line = 0;
};

/**
 * Reads a JSON Lines file and exposes its objects one at a time.
 */
class JsonlReader
{
public:
  JsonlReader();
  ~JsonlReader();

  int open(const std::string &path, const ReadOptions &options, FileError &error);
  /** Infer the column names and the column types of the file. */
  int infer_schema(FileError &error);
  /** Restart the reader at the first object. */
  int rescan(FileError &error);
  /**
   * Read the next object.
   *
   * @retval 0 an object was produced
   * @retval 1 the end of the file was reached
   * @retval -1 the file is malformed
   */
  int next_object(JsonObject &object, FileError &error);
  const std::vector<std::string> &column_names() const { return column_names_; }
  /** The inferred column types. */
  const std::vector<InferredType> &column_types() const { return column_types_; }
  void close();

private:
  int open_stream(FileError &error);
  int read_line(std::string &line, FileError &error);
  int parse_object(const std::string &line, JsonObject &object, FileError &error);

private:
  std::string path_;
  ReadOptions options_;
  std::FILE *stream_ = nullptr;
  long long line_ = 0;
  long long skipped_lines_ = 0;
  std::vector<std::string> column_names_;
  std::vector<InferredType> column_types_;
};

/**
 * Parse one JSON object out of a line.
 *
 * The parser accepts the flat objects the engine supports and reports nested
 * objects, arrays and malformed input as an error.
 */
int parse_json_object(const std::string &text, JsonObject &object, FileError &error);

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_JSON_H_