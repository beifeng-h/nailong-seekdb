#pragma once

#include "minivectordb/sql.hpp"

namespace minivectordb::detail {

struct Identifier {
  std::string name;
  std::size_t offset = 0;
  std::string display_name = {};
};

enum class LiteralKind { number, string, parameter };

struct Literal {
  LiteralKind kind;
  std::string text;
  std::size_t parameter = 0;
  std::size_t offset = 0;
};

enum class ExpressionKind { column, star, distance };

struct Expression {
  ExpressionKind kind;
  Identifier column;
  std::optional<Literal> query;
  std::optional<Identifier> alias;
};

struct Condition {
  Identifier column;
  Relation relation;
  Literal value;
};

struct Order {
  Expression expression;
  bool descending = false;
};

struct CreateTableStatement {
  Schema schema;
};

struct InsertStatement {
  Identifier table;
  std::vector<Identifier> columns;
  std::vector<std::vector<Literal>> rows;
};

struct SelectStatement {
  Identifier table;
  std::vector<Expression> projections;
  std::vector<Condition> conditions;
  std::optional<Order> order;
  std::optional<Literal> limit;
};

struct UpdateAssignment {
  Identifier column;
  Literal value;
};

struct UpdateStatement {
  Identifier table;
  std::vector<UpdateAssignment> assignments;
  std::vector<Condition> conditions;
};

struct DeleteStatement {
  Identifier table;
  std::vector<Condition> conditions;
};

struct BeginStatement {};
struct CommitStatement {};
struct RollbackStatement {};
struct CreateVectorIndexStatement {
  Identifier name;
  Identifier table;
  Identifier column;
};

using Statement = std::variant<CreateTableStatement, InsertStatement, SelectStatement,
                               UpdateStatement, DeleteStatement, BeginStatement,
                               CommitStatement, RollbackStatement, CreateVectorIndexStatement>;

}  // namespace minivectordb::detail

namespace minivectordb {

struct ParsedStatement::Impl {
  detail::Statement statement;
  std::size_t parameters = 0;
};

}  // namespace minivectordb
