#include "minivectordb/sql.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>

using namespace minivectordb;

namespace {

void check(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

template<class Function> void rejected(Function function) {
  bool failed = false;
  try { function(); } catch (const std::runtime_error&) { failed = true; }
  check(failed, "Expected index operation to fail");
}

class Directory {
 public:
  Directory() {
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
      path = std::filesystem::temp_directory_path() / ("minivectordb-index-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("Cannot create vector index test directory");
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

using State = std::map<std::int64_t, Row>;

const Schema docs{"docs", {{"id", Type::bigint}, {"tag", Type::varchar, 16}, {"v", Type::vector, 3}}, 0};

Row make_row(std::int64_t key) {
  return {key, std::string(key % 3 == 0 ? "target" : "other"),
          Vector{static_cast<float>(key % 7), static_cast<float>(key % 5), static_cast<float>(key % 2)}};
}

// The oracle uses only the expected rows. It never reads the implementation's index or distance result.
void verify_query(Database& db, const Schema& schema, const State& expected, const Vector& query,
                  std::optional<std::uint64_t> limit, bool descending, bool filtered,
                  std::size_t budget, SearchMode mode) {
  const auto column = schema.name == "docs" ? 2U : 0U;
  const auto tag = schema.name == "docs" ? 1U : 2U;
  const Predicate predicate = filtered ? Predicate([=](const Row& row) {
    return std::get<std::string>(row[tag]) == "target";
  }) : Predicate{};
  struct Truth { double distance; std::int64_t key; };
  std::vector<Truth> truth;
  for (const auto& [key, row] : expected) {
    if (predicate && !predicate(row)) continue;
    const auto& vector = std::get<Vector>(row[column]);
    double sum = 0;
    for (std::size_t i = 0; i < query.size(); ++i) {
      const auto delta = static_cast<double>(vector[i]) - query[i];
      sum += delta * delta;
    }
    truth.push_back({std::sqrt(sum), key});
  }
  std::sort(truth.begin(), truth.end(), [&](const Truth& left, const Truth& right) {
    if (left.distance != right.distance) return descending ? left.distance > right.distance : left.distance < right.distance;
    return left.key < right.key;
  });
  if (limit && *limit < truth.size()) truth.resize(static_cast<std::size_t>(*limit));
  std::size_t emitted = 0;
  db.scan_distance_ordered(schema.name, query, descending, limit, predicate, [&](const Neighbor& neighbor) {
    check(emitted < truth.size(), "Index returned extra or duplicate IDs");
    const auto& correct = truth[emitted++];
    check(neighbor.primary_key == correct.key, "Index filter, distance order or tie order differs");
    check(std::abs(neighbor.distance - correct.distance) <= 1e-12 * (1 + correct.distance), "Index distance differs");
    check(neighbor.row == expected.at(correct.key), "Index returned a stale record pointer");
    return true;
  }, budget, mode);
  check(emitted == truth.size(), "Index omitted eligible IDs");
}

void verify(Database& db, const State& expected) {
  for (const auto mode : {SearchMode::exact, SearchMode::ann}) {
    for (const auto budget : {std::size_t(1), std::size_t(1024 * 1024)}) {
      for (bool filtered : {false, true}) {
        for (bool descending : {false, true}) {
          verify_query(db, docs, expected, {0, 0, 0}, 10, descending, filtered, budget, mode);
        }
        verify_query(db, docs, expected, {1.25f, -0.5f, 2}, {}, false, filtered, budget, mode);
        verify_query(db, docs, expected, {0, 0, 0}, 0, false, filtered, budget, mode);
      }
    }
  }
  check(db.count("docs") == expected.size(), "Index maintenance changed the table count");
}

State seed(Database& db, std::int64_t count = 24) {
  db.create_table(docs);
  State expected;
  std::vector<Row> rows;
  for (std::int64_t key = 0; key < count; ++key) {
    auto row = make_row(key);
    expected.emplace(key, row); rows.push_back(std::move(row));
  }
  db.insert("docs", rows);
  db.create_vector_index("docs", "docs_vec", 2);
  return expected;
}

void definitions_and_sql_mode() {
  Directory directory;
  Database db(directory.path);
  SqlSession session(db);
  check(session.search_mode() == SearchMode::exact, "New session must use exact mode");
  session.execute("CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(16), v VECTOR(3))");
  const auto parsed = parse_sql("create vector index index on docs(v)");
  check(parsed.kind() == StatementKind::create_vector_index, "Index statement kind differs");
  session.set_search_mode(SearchMode::ann);
  rejected([&] { session.execute("SELECT id FROM docs ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 1"); });
  rejected([&] { session.execute("CREATE VECTOR INDEX bad ON docs(tag)"); });
  rejected([&] { session.execute("CREATE VECTOR INDEX bad ON docs(missing)"); });
  rejected([&] { session.execute("CREATE VECTOR INDEX bad ON missing(v)"); });
  session.execute("BEGIN");
  session.execute(parsed);
  check(session.execute("SELECT id FROM docs ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 1").emitted_rows == 0,
        "New empty vector index returned a row");
  session.execute("ROLLBACK");
  rejected([&] { session.execute("SELECT id FROM docs ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 1"); });
  session.execute(parsed);
  rejected([&] { session.execute(parsed); });
  rejected([&] { db.schema("index"); });
  session.execute("CREATE TABLE on(id BIGINT PRIMARY KEY, index BIGINT)");
  session.execute("INSERT INTO on VALUES(1,2)");
  session.execute("INSERT INTO docs VALUES(1,'target','[3,4,0]')");
  unsigned rows = 0;
  session.execute("SELECT id, L2_DISTANCE(v, '[0,0,0]') AS d FROM docs ORDER BY d LIMIT 1", {},
    {{}, [&](std::span<const SqlValue> row) {
      check(std::get<std::int64_t>(row[0]) == 1 && std::get<double>(row[1]) == 5, "SQL mode lost index result");
      ++rows; return true;
    }});
  check(rows == 1, "SQL index mode emitted no row");
  session.set_search_mode(SearchMode::exact);
  check(session.search_mode() == SearchMode::exact, "Cannot return to exact mode");
}

void mutations_and_statement_atomicity() {
  Directory directory;
  Database db(directory.path);
  auto expected = seed(db);
  verify(db, expected);
  const auto original = expected;
  db.begin();
  db.insert("docs", std::vector<Row>{make_row(-5)}); expected.emplace(-5, make_row(-5));
  db.erase("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) == 0; }); expected.erase(0);
  auto replacement = make_row(0); replacement[1] = std::string("target"); replacement[2] = Vector{0, 0, 0};
  db.insert("docs", std::vector<Row>{replacement}); expected[0] = replacement;
  db.update("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) == 3; },
            std::vector<Assignment>{{1, std::string("changed")}, {2, Vector{-1, 0, 0}}});
  expected.at(3)[1] = std::string("changed"); expected.at(3)[2] = Vector{-1, 0, 0};
  unsigned seen = 0;
  rejected([&] { db.update("docs", [&](const Row&) {
    if (++seen == 4) throw std::runtime_error("Fail after updating index entries");
    return true;
  }, std::vector<Assignment>{{2, Vector{99, 99, 99}}}); });
  seen = 0;
  rejected([&] { db.erase("docs", [&](const Row&) {
    if (++seen == 4) throw std::runtime_error("Fail after erasing index entries");
    return true;
  }); });
  rejected([&] { db.insert("docs", std::vector<Row>{make_row(101), make_row(0)}); });
  verify(db, expected);
  db.commit();
  verify(db, expected);
  db.begin();
  db.update("docs", {}, std::vector<Assignment>{{2, Vector{7, 8, 9}}});
  db.erase("docs", {});
  db.insert("docs", std::vector<Row>{make_row(999)});
  db.rollback();
  verify(db, expected);
  check(original != expected, "Mutation regression did not change the fixture");
  std::size_t visits = 0;
  db.scan_topk("docs", {0, 0, 0}, 10, {}, [&](const Neighbor&) { ++visits; return false; }, 1024, SearchMode::ann);
  check(visits == 1, "Index search ignored visitor cancellation");
}

void restart_and_shutdown_rollback() {
  Directory directory;
  State expected;
  {
    Database db(directory.path); expected = seed(db);
    db.begin();
    db.erase("docs", {});
    db.insert("docs", std::vector<Row>{make_row(200)});
  }
  {
    Database db(directory.path);
    verify(db, expected);
    db.begin();
    db.update("docs", {}, std::vector<Assignment>{{1, std::string("target")}});
    for (auto& [key, row] : expected) { (void)key; row[1] = std::string("target"); }
    db.commit();
  }
  Database db(directory.path); verify(db, expected);
}

void growth_chained_vectors_and_page_reuse() {
  Directory directory;
  const Schema wide{"wide", {{"embedding", Type::vector, 1100}, {"id", Type::bigint}, {"tag", Type::varchar, 16}}, 1};
  State expected;
  {
    Database db(directory.path); db.create_table(wide);
    db.create_vector_index("wide", "wide_vec", 0);
    db.begin();
    for (std::int64_t key = 0; key < 2100; ++key) {
      Row row{Vector(1100, static_cast<float>(key % 19)), key, std::string(key % 2 ? "target" : "other")};
      db.insert("wide", std::vector<Row>{row}); expected.emplace(key, std::move(row));
    }
    db.commit();
    for (const auto mode : {SearchMode::exact, SearchMode::ann}) {
      verify_query(db, wide, expected, Vector(1100, 2.5f), 10, false, true, 1024, mode);
    }
    const auto replace = [&] {
      db.erase("wide", [](const Row& row) { return std::get<std::int64_t>(row[1]) == 0; });
      db.insert("wide", std::vector<Row>{expected.at(0)});
      db.update("wide", [](const Row& row) { return std::get<std::int64_t>(row[1]) == 1; },
                std::vector<Assignment>{{0, std::get<Vector>(expected.at(1)[0])}});
    };
    replace();
    const auto bytes = std::filesystem::file_size(directory.path / "pages.bin");
    for (unsigned repeat = 0; repeat < 20; ++repeat) replace();
    check(std::filesystem::file_size(directory.path / "pages.bin") == bytes, "Repeated index maintenance leaked pages");
  }
  Database db(directory.path);
  verify_query(db, wide, expected, Vector(1100, 2.5f), 10, false, true, 1024, SearchMode::ann);
}

void randomized_index_oracle() {
  Directory directory;
  Schema schema = docs; schema.columns[2].extent = 16;
  Database db(directory.path); db.create_table(schema);
  State expected;
  std::mt19937 random(90211);
  const auto random_vector = [&] {
    Vector vector(16);
    for (auto& value : vector) value = static_cast<float>(static_cast<int>(random() % 2001) - 1000) * 0.125f;
    return vector;
  };
  std::vector<Row> rows;
  for (std::int64_t key = 0; key < 256; ++key) {
    Row row{key, std::string(key % 3 == 0 ? "target" : "other"), random_vector()};
    expected.emplace(key, row); rows.push_back(std::move(row));
  }
  db.insert("docs", rows); db.create_vector_index("docs", "random_vec", 2);
  db.begin();
  db.erase("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) % 7 == 0; });
  for (auto iterator = expected.begin(); iterator != expected.end();) {
    if (iterator->first % 7 == 0) iterator = expected.erase(iterator);
    else ++iterator;
  }
  db.update("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) % 5 == 0; },
            std::vector<Assignment>{{2, Vector(16, 0)}});
  for (auto& [key, row] : expected) if (key % 5 == 0) row[2] = Vector(16, 0);
  db.commit();
  for (unsigned query = 0; query < 32; ++query) {
    const auto vector = random_vector();
    for (bool filtered : {false, true}) {
      for (const auto mode : {SearchMode::exact, SearchMode::ann}) {
        verify_query(db, schema, expected, vector, 10, false, filtered, 32768, mode);
      }
    }
  }
  std::cout << "QUALITY queries=128 checked_rows=1280 unfiltered_recall_at_10=1 filtered_recall_at_10=1\n";
}

}  // namespace

int main() {
  unsigned passed = 0;
  const auto run = [&](const char* name, auto function) {
    function(); ++passed; std::cout << "PASS " << name << '\n';
  };
  try {
    run("index_definition_rollback_and_sql_mode", definitions_and_sql_mode);
    run("index_mutations_failure_atomicity_filter_ties_and_budgets", mutations_and_statement_atomicity);
    run("index_restart_commit_and_shutdown_rollback", restart_and_shutdown_rollback);
    run("index_growth_cross_page_vectors_and_page_reuse", growth_chained_vectors_and_page_reuse);
    run("randomized_index_oracle_after_mutations", randomized_index_oracle);
    std::cout << "RESULT passed=" << passed << " failed=0\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
