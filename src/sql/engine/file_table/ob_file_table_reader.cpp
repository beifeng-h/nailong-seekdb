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

#include "sql/engine/file_table/ob_file_table_reader.h"

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

#include "sql/engine/file_table/ob_file_table_csv.h"
#include "sql/engine/file_table/ob_file_table_json.h"
#include "sql/engine/file_table/ob_file_table_parquet.h"
#include "sql/engine/file_table/ob_file_table_type_infer.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

namespace
{

/** Build the value of a cell out of its text. */
Value make_text_value(const std::string &text, InferredType type)
{
  Value value;
  value.is_null = false;
  value.type = type;
  switch (type) {
  case InferredType::BOOLEAN:
    value.b = (0 == strcasecmp(text.c_str(), "true"));
    break;
  case InferredType::BIGINT:
    value.i64 = std::strtoll(text.c_str(), nullptr, 10);
    break;
  case InferredType::DOUBLE:
    value.f64 = std::strtod(text.c_str(), nullptr);
    break;
  default:
    value.str = text;
    break;
  }
  return value;
}

/** The reader of a CSV file. */
class CsvFileReader : public FileTableReader
{
public:
  CsvFileReader(const std::string &path, const ReadOptions &options) : path_(path), options_(options) {}

  int infer_schema(std::vector<ColumnDef> &schema, FileError &error) override
  {
    if (0 != csv_.open(path_, options_, error)) {
      return -1;
    }
    if (0 != csv_.infer_schema(error)) {
      return -1;
    }
    const std::vector<std::string> &names = csv_.column_names();
    const std::vector<InferredType> &types = csv_.column_types();
    const std::vector<bool> &nullables = csv_.column_nullable();
    schema.clear();
    for (size_t i = 0; i < names.size(); ++i) {
      ColumnDef column;
      column.name = names[i];
      column.type = i < types.size() ? types[i] : InferredType::VARCHAR;
      column.nullable = i < nullables.size() ? nullables[i] : true;
      schema.push_back(column);
    }
    return 0;
  }

  int rescan(FileError &error) override { return 0 == csv_.rescan(error) ? 0 : -1; }

  int next_row(std::vector<Value> &row, FileError &error) override
  {
    std::vector<CsvField> record;
    const int ret = csv_.next_record(record, error);
    if (ret != 0) {
      return ret;
    }
    const std::vector<InferredType> &types = csv_.column_types();
    row.clear();
    for (size_t i = 0; i < record.size(); ++i) {
      const InferredType type = i < types.size() ? types[i] : InferredType::VARCHAR;
      const bool is_null = !record[i].quoted && record[i].text == options_.null_string;
      if (is_null) {
        row.push_back(Value::make_null(type));
      } else {
        row.push_back(make_text_value(record[i].text, type));
      }
    }
    return 0;
  }

private:
  CsvReader csv_;
  std::string path_;
  ReadOptions options_;
};

/** The reader of a JSON Lines file. */
class JsonlFileReader : public FileTableReader
{
public:
  JsonlFileReader(const std::string &path, const ReadOptions &options) : path_(path), options_(options) {}

  int infer_schema(std::vector<ColumnDef> &schema, FileError &error) override
  {
    if (0 != json_.open(path_, options_, error)) {
      return -1;
    }
    if (0 != json_.infer_schema(error)) {
      return -1;
    }
    const std::vector<std::string> &names = json_.column_names();
    const std::vector<InferredType> &types = json_.column_types();
    schema.clear();
    for (size_t i = 0; i < names.size(); ++i) {
      ColumnDef column;
      column.name = names[i];
      column.type = i < types.size() ? types[i] : InferredType::VARCHAR;
      schema.push_back(column);
    }
    return 0;
  }

  int rescan(FileError &error) override { return 0 == json_.rescan(error) ? 0 : -1; }

  int next_row(std::vector<Value> &row, FileError &error) override
  {
    JsonObject object;
    const int ret = json_.next_object(object, error);
    if (ret != 0) {
      return ret;
    }
    const std::vector<std::string> &names = json_.column_names();
    const std::vector<InferredType> &types = json_.column_types();
    row.clear();
    for (size_t i = 0; i < names.size(); ++i) {
      const InferredType type = i < types.size() ? types[i] : InferredType::VARCHAR;
      Value value = Value::make_null(type);
      for (size_t j = 0; j < object.members.size(); ++j) {
        if (object.members[j].name != names[i]) {
          continue;
        }
        const JsonMember &member = object.members[j];
        switch (member.kind) {
        case JsonValueKind::NULL_VALUE:
          value = Value::make_null(type);
          break;
        case JsonValueKind::BOOLEAN_VALUE:
          value = Value::make_bool(member.boolean_value);
          break;
        case JsonValueKind::NUMBER_VALUE:
          if (InferredType::BIGINT == type) {
            value = Value::make_int(member.integer_value);
          } else {
            value = Value::make_double(member.double_value);
          }
          break;
        case JsonValueKind::STRING_VALUE:
          value = make_text_value(member.text, type);
          break;
        }
        break;
      }
      row.push_back(value);
    }
    return 0;
  }

private:
  JsonlReader json_;
  std::string path_;
  ReadOptions options_;
};

/** The reader of a Parquet file. */
class ParquetFileReader : public FileTableReader
{
public:
  ParquetFileReader(const std::string &path, const ReadOptions &options) : path_(path), options_(options) {}

  int infer_schema(std::vector<ColumnDef> &schema, FileError &error) override
  {
    if (0 != parquet_.open(path_, options_, error)) {
      return -1;
    }
    if (0 != parquet_.infer_schema(error)) {
      return -1;
    }
    schema = parquet_.schema();
    return 0;
  }

  int rescan(FileError &error) override { return 0 == parquet_.rescan(error) ? 0 : -1; }

  int next_row(std::vector<Value> &row, FileError &error) override
  {
    return parquet_.next_record(row, error);
  }

  int64_t row_count() const override { return parquet_.row_count(); }

private:
  ParquetReader parquet_;
  std::string path_;
  ReadOptions options_;
};

/** Match a file name against a pattern that may hold the wildcards '*' and '?'. */
bool match_pattern(const std::string &name, const std::string &pattern)
{
  size_t name_index = 0;
  size_t pattern_index = 0;
  size_t star_index = std::string::npos;
  size_t match_index = 0;
  while (name_index < name.size()) {
    if (pattern_index < pattern.size()
        && ('?' == pattern[pattern_index] || pattern[pattern_index] == name[name_index])) {
      ++name_index;
      ++pattern_index;
    } else if (pattern_index < pattern.size() && '*' == pattern[pattern_index]) {
      star_index = pattern_index;
      match_index = name_index;
      ++pattern_index;
    } else if (std::string::npos != star_index) {
      pattern_index = star_index + 1;
      ++match_index;
      name_index = match_index;
    } else {
      return false;
    }
  }
  while (pattern_index < pattern.size() && '*' == pattern[pattern_index]) {
    ++pattern_index;
  }
  return pattern_index == pattern.size();
}

} // namespace

int create_file_reader(FileFormat format,
                       const std::string &path,
                       const ReadOptions &options,
                       std::unique_ptr<FileTableReader> &reader,
                       FileError &error)
{
  switch (format) {
  case FileFormat::CSV:
    reader.reset(new CsvFileReader(path, options));
    break;
  case FileFormat::JSONL:
    reader.reset(new JsonlFileReader(path, options));
    break;
  case FileFormat::PARQUET:
    reader.reset(new ParquetFileReader(path, options));
    break;
  default:
    error.code = 1;
    error.message = "the file format is not supported";
    error.line = 0;
    error.column = 0;
    return -1;
  }
  return 0;
}

int glob_files(const std::string &pattern, std::vector<std::string> &files, FileError &error)
{
  files.clear();
  const size_t separator = pattern.find_last_of('/');
  const std::string directory = (std::string::npos == separator) ? std::string(".")
                                                               : pattern.substr(0, separator);
  const std::string name_pattern = (std::string::npos == separator) ? pattern
                                                                   : pattern.substr(separator + 1);
  DIR *handle = opendir(directory.c_str());
  if (nullptr == handle) {
    error.code = errno ? errno : 1;
    error.message = "cannot read the directory '" + directory + "': " + std::strerror(errno);
    error.line = 0;
    error.column = 0;
    return -1;
  }
  struct dirent *entry = nullptr;
  while (nullptr != (entry = readdir(handle))) {
    const std::string name = entry->d_name;
    if ("." == name || ".." == name) {
      continue;
    }
    if (!match_pattern(name, name_pattern)) {
      continue;
    }
    const std::string path = (std::string::npos == separator) ? name : directory + "/" + name;
    struct stat status;
    if (0 != stat(path.c_str(), &status) || !S_ISREG(status.st_mode)) {
      continue;
    }
    files.push_back(path);
  }
  closedir(handle);
  std::sort(files.begin(), files.end());
  return 0;
}

std::string real_path_of(const std::string &path)
{
  char buffer[PATH_MAX];
  if (nullptr == realpath(path.c_str(), buffer)) {
    return std::string();
  }
  return std::string(buffer);
}

int check_secure_path(const std::string &secure_file_priv, const std::string &path, FileError &error)
{
  error = FileError();
  if (secure_file_priv.empty()) {
    error.code = 1;
    error.message = "the variable secure_file_priv is empty, the file cannot be read";
    return -1;
  }
  const std::string real_secure = real_path_of(secure_file_priv);
  if (real_secure.empty()) {
    error.code = 1;
    error.message = "the directory of secure_file_priv '" + secure_file_priv + "' does not exist";
    return -1;
  }
  std::string real_file = real_path_of(path);
  if (real_file.empty()) {
    // The file does not exist, the check is done on the directory of it.
    const size_t separator = path.find_last_of('/');
    const std::string directory = (std::string::npos == separator) ? std::string(".")
                                                                 : path.substr(0, separator);
    real_file = real_path_of(directory);
    if (real_file.empty()) {
      error.code = 1;
      error.message = "cannot open the file '" + path + "'";
      return -1;
    }
    real_file += "/" + path.substr(separator + 1);
  }
  const bool is_root = ("/" == real_secure);
  if (real_file.compare(0, real_secure.size(), real_secure) != 0) {
    error.code = 1;
    error.message = "the file '" + path + "' is outside of the directory of secure_file_priv";
    return -1;
  }
  if (!is_root && real_file.size() > real_secure.size() && '/' != real_file[real_secure.size()]) {
    error.code = 1;
    error.message = "the file '" + path + "' is outside of the directory of secure_file_priv";
    return -1;
  }
  return 0;
}

} // namespace file_table
} // namespace sql
} // namespace oceanbase