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

#include "sql/engine/file_table/ob_file_table_parquet.h"

#include <cerrno>
#include <cstdio>
#include <cstring>

#include "sql/engine/file_table/ob_file_table_thrift.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

namespace
{
const int PARQUET_OK = 0;
const int PARQUET_EOF = 1;
const int PARQUET_ERROR = -1;

/** The page types of the Parquet format. */
const int32_t PAGE_DATA = 0;
const int32_t PAGE_DICTIONARY = 2;
const int32_t PAGE_DATA_V2 = 3;

/** The encodings of the Parquet format. */
const int32_t ENCODING_PLAIN = 0;
const int32_t ENCODING_PLAIN_DICTIONARY = 2;
const int32_t ENCODING_RLE_DICTIONARY = 8;

/** The compression codecs of the Parquet format. */
const int32_t CODEC_UNCOMPRESSED = 0;
const int32_t CODEC_SNAPPY = 1;

/**
 * Decompress a Snappy block, the raw format the Parquet column pages use.
 *
 * The block starts with the varint length of the output, then a sequence of
 * literals and back references.  A back reference may overlap the bytes it
 * copies, so the bytes are copied one at a time.
 */
bool snappy_decompress(const char *input, size_t length, std::string &output)
{
  size_t position = 0;
  uint64_t expected = 0;
  int shift = 0;
  while (position < length) {
    const unsigned char byte = static_cast<unsigned char>(input[position++]);
    expected |= static_cast<uint64_t>(byte & 0x7F) << shift;
    if (0 == (byte & 0x80)) {
      break;
    }
    shift += 7;
    if (shift > 32) {
      return false;
    }
  }
  output.clear();
  output.reserve(static_cast<size_t>(expected));
  while (position < length) {
    const unsigned char tag = static_cast<unsigned char>(input[position++]);
    const int kind = tag & 0x03;
    size_t copy_length = 0;
    size_t copy_offset = 0;
    if (0 == kind) {
      size_t literal = tag >> 2;
      if (literal < 60) {
        copy_length = literal + 1;
      } else {
        const size_t extra = literal - 59;
        if (extra > 4 || position + extra > length) {
          return false;
        }
        uint64_t value = 0;
        for (size_t i = 0; i < extra; ++i) {
          value |= static_cast<uint64_t>(static_cast<unsigned char>(input[position + i])) << (8 * i);
        }
        position += extra;
        copy_length = static_cast<size_t>(value) + 1;
      }
      if (position + copy_length > length) {
        return false;
      }
      output.append(input + position, copy_length);
      position += copy_length;
      continue;
    } else if (1 == kind) {
      copy_length = 4 + ((tag >> 2) & 0x07);
      if (position + 1 > length) {
        return false;
      }
      copy_offset = (static_cast<size_t>(tag >> 5) << 8) | static_cast<unsigned char>(input[position]);
      position += 1;
    } else if (2 == kind) {
      copy_length = 1 + (tag >> 2);
      if (position + 2 > length) {
        return false;
      }
      copy_offset = static_cast<size_t>(static_cast<unsigned char>(input[position]))
                    | (static_cast<size_t>(static_cast<unsigned char>(input[position + 1])) << 8);
      position += 2;
    } else {
      copy_length = 1 + (tag >> 2);
      if (position + 4 > length) {
        return false;
      }
      copy_offset = static_cast<size_t>(static_cast<unsigned char>(input[position]))
                    | (static_cast<size_t>(static_cast<unsigned char>(input[position + 1])) << 8)
                    | (static_cast<size_t>(static_cast<unsigned char>(input[position + 2])) << 16)
                    | (static_cast<size_t>(static_cast<unsigned char>(input[position + 3])) << 24);
      position += 4;
    }
    if (0 == copy_offset || copy_offset > output.size()) {
      return false;
    }
    const size_t start = output.size() - copy_offset;
    for (size_t i = 0; i < copy_length; ++i) {
      output.push_back(output[start + i]);
    }
  }
  return output.size() == static_cast<size_t>(expected);
}

/** The converted types of the Parquet format. */
const int32_t CONVERTED_DATE = 6;
const int32_t CONVERTED_TIMESTAMP_MILLIS = 9;
const int32_t CONVERTED_TIMESTAMP_MICROS = 10;

uint32_t read_uint32_le(const char *data)
{
  uint32_t result = 0;
  for (int i = 0; i < 4; ++i) {
    result |= static_cast<uint32_t>(static_cast<unsigned char>(data[i])) << (8 * i);
  }
  return result;
}

uint64_t read_uint64_le(const char *data)
{
  uint64_t result = 0;
  for (int i = 0; i < 8; ++i) {
    result |= static_cast<uint64_t>(static_cast<unsigned char>(data[i])) << (8 * i);
  }
  return result;
}

/**
 * The decoder of the run length and bit packing hybrid encoding.
 *
 * The encoding is a sequence of runs.  A run either repeats one value or
 * holds a group of eight bit packed values.
 */
class RleHybridDecoder
{
public:
  void reset(const char *data, size_t size, int32_t width)
  {
    data_ = data;
    size_ = size;
    pos_ = 0;
    width_ = width;
  }

  int64_t decode(int64_t count, std::vector<int32_t> &out)
  {
    if (0 == width_) {
      for (int64_t i = 0; i < count; ++i) {
        out.push_back(0);
      }
      return count;
    }
    const int32_t byte_width = (width_ + 7) / 8;
    int64_t produced = 0;
    while (produced < count) {
      if (pos_ >= size_) {
        break;
      }
      const uint64_t header = read_varint();
      if (header & 1u) {
        // A bit packed run holds groups of eight values, every group takes
        // one byte aligned block of the width of the values.
        const int64_t values = static_cast<int64_t>(header >> 1) * 8;
        const size_t run_start = pos_;
        int64_t index = 0;
        while (index < values && produced < count) {
          const size_t group_offset = run_start
                                     + static_cast<size_t>(index / 8) * static_cast<size_t>(byte_width);
          if (group_offset + static_cast<size_t>(byte_width) > size_) {
            break;
          }
          const int32_t value_offset = static_cast<int32_t>(index % 8) * width_;
          int32_t value = 0;
          for (int32_t bit = 0; bit < width_; ++bit) {
            const size_t byte_index = group_offset + static_cast<size_t>((value_offset + bit) / 8);
            const uint32_t mask = 1u << ((value_offset + bit) % 8);
            if (static_cast<unsigned char>(data_[byte_index]) & mask) {
              value |= (1 << bit);
            }
          }
          out.push_back(value);
          ++produced;
          ++index;
        }
        pos_ = run_start + static_cast<size_t>((values + 7) / 8) * static_cast<size_t>(byte_width);
      } else {
        // A run of one repeated value.
        const int64_t run = static_cast<int64_t>(header >> 1);
        if (pos_ + static_cast<size_t>(byte_width) > size_) {
          break;
        }
        int32_t value = 0;
        for (int32_t bit = 0; bit < width_; ++bit) {
          const uint64_t byte_index = pos_ + static_cast<uint64_t>(bit) / 8u;
          const uint32_t mask = 1u << (bit % 8);
          if (static_cast<unsigned char>(data_[byte_index]) & mask) {
            value |= (1 << bit);
          }
        }
        pos_ += static_cast<size_t>(byte_width);
        for (int64_t i = 0; i < run && produced < count; ++i) {
          out.push_back(value);
          ++produced;
        }
      }
    }
    return produced;
  }

private:
  uint64_t read_varint()
  {
    uint64_t result = 0;
    int shift = 0;
    for (;;) {
      if (pos_ >= size_) {
        return result;
      }
      const uint8_t byte = static_cast<uint8_t>(data_[pos_++]);
      result |= static_cast<uint64_t>(byte & 0x7Fu) << shift;
      if (0 == (byte & 0x80u)) {
        break;
      }
      shift += 7;
      if (shift > 63) {
        break;
      }
    }
    return result;
  }

private:
  const char *data_ = nullptr;
  size_t size_ = 0;
  size_t pos_ = 0;
  int32_t width_ = 0;
};/** The number of bits that are needed to hold the values up to @a max_value. */
int32_t level_bit_width(int32_t max_value)
{
  int32_t width = 0;
  while (max_value > 0) {
    ++width;
    max_value >>= 1;
  }
  return width;
}
/** Decode the plain encoding of the Parquet format. */
int decode_plain_values(const char *data, size_t size, ParquetType type, int32_t type_length,
                        int64_t count, std::vector<Value> &values, FileError &error)
{
  size_t pos = 0;
  for (int64_t i = 0; i < count; ++i) {
    Value value;
    value.is_null = false;
    switch (type) {
    case ParquetType::BOOLEAN: {
      const uint64_t byte_index = static_cast<uint64_t>(pos) + static_cast<uint64_t>(i) / 8u;
      if (byte_index >= size) {
        error.code = 1;
        error.message = "the page ends before the boolean values";
        return PARQUET_ERROR;
      }
      const uint32_t mask = 1u << (i % 8);
      value.type = InferredType::BOOLEAN;
      value.b = (static_cast<unsigned char>(data[byte_index]) & mask) != 0;
      break;
    }
    case ParquetType::INT32: {
      if (pos + 4 > size) {
        error.code = 1;
        error.message = "the page ends before the int32 values";
        return PARQUET_ERROR;
      }
      value.type = InferredType::BIGINT;
      value.i64 = static_cast<int32_t>(read_uint32_le(data + pos));
      pos += 4;
      break;
    }
    case ParquetType::INT64: {
      if (pos + 8 > size) {
        error.code = 1;
        error.message = "the page ends before the int64 values";
        return PARQUET_ERROR;
      }
      value.type = InferredType::BIGINT;
      value.i64 = static_cast<int64_t>(read_uint64_le(data + pos));
      pos += 8;
      break;
    }
    case ParquetType::FLOAT: {
      if (pos + 4 > size) {
        error.code = 1;
        error.message = "the page ends before the float values";
        return PARQUET_ERROR;
      }
      const uint32_t bits = read_uint32_le(data + pos);
      float number = 0.0f;
      std::memcpy(&number, &bits, sizeof(number));
      value.type = InferredType::DOUBLE;
      value.f64 = number;
      pos += 4;
      break;
    }
    case ParquetType::DOUBLE: {
      if (pos + 8 > size) {
        error.code = 1;
        error.message = "the page ends before the double values";
        return PARQUET_ERROR;
      }
      const uint64_t bits = read_uint64_le(data + pos);
      double number = 0.0;
      std::memcpy(&number, &bits, sizeof(number));
      value.type = InferredType::DOUBLE;
      value.f64 = number;
      pos += 8;
      break;
    }
    case ParquetType::BYTE_ARRAY: {
      if (pos + 4 > size) {
        error.code = 1;
        error.message = "the page ends before the byte array values";
        return PARQUET_ERROR;
      }
      const uint32_t length = read_uint32_le(data + pos);
      pos += 4;
      if (pos + length > size) {
        error.code = 1;
        error.message = "the page ends before the byte array values";
        return PARQUET_ERROR;
      }
      value.type = InferredType::VARCHAR;
      value.str.assign(data + pos, length);
      pos += length;
      break;
    }
    case ParquetType::FIXED_LEN_BYTE_ARRAY: {
      const uint32_t length = static_cast<uint32_t>(type_length);
      if (pos + length > size) {
        error.code = 1;
        error.message = "the page ends before the fixed length values";
        return PARQUET_ERROR;
      }
      value.type = InferredType::VARCHAR;
      value.str.assign(data + pos, length);
      pos += length;
      break;
    }
    case ParquetType::INT96: {
      // The legacy timestamp keeps its binary form.
      if (pos + 12 > size) {
        error.code = 1;
        error.message = "the page ends before the int96 values";
        return PARQUET_ERROR;
      }
      value.type = InferredType::VARCHAR;
      char buffer[32];
      std::snprintf(buffer, sizeof(buffer), "%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x",
                    static_cast<unsigned char>(data[pos]), static_cast<unsigned char>(data[pos + 1]),
                    static_cast<unsigned char>(data[pos + 2]), static_cast<unsigned char>(data[pos + 3]),
                    static_cast<unsigned char>(data[pos + 4]), static_cast<unsigned char>(data[pos + 5]),
                    static_cast<unsigned char>(data[pos + 6]), static_cast<unsigned char>(data[pos + 7]),
                    static_cast<unsigned char>(data[pos + 8]), static_cast<unsigned char>(data[pos + 9]),
                    static_cast<unsigned char>(data[pos + 10]), static_cast<unsigned char>(data[pos + 11]));
      value.str = buffer;
      pos += 12;
      break;
    }
    default:
      error.code = 1;
      error.message = "the column uses an unsupported physical type";
      return PARQUET_ERROR;
    }
    values.push_back(value);
  }
  return PARQUET_OK;
}

/** One element of the flat schema list of the Parquet footer. */
struct SchemaElement
{
  int32_t type = -1;
  int32_t type_length = 0;
  int32_t repetition = 0;
  std::string name;
  int32_t num_children = 0;
  int32_t converted_type = -1;
  bool logical_date = false;
  bool logical_timestamp = false;
};

InferredType map_parquet_type(const SchemaElement &element)
{
  switch (static_cast<ParquetType>(element.type)) {
  case ParquetType::BOOLEAN:
    return InferredType::BOOLEAN;
  case ParquetType::INT32:
    return element.logical_date ? InferredType::DATE : InferredType::BIGINT;
  case ParquetType::INT64:
    return element.logical_timestamp ? InferredType::DATETIME : InferredType::BIGINT;
  case ParquetType::FLOAT:
  case ParquetType::DOUBLE:
    return InferredType::DOUBLE;
  default:
    return InferredType::VARCHAR;
  }
}

void walk_schema(const std::vector<SchemaElement> &elements, size_t &pos, int32_t def_level,
                 std::vector<ParquetColumnDef> &leaves)
{
  if (pos >= elements.size()) {
    return;
  }
  const SchemaElement element = elements[pos++];
  int32_t level = def_level;
  if (ParquetRepetition::OPTIONAL == static_cast<ParquetRepetition>(element.repetition)
      || ParquetRepetition::REPEATED == static_cast<ParquetRepetition>(element.repetition)) {
    ++level;
  }
  if (element.num_children > 0) {
    for (int32_t i = 0; i < element.num_children; ++i) {
      walk_schema(elements, pos, level, leaves);
    }
    return;
  }
  ParquetColumnDef leaf;
  leaf.name = element.name;
  leaf.physical_type = static_cast<ParquetType>(element.type);
  leaf.repetition = static_cast<ParquetRepetition>(element.repetition);
  leaf.max_def_level = level;
  leaf.type_length = element.type_length;
  leaf.inferred_type = map_parquet_type(element);
  leaves.push_back(leaf);
}

} // namespace

ParquetReader::ParquetReader() {}

ParquetReader::~ParquetReader()
{
  close();
}

void ParquetReader::close()
{
  if (nullptr != stream_) {
    std::fclose(stream_);
    stream_ = nullptr;
  }
}

int ParquetReader::open(const std::string &path, const ReadOptions &options, FileError &error)
{
  path_ = path;
  options_ = options;
  leaves_.clear();
  row_groups_.clear();
  schema_.clear();
  projection_.clear();
  row_group_values_.clear();
  row_group_index_ = 0;
  row_index_ = 0;
  row_group_loaded_ = false;
  close();
  stream_ = std::fopen(path_.c_str(), "rb");
  if (nullptr == stream_) {
    error.code = errno ? errno : 1;
    error.message = "cannot open file '" + path_ + "': " + std::strerror(errno);
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  std::fseek(stream_, 0, SEEK_END);
  file_size_ = std::ftell(stream_);
  std::fseek(stream_, 0, SEEK_SET);
  return PARQUET_OK;
}

int ParquetReader::read_footer(FileError &error)
{
  if (file_size_ < 12) {
    error.code = 1;
    error.message = "the file is too small to be a Parquet file";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  char tail[8];
  std::fseek(stream_, file_size_ - 8, SEEK_SET);
  if (8 != std::fread(tail, 1, 8, stream_)) {
    error.code = 1;
    error.message = "cannot read the footer of the file";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  if (0 != std::memcmp(tail + 4, "PAR1", 4)) {
    error.code = 1;
    error.message = "the file does not have the Parquet magic number";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  const uint32_t footer_size = read_uint32_le(tail);
  if (static_cast<long long>(footer_size) + 12 > file_size_) {
    error.code = 1;
    error.message = "the footer of the file is out of range";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  std::string footer;
  footer.resize(footer_size);
  std::fseek(stream_, file_size_ - 8 - static_cast<long long>(footer_size), SEEK_SET);
  if (footer_size != std::fread(&footer[0], 1, footer_size, stream_)) {
    error.code = 1;
    error.message = "cannot read the footer of the file";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  return parse_file_metadata(footer, error);
}

int ParquetReader::parse_file_metadata(const std::string &footer, FileError &error)
{
  ThriftCompactReader reader(footer.data(), footer.size());
  int16_t field_id = 0;
  ThriftType type = ThriftType::STOP;
  std::vector<SchemaElement> schema_elements;
  std::vector<ParquetRowGroup> row_groups;

  while (reader.read_field_begin(field_id, type)) {
    switch (field_id) {
    case 1: // version
      reader.read_i32();
      break;
    case 2: { // schema
      ThriftType element_type = ThriftType::STOP;
      const int32_t count = reader.read_list_begin(element_type);
      for (int32_t i = 0; i < count; ++i) {
        SchemaElement element;
        reader.struct_begin();
        int16_t element_field = 0;
        ThriftType element_field_type = ThriftType::STOP;
        while (reader.read_field_begin(element_field, element_field_type)) {
          switch (element_field) {
          case 1: // type
            element.type = reader.read_i32();
            break;
          case 2: // type_length
            element.type_length = reader.read_i32();
            break;
          case 3: // repetition_type
            element.repetition = reader.read_i32();
            break;
          case 4: // name
            element.name = reader.read_binary();
            break;
          case 5: // num_children
            element.num_children = reader.read_i32();
            break;
          case 6: // converted_type
            element.converted_type = reader.read_i32();
            if (CONVERTED_DATE == element.converted_type) {
              element.logical_date = true;
            } else if (CONVERTED_TIMESTAMP_MILLIS == element.converted_type
                       || CONVERTED_TIMESTAMP_MICROS == element.converted_type) {
              element.logical_timestamp = true;
            }
            break;
          case 10: { // logicalType
            // The logical type is a union, the field id selects the member.
            int16_t union_field = 0;
            ThriftType union_type = ThriftType::STOP;
            while (reader.read_field_begin(union_field, union_type)) {
              if (6 == union_field) {
                element.logical_date = true;
              } else if (8 == union_field) {
                element.logical_timestamp = true;
              } else {
                reader.skip(union_type);
              }
            }
            break;
          }
          default:
            reader.skip(element_field_type);
            break;
          }
        }
        reader.struct_end();
        schema_elements.push_back(element);
      }
      break;
    }
    case 3: // num_rows
      row_count_ = reader.read_i64();
      break;
    case 4: { // row_groups
      ThriftType element_type = ThriftType::STOP;
      const int32_t count = reader.read_list_begin(element_type);
      for (int32_t i = 0; i < count; ++i) {
        ParquetRowGroup group;
        reader.struct_begin();
        int16_t group_field = 0;
        ThriftType group_field_type = ThriftType::STOP;
        while (reader.read_field_begin(group_field, group_field_type)) {
          if (1 == group_field) { // columns
            ThriftType chunk_type = ThriftType::STOP;
            const int32_t chunk_count = reader.read_list_begin(chunk_type);
            for (int32_t j = 0; j < chunk_count; ++j) {
              ParquetColumnChunk chunk;
              reader.struct_begin();
              int16_t chunk_field = 0;
              ThriftType chunk_field_type = ThriftType::STOP;
              while (reader.read_field_begin(chunk_field, chunk_field_type)) {
                if (3 == chunk_field) { // meta_data
                  reader.struct_begin();
                  int16_t meta_field = 0;
                  ThriftType meta_field_type = ThriftType::STOP;
                  while (reader.read_field_begin(meta_field, meta_field_type)) {
                    switch (meta_field) {
                    case 4: // codec
                      chunk.codec = reader.read_i32();
                      break;
                    case 5: // num_values
                      chunk.num_values = reader.read_i64();
                      break;
                    case 7: // total_compressed_size
                      chunk.total_compressed_size = reader.read_i64();
                      break;
                    case 9: // data_page_offset
                      chunk.data_page_offset = reader.read_i64();
                      break;
                    case 11: // dictionary_page_offset
                      chunk.dictionary_page_offset = reader.read_i64();
                      break;
                    default:
                      reader.skip(meta_field_type);
                      break;
                    }
                  }
                  reader.struct_end();
                } else {
                  reader.skip(chunk_field_type);
                }
              }
              reader.struct_end();
              group.columns.push_back(chunk);
            }
          } else if (3 == group_field) { // num_rows
            group.num_rows = reader.read_i64();
          } else {
            reader.skip(group_field_type);
          }
        }
        reader.struct_end();
        row_groups.push_back(group);
      }
      break;
    }
    default:
      reader.skip(type);
      break;
    }
  }

  if (schema_elements.empty()) {
    error.code = 1;
    error.message = "the footer of the file does not contain a schema";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  size_t pos = 0;
  walk_schema(schema_elements, pos, 0, leaves_);
  row_groups_ = row_groups;
  return PARQUET_OK;
}

int ParquetReader::infer_schema(FileError &error)
{
  const int ret = read_footer(error);
  if (PARQUET_ERROR == ret) {
    return PARQUET_ERROR;
  }
  schema_.clear();
  for (size_t i = 0; i < leaves_.size(); ++i) {
    ColumnDef column;
    column.name = leaves_[i].name;
    column.type = leaves_[i].inferred_type;
    column.nullable = leaves_[i].max_def_level > 0;
    schema_.push_back(column);
  }
  projection_.clear();
  for (size_t i = 0; i < leaves_.size(); ++i) {
    projection_.push_back(i);
  }
  return PARQUET_OK;
}

int ParquetReader::set_projection(const std::vector<size_t> &column_indexes, FileError &error)
{
  projection_.clear();
  for (size_t i = 0; i < column_indexes.size(); ++i) {
    if (column_indexes[i] >= leaves_.size()) {
      error.code = 1;
      error.message = "the column " + std::to_string(column_indexes[i]) + " is not in the file";
      error.line = 0;
      error.column = 0;
      return PARQUET_ERROR;
    }
    projection_.push_back(column_indexes[i]);
  }
  return PARQUET_OK;
}

int ParquetReader::rescan(FileError &error)
{
  row_group_index_ = 0;
  row_index_ = 0;
  row_group_loaded_ = false;
  row_group_values_.clear();
  if (leaves_.empty() && row_groups_.empty()) {
    const int ret = read_footer(error);
    if (PARQUET_ERROR == ret) {
      return PARQUET_ERROR;
    }
  }
  return PARQUET_OK;
}

int ParquetReader::read_page_header(int32_t &page_type, int32_t &compressed_size, int32_t &num_values,
                                    int32_t &encoding, int32_t &def_levels_length,
                                    int32_t &rep_levels_length, bool &is_compressed,
                                    int32_t &dictionary_values, FileError &error)
{
  std::string header;
  size_t consumed = 0;
  bool parsed = false;
  for (int attempt = 0; attempt < 32 && !parsed; ++attempt) {
    char buffer[4096];
    const size_t got = std::fread(buffer, 1, sizeof(buffer), stream_);
    if (0 == got) {
      break;
    }
    header.append(buffer, got);
    // The page header is a Thrift struct, the reader stops at the end of it.
    ThriftCompactReader reader(header.data(), header.size());
    int16_t field_id = 0;
    ThriftType type = ThriftType::STOP;
    while (reader.read_field_begin(field_id, type)) {
      switch (field_id) {
      case 1:
        page_type = reader.read_i32();
        break;
      case 3:
        compressed_size = reader.read_i32();
        break;
      case 5: { // data_page_header
        reader.struct_begin();
        int16_t sub_field = 0;
        ThriftType sub_type = ThriftType::STOP;
        while (reader.read_field_begin(sub_field, sub_type)) {
          if (1 == sub_field) {
            num_values = reader.read_i32();
          } else if (2 == sub_field) {
            encoding = reader.read_i32();
          } else {
            reader.skip(sub_type);
          }
        }
        reader.struct_end();
        break;
      }
      case 7: { // dictionary_page_header
        reader.struct_begin();
        int16_t sub_field = 0;
        ThriftType sub_type = ThriftType::STOP;
        while (reader.read_field_begin(sub_field, sub_type)) {
          if (1 == sub_field) {
            dictionary_values = reader.read_i32();
          } else {
            reader.skip(sub_type);
          }
        }
        reader.struct_end();
        break;
      }
      case 8: { // data_page_header_v2
        reader.struct_begin();
        int16_t sub_field = 0;
        ThriftType sub_type = ThriftType::STOP;
        while (reader.read_field_begin(sub_field, sub_type)) {
          switch (sub_field) {
          case 1:
            num_values = reader.read_i32();
            break;
          case 4:
            encoding = reader.read_i32();
            break;
          case 5:
            def_levels_length = reader.read_i32();
            break;
          case 6:
            rep_levels_length = reader.read_i32();
            break;
          case 7:
            is_compressed = (ThriftType::BOOLEAN_TRUE == sub_type);
            break;
          default:
            reader.skip(sub_type);
            break;
          }
        }
        reader.struct_end();
        break;
      }
      default:
        reader.skip(type);
        break;
      }
    }
    consumed = reader.position();
    parsed = (consumed > 0 && consumed <= header.size());
    if (!parsed) {
      break;
    }
  }
  if (!parsed) {
    error.code = 1;
    error.message = "cannot read the page header of the column";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  // Give the bytes that do not belong to the header back to the stream.
  if (header.size() > consumed) {
    std::fseek(stream_, -static_cast<long>(header.size() - consumed), SEEK_CUR);
  }
  return PARQUET_OK;
}

int ParquetReader::decode_column(size_t column_index, FileError &error)
{
  const ParquetColumnChunk &chunk = row_groups_[row_group_index_].columns[column_index];
  const ParquetColumnDef &leaf = leaves_[column_index];
  std::vector<Value> &values = row_group_values_[column_index];
  values.clear();

  if (CODEC_UNCOMPRESSED != chunk.codec && CODEC_SNAPPY != chunk.codec) {
    error.code = 1;
    error.message = "the column uses the unsupported compression codec " + std::to_string(chunk.codec);
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }

  const int64_t start = chunk.dictionary_page_offset >= 0 ? chunk.dictionary_page_offset
                                                          : chunk.data_page_offset;
  std::fseek(stream_, static_cast<long>(start), SEEK_SET);

  std::vector<Value> dictionary;
  int64_t produced = 0;
  while (produced < chunk.num_values) {
    int32_t page_type = PAGE_DATA;
    int32_t compressed_size = 0;
    int32_t num_values = 0;
    int32_t encoding = ENCODING_PLAIN;
    int32_t def_levels_length = 0;
    int32_t rep_levels_length = 0;
    bool is_compressed = false;
    int32_t dictionary_values = 0;
    const int header_ret = read_page_header(page_type, compressed_size, num_values, encoding,
                                            def_levels_length, rep_levels_length, is_compressed,
                                            dictionary_values, error);
    if (PARQUET_ERROR == header_ret) {
      return PARQUET_ERROR;
    }
    if (compressed_size < 0) {
      error.code = 1;
      error.message = "the page has a negative size";
      error.line = 0;
      error.column = 0;
      return PARQUET_ERROR;
    }
    std::string page;
    page.resize(static_cast<size_t>(compressed_size));
    if (compressed_size > 0
        && static_cast<size_t>(compressed_size)
             != std::fread(&page[0], 1, static_cast<size_t>(compressed_size), stream_)) {
      error.code = 1;
      error.message = "the page of the column is truncated";
      error.line = 0;
      error.column = 0;
      return PARQUET_ERROR;
    }

    if (CODEC_SNAPPY == chunk.codec && is_compressed && PAGE_DATA_V2 != page_type && !page.empty()) {
      std::string uncompressed;
      if (!snappy_decompress(page.data(), page.size(), uncompressed)) {
        error.code = 1;
        error.message = "the snappy block of the column cannot be decompressed";
        error.line = 0;
        error.column = 0;
        return PARQUET_ERROR;
      }
      page.swap(uncompressed);
    }

    if (PAGE_DICTIONARY == page_type) {
      dictionary.clear();
      const int ret = decode_plain_values(page.data(), page.size(), leaf.physical_type,
                                          leaf.type_length, dictionary_values, dictionary, error);
      if (PARQUET_ERROR == ret) {
        return PARQUET_ERROR;
      }
      continue;
    }
    if (PAGE_DATA != page_type && PAGE_DATA_V2 != page_type) {
      // The index pages do not hold any values.
      continue;
    }
    if (num_values <= 0) {
      error.code = 1;
      error.message = "the page of the column does not announce any value";
      error.line = 0;
      error.column = 0;
      return PARQUET_ERROR;
    }

    // Split the page into the level part and the value part.
    std::string levels;
    std::string payload;
    std::vector<int32_t> definition_levels;
    if (PAGE_DATA == page_type) {
      size_t offset = 0;
      if (leaf.max_def_level > 0) {
        if (offset + 4 > page.size()) {
          error.code = 1;
          error.message = "the page of the column is truncated";
          error.line = 0;
          error.column = 0;
          return PARQUET_ERROR;
        }
        const uint32_t length = read_uint32_le(page.data() + offset);
        offset += 4;
        if (offset + length > page.size()) {
          error.code = 1;
          error.message = "the page of the column is truncated";
          error.line = 0;
          error.column = 0;
          return PARQUET_ERROR;
        }
        levels.assign(page.data() + offset, length);
        offset += length;
      }
      payload.assign(page.data() + offset, page.size() - offset);
    } else {
      if (static_cast<size_t>(rep_levels_length) + static_cast<size_t>(def_levels_length) > page.size()) {
        error.code = 1;
        error.message = "the page of the column is truncated";
        error.line = 0;
        error.column = 0;
        return PARQUET_ERROR;
      }
      size_t offset = static_cast<size_t>(rep_levels_length);
      levels.assign(page.data() + offset, static_cast<size_t>(def_levels_length));
      offset += static_cast<size_t>(def_levels_length);
      payload.assign(page.data() + offset, page.size() - offset);
      // In a v2 data page only the value part is compressed.
      if (CODEC_SNAPPY == chunk.codec && is_compressed && !payload.empty()) {
        std::string uncompressed;
        if (!snappy_decompress(payload.data(), payload.size(), uncompressed)) {
          error.code = 1;
          error.message = "the snappy block of the column cannot be decompressed";
          error.line = 0;
          error.column = 0;
          return PARQUET_ERROR;
        }
        payload.swap(uncompressed);
      }
    }

    if (leaf.max_def_level > 0) {
      RleHybridDecoder decoder;
      decoder.reset(levels.data(), levels.size(), level_bit_width(leaf.max_def_level));
      decoder.decode(num_values, definition_levels);
    }

    // Decode the values of the page.
    // The definition levels hold one entry per value, the NULL values are the
    // ones whose level is smaller than the largest level of the column.
    int64_t null_count = 0;
    for (size_t i = 0; i < definition_levels.size(); ++i) {
      if (definition_levels[i] < leaf.max_def_level) {
        ++null_count;
      }
    }
    const int64_t value_count = num_values - null_count;
    std::vector<Value> page_values;
    if (ENCODING_RLE_DICTIONARY == encoding || ENCODING_PLAIN_DICTIONARY == encoding) {
      if (payload.empty()) {
        error.code = 1;
        error.message = "the page of the column is truncated";
        error.line = 0;
        error.column = 0;
        return PARQUET_ERROR;
      }
      const int32_t index_width = static_cast<unsigned char>(payload[0]);
      std::vector<int32_t> indexes;
      RleHybridDecoder decoder;
      decoder.reset(payload.data() + 1, payload.size() - 1, index_width);
      decoder.decode(value_count, indexes);
      for (size_t i = 0; i < indexes.size(); ++i) {
        if (indexes[i] < 0 || static_cast<size_t>(indexes[i]) >= dictionary.size()) {
          error.code = 1;
          error.message = "the page refers to a dictionary entry that does not exist";
          error.line = 0;
          error.column = 0;
          return PARQUET_ERROR;
        }
        page_values.push_back(dictionary[indexes[i]]);
      }
    } else if (ENCODING_PLAIN == encoding) {
      const int ret = decode_plain_values(payload.data(), payload.size(), leaf.physical_type,
                                          leaf.type_length, value_count, page_values, error);
      if (PARQUET_ERROR == ret) {
        return PARQUET_ERROR;
      }
    } else {
      error.code = 1;
      error.message = "the page uses the unsupported encoding " + std::to_string(encoding);
      error.line = 0;
      error.column = 0;
      return PARQUET_ERROR;
    }

    // Put the values and the NULL markers back together.
    size_t value_index = 0;
    for (int64_t i = 0; i < num_values; ++i) {
      const int32_t level = (i < static_cast<int64_t>(definition_levels.size()))
                                ? definition_levels[i]
                                : leaf.max_def_level;
      if (level < leaf.max_def_level) {
        values.push_back(Value::make_null(leaf.inferred_type));
      } else if (value_index < page_values.size()) {
        values.push_back(page_values[value_index]);
        ++value_index;
      } else {
        error.code = 1;
        error.message = "the page holds fewer values than its header announces";
        error.line = 0;
        error.column = 0;
        return PARQUET_ERROR;
      }
    }
    produced += num_values;
  }
  return PARQUET_OK;
}

int ParquetReader::load_row_group(FileError &error)
{
  row_group_values_.assign(leaves_.size(), std::vector<Value>());
  if (row_group_index_ >= row_groups_.size()) {
    error.code = 1;
    error.message = "the file does not contain a row group";
    error.line = 0;
    error.column = 0;
    return PARQUET_ERROR;
  }
  for (size_t i = 0; i < projection_.size(); ++i) {
    const size_t column_index = projection_[i];
    const int ret = decode_column(column_index, error);
    if (PARQUET_ERROR == ret) {
      return PARQUET_ERROR;
    }
  }
  row_group_loaded_ = true;
  return PARQUET_OK;
}

int ParquetReader::next_record(std::vector<Value> &record, FileError &error)
{
  for (;;) {
    if (row_group_index_ >= row_groups_.size()) {
      return PARQUET_EOF;
    }
    if (!row_group_loaded_) {
      const int ret = load_row_group(error);
      if (PARQUET_ERROR == ret) {
        return PARQUET_ERROR;
      }
    }
    const ParquetRowGroup &group = row_groups_[row_group_index_];
    if (row_index_ >= group.num_rows) {
      ++row_group_index_;
      row_index_ = 0;
      row_group_loaded_ = false;
      row_group_values_.clear();
      continue;
    }
    record.clear();
    for (size_t i = 0; i < projection_.size(); ++i) {
      const size_t column_index = projection_[i];
      if (column_index < row_group_values_.size()
          && static_cast<size_t>(row_index_) < row_group_values_[column_index].size()) {
        record.push_back(row_group_values_[column_index][row_index_]);
      } else {
        record.push_back(Value::make_null(leaves_[column_index].inferred_type));
      }
    }
    ++row_index_;
    return PARQUET_OK;
  }
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase