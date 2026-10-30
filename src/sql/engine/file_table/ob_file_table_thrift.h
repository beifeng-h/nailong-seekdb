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
 * A reader for the Thrift compact protocol.
 *
 * The Parquet format stores its metadata and its page headers with the
 * Thrift compact protocol.  The reader only implements the parts of the
 * protocol the Parquet metadata uses, it never allocates more memory than the
 * buffer it reads from.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_THRIFT_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_THRIFT_H_

#include <cstdint>
#include <string>
#include <vector>

namespace oceanbase
{
namespace sql
{
namespace file_table
{

/** The type codes of the Thrift compact protocol. */
enum class ThriftType
{
  STOP = 0,
  BOOLEAN_TRUE = 1,
  BOOLEAN_FALSE = 2,
  BYTE = 3,
  I16 = 4,
  I32 = 5,
  I64 = 6,
  DOUBLE = 7,
  BINARY = 8,
  LIST = 9,
  SET = 10,
  MAP = 11,
  STRUCT = 12,
};

/**
 * Reads the Thrift compact protocol out of a memory buffer.
 */
class ThriftCompactReader
{
public:
  ThriftCompactReader(const char *data, size_t size)
      : data_(data), size_(size), pos_(0), last_field_id_(0), failed_(false) {}

  /** Whether the reader is at the end of the buffer. */
  bool at_end() const { return pos_ >= size_; }
  /** The number of bytes that were not consumed yet. */
  size_t remaining() const { return pos_ >= size_ ? 0 : size_ - pos_; }

  /**
   * Read the header of the next struct field.
   *
   * @retval true a field was found
   * @retval false the struct ended, the field is left untouched
   */
  bool read_field_begin(int16_t &field_id, ThriftType &type);
  /** Read the value of a struct field. */
  int64_t read_i64();
  int32_t read_i32();
  int16_t read_i16();
  bool read_bool();
  double read_double();
  std::string read_binary();
  /** Read the header of a list, the element type is returned as well. */
  int32_t read_list_begin(ThriftType &element_type);
  /** Read the header of a map. */
  int32_t read_map_begin(ThriftType &key_type, ThriftType &value_type);
  /** Skip a value of the given type. */
  void skip(ThriftType type);
  /** Enter a nested struct, the field ids restart inside of it. */
  void struct_begin();
  /** Leave a nested struct. */
  void struct_end();
  /** The current position in the buffer. */
  size_t position() const { return pos_; }
  /** Whether the reader ran out of data. */
  bool failed() const { return failed_; }
  /** Forget the failure of the reader. */
  void clear_failure() { failed_ = false; }

private:
  uint8_t read_byte();
  uint64_t read_varint();
  int64_t read_zigzag();

private:
  const char *data_;
  size_t size_;
  size_t pos_;
  int16_t last_field_id_;
  bool failed_;
  std::vector<int16_t> field_stack_;
};

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_THRIFT_H_