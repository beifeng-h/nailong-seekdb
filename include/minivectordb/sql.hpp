#pragma once

#include "minivectordb/database.hpp"

#include <memory>
#include <stdexcept>

namespace minivectordb {

enum class SqlErrorCategory { syntax, binding, execution };

class SqlError : public std::runtime_error {
 public:
  SqlError(SqlErrorCategory category, std::string message,
           std::size_t offset = std::string::npos);

  SqlErrorCategory category() const noexcept;
  std::size_t offset() const noexcept;

 private:
  SqlErrorCategory category_;
  std::size_t offset_;
};

enum class StatementKind { create_table, insert, select, update, erase, begin, commit, rollback };

class ParsedStatement {
 public:
  ~ParsedStatement();
  ParsedStatement(ParsedStatement&&) noexcept;
  ParsedStatement& operator=(ParsedStatement&&) noexcept;
  ParsedStatement(const ParsedStatement&) = delete;
  ParsedStatement& operator=(const ParsedStatement&) = delete;

  StatementKind kind() const;
  std::size_t parameter_count() const;

 private:
  struct Impl;
  explicit ParsedStatement(std::unique_ptr<Impl> implementation);
  std::unique_ptr<Impl> impl_;
  friend ParsedStatement parse_sql(std::string_view source);
  friend class SqlSession;
};

ParsedStatement parse_sql(std::string_view source);

enum class SqlType { bigint, varchar, vector, real };
using SqlValue = std::variant<std::int64_t, std::string, Vector, double>;

struct SqlColumn {
  std::string name;
  SqlType type;
};

struct SqlSink {
  std::function<void(std::span<const SqlColumn>)> columns;
  std::function<bool(std::span<const SqlValue>)> row;
};

struct SqlResult {
  bool is_query = false;
  std::vector<SqlColumn> columns;
  std::uint64_t affected_rows = 0;
  std::uint64_t emitted_rows = 0;
  bool complete = true;
};

// Executes complete SQL with vector string literals or typed bindings.
// Contest framing, output serialization and judge codes are adapter concerns.
class SqlSession {
 public:
  explicit SqlSession(Database& database, std::size_t query_working_bytes = 1024 * 1024);

  SqlResult execute(std::string_view source, std::span<const Value> parameters = {},
                    const SqlSink& sink = {});
  SqlResult execute(const ParsedStatement& statement, std::span<const Value> parameters = {},
                    const SqlSink& sink = {});

 private:
  Database& database_;
  std::size_t query_working_bytes_;
};

}  // namespace minivectordb
