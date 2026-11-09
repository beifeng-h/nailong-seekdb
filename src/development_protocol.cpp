#include "minivectordb/development_protocol.hpp"

#include <array>
#include <charconv>
#include <iomanip>
#include <istream>
#include <locale>
#include <ostream>
#include <type_traits>

namespace minivectordb::development {

namespace {

void write_floating(std::ostream& output, double value) {
  std::array<char, 64> buffer{};
  const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value,
                                   std::chars_format::scientific, static_cast<int>(output.precision()));
  if (result.ec != std::errc{}) throw std::runtime_error("Cannot format floating-point value");
  output.write(buffer.data(), static_cast<std::streamsize>(result.ptr - buffer.data()));
  output << std::defaultfloat;
}

}  // namespace

void write_string(std::ostream& output, std::string_view value) {
  constexpr char hex[] = "0123456789abcdef";
  output << '"';
  for (const unsigned char character : value) {
    if (character == '"' || character == '\\') output << '\\' << character;
    else if (character < 32) output << "\\u00" << hex[character >> 4] << hex[character & 15];
    else output << character;
  }
  output << '"';
}

void write_value(std::ostream& output, const SqlValue& cell) {
  std::visit([&](const auto& value) {
    using Current = std::decay_t<decltype(value)>;
    if constexpr (std::is_same_v<Current, std::string>) write_string(output, value);
    else if constexpr (std::is_same_v<Current, Vector>) {
      output << '[';
      for (std::size_t i = 0; i < value.size(); ++i) {
        if (i != 0) output << ',';
        write_floating(output, value[i]);
      }
      output << ']';
    } else if constexpr (std::is_same_v<Current, std::int64_t>) output << value;
    else write_floating(output, value);
  }, cell);
}

namespace {

void event(std::ostream& output, std::string_view name) {
  output << "{\"protocol\":";
  write_string(output, protocol_name);
  output << ",\"event\":";
  write_string(output, name);
}

void flush(std::ostream& output) {
  output << "}\n";
  output.flush();
  if (!output) throw std::runtime_error("Cannot write development protocol output");
}

}  // namespace

void run(SqlSession& session, std::istream& input, std::ostream& output) {
  output.imbue(std::locale::classic());
  output << std::setprecision(17);
  event(output, "ready");
  output << ",\"formal_ready\":false";
  flush(output);
  std::string line;
  while (std::getline(input, line)) {
    const auto first = line.find_first_not_of(" \t\r");
    if (first == std::string::npos) continue;
    const auto text = std::string_view(line).substr(first, line.find_last_not_of(" \t\r") - first + 1);
    if (text == "@quit") return;
    if (text == "@mode exact" || text == "@mode ann") {
      const bool ann = text == "@mode ann";
      session.set_search_mode(ann ? SearchMode::ann : SearchMode::exact);
      event(output, "mode");
      output << ",\"mode\":";
      write_string(output, ann ? "ann" : "exact");
      flush(output);
      continue;
    }
    if (text.starts_with('@')) {
      event(output, "error");
      output << ",\"kind\":\"command\",\"message\":\"Unsupported development command\"";
      flush(output);
      continue;
    }
    const SqlSink sink{
      [&](std::span<const SqlColumn> columns) {
        event(output, "columns");
        output << ",\"columns\":[";
        for (std::size_t i = 0; i < columns.size(); ++i) {
          if (i != 0) output << ',';
          write_string(output, columns[i].name);
        }
        output << ']'; flush(output);
      },
      [&](std::span<const SqlValue> row) {
        event(output, "row");
        output << ",\"values\":[";
        for (std::size_t i = 0; i < row.size(); ++i) {
          if (i != 0) output << ',';
          write_value(output, row[i]);
        }
        output << ']'; flush(output);
        return true;
      }
    };
    try {
      const auto result = session.execute(text, {}, sink);
      event(output, "complete");
      output << ",\"is_query\":" << (result.is_query ? "true" : "false")
             << ",\"affected_rows\":" << result.affected_rows << ",\"emitted_rows\":" << result.emitted_rows;
      flush(output);
    } catch (const SqlError& error) {
      if (!output) throw;
      event(output, "error");
      output << ",\"kind\":\"sql\",\"category\":" << static_cast<int>(error.category()) << ",\"message\":";
      write_string(output, error.what());
      flush(output);
    }
  }
  if (!input.eof()) throw std::runtime_error("Cannot read development protocol input");
}

}  // namespace minivectordb::development
