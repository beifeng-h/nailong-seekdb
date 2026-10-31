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

#include "sql/engine/file_table/ob_file_table_type_infer.h"

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <string>

namespace oceanbase
{
namespace sql
{
namespace file_table
{

const char *to_string(InferredType type)
{
  switch (type) {
  case InferredType::NULL_TYPE: return "NULL";
  case InferredType::BOOLEAN: return "BOOLEAN";
  case InferredType::BIGINT: return "BIGINT";
  case InferredType::DOUBLE: return "DOUBLE";
  case InferredType::VARCHAR: return "VARCHAR";
  case InferredType::DATE: return "DATE";
  case InferredType::DATETIME: return "DATETIME";
  }
  return "VARCHAR";
}

std::string FileError::to_string() const
{
  std::string result = message;
  if (0 != line) {
    result += " (line " + std::to_string(line);
    if (0 != column) {
      result += ", column " + std::to_string(column);
    }
    result += ")";
  }
  return result;
}

InferredType merge_types(InferredType left, InferredType right)
{
  if (left == right) {
    return left;
  }
  if (InferredType::NULL_TYPE == left) {
    return right;
  }
  if (InferredType::NULL_TYPE == right) {
    return left;
  }
  if (InferredType::VARCHAR == left || InferredType::VARCHAR == right) {
    return InferredType::VARCHAR;
  }
  // The numeric branch: BOOLEAN widens into BIGINT, BIGINT widens into DOUBLE.
  const bool left_numeric = (InferredType::BOOLEAN == left || InferredType::BIGINT == left
                             || InferredType::DOUBLE == left);
  const bool right_numeric = (InferredType::BOOLEAN == right || InferredType::BIGINT == right
                              || InferredType::DOUBLE == right);
  if (left_numeric && right_numeric) {
    if (InferredType::DOUBLE == left || InferredType::DOUBLE == right) {
      return InferredType::DOUBLE;
    }
    if (InferredType::BIGINT == left || InferredType::BIGINT == right) {
      return InferredType::BIGINT;
    }
    return InferredType::BOOLEAN;
  }
  // The temporal branch: DATE widens into DATETIME.
  const bool left_temporal = (InferredType::DATE == left || InferredType::DATETIME == left);
  const bool right_temporal = (InferredType::DATE == right || InferredType::DATETIME == right);
  if (left_temporal && right_temporal) {
    return (InferredType::DATETIME == left || InferredType::DATETIME == right) ? InferredType::DATETIME
                                                                             : InferredType::DATE;
  }
  // A number and a date have no common type, the column widens into text.
  return InferredType::VARCHAR;
}

namespace
{

bool is_leap_year(int64_t year)
{
  return (0 == year % 4 && 0 != year % 100) || (0 == year % 400);
}

int days_in_month(int64_t year, int month)
{
  static const int days[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (2 == month && is_leap_year(year)) {
    return 29;
  }
  return days[month - 1];
}

bool all_digits(const char *data, size_t length)
{
  for (size_t i = 0; i < length; ++i) {
    if (data[i] < '0' || data[i] > '9') {
      return false;
    }
  }
  return true;
}

/**
 * Parse the "YYYY-MM-DD" prefix of a timestamp.  The separator between the
 * date and the time part may be a space or the ISO "T".
 */
bool parse_date_prefix(const std::string &text, int64_t &year, int &month, int &day)
{
  if (text.size() < 10) {
    return false;
  }
  if ('-' != text[4] || '-' != text[7]) {
    return false;
  }
  if (!all_digits(text.data(), 4) || !all_digits(text.data() + 5, 2)
      || !all_digits(text.data() + 8, 2)) {
    return false;
  }
  year = std::strtoll(text.substr(0, 4).c_str(), nullptr, 10);
  month = static_cast<int>(std::strtoll(text.substr(5, 2).c_str(), nullptr, 10));
  day = static_cast<int>(std::strtoll(text.substr(8, 2).c_str(), nullptr, 10));
  if (month < 1 || month > 12 || day < 1) {
    return false;
  }
  if (day > days_in_month(year, month)) {
    return false;
  }
  return true;
}

bool parse_time_suffix(const std::string &text, size_t offset)
{
  // The remainder is "HH:MM:SS" with an optional fractional part.
  if (text.size() < offset + 8) {
    return false;
  }
  if (':' != text[offset + 2] || ':' != text[offset + 5]) {
    return false;
  }
  if (!all_digits(text.data() + offset, 2) || !all_digits(text.data() + offset + 3, 2)
      || !all_digits(text.data() + offset + 6, 2)) {
    return false;
  }
  const int hour = static_cast<int>(std::strtoll(text.substr(offset, 2).c_str(), nullptr, 10));
  const int minute = static_cast<int>(std::strtol(text.substr(offset + 3, 2).c_str(), nullptr, 10));
  const int second = static_cast<int>(std::strtol(text.substr(offset + 6, 2).c_str(), nullptr, 10));
  if (hour > 23 || minute > 59 || second > 60) {
    return false;
  }
  size_t index = offset + 8;
  if (index < text.size()) {
    if ('.' != text[index]) {
      return false;
    }
    ++index;
    size_t digits = 0;
    while (index < text.size() && text[index] >= '0' && text[index] <= '9') {
      ++index;
      ++digits;
    }
    if (0 == digits || index != text.size()) {
      return false;
    }
  }
  return true;
}

bool looks_like_integer(const std::string &text)
{
  size_t index = 0;
  if (index < text.size() && ('-' == text[index] || '+' == text[index])) {
    ++index;
  }
  if (index >= text.size()) {
    return false;
  }
  return all_digits(text.data() + index, text.size() - index);
}

/**
 * Whether the text has the shape of a floating point number.
 *
 * The accepted shape is the JSON number shape: an optional sign, the digits,
 * an optional fraction and an optional exponent.  A date such as
 * "2024-01-02" does not have that shape and stays a text.
 */
bool looks_like_double(const std::string &text)
{
  size_t index = 0;
  const size_t length = text.size();
  if (index < length && ('-' == text[index] || '+' == text[index])) {
    ++index;
  }
  size_t digits = 0;
  while (index < length && text[index] >= '0' && text[index] <= '9') {
    ++index;
    ++digits;
  }
  if (0 == digits) {
    return false;
  }
  if (index < length && '.' == text[index]) {
    ++index;
    size_t fraction = 0;
    while (index < length && text[index] >= '0' && text[index] <= '9') {
      ++index;
      ++fraction;
    }
    if (0 == fraction) {
      return false;
    }
  }
  if (index < length && ('e' == text[index] || 'E' == text[index])) {
    ++index;
    if (index < length && ('-' == text[index] || '+' == text[index])) {
      ++index;
    }
    size_t exponent = 0;
    while (index < length && text[index] >= '0' && text[index] <= '9') {
      ++index;
      ++exponent;
    }
    if (0 == exponent) {
      return false;
    }
  }
  return index == length;
}

bool equals_ignore_case(const std::string &text, const char *other)
{
  const size_t length = std::strlen(other);
  if (text.size() != length) {
    return false;
  }
  for (size_t i = 0; i < length; ++i) {
    if (std::tolower(static_cast<unsigned char>(text[i]))
        != std::tolower(static_cast<unsigned char>(other[i]))) {
      return false;
    }
  }
  return true;
}

} // namespace

bool is_valid_date(const std::string &text)
{
  int64_t year = 0;
  int month = 0;
  int day = 0;
  if (!parse_date_prefix(text, year, month, day)) {
    return false;
  }
  return 10 == text.size();
}

bool is_valid_datetime(const std::string &text)
{
  if (text.size() < 19) {
    return false;
  }
  int64_t year = 0;
  int month = 0;
  int day = 0;
  if (!parse_date_prefix(text, year, month, day)) {
    return false;
  }
  const char separator = text[10];
  if (' ' != separator && 'T' != separator) {
    return false;
  }
  return parse_time_suffix(text, 11);
}

InferredType detect_text_type(const std::string &text)
{
  if (text.empty()) {
    return InferredType::NULL_TYPE;
  }
  if (equals_ignore_case(text, "true") || equals_ignore_case(text, "false")) {
    return InferredType::BOOLEAN;
  }
  if (looks_like_integer(text)) {
    errno = 0;
    char *end = nullptr;
    (void)std::strtoll(text.c_str(), &end, 10);
    if (0 == errno && nullptr != end && '\0' == *end) {
      return InferredType::BIGINT;
    }
    // The literal is an integer that does not fit into 64 bits, it still is a
    // number and widens into DOUBLE.
    return InferredType::DOUBLE;
  }
  if (looks_like_double(text)) {
    return InferredType::DOUBLE;
  }
  if (is_valid_datetime(text)) {
    return InferredType::DATETIME;
  }
  if (is_valid_date(text)) {
    return InferredType::DATE;
  }
  return InferredType::VARCHAR;
}

void TypeInferrer::observe_type(InferredType type)
{
  seen_ = true;
  if (InferredType::NULL_TYPE == type) {
    has_null_ = true;
  }
  type_ = merge_types(type_, type);
}

void TypeInferrer::observe_text(const std::string &text, const ReadOptions &options)
{
  if (text.empty() || (!options.null_string.empty() && text == options.null_string)) {
    observe_type(InferredType::NULL_TYPE);
    return;
  }
  observe_type(detect_text_type(text));
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase