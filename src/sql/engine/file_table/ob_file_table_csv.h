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
 * The streaming CSV reader of the file table engine.
 *
 * The reader follows RFC 4180 with the extensions MySQL accepts: a field may
 * be quoted, a quote is escaped by doubling it or by the escape character, a
 * quoted field may contain line breaks and both LF and CRLF terminate a
 * record.  The file is never materialised, the reader keeps a small buffer
 * and produces one record at a time.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_CSV_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_CSV_H_

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

/** One field of a CSV record together with the way it was written. */
struct CsvField
{
  std::string text;
  /** True when the field was written between quotes. */
  bool quoted = false;
};

/**
 * Reads a CSV file and exposes its records one at a time.
 *
 * The schema of a CSV file is inferred from the header line and from every
 * value of the file, therefore the whole file is read once before the first
 * record is handed out.
 */
class CsvReader
{
public:
  CsvReader();
  ~CsvReader();

  /** Open the file and remember the options. */
  int open(const std::string &path, const ReadOptions &options, FileError &error);
  /** Infer the column names and the column types of the file. */
  int infer_schema(FileError &error);
  /** Restart the reader at the first data record. */
  int rescan(FileError &error);
  /**
   * Read the next record.
   *
   * @retval 0 a record was produced
   * @retval 1 the end of the file was reached
   * @retval -1 the file is malformed, the reason is stored in @a error
   */
  int next_record(std::vector<CsvField> &record, FileError &error);
  /** The inferred column names. */
  const std::vector<std::string> &column_names() const { return column_names_; }
  /** The inferred column types. */
  const std::vector<InferredType> &column_types() const { return column_types_; }
  /** The number of records that were skipped because of @c ignore_errors. */
  long long skipped_records() const { return skipped_records_; }
  /** Close the file. */
  void close();

private:
  int open_stream(FileError &error);
  int read_header(FileError &error);
  int fill_buffer();
  int peek_char();
  int get_char();
  int parse_record(std::vector<CsvField> &record, FileError &error);
  int parse_field(CsvField &field, FileError &error);
  bool validate_utf8(const std::string &text) const;

private:
  std::string path_;
  ReadOptions options_;
  std::FILE *stream_ = nullptr;
  std::vector<char> buffer_;
  size_t buffer_pos_ = 0;
  size_t buffer_len_ = 0;
  bool stream_eof_ = false;
  bool first_record_ = true;
  bool header_read_ = false;
  long long line_ = 1;
  long long record_line_ = 1;
  long long column_ = 0;
  long long skipped_records_ = 0;
  std::vector<std::string> column_names_;
  std::vector<InferredType> column_types_;
};

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_CSV_H_