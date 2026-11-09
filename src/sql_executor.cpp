#include "sql_internal.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <numeric>
#include <unordered_set>
#include <utility>

namespace minivectordb {
namespace {

using namespace detail;

[[noreturn]] void binding(std::string message, std::size_t offset = std::string::npos) {
  throw SqlError(SqlErrorCategory::binding, std::move(message), offset);
}

Schema bind_table(Database& database, const Identifier& table) {
  try {
    return database.schema(table.name);
  } catch (const std::runtime_error& error) {
    binding(error.what(), table.offset);
  }
}

std::size_t bind_column(const Schema& schema, const Identifier& name) {
  for (std::size_t i = 0; i < schema.columns.size(); ++i) {
    if (schema.columns[i].name == name.name) return i;
  }
  binding("Unknown column " + name.name, name.offset);
}

Value bind_literal(const Literal& literal, std::span<const Value> parameters) {
  if (literal.kind == LiteralKind::parameter) return parameters[literal.parameter];
  if (literal.kind == LiteralKind::string) return literal.text;
  std::string_view text = literal.text;
  if (text.starts_with('+')) text.remove_prefix(1);
  std::int64_t value = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
  if (result.ec != std::errc{} || result.ptr != text.data() + text.size()) {
    binding("Expected a signed 64-bit integer", literal.offset);
  }
  return value;
}

bool decimal_underflows(std::string_view text) {
  if (text.starts_with('-')) text.remove_prefix(1);
  const auto exponent_position = text.find_first_of("eE");
  const auto mantissa = text.substr(0, exponent_position);
  const auto point = mantissa.find('.');
  const auto integer_digits = point == std::string_view::npos ? mantissa.size() : point;
  std::size_t digit_position = 0;
  std::optional<std::size_t> significant;
  for (const char character : mantissa) {
    if (character == '.') continue;
    if (character != '0' && !significant) significant = digit_position;
    ++digit_position;
  }
  if (!significant) return true;
  std::int64_t exponent = 0;
  if (exponent_position != std::string_view::npos) {
    auto digits = text.substr(exponent_position + 1);
    if (digits.starts_with('+')) digits.remove_prefix(1);
    const auto result = std::from_chars(digits.data(), digits.data() + digits.size(), exponent);
    if (result.ec == std::errc::result_out_of_range) return digits.starts_with('-');
  }
  const auto order = static_cast<long double>(integer_digits) - static_cast<long double>(*significant) - 1 +
                     static_cast<long double>(exponent);
  return order < 0;
}

Vector bind_vector_text(const Column& column, const Literal& literal) {
  const std::string_view text = literal.text;
  std::size_t position = 0;
  const auto space = [&] {
    while (position < text.size() && std::string_view(" \t\r\n\f\v").find(text[position]) != std::string_view::npos) ++position;
  };
  const auto delimiter = [&](char character) {
    space();
    if (position == text.size() || text[position] != character) binding("Malformed VECTOR string literal", literal.offset);
    ++position;
  };
  const auto digit = [](char character) { return character >= '0' && character <= '9'; };
  delimiter('[');
  space();
  std::vector<double> numbers;
  if (position < text.size() && text[position] != ']') {
    for (;;) {
      space();
      const auto start = position;
      if (position < text.size() && (text[position] == '+' || text[position] == '-')) ++position;
      const auto integer = position;
      while (position < text.size() && digit(text[position])) ++position;
      bool has_digits = position != integer;
      if (position < text.size() && text[position] == '.') {
        const auto fraction = ++position;
        while (position < text.size() && digit(text[position])) ++position;
        has_digits = has_digits || position != fraction;
      }
      if (!has_digits) binding("VECTOR components must be numbers", literal.offset);
      if (position < text.size() && (text[position] == 'e' || text[position] == 'E')) {
        ++position;
        if (position < text.size() && (text[position] == '+' || text[position] == '-')) ++position;
        const auto exponent = position;
        while (position < text.size() && digit(text[position])) ++position;
        if (position == exponent) binding("VECTOR exponent needs digits", literal.offset);
      }
      auto number = text.substr(start, position - start);
      if (number.starts_with('+')) number.remove_prefix(1);
      double value = 0;
      const auto parsed = std::from_chars(number.data(), number.data() + number.size(), value);
      if (parsed.ptr != number.data() + number.size() || parsed.ec == std::errc::invalid_argument) {
        binding("Invalid VECTOR number", literal.offset);
      }
      if (parsed.ec == std::errc::result_out_of_range) {
        if (!decimal_underflows(number)) binding("VECTOR number overflows", literal.offset);
        value = std::copysign(0.0, number.starts_with('-') ? -1.0 : 1.0);
      }
      numbers.push_back(value);
      space();
      if (position < text.size() && text[position] == ']') break;
      delimiter(',');
    }
  }
  delimiter(']');
  space();
  if (position != text.size()) binding("Unexpected text after VECTOR literal", literal.offset);
  if (numbers.size() != column.extent) binding("VECTOR dimension mismatch", literal.offset);
  try {
    return make_vector(numbers);
  } catch (const std::runtime_error& error) {
    binding(error.what(), literal.offset);
  }
}

Value bind_value(const Column& column, const Literal& literal, std::span<const Value> parameters) {
  Value value;
  if (column.type == Type::vector && literal.kind == LiteralKind::string) {
    value = bind_vector_text(column, literal);
  } else {
    value = bind_literal(literal, parameters);
  }
  try {
    validate_value(column, value);
  } catch (const std::runtime_error& error) {
    binding(error.what(), literal.offset);
  }
  return value;
}

std::vector<Filter> bind_conditions(const Schema& schema, const std::vector<Condition>& conditions,
                                   std::span<const Value> parameters) {
  std::vector<Filter> filters;
  for (const auto& condition : conditions) {
    const auto index = bind_column(schema, condition.column);
    const auto& column = schema.columns[index];
    if (column.type == Type::vector) binding("WHERE requires an ordinary column", condition.column.offset);
    auto value = bind_literal(condition.value, parameters);
    try {
      if (column.type == Type::varchar) {
        const auto* text = std::get_if<std::string>(&value);
        if (!text) binding("VARCHAR comparison requires a string", condition.value.offset);
        unicode_length(*text);
      } else {
        validate_value(column, value);
      }
    } catch (const SqlError&) {
      throw;
    } catch (const std::runtime_error& error) {
      binding(error.what(), condition.value.offset);
    }
    filters.push_back({index, condition.relation, std::move(value)});
  }
  return filters;
}

Predicate predicate_for(const std::vector<Filter>& filters) {
  if (filters.empty()) return {};
  return [&filters](const Row& row) { return matches(row, filters); };
}

SqlType sql_type(Type type) {
  switch (type) {
    case Type::bigint: return SqlType::bigint;
    case Type::varchar: return SqlType::varchar;
    case Type::vector: return SqlType::vector;
  }
  binding("Unknown column type");
}

struct BoundExpression {
  std::size_t column;
  std::optional<Vector> query;
  SqlColumn output;
};

BoundExpression bind_expression(const Schema& schema, const Expression& expression,
                                std::span<const Value> parameters) {
  const auto index = bind_column(schema, expression.column);
  const auto& column = schema.columns[index];
  const auto& label = column.display_name.empty() ? column.name : column.display_name;
  BoundExpression result{index, {}, {label, sql_type(column.type)}};
  if (expression.kind == ExpressionKind::distance) {
    if (column.type != Type::vector) binding("L2_DISTANCE requires a VECTOR column", expression.column.offset);
    result.query = std::get<Vector>(bind_value(column, *expression.query, parameters));
    result.output = {"L2_DISTANCE(" + label + ")", SqlType::real};
  }
  if (expression.alias) result.output.name = expression.alias->display_name;
  return result;
}

struct BoundSelect {
  Schema schema;
  std::vector<BoundExpression> projections;
  std::vector<Filter> filters;
  std::optional<BoundExpression> order;
  bool descending = false;
  std::optional<std::uint64_t> limit;
};

BoundSelect bind_select(Database& database, const SelectStatement& statement,
                        std::span<const Value> parameters) {
  BoundSelect result;
  result.schema = bind_table(database, statement.table);
  result.filters = bind_conditions(result.schema, statement.conditions, parameters);
  for (const auto& projection : statement.projections) {
    if (projection.kind == ExpressionKind::star) {
      for (std::size_t i = 0; i < result.schema.columns.size(); ++i) {
        const auto& column = result.schema.columns[i];
        result.projections.push_back({i, {}, {column.display_name.empty() ? column.name : column.display_name, sql_type(column.type)}});
      }
    } else {
      result.projections.push_back(bind_expression(result.schema, projection, parameters));
    }
  }
  if (statement.order) {
    const auto& expression = statement.order->expression;
    if (expression.kind == ExpressionKind::column) {
      // Explicit output aliases take precedence over input column names in ORDER BY.
      for (const auto& projection : statement.projections) {
        if (!projection.alias || projection.alias->name != expression.column.name) continue;
        if (result.order) binding("Ambiguous ORDER BY alias", expression.column.offset);
        result.order = bind_expression(result.schema, projection, parameters);
      }
    }
    if (!result.order) result.order = bind_expression(result.schema, expression, parameters);
    if (!result.order->query && result.order->output.type == SqlType::vector) {
      binding("ORDER BY requires an ordinary column or L2_DISTANCE", expression.column.offset);
    }
    result.descending = statement.order->descending;
  }
  if (statement.limit) {
    const auto value = bind_literal(*statement.limit, parameters);
    const auto* integer = std::get_if<std::int64_t>(&value);
    if (!integer || *integer < 0) binding("LIMIT requires a nonnegative BIGINT", statement.limit->offset);
    result.limit = static_cast<std::uint64_t>(*integer);
  }
  return result;
}

SqlResult execute_select(Database& database, const BoundSelect& statement,
                          const SqlSink& sink, std::size_t budget, SearchMode mode) {
  SqlResult result;
  result.is_query = true;
  for (const auto& projection : statement.projections) result.columns.push_back(projection.output);
  if (sink.columns) sink.columns(result.columns);
  if (statement.limit && *statement.limit == 0) return result;
  const auto emit = [&](const Row& row) {
    std::vector<SqlValue> values;
    if (sink.row) {
      values.reserve(statement.projections.size());
      for (const auto& projection : statement.projections) {
        if (projection.query) {
          values.emplace_back(l2_distance(std::get<Vector>(row[projection.column]), *projection.query));
        } else {
          std::visit([&](const auto& value) { values.emplace_back(value); }, row[projection.column]);
        }
      }
    }
    ++result.emitted_rows;
    if (sink.row && !sink.row(values)) {
      result.complete = false;
      return false;
    }
    return !statement.limit || result.emitted_rows < *statement.limit;
  };
  const auto predicate = predicate_for(statement.filters);
  const auto key_filter = std::find_if(statement.filters.begin(), statement.filters.end(), [&](const Filter& filter) {
    return filter.column == statement.schema.primary_key && filter.relation == Relation::equal;
  });
  if (key_filter != statement.filters.end()) {
    const auto row = database.get(statement.schema.name, std::get<std::int64_t>(key_filter->value));
    if (row && (!predicate || predicate(*row))) emit(*row);
    return result;
  }
  if (!statement.order) {
    database.scan(statement.schema.name, predicate, emit);
  } else if (statement.order->query) {
    database.scan_distance_ordered(statement.schema.name, *statement.order->query,
      statement.descending, statement.limit, predicate,
      [&](const Neighbor& neighbor) { return emit(neighbor.row); }, budget, mode);
  } else {
    database.scan_ordered(statement.schema.name, predicate, statement.order->column,
                          statement.descending, statement.limit, emit, budget);
  }
  return result;
}

class Executor {
 public:
  Executor(Database& database, std::span<const Value> parameters, const SqlSink& sink,
           std::size_t budget, SearchMode mode)
      : database_(database), parameters_(parameters), sink_(sink), budget_(budget), mode_(mode) {}

  SqlResult operator()(const CreateTableStatement& statement) const {
    try {
      validate_schema(statement.schema);
    } catch (const std::runtime_error& error) {
      binding(error.what());
    }
    database_.create_table(statement.schema);
    return {};
  }

  SqlResult operator()(const InsertStatement& statement) const {
    const auto schema = bind_table(database_, statement.table);
    std::vector<std::size_t> columns;
    if (statement.columns.empty()) {
      columns.resize(schema.columns.size());
      std::iota(columns.begin(), columns.end(), std::size_t(0));
    } else {
      std::unordered_set<std::size_t> seen;
      for (const auto& name : statement.columns) {
        const auto index = bind_column(schema, name);
        if (!seen.insert(index).second) binding("Repeated INSERT column", name.offset);
        columns.push_back(index);
      }
      if (columns.size() != schema.columns.size()) binding("INSERT must supply every column", statement.table.offset);
    }
    std::vector<Row> rows;
    rows.reserve(statement.rows.size());
    for (const auto& input : statement.rows) {
      if (input.size() != columns.size()) binding("INSERT column count mismatch", statement.table.offset);
      Row row(columns.size());
      for (std::size_t i = 0; i < input.size(); ++i) {
        row[columns[i]] = bind_value(schema.columns[columns[i]], input[i], parameters_);
      }
      rows.push_back(std::move(row));
    }
    database_.insert(schema.name, rows);
    SqlResult result;
    result.affected_rows = rows.size();
    return result;
  }

  SqlResult operator()(const SelectStatement& statement) const {
    const auto bound = bind_select(database_, statement, parameters_);
    return execute_select(database_, bound, sink_, budget_, mode_);
  }

  SqlResult operator()(const CreateVectorIndexStatement& statement) const {
    const auto schema = bind_table(database_, statement.table);
    const auto column = bind_column(schema, statement.column);
    if (schema.columns[column].type != Type::vector) {
      binding("Vector index requires a VECTOR column", statement.column.offset);
    }
    database_.create_vector_index(schema.name, statement.name.name, column);
    return {};
  }

  SqlResult operator()(const UpdateStatement& statement) const {
    const auto schema = bind_table(database_, statement.table);
    const auto filters = bind_conditions(schema, statement.conditions, parameters_);
    std::vector<Assignment> assignments;
    std::unordered_set<std::size_t> seen;
    for (const auto& input : statement.assignments) {
      const auto index = bind_column(schema, input.column);
      if (index == schema.primary_key) binding("Primary-key updates are unsupported", input.column.offset);
      if (!seen.insert(index).second) binding("Repeated UPDATE column", input.column.offset);
      assignments.push_back({index, bind_value(schema.columns[index], input.value, parameters_)});
    }
    SqlResult result;
    result.affected_rows = database_.update(schema.name, predicate_for(filters), assignments);
    return result;
  }

  SqlResult operator()(const DeleteStatement& statement) const {
    const auto schema = bind_table(database_, statement.table);
    const auto filters = bind_conditions(schema, statement.conditions, parameters_);
    SqlResult result;
    result.affected_rows = database_.erase(schema.name, predicate_for(filters));
    return result;
  }

  SqlResult operator()(const BeginStatement&) const { database_.begin(); return {}; }
  SqlResult operator()(const CommitStatement&) const { database_.commit(); return {}; }
  SqlResult operator()(const RollbackStatement&) const { database_.rollback(); return {}; }

 private:
  Database& database_;
  std::span<const Value> parameters_;
  const SqlSink& sink_;
  std::size_t budget_;
  SearchMode mode_;
};

}  // namespace

SqlSession::SqlSession(Database& database, std::size_t query_working_bytes)
    : database_(database), query_working_bytes_(query_working_bytes) {
  if (query_working_bytes == 0) throw std::invalid_argument("SQL query working budget must be positive");
}

void SqlSession::set_search_mode(SearchMode mode) noexcept { search_mode_ = mode; }
SearchMode SqlSession::search_mode() const noexcept { return search_mode_; }

SqlResult SqlSession::execute(std::string_view source, std::span<const Value> parameters, const SqlSink& sink) {
  return execute(parse_sql(source), parameters, sink);
}

SqlResult SqlSession::execute(const ParsedStatement& statement, std::span<const Value> parameters,
                              const SqlSink& sink) {
  if (!statement.impl_) binding("Cannot execute a moved-from SQL statement");
  if (statement.parameter_count() != parameters.size()) binding("SQL parameter count mismatch");
  try {
    return std::visit(Executor(database_, parameters, sink, query_working_bytes_, search_mode_), statement.impl_->statement);
  } catch (const SqlError&) {
    throw;
  } catch (const std::runtime_error& error) {
    throw SqlError(SqlErrorCategory::execution, error.what());
  }
}

}  // namespace minivectordb
