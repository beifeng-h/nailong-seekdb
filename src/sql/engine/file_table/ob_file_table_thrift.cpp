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

#include "sql/engine/file_table/ob_file_table_thrift.h"

#include <cstring>

namespace oceanbase
{
namespace sql
{
namespace file_table
{

uint8_t ThriftCompactReader::read_byte()
{
  if (pos_ >= size_) {
    failed_ = true;
    return 0;
  }
  return static_cast<uint8_t>(data_[pos_++]);
}

uint64_t ThriftCompactReader::read_varint()
{
  uint64_t result = 0;
  int shift = 0;
  for (;;) {
    const uint8_t byte = read_byte();
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

int64_t ThriftCompactReader::read_zigzag()
{
  const uint64_t raw = read_varint();
  return static_cast<int64_t>((raw >> 1) ^ (~(raw & 1u) + 1u));
}

void ThriftCompactReader::struct_begin()
{
  field_stack_.push_back(last_field_id_);
  last_field_id_ = 0;
}

void ThriftCompactReader::struct_end()
{
  if (!field_stack_.empty()) {
    last_field_id_ = field_stack_.back();
    field_stack_.pop_back();
  }
}

bool ThriftCompactReader::read_field_begin(int16_t &field_id, ThriftType &type)
{
  if (pos_ >= size_) {
    failed_ = true;
    return false;
  }
  const uint8_t header = read_byte();
  if (0 == header) {
    type = ThriftType::STOP;
    return false;
  }
  const uint8_t delta = (header & 0xF0u) >> 4;
  const uint8_t raw_type = header & 0x0Fu;
  if (0 == delta) {
    last_field_id_ = static_cast<int16_t>(read_zigzag());
  } else {
    last_field_id_ = static_cast<int16_t>(last_field_id_ + delta);
  }
  field_id = last_field_id_;
  type = static_cast<ThriftType>(raw_type);
  return true;
}

int64_t ThriftCompactReader::read_i64()
{
  return read_zigzag();
}

int32_t ThriftCompactReader::read_i32()
{
  return static_cast<int32_t>(read_zigzag());
}

int16_t ThriftCompactReader::read_i16()
{
  return static_cast<int16_t>(read_zigzag());
}

bool ThriftCompactReader::read_bool()
{
  return true;
}

double ThriftCompactReader::read_double()
{
  uint64_t bits = 0;
  for (int i = 0; i < 8; ++i) {
    bits |= static_cast<uint64_t>(read_byte()) << (8 * i);
  }
  double result = 0.0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

std::string ThriftCompactReader::read_binary()
{
  const uint64_t length = read_varint();
  if (pos_ + length > size_) {
    failed_ = true;
    return std::string();
  }
  std::string result(data_ + pos_, static_cast<size_t>(length));
  pos_ += static_cast<size_t>(length);
  return result;
}

int32_t ThriftCompactReader::read_list_begin(ThriftType &element_type)
{
  const uint8_t header = read_byte();
  uint32_t size = (header & 0xF0u) >> 4;
  element_type = static_cast<ThriftType>(header & 0x0Fu);
  if (15 == size) {
    size = static_cast<uint32_t>(read_varint());
  }
  return static_cast<int32_t>(size);
}

int32_t ThriftCompactReader::read_map_begin(ThriftType &key_type, ThriftType &value_type)
{
  const uint32_t size = static_cast<uint32_t>(read_varint());
  if (size > 0) {
    const uint8_t header = read_byte();
    key_type = static_cast<ThriftType>((header & 0xF0u) >> 4);
    value_type = static_cast<ThriftType>(header & 0x0Fu);
  } else {
    key_type = ThriftType::STOP;
    value_type = ThriftType::STOP;
  }
  return static_cast<int32_t>(size);
}

void ThriftCompactReader::skip(ThriftType type)
{
  switch (type) {
  case ThriftType::BOOLEAN_TRUE:
  case ThriftType::BOOLEAN_FALSE:
    break;
  case ThriftType::BYTE:
    read_byte();
    break;
  case ThriftType::I16:
  case ThriftType::I32:
  case ThriftType::I64:
    read_varint();
    break;
  case ThriftType::DOUBLE:
    for (int i = 0; i < 8; ++i) {
      read_byte();
    }
    break;
  case ThriftType::BINARY: {
    const uint64_t length = read_varint();
    pos_ += static_cast<size_t>(length);
    break;
  }
  case ThriftType::LIST:
  case ThriftType::SET: {
    ThriftType element_type = ThriftType::STOP;
    const int32_t size = read_list_begin(element_type);
    for (int32_t i = 0; i < size; ++i) {
      skip(element_type);
    }
    break;
  }
  case ThriftType::MAP: {
    ThriftType key_type = ThriftType::STOP;
    ThriftType value_type = ThriftType::STOP;
    const int32_t size = read_map_begin(key_type, value_type);
    for (int32_t i = 0; i < size; ++i) {
      skip(key_type);
      skip(value_type);
    }
    break;
  }
  case ThriftType::STRUCT: {
    int16_t field_id = 0;
    ThriftType field_type = ThriftType::STOP;
    struct_begin();
    while (read_field_begin(field_id, field_type)) {
      skip(field_type);
    }
    struct_end();
    break;
  }
  default:
    break;
  }
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase