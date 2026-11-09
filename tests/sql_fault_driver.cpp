#include "minivectordb/sql.hpp"

#include <chrono>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <thread>

using namespace minivectordb;

namespace {

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

void verify_mode(SqlSession& session, bool committed) {
  std::size_t index = 0;
  auto result = session.execute("SELECT id, tag, embedding FROM docs ORDER BY id", {},
    {{}, [&](std::span<const SqlValue> row) {
      const auto expected_id = static_cast<std::int64_t>(index) + (committed ? 2 : 1);
      const float expected_value = static_cast<float>(index) + (committed ? 0.0f : 3.0f);
      require(index < 2 && std::get<std::int64_t>(row[0]) == expected_id &&
                std::get<std::string>(row[1]) == (committed ? "new" : "old") &&
                std::get<Vector>(row[2]) == Vector{expected_value, 0}, "Mixed SQL table state after recovery");
      ++index;
      return true;
    }});
  require(index == 2 && result.complete, "SQL recovered row count differs");
  index = 0;
  result = session.execute("SELECT id, L2_DISTANCE(embedding, ?) AS d FROM docs ORDER BY d LIMIT 10",
    std::vector<Value>{Vector{0, 0}}, {{}, [&](std::span<const SqlValue> row) {
      require(index < 2 && std::get<std::int64_t>(row[0]) == static_cast<std::int64_t>(index) + (committed ? 2 : 1) &&
                std::get<double>(row[1]) == double(index) + (committed ? 0 : 3), "Mixed SQL vector state after recovery");
      ++index;
      return true;
    }});
  require(index == 2 && result.complete, "SQL recovered TopK count differs");
  require(session.execute("SELECT id FROM docs WHERE id = ?",
                           std::vector<Value>{std::int64_t(committed ? 1 : 3)}).emitted_rows == 0,
          "Recovered SQL primary-key index contains an uncommitted row");
}

void verify(SqlSession& session, bool committed) {
  session.set_search_mode(SearchMode::exact);
  verify_mode(session, committed);
  session.set_search_mode(SearchMode::ann);
  verify_mode(session, committed);
  session.set_search_mode(SearchMode::exact);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 3) return 2;
  try {
    const std::string mode = argv[1];
    const std::string stage = argc > 3 ? argv[3] : "";
    const auto hook = [&](std::string_view value) {
      if (value != stage) return;
      std::cout << "FAULT_READY " << value << std::endl;
      for (;;) std::this_thread::sleep_for(std::chrono::seconds(1));
    };
    Database database(argv[2], hook);
    SqlSession session(database, 1);
    if (mode == "seed" || mode == "seed-no-index") {
      session.execute("CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(8), embedding VECTOR(2))");
      session.execute("INSERT INTO docs VALUES(1, 'old', ?), (2, 'old', ?)",
                      std::vector<Value>{Vector{3, 0}, Vector{4, 0}});
      if (mode == "seed") session.execute("CREATE VECTOR INDEX docs_vec ON docs(embedding)");
    } else if (mode == "commit" || mode == "uncommitted" || mode == "index-build" || mode == "index-build-uncommitted") {
      session.execute("BEGIN");
      if (mode == "index-build" || mode == "index-build-uncommitted") {
        session.execute("CREATE VECTOR INDEX docs_vec ON docs(embedding)");
      }
      session.execute("UPDATE docs SET tag = 'new', embedding = ? WHERE id = 2", std::vector<Value>{Vector{0, 0}});
      session.execute("DELETE FROM docs WHERE id = 1");
      session.execute("INSERT INTO docs VALUES(3, 'new', ?)", std::vector<Value>{Vector{1, 0}});
      bool rejected = false;
      try {
        session.execute("INSERT INTO docs VALUES(4, 'bad', ?), (3, 'bad', ?)",
                        std::vector<Value>{Vector{9, 9}, Vector{9, 9}});
      } catch (const SqlError& error) {
        require(error.category() == SqlErrorCategory::execution, "Duplicate SQL failed at the wrong stage");
        rejected = true;
      }
      require(rejected, "Duplicate SQL unexpectedly succeeded");
      verify(session, true);
      if (mode == "commit" || mode == "index-build") {
        session.execute("COMMIT");
        hook("session.commit_returned");
      } else {
        hook("transaction.uncommitted");
      }
    } else if (mode == "sort-kill") {
      session.execute("SELECT id FROM docs ORDER BY tag", {},
                      {{}, [&](std::span<const SqlValue>) { hook("query.first_row"); return true; }});
    } else if (mode == "old" || mode == "new" || mode == "recover" || mode == "old-no-index") {
      if (mode == "old-no-index") {
        verify_mode(session, false);
        session.set_search_mode(SearchMode::ann);
        bool rejected = false;
        try { verify_mode(session, false); }
        catch (const SqlError& error) { rejected = error.category() == SqlErrorCategory::execution; }
        require(rejected, "Uncommitted index definition survived recovery");
      } else {
        verify(session, mode != "old");
      }
      for (const auto& entry : std::filesystem::directory_iterator(argv[2])) {
        require(!entry.path().filename().string().starts_with("minivectordb-sort-"),
                "Interrupted SQL sort workspace was not reclaimed");
      }
    } else {
      throw std::runtime_error("Unknown SQL fault mode");
    }
    std::cout << "PASS " << mode << '\n';
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
