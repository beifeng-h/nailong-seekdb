#include "sql_internal.hpp"

#include <charconv>
#include <limits>
#include <utility>

namespace minivectordb {
namespace {

using namespace detail;

enum class TokenKind { word, quoted_identifier, string, number, parameter, symbol, end };

struct Token {
  TokenKind kind;
  std::string text;
  std::size_t offset;
  std::string original = {};
};

[[noreturn]] void syntax(std::string message, std::size_t offset) {
  throw SqlError(SqlErrorCategory::syntax, std::move(message), offset);
}

bool digit(char character) {
  return character >= '0' && character <= '9';
}

bool letter(char character) {
  return (character >= 'a' && character <= 'z') ||
         (character >= 'A' && character <= 'Z') || character == '_';
}

std::string uppercase(std::string text) {
  for (char& character : text) {
    if (character >= 'a' && character <= 'z') {
      character = static_cast<char>(character - 'a' + 'A');
    }
  }
  return text;
}

bool reserved(std::string_view text) {
  static constexpr std::string_view words[] = {
      "CREATE", "TABLE", "PRIMARY", "KEY", "BIGINT", "VARCHAR", "VECTOR",
      "INSERT", "INTO", "VALUES", "SELECT", "FROM", "WHERE", "AND", "OR",
      "ORDER", "BY", "LIMIT", "ASC", "DESC", "AS", "UPDATE", "SET", "DELETE",
      "BEGIN", "COMMIT", "ROLLBACK", "NOT", "NULL", "JOIN", "UNION"};
  for (const auto word : words) {
    if (text == word) return true;
  }
  return false;
}

class Lexer {
 public:
  explicit Lexer(std::string_view source) : source_(source) {}

  Token next() {
    skip_space_and_comments();
    const auto start = position_;
    if (position_ == source_.size()) return {TokenKind::end, {}, position_};
    const char character = source_[position_];
    if (letter(character)) {
      ++position_;
      while (position_ < source_.size() &&
             (letter(source_[position_]) || digit(source_[position_]))) {
        ++position_;
      }
      auto original = std::string(source_.substr(start, position_ - start));
      return {TokenKind::word, uppercase(original), start, std::move(original)};
    }
    if (character == '\'' || character == '"') return quoted(character);
    if (digit(character) ||
        (character == '.' && position_ + 1 < source_.size() && digit(source_[position_ + 1]))) {
      return number();
    }
    ++position_;
    if (character == '?') return {TokenKind::parameter, "?", start};
    if (character == '<' || character == '>') {
      if (position_ < source_.size() &&
          (source_[position_] == '=' || (character == '<' && source_[position_] == '>'))) {
        ++position_;
      }
      return {TokenKind::symbol, std::string(source_.substr(start, position_ - start)), start};
    }
    if (std::string_view("(),;*=+-").find(character) != std::string_view::npos) {
      return {TokenKind::symbol, std::string(1, character), start};
    }
    syntax("Unsupported SQL character", start);
  }

 private:
  void skip_space_and_comments() {
    for (;;) {
      while (position_ < source_.size() &&
             std::string_view(" \t\r\n\f\v").find(source_[position_]) != std::string_view::npos) {
        ++position_;
      }
      if (source_.substr(position_, 2) == "--") {
        position_ += 2;
        while (position_ < source_.size() && source_[position_] != '\n') ++position_;
        continue;
      }
      if (source_.substr(position_, 2) == "/*") {
        const auto start = position_;
        position_ += 2;
        unsigned depth = 1;
        while (depth != 0) {
          if (position_ == source_.size()) syntax("Unterminated SQL comment", start);
          if (source_.substr(position_, 2) == "/*") {
            ++depth;
            position_ += 2;
          } else if (source_.substr(position_, 2) == "*/") {
            --depth;
            position_ += 2;
          } else {
            ++position_;
          }
        }
        continue;
      }
      return;
    }
  }

  Token quoted(char delimiter) {
    const auto start = position_++;
    std::string text;
    while (position_ < source_.size()) {
      const char character = source_[position_++];
      if (character != delimiter) {
        text.push_back(character);
        continue;
      }
      if (position_ < source_.size() && source_[position_] == delimiter) {
        text.push_back(delimiter);
        ++position_;
        continue;
      }
      return {delimiter == '\'' ? TokenKind::string : TokenKind::quoted_identifier,
              std::move(text), start};
    }
    syntax("Unterminated SQL quoted token", start);
  }

  Token number() {
    const auto start = position_;
    while (position_ < source_.size() && digit(source_[position_])) ++position_;
    if (position_ < source_.size() && source_[position_] == '.') {
      ++position_;
      while (position_ < source_.size() && digit(source_[position_])) ++position_;
    }
    if (position_ < source_.size() &&
        (source_[position_] == 'e' || source_[position_] == 'E')) {
      ++position_;
      if (position_ < source_.size() && (source_[position_] == '+' || source_[position_] == '-')) ++position_;
      const auto exponent = position_;
      while (position_ < source_.size() && digit(source_[position_])) ++position_;
      if (position_ == exponent) syntax("Numeric exponent needs digits", start);
    }
    return {TokenKind::number, std::string(source_.substr(start, position_ - start)), start};
  }

  std::string_view source_;
  std::size_t position_ = 0;
};

class Parser {
 public:
  explicit Parser(std::string_view source) : lexer_(source), token_(lexer_.next()) {}

  Statement parse() {
    Statement statement;
    if (take_word("CREATE")) statement = create_table();
    else if (take_word("INSERT")) statement = insert();
    else if (take_word("SELECT")) statement = select();
    else if (take_word("UPDATE")) statement = update();
    else if (take_word("DELETE")) statement = erase();
    else if (take_word("BEGIN")) statement = BeginStatement{};
    else if (take_word("COMMIT")) statement = CommitStatement{};
    else if (take_word("ROLLBACK")) statement = RollbackStatement{};
    else syntax("Unsupported SQL statement", token_.offset);
    take_symbol(";");
    if (token_.kind != TokenKind::end) syntax("Unexpected text after complete SQL statement", token_.offset);
    return statement;
  }

  std::size_t parameter_count() const { return parameters_; }

 private:
  void advance() { token_ = lexer_.next(); }

  bool word(std::string_view text) const {
    return token_.kind == TokenKind::word && token_.text == text;
  }

  bool take_word(std::string_view text) {
    if (!word(text)) return false;
    advance();
    return true;
  }

  bool take_symbol(std::string_view text) {
    if (token_.kind != TokenKind::symbol || token_.text != text) return false;
    advance();
    return true;
  }

  void expect_word(std::string_view text) {
    if (!take_word(text)) syntax("Expected SQL keyword " + std::string(text), token_.offset);
  }

  void expect_symbol(std::string_view text) {
    if (!take_symbol(text)) syntax("Expected SQL symbol " + std::string(text), token_.offset);
  }

  Identifier identifier() {
    if (token_.kind != TokenKind::quoted_identifier &&
        (token_.kind != TokenKind::word || reserved(token_.text))) {
      syntax("Expected an identifier", token_.offset);
    }
    if (token_.text.empty()) syntax("Empty identifier", token_.offset);
    Identifier result{token_.text, token_.offset, token_.original.empty() ? token_.text : token_.original};
    advance();
    return result;
  }

  Literal literal() {
    if (token_.kind == TokenKind::parameter) {
      Literal result{LiteralKind::parameter, {}, parameters_++, token_.offset};
      advance();
      return result;
    }
    if (token_.kind == TokenKind::string) {
      Literal result{LiteralKind::string, token_.text, 0, token_.offset};
      advance();
      return result;
    }
    std::string sign;
    auto offset = token_.offset;
    if (take_symbol("-")) sign = "-";
    else if (take_symbol("+")) sign = "+";
    if (token_.kind != TokenKind::number) syntax("Expected a value or typed parameter", token_.offset);
    Literal result{LiteralKind::number, sign + token_.text, 0, offset};
    advance();
    return result;
  }

  std::uint32_t extent() {
    const auto token = token_;
    if (token.kind != TokenKind::number) syntax("Expected a positive column extent", token.offset);
    std::uint32_t value = 0;
    const auto result = std::from_chars(token.text.data(), token.text.data() + token.text.size(), value);
    if (result.ec != std::errc{} || result.ptr != token.text.data() + token.text.size() || value == 0) {
      syntax("Column extent must be a positive uint32 integer", token.offset);
    }
    advance();
    return value;
  }

  CreateTableStatement create_table() {
    expect_word("TABLE");
    Schema schema;
    schema.name = identifier().name;
    std::optional<Identifier> primary;
    expect_symbol("(");
    do {
      if (take_word("PRIMARY")) {
        expect_word("KEY");
        expect_symbol("(");
        auto column = identifier();
        expect_symbol(")");
        if (primary) syntax("Multiple primary-key definitions", column.offset);
        primary = std::move(column);
      } else {
        const auto name = identifier();
        Column column{name.name, Type::bigint};
        column.display_name = name.display_name;
        if (take_word("BIGINT")) column.type = Type::bigint;
        else if (take_word("VARCHAR")) column.type = Type::varchar;
        else if (take_word("VECTOR")) column.type = Type::vector;
        else syntax("Unsupported column type", token_.offset);
        if (column.type != Type::bigint) {
          expect_symbol("(");
          column.extent = extent();
          expect_symbol(")");
        }
        bool not_null = false;
        while (word("PRIMARY") || word("NOT")) {
          if (take_word("PRIMARY")) {
            expect_word("KEY");
            if (primary) syntax("Multiple primary-key definitions", name.offset);
            primary = name;
          } else {
            expect_word("NOT");
            expect_word("NULL");
            if (not_null) syntax("Repeated NOT NULL constraint", name.offset);
            not_null = true;
          }
        }
        schema.columns.push_back(std::move(column));
      }
    } while (take_symbol(","));
    expect_symbol(")");
    if (!primary) syntax("A primary key is required", token_.offset);
    std::optional<std::uint32_t> key_column;
    for (std::size_t i = 0; i < schema.columns.size(); ++i) {
      if (schema.columns[i].name == primary->name) {
        if (i > std::numeric_limits<std::uint32_t>::max()) syntax("Too many columns", primary->offset);
        key_column = static_cast<std::uint32_t>(i);
      }
    }
    if (!key_column) syntax("Primary-key column is not declared", primary->offset);
    schema.primary_key = *key_column;
    return {std::move(schema)};
  }

  InsertStatement insert() {
    expect_word("INTO");
    InsertStatement statement;
    statement.table = identifier();
    if (take_symbol("(")) {
      do { statement.columns.push_back(identifier()); } while (take_symbol(","));
      expect_symbol(")");
    }
    expect_word("VALUES");
    do {
      expect_symbol("(");
      std::vector<Literal> row;
      do { row.push_back(literal()); } while (take_symbol(","));
      expect_symbol(")");
      statement.rows.push_back(std::move(row));
    } while (take_symbol(","));
    return statement;
  }

  Expression expression(bool allow_star) {
    if (allow_star && take_symbol("*")) return {ExpressionKind::star, {}, {}, {}};
    const auto name = identifier();
    if (name.name != "L2_DISTANCE" || !take_symbol("(")) {
      return {ExpressionKind::column, name, {}, {}};
    }
    const auto column = identifier();
    expect_symbol(",");
    auto query = literal();
    expect_symbol(")");
    return {ExpressionKind::distance, column, std::move(query), {}};
  }

  std::vector<Condition> conditions() {
    std::vector<Condition> result;
    if (!take_word("WHERE")) return result;
    do {
      auto column = identifier();
      Relation relation;
      if (take_symbol("=")) relation = Relation::equal;
      else if (take_symbol("<")) relation = Relation::less;
      else if (take_symbol("<=")) relation = Relation::less_equal;
      else if (take_symbol(">")) relation = Relation::greater;
      else if (take_symbol(">=")) relation = Relation::greater_equal;
      else syntax("Unsupported comparison operator", token_.offset);
      result.push_back({std::move(column), relation, literal()});
    } while (take_word("AND"));
    return result;
  }

  SelectStatement select() {
    SelectStatement statement;
    do {
      auto projection = expression(true);
      if (take_word("AS")) {
        if (projection.kind == ExpressionKind::star) syntax("Star projection cannot have an alias", token_.offset);
        projection.alias = identifier();
      }
      statement.projections.push_back(std::move(projection));
    } while (take_symbol(","));
    expect_word("FROM");
    statement.table = identifier();
    statement.conditions = conditions();
    if (take_word("ORDER")) {
      expect_word("BY");
      Order order{expression(false), false};
      if (!take_word("ASC")) order.descending = take_word("DESC");
      statement.order = std::move(order);
    }
    if (take_word("LIMIT")) statement.limit = literal();
    return statement;
  }

  UpdateStatement update() {
    UpdateStatement statement;
    statement.table = identifier();
    expect_word("SET");
    do {
      auto column = identifier();
      expect_symbol("=");
      statement.assignments.push_back({std::move(column), literal()});
    } while (take_symbol(","));
    statement.conditions = conditions();
    return statement;
  }

  DeleteStatement erase() {
    expect_word("FROM");
    DeleteStatement statement;
    statement.table = identifier();
    statement.conditions = conditions();
    return statement;
  }

  Lexer lexer_;
  Token token_;
  std::size_t parameters_ = 0;
};

}  // namespace

SqlError::SqlError(SqlErrorCategory category, std::string message, std::size_t offset)
    : std::runtime_error(std::move(message)), category_(category), offset_(offset) {}

SqlErrorCategory SqlError::category() const noexcept { return category_; }
std::size_t SqlError::offset() const noexcept { return offset_; }

ParsedStatement::ParsedStatement(std::unique_ptr<Impl> implementation) : impl_(std::move(implementation)) {}
ParsedStatement::~ParsedStatement() = default;
ParsedStatement::ParsedStatement(ParsedStatement&&) noexcept = default;
ParsedStatement& ParsedStatement::operator=(ParsedStatement&&) noexcept = default;

StatementKind ParsedStatement::kind() const {
  return static_cast<StatementKind>(impl_->statement.index());
}

std::size_t ParsedStatement::parameter_count() const { return impl_->parameters; }

ParsedStatement parse_sql(std::string_view source) {
  const auto nul = source.find('\0');
  if (nul != std::string_view::npos) syntax("NUL is not accepted in SQL text", nul);
  try {
    unicode_length(source);
  } catch (const std::exception&) {
    syntax("SQL source must be valid UTF-8", 0);
  }
  Parser parser(source);
  auto implementation = std::make_unique<ParsedStatement::Impl>();
  implementation->statement = parser.parse();
  implementation->parameters = parser.parameter_count();
  return ParsedStatement(std::move(implementation));
}

}  // namespace minivectordb
