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

#include "sql/engine/file_table/ob_file_table_json.h"

#include <cerrno>
#include <cstdlib>
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
const int JSON_OK = 0;
const int JSON_ERROR = -1;
const size_t JSON_MAX_DEPTH = 32;

/** A small recursive descent parser for the flat objects of a JSON Lines file. */
class JsonParser
{
public:
  JsonParser(const std::string &text) : text_(text), pos_(0) {}

  bool parse_object(JsonObject &object, FileError &error)
  {
    skip_space();
    if (!expect('{')) {
      return fail(error, "the line does not start with a JSON object");
    }
    skip_space();
    if (peek() == '}') {
      ++pos_;
      skip_space();
      if (pos_ != text_.size()) {
        return fail(error, "unexpected data after the JSON object");
      }
      return true;
    }
    for (;;) {
      skip_space();
      std::string name;
      if (!parse_string(name, error)) {
        return false;
      }
      skip_space();
      if (!expect(':')) {
        return fail(error, "expected ':' after the member name");
      }
      skip_space();
      JsonMember member;
      member.name = name;
      if (!parse_value(member, error)) {
        return false;
      }
      for (size_t i = 0; i < object.members.size(); ++i) {
        if (object.members[i].name == member.name) {
          return fail(error, "duplicate member '" + member.name + "' in the JSON object");
        }
      }
      object.members.push_back(member);
      skip_space();
      if (peek() == ',') {
        ++pos_;
        continue;
      }
      if (peek() == '}') {
        ++pos_;
        break;
      }
      return fail(error, "expected ',' or '}' in the JSON object");
    }
    skip_space();
    if (pos_ != text_.size()) {
      return fail(error, "unexpected data after the JSON object");
    }
    return true;
  }

private:
  int peek() const { return pos_ < text_.size() ? static_cast<unsigned char>(text_[pos_]) : -1; }

  void skip_space()
  {
    while (pos_ < text_.size()) {
      const char c = text_[pos_];
      if (' ' == c || '\t' == c || '\r' == c || '\n' == c) {
        ++pos_;
      } else {
        break;
      }
    }
  }

  bool expect(char c)
  {
    if (pos_ < text_.size() && text_[pos_] == c) {
      ++pos_;
      return true;
    }
    return false;
  }

  bool fail(FileError &error, const std::string &message)
  {
    error.code = 1;
    error.message = message;
    error.line = 0;
    error.column = static_cast<long long>(pos_) + 1;
    return false;
  }

  bool parse_string(std::string &out, FileError &error)
  {
    if (!expect('"')) {
      return fail(error, "expected a string");
    }
    out.clear();
    while (pos_ < text_.size()) {
      const unsigned char c = static_cast<unsigned char>(text_[pos_]);
      if ('"' == c) {
        ++pos_;
        return true;
      }
      if ('\\' == c) {
        ++pos_;
        if (pos_ >= text_.size()) {
          return fail(error, "unterminated escape sequence");
        }
        const char escaped = text_[pos_++];
        switch (escaped) {
        case '"': out.push_back('"'); break;
        case '\\': out.push_back('\\'); break;
        case '/': out.push_back('/'); break;
        case 'b': out.push_back('\b'); break;
        case 'f': out.push_back('\f'); break;
        case 'n': out.push_back('\n'); break;
        case 'r': out.push_back('\r'); break;
        case 't': out.push_back('\t'); break;
        case 'u': {
          if (pos_ + 4 > text_.size()) {
            return fail(error, "truncated unicode escape sequence");
          }
          unsigned int code = 0;
          for (int i = 0; i < 4; ++i) {
            const char digit = text_[pos_ + i];
            code <<= 4;
            if (digit >= '0' && digit <= '9') {
              code |= static_cast<unsigned int>(digit - '0');
            } else if (digit >= 'a' && digit <= 'f') {
              code |= static_cast<unsigned int>(digit - 'a' + 10);
            } else if (digit >= 'A' && digit <= 'F') {
              code |= static_cast<unsigned int>(digit - 'A' + 10);
            } else {
              return fail(error, "invalid unicode escape sequence");
            }
          }
          pos_ += 4;
          // Encode the code point as UTF-8.
          if (code < 0x80) {
            out.push_back(static_cast<char>(code));
          } else if (code < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (code >> 6)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
          } else {
            out.push_back(static_cast<char>(0xE0 | (code >> 12)));
            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
          }
          break;
        }
        default:
          return fail(error, "invalid escape sequence in a string");
        }
        continue;
      }
      if (c < 0x20) {
        return fail(error, "a control character is not allowed in a string");
      }
      out.push_back(static_cast<char>(c));
      ++pos_;
    }
    return fail(error, "unterminated string");
  }

  bool parse_number(std::string &out, FileError &error)
  {
    const size_t begin = pos_;
    if (pos_ < text_.size() && ('-' == text_[pos_])) {
      ++pos_;
    }
    bool has_digit = false;
    while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
      has_digit = true;
      ++pos_;
    }
    if (!has_digit) {
      return fail(error, "expected a number");
    }
    if (pos_ < text_.size() && '.' == text_[pos_]) {
      ++pos_;
      bool has_fraction = false;
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
        has_fraction = true;
        ++pos_;
      }
      if (!has_fraction) {
        return fail(error, "expected digits after the decimal point");
      }
    }
    if (pos_ < text_.size() && ('e' == text_[pos_] || 'E' == text_[pos_])) {
      ++pos_;
      if (pos_ < text_.size() && ('+' == text_[pos_] || '-' == text_[pos_])) {
        ++pos_;
      }
      bool has_exponent = false;
      while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') {
        has_exponent = true;
        ++pos_;
      }
      if (!has_exponent) {
        return fail(error, "expected digits in the exponent");
      }
    }
    out = text_.substr(begin, pos_ - begin);
    return true;
  }

  bool parse_value(JsonMember &member, FileError &error)
  {
    const int c = peek();
    if (c < 0) {
      return fail(error, "unexpected end of the JSON object");
    }
    if ('{' == c) {
      return fail(error, "nested objects are not supported");
    }
    if ('[' == c) {
      return fail(error, "arrays are not supported");
    }
    if ('"' == c) {
      member.kind = JsonValueKind::STRING_VALUE;
      return parse_string(member.text, error);
    }
    if ('t' == c) {
      if (0 != text_.compare(pos_, 4, "true")) {
        return fail(error, "invalid literal");
      }
      pos_ += 4;
      member.kind = JsonValueKind::BOOLEAN_VALUE;
      member.boolean_value = true;
      return true;
    }
    if ('f' == c) {
      if (0 != text_.compare(pos_, 5, "false")) {
        return fail(error, "invalid literal");
      }
      pos_ += 5;
      member.kind = JsonValueKind::BOOLEAN_VALUE;
      member.boolean_value = false;
      return true;
    }
    if ('n' == c) {
      if (0 != text_.compare(pos_, 4, "null")) {
        return fail(error, "invalid literal");
      }
      pos_ += 4;
      member.kind = JsonValueKind::NULL_VALUE;
      return true;
    }
    if ('-' == c || (c >= '0' && c <= '9')) {
      member.kind = JsonValueKind::NUMBER_VALUE;
      if (!parse_number(member.text, error)) {
        return false;
      }
      errno = 0;
      char *end = nullptr;
      const long long parsed = std::strtoll(member.text.c_str(), &end, 10);
      if (0 == errno && nullptr != end && '\0' == *end) {
        member.is_integer = true;
        member.integer_value = parsed;
      } else {
        member.is_integer = false;
      }
      member.double_value = std::strtod(member.text.c_str(), nullptr);
      return true;
    }
    return fail(error, "unexpected character in the JSON object");
  }

private:
  const std::string &text_;
  size_t pos_;
};

} // namespace

int parse_json_object(const std::string &text, JsonObject &object, FileError &error)
{
  JsonParser parser(text);
  object.members.clear();
  return parser.parse_object(object, error) ? JSON_OK : JSON_ERROR;
}

JsonlReader::JsonlReader() {}

JsonlReader::~JsonlReader()
{
  close();
}

void JsonlReader::close()
{
  if (nullptr != stream_) {
    std::fclose(stream_);
    stream_ = nullptr;
  }
}

int JsonlReader::open(const std::string &path, const ReadOptions &options, FileError &error)
{
  path_ = path;
  options_ = options;
  line_ = 0;
  skipped_lines_ = 0;
  column_names_.clear();
  return open_stream(error);
}

int JsonlReader::open_stream(FileError &error)
{
  close();
  stream_ = std::fopen(path_.c_str(), "rb");
  if (nullptr == stream_) {
    error.code = errno ? errno : 1;
    error.message = "cannot open file '" + path_ + "': " + std::strerror(errno);
    error.line = 0;
    error.column = 0;
    return JSON_ERROR;
  }
  return JSON_OK;
}

int JsonlReader::read_line(std::string &line, FileError &error)
{
  line.clear();
  int c = std::fgetc(stream_);
  if (c < 0) {
    return 1;
  }
  while (c >= 0 && '\n' != c) {
    if ('\r' != c) {
      line.push_back(static_cast<char>(c));
    }
    c = std::fgetc(stream_);
  }
  ++line_;
  return 0;
}

int JsonlReader::parse_object(const std::string &line, JsonObject &object, FileError &error)
{
  const int ret = parse_json_object(line, object, error);
  if (JSON_ERROR == ret) {
    error.line = line_;
  }
  object.line = line_;
  return ret;
}

int JsonlReader::infer_schema(FileError &error)
{
  const int open_ret = open_stream(error);
  if (JSON_ERROR == open_ret) {
    return JSON_ERROR;
  }
  line_ = 0;
  column_names_.clear();
  std::vector<TypeInferrer> inferrers;
  std::vector<std::string> names;
  std::string text;
  JsonObject object;
  bool seen_object = false;
  for (;;) {
    const int line_ret = read_line(text, error);
    if (1 == line_ret) {
      break;
    }
    // Skip the empty lines, they do not contribute to the schema.
    bool only_space = true;
    for (size_t i = 0; i < text.size(); ++i) {
      if (' ' != text[i] && '\t' != text[i]) {
        only_space = false;
        break;
      }
    }
    if (only_space) {
      continue;
    }
    const int parse_ret = parse_object(text, object, error);
    if (JSON_ERROR == parse_ret) {
      if (!options_.ignore_errors) {
        return JSON_ERROR;
      }
      error = FileError();
      ++skipped_lines_;
      continue;
    }
    seen_object = true;
    for (size_t i = 0; i < object.members.size(); ++i) {
      const JsonMember &member = object.members[i];
      size_t index = names.size();
      for (size_t j = 0; j < names.size(); ++j) {
        if (names[j] == member.name) {
          index = j;
          break;
        }
      }
      if (index == names.size()) {
        names.push_back(member.name);
        inferrers.push_back(TypeInferrer());
      }
      switch (member.kind) {
      case JsonValueKind::NULL_VALUE:
        inferrers[index].observe_type(InferredType::NULL_TYPE);
        break;
      case JsonValueKind::BOOLEAN_VALUE:
        inferrers[index].observe_type(InferredType::BOOLEAN);
        break;
      case JsonValueKind::NUMBER_VALUE:
        inferrers[index].observe_type(member.is_integer ? InferredType::BIGINT : InferredType::DOUBLE);
        break;
      case JsonValueKind::STRING_VALUE:
        inferrers[index].observe_type(detect_text_type(member.text));
        break;
      }
    }
  }
  if (!seen_object) {
    error.code = 1;
    error.message = "the file does not contain a JSON object";
    error.line = 0;
    error.column = 0;
    return JSON_ERROR;
  }
  column_names_ = names;
  column_types_.clear();
  column_types_.resize(inferrers.size(), InferredType::VARCHAR);
  for (size_t i = 0; i < inferrers.size(); ++i) {
    column_types_[i] = inferrers[i].result();
  }
  return JSON_OK;
}

int JsonlReader::rescan(FileError &error)
{
  const int ret = open_stream(error);
  if (JSON_ERROR == ret) {
    return JSON_ERROR;
  }
  line_ = 0;
  return JSON_OK;
}

int JsonlReader::next_object(JsonObject &object, FileError &error)
{
  for (;;) {
    std::string text;
    const int line_ret = read_line(text, error);
    if (1 == line_ret) {
      return 1;
    }
    bool only_space = true;
    for (size_t i = 0; i < text.size(); ++i) {
      if (' ' != text[i] && '\t' != text[i]) {
        only_space = false;
        break;
      }
    }
    if (only_space) {
      continue;
    }
    const int parse_ret = parse_object(text, object, error);
    if (JSON_ERROR == parse_ret) {
      if (!options_.ignore_errors) {
        return JSON_ERROR;
      }
      error = FileError();
      ++skipped_lines_;
      continue;
    }
    return JSON_OK;
  }
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase