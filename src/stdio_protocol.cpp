#include "minivectordb/stdio_protocol.hpp"

#include "minivectordb/development_protocol.hpp"

#include <nlohmann/json.hpp>

#include <array>
#include <fstream>
#include <iomanip>
#include <locale>
#include <optional>
#include <unordered_set>

namespace minivectordb::stdio {

namespace {

using Json = nlohmann::json;
constexpr std::string_view response_file_name = ".minivectordb-response.tmp";

// A failed query can discard its rows before publishing a single response.
class ResponseRows {
 public:
  explicit ResponseRows(const std::filesystem::path& directory)
      : path_(directory / response_file_name),
        file_(path_, std::ios::in | std::ios::out | std::ios::binary | std::ios::trunc) {
    if (!file_) throw std::runtime_error("Cannot open response workspace");
    file_.imbue(std::locale::classic());
    file_ << std::setprecision(17);
  }

  ~ResponseRows() {
    file_.close();
    std::error_code error;
    std::filesystem::remove(path_, error);
  }

  void append(std::span<const SqlValue> row) {
    if (has_rows_) file_ << ',';
    file_ << '[';
    for (std::size_t i = 0; i < row.size(); ++i) {
      if (i != 0) file_ << ',';
      development::write_value(file_, row[i]);
    }
    file_ << ']';
    if (!file_) throw std::runtime_error("Cannot write response workspace");
    has_rows_ = true;
  }

  void finish() {
    file_.flush();
    file_.seekg(0);
    if (!file_) throw std::runtime_error("Cannot rewind response workspace");
  }

  void copy_to(std::ostream& output) {
    std::array<char, 65536> buffer{};
    while (file_) {
      file_.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
      output.write(buffer.data(), file_.gcount());
      if (!output) throw std::runtime_error("Cannot write protocol response");
    }
    if (!file_.eof()) throw std::runtime_error("Cannot read response workspace");
  }

 private:
  std::filesystem::path path_;
  std::fstream file_;
  bool has_rows_ = false;
};

Json failure(std::string_view code, std::string_view message) {
  return {{"ok", false}, {"error", {{"code", code}, {"message", message}}}};
}

void respond(std::ostream& output, const Json& response,
             std::optional<std::string_view> identifier = {}, ResponseRows* rows = nullptr) {
  const auto text = response.dump(-1, ' ', false, Json::error_handler_t::replace);
  output << '{';
  if (identifier) output << "\"id\":" << *identifier << ',';
  output << std::string_view(text).substr(1, text.size() - 2);
  if (rows) {
    output << ",\"rows\":[";
    rows->copy_to(output);
    output << ']';
  }
  output << "}\n";
  output.flush();
  if (!output) throw std::runtime_error("Cannot flush protocol response");
}

Json parse_request(std::string_view line) {
  std::vector<std::unordered_set<std::string>> keys;
  return Json::parse(line, [&](int, Json::parse_event_t event, Json& value) {
    if (event == Json::parse_event_t::object_start) keys.emplace_back();
    if (event == Json::parse_event_t::key &&
        !keys.back().insert(value.get<std::string>()).second) {
      throw std::invalid_argument("Duplicate JSON object key");
    }
    if (event == Json::parse_event_t::object_end) keys.pop_back();
    return true;
  });
}

// The envelope has already been validated. Locate its id without converting numbers.
std::string_view identifier_text(std::string_view source) {
  const auto skip_string = [&](std::size_t position) {
    ++position;
    while (source[position] != '"') {
      if (source[position] == '\\') ++position;
      ++position;
    }
    return position + 1;
  };
  auto position = source.find('{') + 1;
  while (true) {
    position = source.find_first_not_of(" \t\r\n", position);
    const auto key_end = skip_string(position);
    const auto key = Json::parse(source.substr(position, key_end - position)).get<std::string>();
    position = source.find_first_not_of(" \t\r\n", key_end);
    const auto start = source.find_first_not_of(" \t\r\n", position + 1);
    position = start;
    std::size_t depth = 0;
    while (true) {
      const auto character = source[position];
      if (character == '"') {
        position = skip_string(position);
        continue;
      }
      if (character == '{' || character == '[') ++depth;
      if (character == '}' || character == ']') {
        if (depth == 0) break;
        --depth;
      }
      if (character == ',' && depth == 0) break;
      ++position;
    }
    if (key == "id") {
      const auto end = source.find_last_not_of(" \t\r\n", position - 1);
      return source.substr(start, end - start + 1);
    }
    ++position;
  }
}

std::string_view error_code(SqlErrorCategory category) {
  switch (category) {
    case SqlErrorCategory::syntax: return "syntax_error";
    case SqlErrorCategory::binding: return "binding_error";
    case SqlErrorCategory::execution: return "execution_error";
  }
  return "sql_error";
}

void execute(SqlSession& session, const Json& request, std::string_view id,
             const std::filesystem::path& directory, std::ostream& output) {
  Json response;
  std::optional<ResponseRows> rows;
  try {
    const auto& sql = request.at("sql").get_ref<const std::string&>();
    const auto& mode = request.at("search_mode").get_ref<const std::string&>();
    if (mode != "exact" && mode != "ann") throw std::invalid_argument("Unknown search_mode");
    session.set_search_mode(mode == "ann" ? SearchMode::ann : SearchMode::exact);
    const auto statement = parse_sql(sql);
    if (statement.kind() == StatementKind::select) {
      rows.emplace(directory);
      const SqlSink sink{{}, [&](std::span<const SqlValue> row) {
        rows->append(row);
        return true;
      }};
      const auto result = session.execute(statement, {}, sink);
      rows->finish();
      response = {{"ok", true}, {"columns", Json::array()}};
      for (const auto& column : result.columns) response["columns"].push_back(column.name);
    } else {
      const auto result = session.execute(statement);
      response = {{"ok", true}, {"affected_rows", result.affected_rows}};
    }
  } catch (const Json::exception& error) {
    rows.reset();
    response = failure("invalid_request", error.what());
  } catch (const SqlError& error) {
    rows.reset();
    response = failure(error_code(error.category()), error.what());
  } catch (const std::invalid_argument& error) {
    rows.reset();
    response = failure("invalid_request", error.what());
  } catch (const std::exception& error) {
    rows.reset();
    response = failure("execution_error", error.what());
  }
  // Publishing is outside the recovery boundary. A broken stream ends the process.
  respond(output, response, id, rows ? &*rows : nullptr);
}

}  // namespace

void run(const std::filesystem::path& directory, std::istream& input, std::ostream& output) {
  output.imbue(std::locale::classic());
  std::optional<std::string> shutdown_id;
  {
    Database database(directory);
    std::filesystem::remove(directory / response_file_name);
    SqlSession session(database);
    respond(output, {{"type", "ready"}});
    std::string line;
    while (std::getline(input, line)) {
      Json request;
      std::string_view id = "null";
      std::string operation;
      try {
        request = parse_request(line);
        request.at("id");
        id = identifier_text(line);
        operation = request.at("op").get<std::string>();
      } catch (const std::exception& error) {
        respond(output, failure("invalid_request", error.what()), id);
        continue;
      }
      if (operation == "shutdown") {
        shutdown_id = std::string(id);
        break;
      }
      if (operation != "execute") {
        respond(output, failure("invalid_request", "Unknown operation"), id);
        continue;
      }
      execute(session, request, id, directory, output);
    }
    if (!input.eof() && !shutdown_id) throw std::runtime_error("Cannot read protocol input");
  }
  if (shutdown_id) respond(output, {{"ok", true}}, *shutdown_id);
}

}  // namespace minivectordb::stdio
