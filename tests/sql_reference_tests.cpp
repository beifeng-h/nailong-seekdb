#include "minivectordb/sql.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>

using namespace minivectordb;

namespace {

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

template<class Function>
void rejected(SqlErrorCategory category, Function function) {
  bool failed = false;
  try {
    function();
  } catch (const SqlError& error) {
    require(error.category() == category, "Unexpected SQL error category for " + std::string(error.what()));
    failed = true;
  }
  require(failed, "Expected SQL rejection");
}

class Directory {
 public:
  Directory() {
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
      path = std::filesystem::temp_directory_path() / ("minivectordb-sql-reference-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("Cannot create SQL reference directory");
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

struct ReferenceRow {
  std::string tag;
  std::int64_t score;
  Vector vector;
};
using Reference = std::map<std::int64_t, ReferenceRow>;

void check_row(std::span<const SqlValue> row, std::int64_t id, const ReferenceRow& expected) {
  require(row.size() == 4 && std::get<std::int64_t>(row[0]) == id &&
            std::get<std::string>(row[1]) == expected.tag && std::get<std::int64_t>(row[2]) == expected.score &&
            std::get<Vector>(row[3]) == expected.vector, "SQL row differs from independent reference");
}

void check_state(SqlSession& session, const Reference& reference) {
  auto expected = reference.begin();
  const auto result = session.execute("SELECT id, tag, score, v FROM mixed ORDER BY id", {},
    {{}, [&](std::span<const SqlValue> row) {
      require(expected != reference.end(), "SQL returned an extra reference row");
      check_row(row, expected->first, expected->second);
      ++expected;
      return true;
    }});
  require(expected == reference.end() && result.emitted_rows == reference.size() && result.complete,
          "SQL reference row count differs");
}

ReferenceRow next_row(std::mt19937_64& random) {
  static const std::vector<std::string> tags{"", "a", "z", "中文", "😀", "x'y", "a;b", "/*x*/", "--"};
  ReferenceRow row{tags[static_cast<std::size_t>(random() % tags.size())],
                   static_cast<std::int64_t>(random() % 21) - 10, {}};
  for (unsigned dimension = 0; dimension < 3; ++dimension) {
    row.vector.push_back(static_cast<float>(static_cast<std::int64_t>(random() % 17) - 8) * 0.25f);
  }
  return row;
}

std::string quoted(const std::string& text) {
  std::string result = "'";
  for (const auto character : text) {
    result += character;
    if (character == '\'') result += '\'';
  }
  return result + "'";
}

void check_order(SqlSession& session, const Reference& reference, std::int64_t minimum_id,
                  std::int64_t score_limit, std::int64_t limit) {
  std::vector<std::pair<std::int64_t, const ReferenceRow*>> expected;
  for (const auto& [id, row] : reference) {
    if (id >= minimum_id && row.score < score_limit) expected.emplace_back(id, &row);
  }
  std::sort(expected.begin(), expected.end(), [](const auto& left, const auto& right) {
    if (left.second->score != right.second->score) return left.second->score > right.second->score;
    return left.first < right.first;
  });
  expected.resize(std::min(expected.size(), static_cast<std::size_t>(limit)));
  std::size_t index = 0;
  const auto result = session.execute(
    "SELECT id, tag, score, v FROM mixed WHERE id >= ? AND score < ? ORDER BY score DESC LIMIT ?",
    std::vector<Value>{minimum_id, score_limit, limit}, {{}, [&](std::span<const SqlValue> row) {
      require(index < expected.size(), "SQL ordered query returned extra rows");
      check_row(row, expected[index].first, *expected[index].second);
      ++index;
      return true;
    }});
  require(index == expected.size() && result.complete, "SQL ordered reference count differs");
}

void check_topk(SqlSession& session, const Reference& reference, const Vector& query,
                 const std::optional<std::string>& tag, bool descending, std::int64_t limit) {
  std::vector<std::pair<std::int64_t, double>> expected;
  for (const auto& [id, row] : reference) {
    if (tag && row.tag != *tag) continue;
    double squared = 0;
    for (std::size_t dimension = 0; dimension < query.size(); ++dimension) {
      const auto delta = double(row.vector[dimension]) - double(query[dimension]);
      squared += delta * delta;
    }
    expected.emplace_back(id, std::sqrt(squared));
  }
  std::sort(expected.begin(), expected.end(), [&](const auto& left, const auto& right) {
    if (left.second != right.second) return descending ? left.second > right.second : left.second < right.second;
    return left.first < right.first;
  });
  expected.resize(std::min(expected.size(), static_cast<std::size_t>(limit)));
  std::vector<Value> parameters{query};
  std::string sql = "SELECT id, L2_DISTANCE(v, ?) AS d FROM mixed ";
  if (tag) {
    sql += "WHERE tag = ? ";
    parameters.emplace_back(*tag);
  }
  sql += descending ? "ORDER BY d DESC LIMIT ?" : "ORDER BY d ASC LIMIT ?";
  parameters.emplace_back(limit);
  std::size_t index = 0;
  const auto result = session.execute(sql, parameters, {{}, [&](std::span<const SqlValue> row) {
    require(index < expected.size() && std::get<std::int64_t>(row[0]) == expected[index].first &&
              std::get<double>(row[1]) == expected[index].second, "SQL TopK differs from independent reference");
    ++index;
    return true;
  }});
  require(index == expected.size() && result.complete, "SQL TopK reference count differs");
}

void malformed_and_binding_boundaries(SqlSession& session, const Reference& reference) {
  std::mt19937_64 random(0xabad1dea);
  const std::array<std::string, 5> prefixes{
    "DELETE FROM mixed", "UPDATE mixed SET score = 123", "BEGIN", "COMMIT", "ROLLBACK"};
  for (unsigned test = 0; test < 128; ++test) {
    auto sql = prefixes[static_cast<std::size_t>(random() % prefixes.size())] + "; @";
    for (unsigned byte = 0; byte < 24; ++byte) sql.push_back(static_cast<char>(random() % 256));
    rejected(SqlErrorCategory::syntax, [&] { session.execute(sql); });
  }
  const auto prepared = parse_sql("SELECT id, L2_DISTANCE(v, ?) AS d FROM mixed WHERE score >= ? ORDER BY d LIMIT ?");
  const std::vector<std::vector<Value>> invalid{
    {}, {Vector{0, 0, 0}}, {Vector{0, 0, 0}, std::int64_t(0)},
    {Vector{0, 0, 0}, std::int64_t(0), std::int64_t(1), std::int64_t(1)},
    {Vector{0, 0}, std::int64_t(0), std::int64_t(1)},
    {Vector{0, 0, 0}, std::string("0"), std::int64_t(1)},
    {Vector{0, 0, 0}, std::int64_t(0), std::int64_t(-1)}};
  unsigned callbacks = 0;
  const SqlSink sink{[&](std::span<const SqlColumn>) { ++callbacks; },
                     [&](std::span<const SqlValue>) { ++callbacks; return true; }};
  for (const auto& parameters : invalid) {
    rejected(SqlErrorCategory::binding, [&] { session.execute(prepared, parameters, sink); });
  }
  require(callbacks == 0, "Prepared binding failure emitted results");
  check_state(session, reference);
  std::cout << "EXPERIMENT malformed_sql cases=128 prepared_binding_cases=" << invalid.size() << '\n';
}

void mixed_sql() {
  constexpr std::uint64_t seed = 0x5eed1234;
  constexpr unsigned steps = 400;
  Directory directory;
  Reference reference;
  std::optional<Reference> before_transaction;
  std::array<unsigned, 10> operations{};
  unsigned errors = 0, begins = 0, commits = 0, rollbacks = 0;
  std::mt19937_64 random(seed);
  {
    Database database(directory.path);
    SqlSession session(database, 4096);
    session.execute("CREATE TABLE mixed(tag VARCHAR(8), id BIGINT PRIMARY KEY, score BIGINT, v VECTOR(3))");
    const auto point_query = parse_sql("SELECT id, tag, score, v FROM mixed WHERE id = ?");
    for (unsigned step = 0; step < steps; ++step) {
      const auto operation = static_cast<std::size_t>(random() % operations.size());
      ++operations[operation];
      const auto id = static_cast<std::int64_t>(random() % 41) - 20;
      const auto row = next_row(random);
      try {
        switch (operation) {
          case 0: {
            const auto insert = [&] {
              return session.execute("INSERT INTO mixed(id, tag, score, v) VALUES(" + std::to_string(id) + ", " +
                                      quoted(row.tag) + ", " + std::to_string(row.score) + ", ?)",
                                      std::vector<Value>{row.vector});
            };
            if (reference.contains(id)) {
              rejected(SqlErrorCategory::execution, insert);
              ++errors;
            } else {
              require(insert().affected_rows == 1, "SQL INSERT count differs");
              reference.emplace(id, row);
            }
            break;
          }
          case 1: {
            std::uint64_t changed = 0;
            const auto result = session.execute("UPDATE mixed SET tag = ?, score = ?, v = ? WHERE id <= ?",
                                               std::vector<Value>{row.tag, row.score, row.vector, id});
            for (auto& [key, current] : reference) {
              if (key <= id) { current = row; ++changed; }
            }
            require(result.affected_rows == changed, "SQL UPDATE count differs");
            break;
          }
          case 2: {
            const auto result = session.execute("DELETE FROM mixed WHERE score > ? AND id >= ?",
                                               std::vector<Value>{row.score, id});
            std::uint64_t changed = 0;
            for (auto current = reference.begin(); current != reference.end();) {
              if (current->first >= id && current->second.score > row.score) {
                current = reference.erase(current);
                ++changed;
              } else {
                ++current;
              }
            }
            require(result.affected_rows == changed, "SQL DELETE count differs");
            break;
          }
          case 3:
            if (!before_transaction) {
              session.execute("BEGIN");
              before_transaction = reference;
              ++begins;
            } else if (random() % 2 == 0) {
              session.execute("COMMIT");
              before_transaction.reset();
              ++commits;
            } else {
              session.execute("ROLLBACK");
              reference = std::move(*before_transaction);
              before_transaction.reset();
              ++rollbacks;
            }
            break;
          case 4:
            if (reference.empty()) {
              rejected(SqlErrorCategory::binding, [&] {
                session.execute("INSERT INTO mixed(id, tag, score, v) VALUES(1001, 'a', 0, ?), (1002, 'b', 0, ?)",
                                std::vector<Value>{Vector{0, 0, 0}, Vector{0}});
              });
            } else {
              rejected(SqlErrorCategory::execution, [&] {
                session.execute("INSERT INTO mixed(id, tag, score, v) VALUES(1001, 'a', 0, ?), (?, 'b', 0, ?)",
                                std::vector<Value>{Vector{0, 0, 0}, reference.begin()->first, Vector{0, 0, 0}});
              });
            }
            ++errors;
            break;
          case 5:
            if (step % 2 == 0) {
              rejected(SqlErrorCategory::syntax, [&] { session.execute("DELETE FROM mixed; COMMIT"); });
            } else {
              rejected(SqlErrorCategory::binding, [&] { session.execute("UPDATE mixed SET score = 1e3"); });
            }
            ++errors;
            break;
          case 6:
            check_order(session, reference, id, row.score, static_cast<std::int64_t>(random() % 15));
            break;
          case 7: {
            const auto expected = reference.find(id);
            unsigned count = 0;
            session.execute(point_query, std::vector<Value>{id}, {{}, [&](std::span<const SqlValue> current) {
              require(expected != reference.end(), "SQL point query returned an extra row");
              check_row(current, id, expected->second);
              ++count;
              return true;
            }});
            require(count == (expected == reference.end() ? 0u : 1u), "SQL point query count differs");
            break;
          }
          case 8: {
            const bool filtered = random() % 2 == 0;
            const bool descending = random() % 2 != 0;
            const auto limit = static_cast<std::int64_t>(random() % 12);
            check_topk(session, reference, row.vector, filtered ? std::optional<std::string>(row.tag) : std::nullopt,
                         descending, limit);
            break;
          }
          case 9:
            rejected(SqlErrorCategory::execution, [&] { session.execute(before_transaction ? "BEGIN" : "COMMIT"); });
            ++errors;
            break;
        }
        check_state(session, reference);
      } catch (const std::exception& error) {
        throw std::runtime_error("Mixed SQL step=" + std::to_string(step) + " operation=" +
                                 std::to_string(operation) + " error=" + error.what());
      }
    }
    if (before_transaction) {
      session.execute("ROLLBACK");
      reference = std::move(*before_transaction);
      before_transaction.reset();
      ++rollbacks;
    }
    check_state(session, reference);
    malformed_and_binding_boundaries(session, reference);
  }
  {
    Database database(directory.path);
    SqlSession session(database, 4096);
    check_state(session, reference);
    for (const bool descending : {false, true}) {
      check_topk(session, reference, {0, 0, 0}, {}, descending, 100);
      check_topk(session, reference, {0, 0, 0}, std::string("中文"), descending, 100);
    }
  }
  require(std::all_of(operations.begin(), operations.end(), [](unsigned count) { return count > 0; }) &&
            begins > 0 && commits > 0 && rollbacks > 0, "Fixed seed missed a required SQL operation");
  std::cout << "EXPERIMENT mixed_sql seed=" << seed << " steps=" << steps << " state_checks=" << steps + 3
            << " expected_errors=" << errors << " begins=" << begins << " commits=" << commits
            << " rollbacks=" << rollbacks << " final_rows=" << reference.size() << '\n';
}

}  // namespace

int main() {
  try {
    mixed_sql();
    std::cout << "PASS fixed_seed_mixed_sql_against_independent_reference\nRESULT passed=1 failed=0\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
