#include "minivectordb/database.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <limits>
#include <map>
#include <random>
#include <stdexcept>
#include <string>

using namespace minivectordb;

namespace {
void check(bool condition, const std::string& message) { if (!condition) throw std::runtime_error(message); }
template<class Function> void rejected(Function function) {
  bool failed = false;
  try { function(); } catch (const std::runtime_error&) { failed = true; }
  check(failed, "Expected an invalid statement to fail");
}
class Directory {
 public:
  Directory() {
    for (unsigned i = 0; i < 100; ++i) {
      path = std::filesystem::temp_directory_path() / ("minivectordb-table-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("Cannot create test directory");
  }
  ~Directory() {
    std::error_code error;
    for (const auto& entry : std::filesystem::directory_iterator(path, error))
      if (entry.is_regular_file()) std::filesystem::remove(entry.path(), error);
    std::filesystem::remove(path, error);
  }
  std::filesystem::path path;
};
Schema sample_schema() {
  return {"docs", {{"id", Type::bigint}, {"tag", Type::varchar, 16}, {"embedding", Type::vector, 2}}, 0};
}
Row row(std::int64_t id, std::string tag, float x, float y) { return {id, std::move(tag), Vector{x, y}}; }
std::map<std::int64_t, Row> state(Database& db, const std::string& table = "docs") {
  std::map<std::int64_t, Row> result;
  db.scan(table, {}, [&](const Row& current) {
    check(result.emplace(std::get<std::int64_t>(current[0]), current).second, "Duplicate visible primary key"); return true;
  });
  check(result.size() == db.count(table), "Catalog count differs from scan"); return result;
}

void durable_records() {
  Directory d;
  const std::vector<Row> rows{row(std::numeric_limits<std::int64_t>::min(), "中文😀", 1, 2),
                              row(std::numeric_limits<std::int64_t>::max(), "z", -3, 4)};
  { Database db(d.path); db.create_table(sample_schema()); db.insert("docs", rows); }
  Database db(d.path);
  check(db.schema("docs").columns.size() == 3 && db.count("docs") == 2, "Schema/count did not survive restart");
  const auto actual = state(db);
  for (const auto& expected : rows) check(actual.at(std::get<std::int64_t>(expected[0])) == expected, "Stored row differs");
}

void atomic_batches() {
  Directory d; Database db(d.path); db.create_table(sample_schema());
  db.insert("docs", std::vector<Row>{row(1, "old", 1, 2)});
  rejected([&] { db.insert("docs", std::vector<Row>{row(2, "new", 3, 4), row(1, "duplicate", 5, 6)}); });
  check(db.count("docs") == 1, "Duplicate batch left a partial row");
  db.begin(); db.insert("docs", std::vector<Row>{row(3, "prior", 1, 1)});
  rejected([&] { db.insert("docs", std::vector<Row>{row(4, "good", 2, 2), row(4, "bad", 3, 3)}); });
  db.commit(); const auto actual = state(db);
  check(actual.size() == 2 && actual.count(3) == 1 && actual.count(4) == 0, "Failed statement lost earlier changes");
}

void unicode_and_numeric() {
  check(unicode_length("中😀a") == 3, "Unicode length counted UTF-8 bytes");
  check(compare_strings("z", "中") < 0 && compare_strings("中", "😀") < 0 && compare_strings("a", "aa") < 0,
        "String ordering differs from Unicode code points");
  rejected([] { unicode_length(std::string("\xed\xa0\x80", 3)); });
  rejected([] { unicode_length(std::string("\xc0\x80", 2)); });
  rejected([] { unicode_length(std::string("\xf0\x9f", 2)); });
  const std::vector<double> input{0.1, 1e-50}; const auto rounded = make_vector(input);
  check(rounded[0] == static_cast<float>(input[0]) && rounded[1] == 0, "Float32 conversion differs");
  rejected([] { make_vector(std::vector<double>{1e300}); });
  rejected([] { make_vector(std::vector<double>{std::numeric_limits<double>::infinity()}); });
  rejected([] { make_vector(std::vector<double>{std::numeric_limits<double>::quiet_NaN()}); });
  Directory d; Database db(d.path); auto schema = sample_schema(); schema.columns[1].extent = 2;
  db.create_table(schema); db.insert("docs", std::vector<Row>{row(1, "中😀", 0, 0)});
  rejected([&] { db.insert("docs", std::vector<Row>{row(2, "中😀a", 0, 0)}); });
  rejected([&] { db.insert("docs", std::vector<Row>{{std::int64_t(2), std::string("x"), Vector{1}}}); });
  rejected([&] { db.insert("docs", std::vector<Row>{row(2, "x", std::numeric_limits<float>::infinity(), 0)}); });
  check(db.count("docs") == 1, "Invalid values changed stored rows");
}

void exact_filtered_topk() {
  Directory d; Database db(d.path); db.create_table(sample_schema());
  db.insert("docs", std::vector<Row>{row(1, "other", 0, 0), row(2, "target", 10, 0), row(3, "target", 11, 0),
                                     row(-1, "tie", 0, 1), row(-2, "tie", 1, 0)});
  const Predicate target = [](const Row& value) { return std::get<std::string>(value[1]) == "target"; };
  const auto filtered = db.topk("docs", {0, 0}, 1, target);
  check(filtered.size() == 1 && filtered[0].primary_key == 2 && filtered[0].distance == 10,
        "Filtering was applied after global TopK");
  const auto global = db.topk("docs", {0, 0}, 1);
  std::cout << "EXPERIMENT filter_order global_top1_id=" << global[0].primary_key
            << " global_top1_tag=" << std::get<std::string>(global[0].row[1])
            << " after_filter_count=" << (target(global[0].row) ? 1 : 0)
            << " filtered_top1_id=" << filtered[0].primary_key << " filtered_distance=" << filtered[0].distance << '\n';
  const auto tied = db.topk("docs", {0, 0}, 10, [](const Row& value) { return std::get<std::string>(value[1]) == "tie"; });
  check(tied.size() == 2 && tied[0].primary_key == -2 && tied[1].primary_key == -1 && tied[0].distance == 1,
        "Equal distances must use primary-key order");
  check(db.topk("docs", {0, 0}, 10, [](const Row&) { return false; }).empty(), "Empty filter returned neighbors");
  check(db.topk("docs", {0, 0}, 0).empty(), "K=0 returned rows");
  rejected([&] { db.topk("docs", {0}, 2); });
  db.insert("docs", std::vector<Row>{row(9, "huge", std::numeric_limits<float>::max(), 0)});
  const auto huge = db.topk("docs", {0, 0}, 20);
  check(std::isfinite(huge.back().distance), "Float32 squared-distance accumulation overflowed");
}

void float32_rounding_boundaries() {
  const double maximum = std::numeric_limits<float>::max();
  const double threshold = std::ldexp(1.0, 128) - std::ldexp(1.0, 103);
  const double infinity = std::numeric_limits<double>::infinity();
  for (double sign : {1.0, -1.0}) {
    for (double value : {maximum, std::nextafter(maximum, infinity), std::nextafter(threshold, 0.0)}) {
      const auto result = make_vector(std::vector<double>{sign * value});
      check(result[0] == sign * maximum && std::isfinite(result[0]), "Finite float32 boundary was rejected");
    }
    rejected([&] { make_vector(std::vector<double>{sign * threshold}); });
    rejected([&] { make_vector(std::vector<double>{sign * std::nextafter(threshold, infinity)}); });
  }
  const double tiny = std::numeric_limits<float>::denorm_min();
  const auto result = make_vector(std::vector<double>{0.0, -0.0, tiny, tiny / 2, -tiny / 2});
  check(!std::signbit(result[0]) && std::signbit(result[1]) && result[2] == tiny &&
        result[3] == 0 && result[4] == 0 && std::signbit(result[4]), "Subnormal/signed-zero rounding differs");
}

void transactional_visibility() {
  Directory d; Database db(d.path); db.create_table(sample_schema());
  db.insert("docs", std::vector<Row>{row(1, "old", 3, 0), row(2, "old", 4, 0)});
  const auto original = state(db); db.begin();
  db.update("docs", [](const Row& value) { return std::get<std::int64_t>(value[0]) == 2; },
            std::vector<Assignment>{{1, std::string("new")}, {2, Vector{0, 0}}});
  db.erase("docs", [](const Row& value) { return std::get<std::int64_t>(value[0]) == 1; });
  db.insert("docs", std::vector<Row>{row(3, "new", 1, 0)});
  const auto visible = db.topk("docs", {0, 0}, 10);
  check(visible.size() == 2 && visible[0].primary_key == 2 && visible[0].distance == 0 && visible[1].primary_key == 3,
        "Vector queries disagree with transaction-visible data");
  db.rollback(); check(state(db) == original, "Rollback did not restore records/index");
  check(db.topk("docs", {0, 0}, 1)[0].primary_key == 1, "Rollback did not restore vector values");
}

void update_failure_and_reuse() {
  Directory d; Database db(d.path); db.create_table(sample_schema());
  db.insert("docs", std::vector<Row>{row(1, "a", 1, 0), row(2, "b", 2, 0), row(3, "c", 3, 0)});
  const auto original = state(db); db.begin(); unsigned seen = 0;
  rejected([&] { db.update("docs", [&](const Row&) {
    if (++seen == 3) throw std::runtime_error("Predicate failure after earlier updates");
    return true;
  }, std::vector<Assignment>{{1, std::string("changed")}}); });
  check(state(db) == original, "Partially completed UPDATE was not rolled back"); db.commit();
  rejected([&] { db.update("docs", {}, std::vector<Assignment>{{0, std::int64_t(4)}}); });
  check(db.erase("docs", {}) == 3 && db.count("docs") == 0, "DELETE count mismatch");
  db.insert("docs", std::vector<Row>{row(1, "again", 0, 0), row(2, "again", 1, 0)});
  check(db.topk("docs", {0, 0}, 10).size() == 2, "Deleted key slots or freed pages were not reusable");
}

void chained_records() {
  Directory d; Schema schema{"wide", {{"id", Type::bigint}, {"text", Type::varchar, 15000}, {"v", Type::vector, 2000}}, 0};
  Row expected{std::int64_t(5), std::string(12000, 'x'), Vector(2000, 0.25f)};
  {
    Database db(d.path); db.create_table(schema); db.insert("wide", std::vector<Row>{expected});
    check(state(db, "wide").at(5) == expected, "Multi-page record encoding differs");
    const auto bytes = std::filesystem::file_size(d.path / "pages.bin");
    for (unsigned i = 0; i < 12; ++i)
      db.update("wide", {}, std::vector<Assignment>{{1, std::string(i % 2 == 0 ? 100 : 12000, 'x')}});
    check(std::filesystem::file_size(d.path / "pages.bin") <= bytes + 8 * page_size,
          "Replaced blobs were not reclaimed");
  }
  Database db(d.path); check(state(db, "wide").at(5) == expected, "Wide row restart mismatch");
}

void row_prefix_boundaries() {
  for (const std::uint32_t extent : {8U, 889U, 890U, std::numeric_limits<std::uint32_t>::max()}) {
    Directory d;
    std::string text;
    for (std::uint32_t i = 0; i < std::min(extent, 890U); ++i) text += "\xf0\x9f\x98\x80";
    const Schema schema{"bounded", {{"id", Type::bigint}, {"text", Type::varchar, extent},
                                     {"v", Type::vector, 128}}, 0};
    const std::vector<Row> expected{{std::int64_t(1), text, Vector(128, 0.25f)},
                                    {std::int64_t(2), text, Vector(128, 0.25f)}};
    const auto verify = [&](Database& db) {
      check(*db.get("bounded", 1) == expected[0], "Bounded record lookup lost UTF-8 or vector bytes");
      check(state(db, "bounded").at(2) == expected[1], "Bounded record scan differs");
      for (const auto mode : {SearchMode::exact, SearchMode::ann}) {
        for (const auto budget : {std::size_t(128), std::size_t(1024)}) {
          std::size_t seen = 0;
          db.scan_topk("bounded", Vector(128, 0), 2,
            [&](const Row& value) { return std::get<std::string>(value[1]) == text; },
            [&](const Neighbor& neighbor) {
              check(seen < expected.size() && neighbor.row == expected[seen], "Bounded TopK row or tie order differs");
              check(std::abs(neighbor.distance - std::sqrt(8.0)) < 1e-12, "Bounded TopK distance differs");
              ++seen; return true;
            }, budget, mode);
          check(seen == expected.size(), "Bounded TopK omitted rows");
        }
      }
    };
    {
      Database db(d.path); db.create_table(schema); db.insert("bounded", expected);
      db.create_vector_index("bounded", "bounded_vec", 2); verify(db);
      db.begin();
      db.update("bounded", [](const Row& value) { return std::get<std::int64_t>(value[0]) == 1; },
                std::vector<Assignment>{{1, std::string{}}});
      check(std::get<std::string>((*db.get("bounded", 1))[1]).empty(), "Bounded read missed staged replacement");
      db.rollback(); verify(db);
    }
    Database reopened(d.path); verify(reopened);
  }
}

void growth_against_reference() {
  Directory d; std::map<std::int64_t, Row> reference;
  {
    Database db(d.path); db.create_table(sample_schema()); std::vector<Row> batch;
    for (std::int64_t i = 0; i < 2100; ++i) {
      auto value = row(i - 1050, i % 2 ? "odd" : "even", float(i % 17), float(i % 11));
      reference.emplace(i - 1050, value); batch.push_back(std::move(value));
    }
    db.insert("docs", batch); check(state(db) == reference, "Hash growth changed records");
    for (const auto& [key, expected] : reference) check(db.get("docs", key) == expected, "Disk primary-key lookup differs");
    check(!db.get("docs", 9000000), "Absent primary key returned a row");
    db.erase("docs", [](const Row& value) { return std::get<std::int64_t>(value[0]) % 3 == 0; });
    for (auto it = reference.begin(); it != reference.end();) {
      if (it->first % 3 == 0) it = reference.erase(it); else ++it;
    }
    check(state(db) == reference, "Hash traversal/delete differs from reference");
  }
  Database db(d.path); check(state(db) == reference, "Grown hash index restart mismatch");
}

void ordinary_filters_and_external_order() {
  Directory d; Database db(d.path); db.create_table(sample_schema()); std::vector<Row> input;
  const std::vector<std::string> tags{"z", "中", "😀", "a"};
  for (std::int64_t id = 120; id >= -120; --id) input.push_back(row(id, tags[static_cast<std::size_t>(id + 120) % 4], 0, 0));
  db.insert("docs", input);
  const std::vector<Filter> filters{{0, Relation::greater_equal, std::int64_t(-13)},
                                   {0, Relation::less, std::int64_t(23)}};
  const Predicate predicate = [&](const Row& value) { return matches(value, filters); };
  for (bool descending : {false, true}) {
    auto reference = input;
    std::erase_if(reference, [&](const Row& value) { return !predicate(value); });
    std::sort(reference.begin(), reference.end(), [&](const Row& a, const Row& b) {
      const auto order = compare_strings(std::get<std::string>(a[1]), std::get<std::string>(b[1]));
      return order == 0 ? std::get<std::int64_t>(a[0]) < std::get<std::int64_t>(b[0]) : descending ? order > 0 : order < 0;
    });
    std::vector<Row> actual;
    db.scan_ordered("docs", predicate, 1, descending, {}, [&](const Row& value) { actual.push_back(value); return true; }, 512);
    check(actual == reference, "External ORDER BY or primary-key tie rule differs from reference");
    actual.clear();
    db.scan_ordered("docs", predicate, 1, descending, 7, [&](const Row& value) { actual.push_back(value); return true; }, 512);
    reference.resize(7); check(actual == reference, "ORDER BY LIMIT differs from reference");
  }
  for (const auto relation : {Relation::equal, Relation::less, Relation::less_equal, Relation::greater, Relation::greater_equal}) {
    const std::vector<Filter> test{{0, relation, std::int64_t(1)}};
    check(matches(row(1, "x", 0, 0), test) == (relation == Relation::equal || relation == Relation::less_equal || relation == Relation::greater_equal),
          "Comparison equality boundary differs");
  }
  unsigned calls = 0;
  db.scan_ordered("docs", {}, 0, false, 0, [&](const Row&) { ++calls; return true; }, 512);
  check(calls == 0, "LIMIT 0 called result visitor");
  db.scan_ordered("docs", {}, 0, false, {}, [&](const Row&) { ++calls; return false; }, 512);
  check(calls == 1, "Result visitor stop was ignored");
  rejected([&] { db.scan_ordered("docs", {}, 2, false, {}, [](const Row&) { return true; }); });
  for (const auto& entry : std::filesystem::directory_iterator(d.path))
    check(!entry.is_directory(), "Completed sort leaked temporary files");
}

void streaming_large_k() {
  Directory d; Database db(d.path); db.create_table(sample_schema()); std::vector<Row> input;
  for (std::int64_t id = 120; id >= -120; --id) input.push_back(row(id, id % 2 == 0 ? "even" : "odd", 1, 0));
  db.insert("docs", input);
  for (const std::uint64_t k : {std::uint64_t(0), std::uint64_t(1), std::uint64_t(241), std::uint64_t(1000)}) {
    std::uint64_t seen = 0; std::int64_t previous = -121;
    db.scan_topk("docs", {0, 0}, k, {}, [&](const Neighbor& value) {
      check(value.primary_key == previous + 1 && value.distance == 1, "Streaming TopK order/tie mismatch");
      previous = value.primary_key; ++seen; return true;
    }, k == 1 ? 1024 : 128);
    check(seen == std::min<std::uint64_t>(k, 241), "Streaming TopK count mismatch");
  }
  std::uint64_t seen = 0; std::int64_t previous = -122;
  db.scan_topk("docs", {0, 0}, 1000, [](const Row& value) { return std::get<std::string>(value[1]) == "even"; },
    [&](const Neighbor& value) { check(value.primary_key == previous + 2, "Filtered large-K order mismatch"); previous = value.primary_key; ++seen; return true; }, 128);
  check(seen == 121, "Filtered large-K omitted rows");
}
}  // namespace

int main() {
  unsigned passed = 0;
  auto run = [&](const char* name, auto function) { function(); ++passed; std::cout << "PASS " << name << '\n'; };
  try {
    run("durable_schema_records_and_full_int64_keys", durable_records);
    run("multirow_statement_atomicity", atomic_batches);
    run("unicode_float32_and_invalid_values", unicode_and_numeric);
    run("exact_filter_first_topk_and_ties", exact_filtered_topk);
    run("float32_overflow_rounding_and_signed_zero", float32_rounding_boundaries);
    run("transaction_vector_visibility_and_rollback", transactional_visibility);
    run("update_failure_and_deleted_slot_reuse", update_failure_and_reuse);
    run("multi_page_records_and_reclamation", chained_records);
    run("row_prefix_utf8_page_boundary_and_extent_overflow", row_prefix_boundaries);
    run("hash_growth_against_independent_reference", growth_against_reference);
    run("ordinary_filters_external_sort_and_limit", ordinary_filters_and_external_order);
    run("streaming_topk_zero_small_full_and_oversized_k", streaming_large_k);
    std::cout << "RESULT passed=" << passed << " failed=0\n"; return 0;
  } catch (const std::exception& error) { std::cerr << "FAIL " << error.what() << '\n'; return 1; }
}
