#include "minivectordb/database.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using namespace minivectordb;

Vector scale_vector(std::int64_t id) {
  Vector vector(128);
  for (std::int64_t j = 0; j < 128; ++j) vector[static_cast<std::size_t>(j)] = float((id * (j + 3) + j * j) % 1009 - 504) * 0.25f;
  return vector;
}

int main(int argc, char** argv) {
  if (argc < 3) return 2;
  try {
    const std::string mode = argv[1], stage = argc > 3 ? argv[3] : "";
    const auto hook = [&](std::string_view value) {
      if (value != stage) return;
      std::cout << "FAULT_READY " << value << std::endl;
      for (;;) std::this_thread::sleep_for(std::chrono::seconds(1));
    };
    Database db(argv[2], hook);
    const bool indexed = mode == "index-scale-write" || mode == "index-scale-read";
    if (mode == "scale-write" || mode == "index-scale-write") {
      const auto count = static_cast<std::int64_t>(std::stoll(stage));
      db.create_table({"docs", {{"id", Type::bigint}, {"tag", Type::varchar, 8}, {"v", Type::vector, 128}}, 0});
      if (indexed) db.create_vector_index("docs", "docs_vec", 2);
      for (std::int64_t start = 0; start < count; start += 100) {
        std::vector<Row> rows;
        for (auto id = start; id < std::min(start + 100, count); ++id)
          rows.push_back({id, std::to_string(id % 7), scale_vector(id)});
        db.insert("docs", rows);
      }
      std::cout << "SCALE rows=" << count << " logical_record_bytes=" << count * 525
                << " physical_data_bytes=" << std::filesystem::file_size(std::filesystem::path(argv[2]) / "pages.bin") << '\n';
    } else if (mode == "scale-read" || mode == "index-scale-read") {
      const auto count = static_cast<std::int64_t>(std::stoll(stage));
      if (db.count("docs") != static_cast<std::uint64_t>(count)) throw std::runtime_error("Scale count mismatch");
      for (std::int64_t id = 0; id < count; ++id) {
        const auto actual = db.get("docs", id);
        const Row expected{id, std::to_string(id % 7), scale_vector(id)};
        if (!actual || *actual != expected) throw std::runtime_error("Scale primary-key row mismatch");
      }
      const auto query = scale_vector(17);
      const auto search_mode = indexed ? SearchMode::ann : SearchMode::exact;
      const auto neighbors = [&](const Predicate& predicate) {
        std::vector<Neighbor> result;
        db.scan_topk("docs", query, 10, predicate, [&](const Neighbor& neighbor) {
          result.push_back(neighbor); return true;
        }, 32768, search_mode);
        return result;
      };
      const auto all = neighbors({});
      const auto filtered = neighbors([](const Row& row) { return std::get<std::string>(row[1]) == "3"; });
      if (all.size() != 10 || filtered.size() != 10) throw std::runtime_error("Scale TopK count mismatch");
      for (std::int64_t i = 0; i < 10; ++i)
        if (all[static_cast<std::size_t>(i)].primary_key != 17 + 1009 * i ||
            filtered[static_cast<std::size_t>(i)].primary_key != 17 + 7063 * i ||
            all[static_cast<std::size_t>(i)].distance != 0 || filtered[static_cast<std::size_t>(i)].distance != 0)
          throw std::runtime_error("Scale filtered/tied TopK differs from independent periodic-key truth");
      std::uint64_t seen = 0; double previous_distance = -1; std::int64_t previous_key = -1;
      db.scan_topk("docs", query, static_cast<std::uint64_t>(count), {}, [&](const Neighbor& value) {
        if (value.primary_key < 0 || value.primary_key >= count || value.distance < previous_distance ||
            (value.distance == previous_distance && value.primary_key <= previous_key))
          throw std::runtime_error("Full-K output is not a strictly ordered set");
        previous_distance = value.distance; previous_key = value.primary_key; ++seen; return true;
      }, 32768, search_mode);
      if (seen != static_cast<std::uint64_t>(count)) throw std::runtime_error("Full-K count mismatch");
      std::cout << "SCALE verified_rows=" << count << " topk_groups=2 topk_rows=20 streamed_full_k_rows=" << seen << '\n';
    } else if (mode == "seed") {
      db.create_table({"docs", {{"id", Type::bigint}, {"tag", Type::varchar, 8}, {"v", Type::vector, 2}}, 0});
      db.insert("docs", std::vector<Row>{{std::int64_t(1), std::string("old"), Vector{3, 0}},
                                         {std::int64_t(2), std::string("old"), Vector{4, 0}}});
    } else if (mode == "commit" || mode == "uncommitted") {
      db.begin();
      db.update("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) == 2; },
                std::vector<Assignment>{{1, std::string("new")}, {2, Vector{0, 0}}});
      db.erase("docs", [](const Row& row) { return std::get<std::int64_t>(row[0]) == 1; });
      db.insert("docs", std::vector<Row>{{std::int64_t(3), std::string("new"), Vector{1, 0}}});
      if (mode == "commit") db.commit(); else hook("transaction.uncommitted");
    } else if (mode == "sort-kill") {
      db.scan_ordered("docs", {}, 1, false, {}, [&](const Row&) { hook("query.first_row"); return true; }, 1);
    } else if (mode == "old" || mode == "new" || mode == "recover") {
      const bool next = mode != "old"; const auto result = db.topk("docs", {0, 0}, 10);
      if (db.count("docs") != 2 || result.size() != 2 || result[0].primary_key != (next ? 2 : 1) ||
          result[1].primary_key != (next ? 3 : 2) || result[0].distance != (next ? 0 : 3) ||
          result[1].distance != (next ? 1 : 4)) throw std::runtime_error("Mixed table/vector state");
      for (const auto& row : result)
        if (std::get<std::string>(row.row[1]) != (next ? "new" : "old")) throw std::runtime_error("Mixed stored tags");
      for (const auto& entry : std::filesystem::directory_iterator(argv[2]))
        if (entry.path().filename().string().starts_with("minivectordb-sort-"))
          throw std::runtime_error("Interrupted sort workspace was not reclaimed");
    } else throw std::runtime_error("Unknown database fault mode");
    std::cout << "PASS " << mode << '\n'; return 0;
  } catch (const std::exception& error) { std::cerr << "FAIL " << error.what() << '\n'; return 1; }
}
