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

#include "sql/engine/file_table/ob_file_table_csv.h"

#include <cerrno>
#include <cstring>

#include "sql/engine/file_table/ob_file_table_type_infer.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

namespace
{
const size_t CSV_BUFFER_SIZE = 64 * 1024;

/** The result of a parse step: 0 is a record, 1 is the end of the file. */
const int CSV_OK = 0;
const int CSV_EOF = 1;
const int CSV_ERROR = -1;
} // namespace

CsvReader::CsvReader() : buffer_(CSV_BUFFER_SIZE) {}

CsvReader::~CsvReader()
{
  close();
}

void CsvReader::close()
{
  if (nullptr != stream_) {
    std::fclose(stream_);
    stream_ = nullptr;
  }
  buffer_pos_ = 0;
  buffer_len_ = 0;
  stream_eof_ = false;
}

int CsvReader::open(const std::string &path, const ReadOptions &options, FileError &error)
{
  path_ = path;
  options_ = options;
  line_ = 1;
  record_line_ = 1;
  column_ = 0;
  skipped_records_ = 0;
  column_names_.clear();
  return open_stream(error);
}

int CsvReader::open_stream(FileError &error)
{
  close();
  stream_ = std::fopen(path_.c_str(), "rb");
  if (nullptr == stream_) {
    error.code = errno ? errno : 1;
    error.message = "cannot open file '" + path_ + "': " + std::strerror(errno);
    error.line = 0;
    error.column = 0;
    return CSV_ERROR;
  }
  buffer_pos_ = 0;
  buffer_len_ = 0;
  stream_eof_ = false;
  // A file may start with the UTF-8 byte order mark, it is not part of the
  // first column name.
  if (0xEF == peek_char()) {
    get_char();
    if (0xBB == peek_char()) {
      get_char();
      if (0xBF == peek_char()) {
        get_char();
      }
    }
  }
  return CSV_OK;
}

int CsvReader::fill_buffer()
{
  if (stream_eof_) {
    return 0;
  }
  const size_t read = std::fread(buffer_.data(), 1, buffer_.size(), stream_);
  buffer_pos_ = 0;
  buffer_len_ = read;
  if (0 == read) {
    stream_eof_ = true;
  }
  return read > 0 ? 1 : 0;
}

int CsvReader::peek_char()
{
  if (buffer_pos_ >= buffer_len_) {
    if (0 == fill_buffer()) {
      return -1;
    }
  }
  return static_cast<unsigned char>(buffer_[buffer_pos_]);
}

int CsvReader::get_char()
{
  const int c = peek_char();
  if (c >= 0) {
    ++buffer_pos_;
    if ('\n' == c) {
      ++line_;
    }
  }
  return c;
}

bool CsvReader::validate_utf8(const std::string &text) const
{
  size_t index = 0;
  const size_t length = text.size();
  while (index < length) {
    const unsigned char c = static_cast<unsigned char>(text[index]);
    size_t extra = 0;
    unsigned int code = 0;
    if (c < 0x80) {
      ++index;
      continue;
    } else if (0xC2 == (c & 0xFE)) {
      extra = 1;
      code = c & 0x1Fu;
    } else if (0xE0 == (c & 0xF0)) {
      extra = 2;
      code = c & 0x0Fu;
    } else if (0xF0 == (c & 0xF8)) {
      extra = 3;
      code = c & 0x07u;
    } else {
      return false;
    }
    if (index + extra >= length) {
      return false;
    }
    for (size_t i = 1; i <= extra; ++i) {
      const unsigned char continuation = static_cast<unsigned char>(text[index + i]);
      if (0x80 != (continuation & 0xC0)) {
        return false;
      }
      code = (code << 6) | (continuation & 0x3Fu);
    }
    if ((1 == extra && code < 0x80) || (2 == extra && code < 0x800)
        || (3 == extra && code < 0x10000)) {
      // An overlong encoding is not valid UTF-8.
      return false;
    }
    if (code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF)) {
      return false;
    }
    index += extra + 1;
  }
  return true;
}

int CsvReader::parse_field(CsvField &field, FileError &error)
{
  field.text.clear();
  field.quoted = false;
  const int first = peek_char();
  if (options_.quote == first) {
    field.quoted = true;
    get_char();
    for (;;) {
      const int c = get_char();
      if (c < 0) {
        error.code = 1;
        error.message = "unterminated quoted field";
        error.line = record_line_;
        error.column = column_;
        return CSV_ERROR;
      }
      if (options_.escape != options_.quote && options_.escape == c) {
        const int escaped = get_char();
        if (escaped < 0) {
          error.code = 1;
          error.message = "unterminated escape sequence";
          error.line = record_line_;
          error.column = column_;
          return CSV_ERROR;
        }
        field.text.push_back(static_cast<char>(escaped));
        continue;
      }
      if (options_.quote == c) {
        const int next = peek_char();
        if (options_.quote == next) {
          get_char();
          field.text.push_back(options_.quote);
          continue;
        }
        break;
      }
      field.text.push_back(static_cast<char>(c));
    }
    const int next = peek_char();
    if (next >= 0 && options_.delimiter != next && '\n' != next && '\r' != next) {
      error.code = 1;
      error.message = "unexpected data after the closing quote";
      error.line = record_line_;
      error.column = column_;
      return CSV_ERROR;
    }
  } else {
    for (;;) {
      const int c = peek_char();
      if (c < 0 || options_.delimiter == c || '\n' == c || '\r' == c) {
        break;
      }
      if (options_.quote == c) {
        error.code = 1;
        error.message = "unexpected quote character in an unquoted field";
        error.line = record_line_;
        error.column = column_;
        return CSV_ERROR;
      }
      get_char();
      field.text.push_back(static_cast<char>(c));
    }
  }
  if (!validate_utf8(field.text)) {
    error.code = 1;
    error.message = "the field is not valid UTF-8";
    error.line = record_line_;
    error.column = column_;
    return CSV_ERROR;
  }
  return CSV_OK;
}

int CsvReader::parse_record(std::vector<CsvField> &record, FileError &error)
{
  record.clear();
  column_ = 0;
  if (peek_char() < 0) {
    return CSV_EOF;
  }
  for (;;) {
    CsvField field;
    const int ret = parse_field(field, error);
    if (CSV_ERROR == ret) {
      return CSV_ERROR;
    }
    record.push_back(field);
    ++column_;
    const int c = peek_char();
    if (options_.delimiter == c) {
      get_char();
      continue;
    }
    if ('\r' == c) {
      get_char();
      if ('\n' == peek_char()) {
        get_char();
      }
      return CSV_OK;
    }
    if ('\n' == c) {
      get_char();
      return CSV_OK;
    }
    if (c < 0) {
      return CSV_OK;
    }
    error.code = 1;
    error.message = "unexpected character after a field";
    error.line = record_line_;
    error.column = column_ + 1;
    return CSV_ERROR;
  }
}

int CsvReader::read_header(FileError &error)
{
  std::vector<CsvField> record;
  record_line_ = line_;
  const int ret = parse_record(record, error);
  if (CSV_ERROR == ret) {
    return CSV_ERROR;
  }
  if (CSV_EOF == ret) {
    error.code = 1;
    error.message = "the file is empty";
    error.line = 1;
    error.column = 0;
    return CSV_ERROR;
  }
  column_names_.clear();
  if (options_.has_header) {
    for (size_t i = 0; i < record.size(); ++i) {
      std::string name = record[i].text;
      const size_t begin = name.find_first_not_of(" \t");
      const size_t end = name.find_last_not_of(" \t");
      name = (std::string::npos == begin) ? std::string() : name.substr(begin, end - begin + 1);
      if (name.empty()) {
        error.code = 1;
        error.message = "the header of column " + std::to_string(i + 1) + " is empty";
        error.line = record_line_;
        error.column = static_cast<long long>(i) + 1;
        return CSV_ERROR;
      }
      // A repeated header name keeps the column addressable: the second
      // occurrence of "id" becomes "id__2", the third "id__3", and so on.
      std::string unique_name = name;
      for (int64_t occurrence = 2;; ++occurrence) {
        bool clash = false;
        for (size_t j = 0; !clash && j < column_names_.size(); ++j) {
          clash = (column_names_[j] == unique_name);
        }
        if (!clash) {
          break;
        }
        unique_name = name + "__" + std::to_string(occurrence);
      }
      column_names_.push_back(unique_name);
    }
  } else {
    for (size_t i = 0; i < record.size(); ++i) {
      column_names_.push_back("col" + std::to_string(i));
    }
  }
  return CSV_OK;
}

int CsvReader::infer_schema(FileError &error)
{
  const int ret = open_stream(error);
  if (CSV_ERROR == ret) {
    return CSV_ERROR;
  }
  line_ = 1;
  record_line_ = 1;
  const int header_ret = read_header(error);
  if (CSV_ERROR == header_ret) {
    return CSV_ERROR;
  }
  const size_t column_count = column_names_.size();
  std::vector<TypeInferrer> inferrers(column_count);
  std::vector<CsvField> record;
  for (;;) {
    record_line_ = line_;
    const int record_ret = parse_record(record, error);
    if (CSV_EOF == record_ret) {
      break;
    }
    if (CSV_ERROR == record_ret) {
      if (!options_.ignore_errors) {
        return CSV_ERROR;
      }
      error = FileError();
      ++skipped_records_;
      continue;
    }
    if (record.size() != column_count) {
      error.code = 1;
      error.message = "the record has " + std::to_string(record.size())
                      + " fields, but the file has " + std::to_string(column_count) + " columns";
      error.line = record_line_;
      error.column = 0;
      if (!options_.ignore_errors) {
        return CSV_ERROR;
      }
      ++skipped_records_;
      continue;
    }
    for (size_t i = 0; i < column_count; ++i) {
      const CsvField &field = record[i];
      if (!field.quoted && field.text == options_.null_string) {
        inferrers[i].observe_type(InferredType::NULL_TYPE);
      } else if (field.quoted && field.text.empty()) {
        // A quoted empty field is an empty string and not a NULL value.
        inferrers[i].observe_type(InferredType::VARCHAR);
      } else {
        inferrers[i].observe_type(detect_text_type(field.text));
      }
    }
  }
  column_types_.clear();
  column_types_.resize(column_count, InferredType::VARCHAR);
  for (size_t i = 0; i < column_count; ++i) {
    column_types_[i] = inferrers[i].result();
  }
  return CSV_OK;
}

int CsvReader::rescan(FileError &error)
{
  const int ret = open_stream(error);
  if (CSV_ERROR == ret) {
    return CSV_ERROR;
  }
  line_ = 1;
  record_line_ = 1;
  // The header line is always consumed, the column names stay untouched.
  const int header_ret = read_header(error);
  if (CSV_ERROR == header_ret) {
    return CSV_ERROR;
  }
  return CSV_OK;
}

int CsvReader::next_record(std::vector<CsvField> &record, FileError &error)
{
  for (;;) {
    record_line_ = line_;
    const int ret = parse_record(record, error);
    if (CSV_EOF == ret) {
      return CSV_EOF;
    }
    if (CSV_ERROR == ret) {
      if (!options_.ignore_errors) {
        return CSV_ERROR;
      }
      error = FileError();
      ++skipped_records_;
      continue;
    }
    if (!column_names_.empty() && record.size() != column_names_.size()) {
      error.code = 1;
      error.message = "the record has " + std::to_string(record.size())
                      + " fields, but the file has " + std::to_string(column_names_.size())
                      + " columns";
      error.line = record_line_;
      error.column = 0;
      if (!options_.ignore_errors) {
        return CSV_ERROR;
      }
      ++skipped_records_;
      continue;
    }
    return CSV_OK;
  }
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase