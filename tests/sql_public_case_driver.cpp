#include "minivectordb/sql.hpp"

#include <fstream>
#include <iomanip>
#include <iostream>
#include <locale>
#include <stdexcept>
#include <type_traits>

using namespace minivectordb;

namespace {

void string(std::ostream& output, std::string_view value) {
  constexpr char hex[] = "0123456789abcdef";
  output << '"';
  for (const unsigned char character : value) {
    if (character == '"' || character == '\\') {
      output << '\\' << character;
    } else if (character < 32) {
      output << "\\u00" << hex[character >> 4] << hex[character & 15];
    } else {
      output << character;
    }
  }
  output << '"';
}

void value(std::ostream& output, const SqlValue& cell) {
  std::visit([&](const auto& current) {
    using Current = std::decay_t<decltype(current)>;
    if constexpr (std::is_same_v<Current, std::string>) {
      string(output, current);
    } else if constexpr (std::is_same_v<Current, Vector>) {
      output << '[';
      for (std::size_t i = 0; i < current.size(); ++i) {
        if (i != 0) output << ',';
        output << std::scientific << current[i] << std::defaultfloat;
      }
      output << ']';
    } else if constexpr (std::is_same_v<Current, std::int64_t>) {
      output << current;
    } else {
      output << std::scientific << current << std::defaultfloat;
    }
  }, cell);
}

}  // namespace

// Captures actual SqlSession results in a test artifact selected by argv.
int main(int argc, char** argv) {
  if (argc != 4) return 2;
  try {
    std::ifstream input(argv[2], std::ios::binary);
    std::ofstream output(argv[3], std::ios::binary);
    if (!input || !output) throw std::runtime_error("Cannot open public-case files");
    output.imbue(std::locale::classic());
    output << std::setprecision(17) << '[';
    Database database(argv[1]);
    SqlSession session(database);
    std::string sql;
    unsigned statements = 0;
    while (std::getline(input, sql)) {
      if (!sql.empty() && sql.back() == '\r') sql.pop_back();
      const auto first = sql.find_first_not_of(" \t");
      if (first == std::string::npos) continue;
      const auto command = std::string_view(sql).substr(first, sql.find_last_not_of(" \t") - first + 1);
      if (command == "--mode ann" || command == "--mode exact") {
        session.set_search_mode(command == "--mode ann" ? SearchMode::ann : SearchMode::exact);
        continue;
      }
      if (command.starts_with("--")) {
        throw std::runtime_error("Unsupported driver directive. Use public_samples.py for process restarts");
      }
      if (statements++ != 0) output << ',';
      std::vector<std::vector<SqlValue>> rows;
      try {
        const auto result = session.execute(sql, {}, {{}, [&](std::span<const SqlValue> row) {
          rows.emplace_back(row.begin(), row.end());
          return true;
        }});
        output << "{\"affected_rows\":" << result.affected_rows << ",\"columns\":[";
        for (std::size_t column = 0; column < result.columns.size(); ++column) {
          if (column != 0) output << ',';
          string(output, result.columns[column].name);
        }
        output << "],\"rows\":[";
        for (std::size_t row = 0; row < rows.size(); ++row) {
          if (row != 0) output << ',';
          output << '[';
          for (std::size_t column = 0; column < rows[row].size(); ++column) {
            if (column != 0) output << ',';
            value(output, rows[row][column]);
          }
          output << ']';
        }
        output << "]}";
      } catch (const SqlError& error) {
        output << "{\"error\":";
        string(output, error.what());
        output << ",\"category\":" << static_cast<int>(error.category()) << '}';
      }
    }
    if (!input.eof()) throw std::runtime_error("Cannot read public SQL input");
    output << "]\n";
    output.close();
    if (!output) throw std::runtime_error("Cannot write captured SQL results");
    std::cout << "CAPTURED statements=" << statements << '\n';
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
