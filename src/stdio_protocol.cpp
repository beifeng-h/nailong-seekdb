#include "minivectordb/stdio_protocol.hpp"

#include "minivectordb/development_protocol.hpp"

#include <nlohmann/json.hpp>

#include <array>
#include <fstream>
#include <iomanip>
#include <locale>
#include <optional>

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

Json failure(const Json& id, std::string_view code, std::string_view message) {
  return {{"id", id}, {"ok", false}, {"error", {{"code", code}, {"message", message}}}};
}

void respond(std::ostream& output, const Json& response, ResponseRows* rows = nullptr) {
  auto text = response.dump(-1, ' ', false, Json::error_handler_t::replace);
  if (rows) {
    text.pop_back();
    output << text << ",\"rows\":[";
    rows->copy_to(output);
    output << "]}";
  } else {
    output << text;
  }
  output << '\n';
  output.flush();
  if (!output) throw std::runtime_error("Cannot flush protocol response");
}

std::string_view error_code(SqlErrorCategory category) {
  switch (category) {
    case SqlErrorCategory::syntax: return "syntax_error";
    case SqlErrorCategory::binding: return "binding_error";
    case SqlErrorCategory::execution: return "execution_error";
  }
  return "sql_error";
}

void execute(SqlSession& session, const Json& request, const Json& id,
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
      response = {{"id", id}, {"ok", true}, {"columns", Json::array()}};
      for (const auto& column : result.columns) response["columns"].push_back(column.name);
    } else {
      const auto result = session.execute(statement);
      response = {{"id", id}, {"ok", true}, {"affected_rows", result.affected_rows}};
    }
  } catch (const Json::exception& error) {
    rows.reset();
    response = failure(id, "invalid_request", error.what());
  } catch (const SqlError& error) {
    rows.reset();
    response = failure(id, error_code(error.category()), error.what());
  } catch (const std::invalid_argument& error) {
    rows.reset();
    response = failure(id, "invalid_request", error.what());
  } catch (const std::exception& error) {
    rows.reset();
    response = failure(id, "execution_error", error.what());
  }
  // Publishing is outside the recovery boundary. A broken stream ends the process.
  respond(output, response, rows ? &*rows : nullptr);
}

}  // namespace

void run(const std::filesystem::path& directory, std::istream& input, std::ostream& output) {
  output.imbue(std::locale::classic());
  std::optional<Json> shutdown_id;
  {
    Database database(directory);
    std::filesystem::remove(directory / response_file_name);
    SqlSession session(database);
    respond(output, {{"type", "ready"}});
    std::string line;
    while (std::getline(input, line)) {
      Json request;
      Json id = nullptr;
      std::string operation;
      try {
        request = Json::parse(line);
        id = request.at("id");
        operation = request.at("op").get<std::string>();
      } catch (const Json::exception& error) {
        respond(output, failure(id, "invalid_request", error.what()));
        continue;
      }
      if (operation == "shutdown") {
        shutdown_id = id;
        break;
      }
      if (operation != "execute") {
        respond(output, failure(id, "invalid_request", "Unknown operation"));
        continue;
      }
      execute(session, request, id, directory, output);
    }
    if (!input.eof() && !shutdown_id) throw std::runtime_error("Cannot read protocol input");
  }
  if (shutdown_id) respond(output, {{"id", *shutdown_id}, {"ok", true}});
}

}  // namespace minivectordb::stdio
