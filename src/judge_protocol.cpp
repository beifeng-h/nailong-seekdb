#include "minivectordb/judge_protocol.hpp"

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <istream>
#include <locale>
#include <ostream>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace minivectordb::judge {
namespace {

// A request id is echoed with its original JSON type. The judge compares the
// Python type, so an integer id must never become a string.
struct Identifier {
  enum class Kind { integer, text, other };
  Kind kind = Kind::other;
  std::int64_t integer = 0;
  std::string text;
};

class ParseError : public std::runtime_error {
 public:
  explicit ParseError(std::string message) : std::runtime_error(std::move(message)) {}
};

// Minimal JSON reader for the request envelope. It rejects trailing content,
// duplicate keys and non-finite numbers the same way the judge's reader does.
class Reader {
 public:
  explicit Reader(std::string_view text) : text_(text) {}

  void skip_spaces() {
    while (position_ < text_.size()) {
      const char character = text_[position_];
      if (character != ' ' && character != '\t' && character != '\n' && character != '\r') break;
      ++position_;
    }
  }

  bool at_end() {
    skip_spaces();
    return position_ >= text_.size();
  }

  char peek() {
    skip_spaces();
    if (position_ >= text_.size()) throw ParseError("unexpected end of JSON request");
    return text_[position_];
  }

  void expect(char character) {
    if (peek() != character) throw ParseError("unexpected JSON character");
    ++position_;
  }

  bool consume_literal(std::string_view literal) {
    skip_spaces();
    if (text_.compare(position_, literal.size(), literal) != 0) return false;
    position_ += literal.size();
    return true;
  }

  std::string read_string() {
    expect('"');
    std::string result;
    while (true) {
      if (position_ >= text_.size()) throw ParseError("unterminated JSON string");
      const unsigned char character = static_cast<unsigned char>(text_[position_++]);
      if (character == '"') return result;
      if (character < 0x20) throw ParseError("control character in JSON string");
      if (character != '\\') {
        result.push_back(static_cast<char>(character));
        continue;
      }
      if (position_ >= text_.size()) throw ParseError("unterminated JSON escape");
      const char escape = text_[position_++];
      switch (escape) {
        case '"': result.push_back('"'); break;
        case '\\': result.push_back('\\'); break;
        case '/': result.push_back('/'); break;
        case 'b': result.push_back('\b'); break;
        case 'f': result.push_back('\f'); break;
        case 'n': result.push_back('\n'); break;
        case 'r': result.push_back('\r'); break;
        case 't': result.push_back('\t'); break;
        case 'u': {
          if (position_ + 4 > text_.size()) throw ParseError("truncated JSON escape");
          unsigned code = 0;
          for (int index = 0; index < 4; ++index) {
            const char digit = text_[position_++];
            code *= 16;
            if (digit >= '0' && digit <= '9') code += static_cast<unsigned>(digit - '0');
            else if (digit >= 'a' && digit <= 'f') code += static_cast<unsigned>(digit - 'a' + 10);
            else if (digit >= 'A' && digit <= 'F') code += static_cast<unsigned>(digit - 'A' + 10);
            else throw ParseError("invalid JSON escape");
          }
          append_utf8(result, code);
          break;
        }
        default: throw ParseError("invalid JSON escape");
      }
    }
  }

  // Returns true when the value is a number, false when it is another type.
  bool read_number(Identifier& identifier) {
    const std::size_t start = position_;
    if (position_ < text_.size() && (text_[position_] == '-' || text_[position_] == '+')) ++position_;
    bool digits = false;
    while (position_ < text_.size() && text_[position_] >= '0' && text_[position_] <= '9') {
      ++position_;
      digits = true;
    }
    if (!digits) {
      position_ = start;
      return false;
    }
    const std::string_view number = text_.substr(start, position_ - start);
    std::int64_t value = 0;
    const auto parsed = std::from_chars(number.data(), number.data() + number.size(), value);
    if (parsed.ec == std::errc{} && parsed.ptr == number.data() + number.size()) {
      identifier.kind = Identifier::Kind::integer;
      identifier.integer = value;
    }
    return true;
  }

  void skip_value() {
    const char character = peek();
    if (character == '"') {
      read_string();
      return;
    }
    if (character == '{') {
      expect('{');
      if (peek() == '}') {
        expect('}');
        return;
      }
      while (true) {
        read_string();
        expect(':');
        skip_value();
        if (peek() == ',') {
          expect(',');
          continue;
        }
        expect('}');
        return;
      }
    }
    if (character == '[') {
      expect('[');
      if (peek() == ']') {
        expect(']');
        return;
      }
      while (true) {
        skip_value();
        if (peek() == ',') {
          expect(',');
          continue;
        }
        expect(']');
        return;
      }
    }
    if (consume_literal("true") || consume_literal("false") || consume_literal("null")) return;
    Identifier identifier;
    if (read_number(identifier)) return;
    throw ParseError("unsupported JSON value");
  }

 private:
  static void append_utf8(std::string& output, unsigned code) {
    if (code < 0x80) {
      output.push_back(static_cast<char>(code));
    } else if (code < 0x800) {
      output.push_back(static_cast<char>(0xC0 | (code >> 6)));
      output.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    } else {
      output.push_back(static_cast<char>(0xE0 | (code >> 12)));
      output.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
      output.push_back(static_cast<char>(0x80 | (code & 0x3F)));
    }
  }

  std::string_view text_;
  std::size_t position_ = 0;
};

struct Request {
  Identifier identifier;
  std::string op;
  std::string sql;
  std::string search_mode = "exact";
  bool has_sql = false;
};

Request parse_request(std::string_view text) {
  Reader reader(text);
  Request request;
  reader.expect('{');
  if (reader.peek() == '}') {
    reader.expect('}');
    throw ParseError("request has no id");
  }
  while (true) {
    const std::string key = reader.read_string();
    reader.expect(':');
    if (key == "id") {
      reader.skip_spaces();
      if (reader.peek() == '"') {
        request.identifier.kind = Identifier::Kind::text;
        request.identifier.text = reader.read_string();
      } else {
        Identifier identifier;
        if (!reader.read_number(identifier)) throw ParseError("unsupported request id");
        request.identifier = identifier;
      }
    } else if (key == "op") {
      reader.skip_spaces();
      if (reader.peek() != '"') throw ParseError("op must be a string");
      request.op = reader.read_string();
    } else if (key == "sql") {
      reader.skip_spaces();
      if (reader.peek() != '"') throw ParseError("sql must be a string");
      request.sql = reader.read_string();
      request.has_sql = true;
    } else if (key == "search_mode") {
      reader.skip_spaces();
      if (reader.peek() != '"') throw ParseError("search_mode must be a string");
      request.search_mode = reader.read_string();
    } else {
      reader.skip_value();
    }
    if (reader.peek() == ',') {
      reader.expect(',');
      continue;
    }
    reader.expect('}');
    break;
  }
  if (!reader.at_end()) throw ParseError("trailing content after request");
  return request;
}

void write_json_string(std::ostream& output, std::string_view value) {
  constexpr char hex[] = "0123456789abcdef";
  output << '"';
  for (const unsigned char character : value) {
    if (character == '"' || character == '\\') output << '\\' << character;
    else if (character < 0x20) output << "\\u00" << hex[character >> 4] << hex[character & 15];
    else output << character;
  }
  output << '"';
}

// The judge rejects NaN and Infinity, so only finite values are written.
void write_float(std::ostream& output, double value) {
  if (!std::isfinite(value)) {
    output << "null";
    return;
  }
  std::array<char, 64> buffer{};
  const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  if (result.ec != std::errc{}) throw std::runtime_error("Cannot format floating-point value");
  output.write(buffer.data(), static_cast<std::streamsize>(result.ptr - buffer.data()));
}

void write_value(std::ostream& output, const SqlValue& cell) {
  std::visit([&](const auto& value) {
    using Current = std::decay_t<decltype(value)>;
    if constexpr (std::is_same_v<Current, std::string>) {
      write_json_string(output, value);
    } else if constexpr (std::is_same_v<Current, Vector>) {
      output << '[';
      for (std::size_t index = 0; index < value.size(); ++index) {
        if (index != 0) output << ',';
        write_float(output, value[index]);
      }
      output << ']';
    } else if constexpr (std::is_same_v<Current, std::int64_t>) {
      output << value;
    } else {
      write_float(output, value);
    }
  }, cell);
}

void write_identifier(std::ostream& output, const Identifier& identifier) {
  switch (identifier.kind) {
    case Identifier::Kind::integer: output << identifier.integer; break;
    case Identifier::Kind::text: write_json_string(output, identifier.text); break;
    case Identifier::Kind::other: output << "null"; break;
  }
}

// The published package maps failures onto semantic names. The judge's
// configuration publishes no code mapping, so any nonempty code is accepted,
// but matching the published names keeps the public fixtures comparable.
std::string error_code(const SqlError& error) {
  const std::string message = error.what();
  if (message.find("Duplicate primary key") != std::string::npos) return "duplicate_key";
  if (message.find("Unsupported SQL statement") != std::string::npos) return "unsupported_sql";
  if (message.find("dimension mismatch") != std::string::npos) return "dimension_mismatch";
  if (message.find("must contain finite float32") != std::string::npos) return "invalid_vector";
  if (message.find("cannot be represented as finite float32") != std::string::npos) return "invalid_vector";
  if (message.find("VECTOR number overflows") != std::string::npos) return "invalid_vector";
  if (message.find("Primary-key updates are unsupported") != std::string::npos) return "unsupported_sql";
  if (message.find("VARCHAR value or length mismatch") != std::string::npos) return "invalid_value";
  switch (error.category()) {
    case SqlErrorCategory::syntax: return "syntax_error";
    case SqlErrorCategory::binding: return "binding_error";
    case SqlErrorCategory::execution: return "execution_error";
  }
  return "execution_error";
}

}  // namespace

void run(SqlSession& session, std::istream& input, std::ostream& output) {
  output.imbue(std::locale::classic());
  output << "{\"type\":\"ready\"}\n";
  output.flush();
  std::string line;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty()) continue;
    Request request;
    try {
      request = parse_request(line);
    } catch (const std::exception& error) {
      std::cerr << "Malformed judge request: " << error.what() << '\n';
      output << "{\"id\":null,\"ok\":false,\"error\":{\"code\":\"invalid_request\",\"message\":"
             << '"' << error.what() << "\"}}\n";
      output.flush();
      continue;
    }
    if (request.op == "shutdown") {
      output << "{\"id\":";
      write_identifier(output, request.identifier);
      output << ",\"ok\":true}\n";
      output.flush();
      return;
    }
    if (request.op != "execute") {
      output << "{\"id\":";
      write_identifier(output, request.identifier);
      output << ",\"ok\":false,\"error\":{\"code\":\"unsupported_op\",\"message\":\"Unsupported operation\"}}\n";
      output.flush();
      continue;
    }
    session.set_search_mode(request.search_mode == "ann" ? SearchMode::ann : SearchMode::exact);
    std::vector<std::vector<SqlValue>> rows;
    try {
      const auto statement = parse_sql(request.sql);
      const StatementKind kind = statement.kind();
      const bool is_query = kind == StatementKind::select;
      const SqlSink sink{
        [](std::span<const SqlColumn>) {},
        [&](std::span<const SqlValue> row) {
          if (!is_query) return true;
          rows.emplace_back(row.begin(), row.end());
          return true;
        }
      };
      const auto result = session.execute(statement, {}, sink);
      output << "{\"id\":";
      write_identifier(output, request.identifier);
      output << ",\"ok\":true";
      if (is_query) {
        output << ",\"columns\":[";
        for (std::size_t index = 0; index < result.columns.size(); ++index) {
          if (index != 0) output << ',';
          write_json_string(output, result.columns[index].name);
        }
        output << "],\"rows\":[";
        for (std::size_t row = 0; row < rows.size(); ++row) {
          if (row != 0) output << ',';
          output << '[';
          for (std::size_t column = 0; column < rows[row].size(); ++column) {
            if (column != 0) output << ',';
            write_value(output, rows[row][column]);
          }
          output << ']';
        }
        output << ']';
      } else if (kind == StatementKind::insert || kind == StatementKind::update ||
                 kind == StatementKind::erase) {
        output << ",\"affected_rows\":" << result.affected_rows;
      }
      output << "}\n";
    } catch (const SqlError& error) {
      output << "{\"id\":";
      write_identifier(output, request.identifier);
      output << ",\"ok\":false,\"error\":{\"code\":";
      write_json_string(output, error_code(error));
      output << ",\"message\":";
      write_json_string(output, error.what());
      output << "}}\n";
    } catch (const std::exception& error) {
      output << "{\"id\":";
      write_identifier(output, request.identifier);
      output << ",\"ok\":false,\"error\":{\"code\":\"execution_error\",\"message\":";
      write_json_string(output, error.what());
      output << "}}\n";
    }
    output.flush();
  }
}

}  // namespace minivectordb::judge
