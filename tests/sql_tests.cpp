#include "minivectordb/sql.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <utility>

using namespace minivectordb;

namespace {

void check(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

template<class Function>
void rejected(SqlErrorCategory category, Function function) {
  bool failed = false;
  try {
    function();
  } catch (const SqlError& error) {
    check(error.category() == category, "Incorrect SQL error category for " + std::string(error.what()));
    failed = true;
  }
  check(failed, "Expected SQL to be rejected");
}

class Directory {
 public:
  Directory() {
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
      path = std::filesystem::temp_directory_path() / ("minivectordb-sql-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("Cannot create SQL test directory");
  }
  ~Directory() {
    std::error_code error;
    for (const auto& entry : std::filesystem::directory_iterator(path, error)) {
      if (entry.is_regular_file()) std::filesystem::remove(entry.path(), error);
    }
    std::filesystem::remove(path, error);
  }
  std::filesystem::path path;
};

struct Collected {
  SqlResult result;
  std::vector<std::vector<SqlValue>> rows;
};

Collected collect(SqlSession& session, std::string_view sql, std::span<const Value> parameters = {}) {
  Collected output;
  unsigned metadata_calls = 0;
  SqlSink sink;
  sink.columns = [&](std::span<const SqlColumn> columns) {
    ++metadata_calls;
    check(!columns.empty(), "Query has no metadata columns");
  };
  sink.row = [&](std::span<const SqlValue> row) {
    check(metadata_calls == 1, "Rows arrived before metadata");
    output.rows.emplace_back(row.begin(), row.end());
    return true;
  };
  output.result = session.execute(sql, parameters, sink);
  check(output.result.is_query && metadata_calls == 1, "Query metadata callback count differs");
  check(output.result.emitted_rows == output.rows.size() && output.result.complete, "Query result summary differs");
  return output;
}

std::vector<std::int64_t> ids(const Collected& output) {
  std::vector<std::int64_t> result;
  for (const auto& row : output.rows) result.push_back(std::get<std::int64_t>(row[0]));
  return result;
}

void create_docs(SqlSession& session) {
  session.execute("CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(16), embedding VECTOR(2))");
}

void insert_doc(SqlSession& session, std::int64_t id, const std::string& tag, Vector vector) {
  session.execute("INSERT INTO docs VALUES(?, ?, ?)", std::vector<Value>{id, tag, std::move(vector)});
}

void parser_contracts() {
  const auto statement = parse_sql("/* outer /* nested */ */ sElEcT id AS key_id, L2_DISTANCE(v, ?) AS d "
                                   "FROM docs WHERE tag = 'a;''b' AND id >= ? ORDER BY d DESC LIMIT ?; -- end");
  check(statement.kind() == StatementKind::select && statement.parameter_count() == 3, "Statement/parameter parsing differs");
  const std::vector<std::pair<std::string, StatementKind>> kinds{
    {"CREATE TABLE t(id BIGINT, PRIMARY KEY(id))", StatementKind::create_table},
    {"INSERT INTO t VALUES(1), (2)", StatementKind::insert},
    {"UPDATE t SET x = ?", StatementKind::update}, {"DELETE FROM t", StatementKind::erase},
    {"BEGIN;", StatementKind::begin}, {"COMMIT", StatementKind::commit}, {"ROLLBACK", StatementKind::rollback}};
  for (const auto& [text, kind] : kinds) check(parse_sql(text).kind() == kind, "Parsed wrong statement kind");
  for (const auto& text : {
      "", "-- only comment", "SELECT id FROM t;;", "SELECT id FROM t; DELETE FROM t",
      "SELECT id FROM t trailing", "SELECT id FROM t WHERE id = 1 OR id = 2",
      "SELECT id FROM t WHERE id <> 1", "SELECT id FROM t ORDER BY id, tag",
      "SELECT * AS x FROM t", "SELECT id FROM t WHERE id = NULL", "SELECT id FROM t /* unterminated",
      "SELECT 'unterminated FROM t", "SELECT id FROM t WHERE id = 1e+",
      "CREATE TABLE t(id BIGINT)", "CREATE TABLE t(id BIGINT PRIMARY KEY, PRIMARY KEY(id))",
      "CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(0))", "SELECT L2_DISTANCE(v, [1,2]) FROM t",
      "INSERT INTO t VALUES(1) garbage", "BEGIN TRANSACTION", "COMMIT AND CHAIN"}) {
    rejected(SqlErrorCategory::syntax, [&] { parse_sql(text); });
  }
  rejected(SqlErrorCategory::syntax, [] { parse_sql(std::string("SELECT \xc0\x80", 9)); });
  bool checked_offset = false;
  try {
    parse_sql("SELECT id FROM t;!");
  } catch (const SqlError& error) {
    check(error.offset() == 17, "Syntax error offset differs");
    checked_offset = true;
  }
  check(checked_offset, "Trailing character was accepted");
  std::string nul_comment = "BEGIN -- ";
  nul_comment.push_back('\0');
  nul_comment += "hidden";
  rejected(SqlErrorCategory::syntax, [&] { parse_sql(nul_comment); });
}

void quoting_unicode_and_restart() {
  Directory directory;
  {
    Database database(directory.path);
    SqlSession session(database);
    session.execute("CREATE TABLE \"中文;表\"(\"key\" BIGINT NOT NULL PRIMARY KEY, \"a\"\"b\" VARCHAR(3) NOT NULL)");
    session.execute("INSERT INTO \"中文;表\" VALUES(-9223372036854775808, '中😀a'), (+9223372036854775807, 'a''b')");
    rejected(SqlErrorCategory::binding, [&] {
      session.execute("INSERT INTO \"中文;表\" VALUES(1, '中😀ab')");
    });
    rejected(SqlErrorCategory::syntax, [&] { session.execute("SELECT key FROM \"中文;表\""); });
    rejected(SqlErrorCategory::binding, [&] { session.execute("SELECT \"KEY\" FROM \"中文;表\""); });
  }
  Database database(directory.path);
  SqlSession session(database);
  const auto output = collect(session, "SELECT \"key\", \"a\"\"b\" FROM \"中文;表\" ORDER BY \"key\"");
  check(ids(output) == std::vector<std::int64_t>{std::numeric_limits<std::int64_t>::min(),
                                              std::numeric_limits<std::int64_t>::max()}, "BIGINT keys did not survive restart");
  check(std::get<std::string>(output.rows[0][1]) == "中😀a" && std::get<std::string>(output.rows[1][1]) == "a'b",
        "Escaped strings or Unicode changed");
  check(output.result.columns[1].name == "a\"b" && output.result.columns[1].type == SqlType::varchar,
        "Quoted metadata changed");
}

void typed_bindings_and_atomicity() {
  Directory directory;
  Database database(directory.path);
  SqlSession session(database);
  create_docs(session);
  const auto insert = parse_sql("INSERT INTO docs(embedding, id, tag) VALUES(?, ?, ?), (?, ?, ?)");
  const std::vector<Value> parameters{Vector{1, 2}, std::int64_t(1), std::string("a"),
                                      Vector{3, 4}, std::int64_t(2), std::string("b")};
  check(session.execute(insert, parameters).affected_rows == 2, "Multirow INSERT count differs");
  rejected(SqlErrorCategory::execution, [&] { session.execute(insert, parameters); });
  check(database.count("DOCS") == 2, "Duplicate INSERT changed rows");
  rejected(SqlErrorCategory::binding, [&] { session.execute(insert, std::span<const Value>(parameters).first(5)); });
  rejected(SqlErrorCategory::binding, [&] { session.execute("DELETE FROM docs", parameters); });
  for (const auto& text : {
      "INSERT INTO docs VALUES(3, 'x', ?), (4, 'abcdefghijklmnopq', ?)",
      "INSERT INTO docs(id, id, tag) VALUES(3, 4, 'x')", "INSERT INTO docs(id, tag) VALUES(3, 'x')",
      "INSERT INTO docs VALUES(3, 'x')", "INSERT INTO docs VALUES(9223372036854775808, 'x', ?)",
      "INSERT INTO docs VALUES(-9223372036854775809, 'x', ?)", "INSERT INTO docs VALUES(1e0, 'x', ?)",
      "INSERT INTO docs VALUES('3', 'x', ?)", "INSERT INTO docs VALUES(3, 1, ?)"}) {
    const auto parsed = parse_sql(text);
    const std::vector<Value> values(parsed.parameter_count(), Vector{0, 0});
    rejected(SqlErrorCategory::binding, [&] { session.execute(parsed, values); });
  }
  for (const Vector& vector : {Vector{1}, Vector{1, std::numeric_limits<float>::infinity()},
                                Vector{std::numeric_limits<float>::quiet_NaN(), 0}}) {
    rejected(SqlErrorCategory::binding, [&] { insert_doc(session, 3, "x", vector); });
  }
  rejected(SqlErrorCategory::binding, [&] { insert_doc(session, 3, std::string("\xc0\x80", 2), {0, 0}); });
  rejected(SqlErrorCategory::syntax, [&] {
    session.execute("INSERT INTO docs VALUES(3, 'x', ?); trailing", std::vector<Value>{Vector{0, 0}});
  });
  check(database.count("DOCS") == 2, "Rejected statement left partial modifications");
  check(ids(collect(session, "SELECT id FROM docs ORDER BY id")) == std::vector<std::int64_t>{1, 2},
        "Rows changed after binding failure");
}

void schema_and_bind_before_output() {
  Directory directory;
  Database database(directory.path);
  SqlSession session(database);
  for (const auto& text : {
      "CREATE TABLE bad(id VARCHAR(2) PRIMARY KEY)",
      "CREATE TABLE bad(id BIGINT PRIMARY KEY, v VECTOR(2), w VECTOR(2))",
      "CREATE TABLE bad(id BIGINT PRIMARY KEY, ID VARCHAR(2))"}) {
    rejected(SqlErrorCategory::binding, [&] { session.execute(text); });
  }
  create_docs(session);
  unsigned callbacks = 0;
  const SqlSink sink{[&](std::span<const SqlColumn>) { ++callbacks; },
                     [&](std::span<const SqlValue>) { ++callbacks; return true; }};
  for (const auto& text : {
      "SELECT missing FROM docs", "SELECT * FROM unknown_table", "SELECT id FROM docs WHERE missing = 1",
      "SELECT id FROM docs WHERE tag = 1", "SELECT id FROM docs WHERE embedding = ?",
      "SELECT id FROM docs ORDER BY embedding", "SELECT L2_DISTANCE(id, ?) FROM docs",
      "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?)", "SELECT id FROM docs LIMIT -1",
      "SELECT id FROM docs LIMIT 9223372036854775808", "SELECT id FROM docs LIMIT '1'",
      "SELECT id AS x, tag AS x FROM docs ORDER BY x", "SELECT missing FROM docs LIMIT 0"}) {
    const auto statement = parse_sql(text);
    const std::vector<Value> parameters(statement.parameter_count(), Vector{0});
    rejected(SqlErrorCategory::binding, [&] { session.execute(statement, parameters, sink); });
  }
  check(callbacks == 0, "Binding failure emitted metadata or rows");
  rejected(SqlErrorCategory::execution, [&] { create_docs(session); });
  check(collect(session, "SELECT * FROM docs LIMIT 0").rows.empty(), "LIMIT 0 returned rows");
  session.execute("CREATE TABLE reordered(tag VARCHAR(2), id BIGINT, v VECTOR(2), PRIMARY KEY(id))");
  session.execute("INSERT INTO reordered(v, id, tag) VALUES(?, 2, 'a'), (?, 1, 'b')",
                  std::vector<Value>{Vector{1, 0}, Vector{0, 1}});
  check(ids(collect(session, "SELECT id FROM reordered ORDER BY L2_DISTANCE(v, ?)",
                      std::vector<Value>{Vector{0, 0}})) == std::vector<std::int64_t>{1, 2},
        "Table-level primary key in a later column changed distance ties");
}

void ordinary_query_semantics() {
  Directory directory;
  Database database(directory.path);
  SqlSession session(database, 128);
  session.execute("CREATE TABLE items(id BIGINT PRIMARY KEY, tag VARCHAR(2), score BIGINT)");
  session.execute("INSERT INTO items VALUES(4, '😀', 2), (3, '中', 2), (2, 'z', 2), (1, 'z', 1), (-1, 'z', 2)");
  check(ids(collect(session, "SELECT id FROM items ORDER BY score DESC")) == std::vector<std::int64_t>{-1, 2, 3, 4, 1},
        "Descending ordinary ties did not use ascending primary keys");
  check(ids(collect(session, "SELECT id FROM items WHERE score >= 2 AND tag < '😀' ORDER BY tag DESC LIMIT 3")) ==
          std::vector<std::int64_t>{3, -1, 2}, "AND, Unicode order or LIMIT differs");
  check(ids(collect(session, "SELECT id, score AS tag FROM items ORDER BY tag")) ==
          std::vector<std::int64_t>{1, -1, 2, 3, 4}, "ORDER BY output alias precedence differs");
  check(ids(collect(session, "SELECT id FROM items WHERE tag < 'zzzz' ORDER BY id")) ==
          std::vector<std::int64_t>{-1, 1, 2}, "Long comparison literal was rejected or truncated");
  check(collect(session, "SELECT id FROM items WHERE id <= 2 AND id > 1").rows.size() == 1,
        "Ordinary range comparison differs");
  check(ids(collect(session, "SELECT * FROM items WHERE id = 3")) == std::vector<std::int64_t>{3}, "Star projection differs");
  check(collect(session, "SELECT * FROM items WHERE id = 3 AND id = 4 ORDER BY tag").rows.empty(),
        "Primary-key lookup ignored additional conditions");
  check(collect(session, "SELECT * FROM items WHERE id = 99").rows.empty(), "Missing primary-key lookup returned a row");
  check(collect(session, "SELECT id FROM items LIMIT 2").rows.size() == 2, "Unordered LIMIT differs");
  check(collect(session, "SELECT id FROM items LIMIT ?", std::vector<Value>{std::int64_t(2)}).rows.size() == 2,
        "Bound LIMIT differs");
  check(collect(session, "SELECT id FROM items LIMIT 9223372036854775807").rows.size() == 5,
        "Large LIMIT omitted records");
  check(session.execute("UPDATE items SET tag = 'x', score = -3 WHERE id >= 3").affected_rows == 2,
        "UPDATE count differs");
  check(session.execute("DELETE FROM items WHERE score < 0").affected_rows == 2, "DELETE count differs");
  check(session.execute("SELECT * FROM items").emitted_rows == 3, "Query without a sink did not count rows");
}

void distance_projection_order_and_filter() {
  Directory directory;
  Database database(directory.path);
  SqlSession session(database, 128);
  create_docs(session);
  insert_doc(session, 1, "other", {0, 0});
  insert_doc(session, 2, "target", {10, 0});
  insert_doc(session, 3, "target", {11, 0});
  insert_doc(session, -1, "tie", {0, 1});
  insert_doc(session, -2, "tie", {1, 0});
  const std::vector<Value> query{Vector{0, 0}};
  const auto filtered = collect(session, "SELECT id, L2_DISTANCE(embedding, ?) AS d FROM docs "
                                          "WHERE tag = 'target' ORDER BY d LIMIT 1", query);
  check(ids(filtered) == std::vector<std::int64_t>{2} && std::get<double>(filtered.rows[0][1]) == 10,
        "SQL applied filtering after TopK");
  check(filtered.result.columns[1].name == "d" && filtered.result.columns[1].type == SqlType::real,
        "Distance metadata differs");
  check(ids(collect(session, "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?) DESC", query)) ==
          std::vector<std::int64_t>{3, 2, -2, -1, 1}, "Descending distance order or tie rule differs");
  const auto output = collect(session, "SELECT id, L2_DISTANCE(embedding, ?) AS a, L2_DISTANCE(embedding, ?) AS b "
                                      "FROM docs ORDER BY b DESC LIMIT 2", std::vector<Value>{Vector{0, 0}, Vector{10, 0}});
  check(ids(output) == std::vector<std::int64_t>{-1, 1}, "ORDER BY reused the wrong distance parameter");
  check(std::get<double>(output.rows[0][1]) == 1 && std::get<double>(output.rows[0][2]) == std::sqrt(101.0),
        "Multiple distance projections differ");
  const auto stored = make_vector(std::vector<double>{0.1, -0.0});
  insert_doc(session, 5, "rounded", stored);
  const auto rounded = collect(session, "SELECT id, embedding, L2_DISTANCE(embedding, ?) FROM docs WHERE id = 5", query);
  check(std::get<Vector>(rounded.rows[0][1]) == stored && std::get<double>(rounded.rows[0][2]) == double(stored[0]),
        "SQL distance did not use stored float32 values");
  check(collect(session, "SELECT id FROM docs WHERE tag = 'missing' ORDER BY L2_DISTANCE(embedding, ?) LIMIT 4", query).rows.empty(),
        "Empty filtered TopK returned rows");
}

void transaction_visibility_and_failures() {
  Directory directory;
  {
    Database database(directory.path);
    SqlSession session(database);
    create_docs(session);
    insert_doc(session, 1, "old", {1, 0});
    insert_doc(session, 2, "old", {2, 0});
    session.execute("BEGIN");
    session.execute("UPDATE docs SET embedding = ?, tag = 'new' WHERE id = 1", std::vector<Value>{Vector{20, 0}});
    session.execute("DELETE FROM docs WHERE id = 2");
    insert_doc(session, 3, "new", {3, 0});
    rejected(SqlErrorCategory::execution, [&] { session.execute("BEGIN"); });
    rejected(SqlErrorCategory::binding, [&] { session.execute("UPDATE docs SET id = 8"); });
    rejected(SqlErrorCategory::binding, [&] { session.execute("UPDATE docs SET tag = 'x', TAG = 'y'"); });
    rejected(SqlErrorCategory::execution, [&] { insert_doc(session, 3, "duplicate", {0, 0}); });
    check(ids(collect(session, "SELECT id FROM docs WHERE tag = 'new' ORDER BY L2_DISTANCE(embedding, ?) LIMIT 2",
                       std::vector<Value>{Vector{0, 0}})) == std::vector<std::int64_t>{3, 1},
          "Vector query did not see transaction modifications");
    session.execute("ROLLBACK");
    check(ids(collect(session, "SELECT id FROM docs ORDER BY id")) == std::vector<std::int64_t>{1, 2}, "ROLLBACK differs");
    session.execute("BEGIN");
    insert_doc(session, 4, "commit", {4, 0});
    rejected(SqlErrorCategory::syntax, [&] { session.execute("DELETE FROM docs; trailing"); });
    rejected(SqlErrorCategory::execution, [&] { insert_doc(session, 1, "duplicate", {0, 0}); });
    session.execute("COMMIT");
    rejected(SqlErrorCategory::execution, [&] { session.execute("COMMIT"); });
    rejected(SqlErrorCategory::execution, [&] { session.execute("ROLLBACK"); });
    session.execute("BEGIN");
    session.execute("CREATE TABLE transient(id BIGINT PRIMARY KEY)");
    session.execute("INSERT INTO transient VALUES(9)");
    check(collect(session, "SELECT * FROM transient").rows.size() == 1, "Transactional DDL was not visible");
    session.execute("ROLLBACK");
    rejected(SqlErrorCategory::binding, [&] { session.execute("SELECT * FROM transient"); });
    session.execute("BEGIN");
    session.execute("DELETE FROM docs");
  }
  Database database(directory.path);
  SqlSession session(database);
  check(ids(collect(session, "SELECT id FROM docs ORDER BY id")) == std::vector<std::int64_t>{1, 2, 4},
        "Committed or uncommitted state changed across close/reopen");
}

void streaming_and_reusable_statements() {
  Directory directory;
  Database database(directory.path);
  SqlSession session(database, 128);
  create_docs(session);
  const auto insert = parse_sql("INSERT INTO docs VALUES(?, 'tie', ?)");
  session.execute("BEGIN");
  for (std::int64_t id = 200; id >= -200; --id) session.execute(insert, std::vector<Value>{id, Vector{1, 0}});
  session.execute("COMMIT");
  for (const auto& text : {
      "SELECT id FROM docs ORDER BY id", "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?)",
      "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?) DESC LIMIT 9999"}) {
    const auto statement = parse_sql(text);
    const std::vector<Value> query(statement.parameter_count(), Vector{0, 0});
    std::int64_t previous = -201;
    const auto result = session.execute(statement, query, {{}, [&](std::span<const SqlValue> row) {
      check(std::get<std::int64_t>(row[0]) == ++previous, "Streaming order or tie rule differs");
      return true;
    }});
    check(result.emitted_rows == 401 && result.complete, "Streaming query omitted rows");
  }
  for (const auto& text : {
      "SELECT id FROM docs", "SELECT id FROM docs ORDER BY id",
      "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?) LIMIT 3",
      "SELECT id FROM docs ORDER BY L2_DISTANCE(embedding, ?) DESC"}) {
    const auto statement = parse_sql(text);
    const std::vector<Value> query(statement.parameter_count(), Vector{0, 0});
    unsigned calls = 0;
    const auto result = session.execute(statement, query, {{}, [&](std::span<const SqlValue>) { ++calls; return false; }});
    check(calls == 1 && result.emitted_rows == 1 && !result.complete, "Sink cancellation differs");
  }
  for (const auto& entry : std::filesystem::directory_iterator(directory.path)) {
    check(!entry.is_directory(), "Completed or cancelled SQL sort leaked temporary files");
  }
  auto source = parse_sql("SELECT id FROM docs WHERE id = ?");
  const auto moved = std::move(source);
  rejected(SqlErrorCategory::binding, [&] { session.execute(source, std::vector<Value>{std::int64_t(0)}); });
  check(session.execute(moved, std::vector<Value>{std::int64_t(0)}).emitted_rows == 1, "Moved statement lost parameters");
}

void existing_typed_database() {
  Directory directory;
  {
    Database database(directory.path);
    database.create_table({"legacy", {{"key", Type::bigint}, {"text", Type::varchar, 8}}, 0});
    database.insert("legacy", std::vector<Row>{{std::int64_t(5), std::string("old")}});
  }
  Database database(directory.path);
  SqlSession session(database);
  check(ids(collect(session, "SELECT \"key\" FROM \"legacy\"")) == std::vector<std::int64_t>{5},
        "SQL could not read existing case-sensitive typed schema");
  session.execute("UPDATE \"legacy\" SET \"text\" = 'new' WHERE \"key\" = 5");
  check(std::get<std::string>(database.get("legacy", 5)->at(1)) == "new", "SQL changed legacy schema conventions");
}

void vector_strings_and_preserved_labels() {
  Directory directory;
  {
    Database database(directory.path);
    SqlSession session(database);
    session.execute("CREATE TABLE LabelStore(Id BIGINT PRIMARY KEY, \"类别\" VARCHAR(8), Embedding VECTOR(2))");
    session.execute("INSERT INTO labelstore VALUES(1, '甲', '[3, 4]'), (2, '乙', '[-0, -1e-500]')");
  }
  Database database(directory.path);
  SqlSession session(database);
  const auto star = collect(session, "SELECT * FROM labelstore ORDER BY ID");
  check(star.result.columns[0].name == "Id" && star.result.columns[1].name == "类别" &&
          star.result.columns[2].name == "Embedding", "Declared column spelling did not survive restart");
  const auto& zeros = std::get<Vector>(star.rows[1][2]);
  check(zeros[0] == 0 && zeros[1] == 0 && std::signbit(zeros[0]) && std::signbit(zeros[1]),
        "Vector string underflow or signed zero changed");
  const auto nearest = collect(session, "SELECT ID AS copyName, L2_DISTANCE(embedding, '[0,0]') AS distance "
                                        "FROM labelstore ORDER BY distance");
  check(nearest.result.columns[0].name == "copyName" && nearest.result.columns[1].name == "distance" &&
          ids(nearest) == std::vector<std::int64_t>{2, 1} && std::get<double>(nearest.rows[1][1]) == 5,
        "String-vector distance or alias spelling differs");
  session.execute("INSERT INTO labelstore VALUES(3, '[1,2]', '[+0.1,.25]')");
  const auto ordinary = collect(session, "SELECT \"类别\" FROM labelstore WHERE id = 3");
  check(std::get<std::string>(ordinary.rows[0][0]) == "[1,2]", "VARCHAR string was converted as a vector");
  for (const auto& vector : {"[NaN,0]", "[Infinity,0]", "[1e300,0]", "[3.4028236e38,0]", "[]", "[1,2,3]",
                             "[1,,2]", "[1,2,]", "[1 2]", "[[1],2]", "[\"1\",2]", "[1e,0]", "[1,2] extra", "1,2", "[1,2"}) {
    rejected(SqlErrorCategory::binding, [&] {
      session.execute(std::string("UPDATE labelstore SET Embedding = '") + vector + "'");
    });
  }
  rejected(SqlErrorCategory::binding, [&] {
    session.execute("UPDATE labelstore SET embedding = ?", std::vector<Value>{std::string("[1,2]")});
  });
  session.execute("BEGIN");
  session.execute("INSERT INTO labelstore VALUES(4, 'large', '[3.4028235e38,0]')");
  rejected(SqlErrorCategory::binding, [&] {
    session.execute("INSERT INTO labelstore VALUES(5, 'good', '[0,0]'), (6, 'bad', '[0]')");
  });
  session.execute("COMMIT");
  const auto maximum = collect(session, "SELECT embedding FROM labelstore WHERE id = 4");
  check(std::get<Vector>(maximum.rows[0][0])[0] == std::numeric_limits<float>::max(),
        "Vector string rejected finite float32 rounding band");
  check(collect(session, "SELECT id FROM labelstore").rows.size() == 4,
        "Failed vector-string statement left partial modifications");
}

}  // namespace

int main() {
  unsigned passed = 0;
  const auto run = [&](const char* name, auto function) {
    function();
    ++passed;
    std::cout << "PASS " << name << '\n';
  };
  try {
    run("complete_statement_lexer_and_parser", parser_contracts);
    run("quoted_identifiers_unicode_and_restart", quoting_unicode_and_restart);
    run("typed_parameters_multirow_atomicity_and_numeric_boundaries", typed_bindings_and_atomicity);
    run("schema_and_binding_before_output", schema_and_bind_before_output);
    run("ordinary_projection_filter_order_alias_and_limit", ordinary_query_semantics);
    run("distance_projection_filter_first_ascending_and_descending", distance_projection_order_and_filter);
    run("transaction_visibility_failures_commit_and_rollback", transaction_visibility_and_failures);
    run("streaming_large_results_cancellation_and_reuse", streaming_and_reusable_statements);
    run("existing_typed_database_compatibility", existing_typed_database);
    run("vector_string_literals_and_persistent_column_labels", vector_strings_and_preserved_labels);
    std::cout << "RESULT passed=" << passed << " failed=0\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
