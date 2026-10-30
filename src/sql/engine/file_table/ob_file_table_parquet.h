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
 * The streaming Parquet reader of the file table engine.
 *
 * The reader takes the schema of the table from the footer of the file, it
 * walks the row groups one after the other and decodes the columns that the
 * query asks for.  The columns that are not requested are never decoded,
 * which is the column pruning of the Parquet format.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_PARQUET_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_PARQUET_H_

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

/** The physical types of the Parquet format. */
enum class ParquetType
{
  BOOLEAN = 0,
  INT32 = 1,
  INT64 = 2,
  INT96 = 3,
  FLOAT = 4,
  DOUBLE = 5,
  BYTE_ARRAY = 6,
  FIXED_LEN_BYTE_ARRAY = 7,
};

/** The repetition types of the Parquet format. */
enum class ParquetRepetition
{
  REQUIRED = 0,
  OPTIONAL = 1,
  REPEATED = 2,
};

/** The description of one leaf column of a Parquet file. */
struct ParquetColumnDef
{
  std::string name;
  ParquetType physical_type = ParquetType::BYTE_ARRAY;
  ParquetRepetition repetition = ParquetRepetition::OPTIONAL;
  /** The largest definition level of the column. */
  int32_t max_def_level = 1;
  /** The length of a fixed length byte array. */
  int32_t type_length = 0;
  /** The logical type of the column after the conversion. */
  InferredType inferred_type = InferredType::VARCHAR;
};

/** The metadata of one column chunk of a row group. */
struct ParquetColumnChunk
{
  int64_t num_values = 0;
  int64_t data_page_offset = 0;
  int64_t dictionary_page_offset = -1;
  int64_t total_compressed_size = 0;
  int32_t codec = 0;
};

/** The metadata of one row group. */
struct ParquetRowGroup
{
  int64_t num_rows = 0;
  std::vector<ParquetColumnChunk> columns;
};

/**
 * Reads a Parquet file and exposes its rows one at a time.
 */
class ParquetReader
{
public:
  ParquetReader();
  ~ParquetReader();

  int open(const std::string &path, const ReadOptions &options, FileError &error);
  /** Read the schema of the file out of its footer. */
  int infer_schema(FileError &error);
  /** Restart the reader at the first row of the first row group. */
  int rescan(FileError &error);
  /**
   * Read the next row.
   *
   * @retval 0 a row was produced
   * @retval 1 the end of the file was reached
   * @retval -1 the file is malformed
   */
  int next_record(std::vector<Value> &record, FileError &error);
  const std::vector<ColumnDef> &schema() const { return schema_; }
  /** The number of rows the file holds. */
  int64_t row_count() const { return row_count_; }
  /** Restrict the reader to the given columns of the file. */
  int set_projection(const std::vector<size_t> &column_indexes, FileError &error);
  void close();

private:
  int read_footer(FileError &error);
  int parse_file_metadata(const std::string &footer, FileError &error);
  int build_leaves(FileError &error);
  int load_row_group(FileError &error);
  int decode_column(size_t column_index, FileError &error);
  int read_page_header(int32_t &page_type, int32_t &compressed_size, int32_t &num_values,
                       int32_t &encoding, int32_t &def_levels_length,
                       int32_t &rep_levels_length, bool &is_compressed, int32_t &dictionary_values,
                       FileError &error);

private:
  std::string path_;
  ReadOptions options_;
  std::FILE *stream_ = nullptr;
  long long file_size_ = 0;
  std::vector<ParquetColumnDef> leaves_;
  std::vector<ParquetRowGroup> row_groups_;
  std::vector<ColumnDef> schema_;
  std::vector<size_t> projection_;
  /** The values of the row group that is currently loaded. */
  std::vector<std::vector<Value>> row_group_values_;
  int64_t row_count_ = 0;
  size_t row_group_index_ = 0;
  int64_t row_index_ = 0;
  bool row_group_loaded_ = false;
};

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_PARQUET_H_