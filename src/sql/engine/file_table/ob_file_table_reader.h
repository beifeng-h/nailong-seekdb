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
 * The common interface of the readers of the file table engine.
 *
 * A reader knows the schema of its file and hands out the rows of the file
 * one at a time.  The rows are never materialised, the reader only keeps the
 * data of the row that it produces.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_READER_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_READER_H_

#include <memory>
#include <string>
#include <vector>

#include "sql/engine/file_table/ob_file_table_def.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

/**
 * The reader of one file.
 */
class FileTableReader
{
public:
  virtual ~FileTableReader() {}
  /** Determine the schema of the file. */
  virtual int infer_schema(std::vector<ColumnDef> &schema, FileError &error) = 0;
  /** Restart the reader at the first row. */
  virtual int rescan(FileError &error) = 0;
  /**
   * Read the next row.
   *
   * @retval 0 a row was produced
   * @retval 1 the end of the file was reached
   * @retval -1 the file is malformed
   */
  virtual int next_row(std::vector<Value> &row, FileError &error) = 0;
  /** The number of rows the file holds, -1 when it is unknown. */
  virtual int64_t row_count() const { return -1; }
};

/**
 * Open a file and return the reader that fits its format.
 *
 * @param format the format of the file
 * @param path the path of the file
 * @param options the options of the reader
 * @param reader the reader of the file
 * @param error the diagnostic of a failure
 */
int create_file_reader(FileFormat format,
                       const std::string &path,
                       const ReadOptions &options,
                       std::unique_ptr<FileTableReader> &reader,
                       FileError &error);

/**
 * List the files that match a pattern.
 *
 * The pattern may hold the wildcards '*' and '?'.  Only the files that exist
 * are returned, the list is sorted by the path.
 */
int glob_files(const std::string &pattern, std::vector<std::string> &files, FileError &error);

/**
 * Check that a path stays inside the directory of @a secure_file_priv.
 *
 * The check resolves the symbolic links of the path, a path that leaves the
 * directory is rejected.
 */
int check_secure_path(const std::string &secure_file_priv,
                      const std::string &path,
                      FileError &error);

/** The absolute path of a file, the empty string when it does not exist. */
std::string real_path_of(const std::string &path);

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_READER_H_