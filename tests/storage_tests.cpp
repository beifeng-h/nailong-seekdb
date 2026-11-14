#include "minivectordb/page_store.hpp"

#include <chrono>
#include <filesystem>
#include <functional>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

using minivectordb::Page;
using minivectordb::PageStore;

namespace {
void check(bool condition, const std::string& description) {
  if (!condition) throw std::runtime_error(description);
}
template<class Function>
void rejected(Function function) {
  bool threw = false;
  try { function(); } catch (const std::runtime_error&) { threw = true; }
  check(threw, "Expected operation to fail");
}
Page pattern(unsigned seed) {
  Page page{};
  for (std::size_t i = 0; i < page.size(); ++i) page[i] = std::byte((seed * 31 + i * 17) & 255);
  return page;
}
class Directory {
 public:
  Directory() {
    const auto base = std::filesystem::temp_directory_path();
    for (int i = 0; i < 100; ++i) {
      path = base / ("minivectordb-storage-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(path)) return;
    }
    throw std::runtime_error("Cannot create owned test directory");
  }
  ~Directory() {
    std::error_code error;
    // The directory was created exclusively by this test and contains no subdirectories.
    for (const auto& entry : std::filesystem::directory_iterator(path, error))
      if (entry.is_regular_file()) std::filesystem::remove(entry.path(), error);
    std::filesystem::remove(path, error);
  }
  std::filesystem::path path;
};

void durable_pages() {
  Directory d;
  {
    PageStore store(d.path); check(store.size() == 0, "Fresh database must be empty");
    store.begin(); const auto a = store.allocate(), b = store.allocate();
    store.write(a, pattern(1)); store.write(b, pattern(2));
    check(store.read(a) == pattern(1), "Read own writes"); store.commit();
  }
  PageStore reopened(d.path);
  check(reopened.size() == 2 && reopened.read(0) == pattern(1) && reopened.read(1) == pattern(2),
        "Committed pages did not survive restart");
}

void zero_pages() {
  Directory d;
  { PageStore store(d.path); store.begin(); store.allocate(); store.allocate(); store.commit(); }
  PageStore store(d.path);
  check(store.size() == 2 && store.read(0) == Page{} && store.read(1) == Page{}, "Allocated pages must be zero");
}

void page_prefixes() {
  Directory d;
  const auto original = pattern(3), changed = pattern(8);
  const auto verify = [&](PageStore& store, const Page& expected) {
    for (const std::size_t length : {std::size_t(0), std::size_t(1), std::size_t(17),
                                     minivectordb::page_size - 1, minivectordb::page_size}) {
      const auto actual = store.read(0, length);
      for (std::size_t i = 0; i < actual.size(); ++i)
        check(actual[i] == (i < length ? expected[i] : std::byte{}), "Page prefix bytes differ");
    }
    rejected([&] { store.read(0, minivectordb::page_size + 1); });
    rejected([&] { store.read(store.size(), 0); });
  };
  {
    PageStore store(d.path);
    store.begin(); store.allocate(); verify(store, Page{});
    store.write(0, original); verify(store, original); store.commit(); verify(store, original);
    store.begin(); const auto point = store.savepoint(); store.write(0, changed); verify(store, changed);
    store.rollback_to(point); verify(store, original); store.rollback(); verify(store, original);
  }
  PageStore reopened(d.path); verify(reopened, original);
}

void rollback_and_close() {
  Directory d;
  {
    PageStore store(d.path); store.begin(); store.allocate(); store.write(0, pattern(3)); store.commit();
    store.begin(); store.write(0, pattern(4)); store.allocate(); store.write(1, pattern(5)); store.rollback();
    check(store.size() == 1 && store.read(0) == pattern(3), "Rollback changed committed state");
    store.begin(); store.write(0, pattern(6)); store.allocate();
  }
  PageStore store(d.path);
  check(store.size() == 1 && store.read(0) == pattern(3), "Uncommitted close changed committed state");
}

void statement_savepoints() {
  Directory d; PageStore store(d.path);
  store.begin(); store.allocate(); store.write(0, pattern(1));
  const auto point = store.savepoint();
  store.write(0, pattern(2)); store.write(0, pattern(3)); store.allocate(); store.write(1, pattern(9));
  store.rollback_to(point);
  check(store.size() == 1 && store.read(0) == pattern(1), "Failed statement lost earlier transaction changes");
  check(store.allocate() == 1 && store.read(1) == Page{}, "Rolled back allocation exposed stale bytes");
  store.write(1, pattern(8)); store.commit();
  check(store.read(0) == pattern(1) && store.read(1) == pattern(8), "Commit after statement rollback failed");
}

void bitmap_window_eviction() {
  Directory d; PageStore store(d.path);
  constexpr std::uint64_t stride = minivectordb::page_size * 8;
  const std::array<std::uint64_t, 5> ids{0, stride - 1, stride, 2 * stride - 1, 2 * stride};
  store.begin();
  for (std::uint64_t id = 0; id <= ids.back(); ++id) store.allocate();
  for (std::size_t i = 0; i < ids.size(); ++i) store.write(ids[i], pattern(static_cast<unsigned>(i + 1)));
  const auto point = store.savepoint();
  for (unsigned round = 0; round < 3; ++round) {
    for (auto id = ids.rbegin(); id != ids.rend(); ++id) store.write(*id, pattern(20 + round));
  }
  const auto extra = store.allocate(); store.write(extra, pattern(30));
  store.rollback_to(point);
  for (std::size_t i = 0; i < ids.size(); ++i)
    check(store.read(ids[i]) == pattern(static_cast<unsigned>(i + 1)), "Bitmap eviction lost saved page state");
  check(store.allocate() == extra && store.read(extra) == Page{}, "Evicted bitmap exposed rolled-back allocation");
  store.commit();
  for (std::size_t i = 0; i < ids.size(); ++i)
    check(store.read(ids[i]) == pattern(static_cast<unsigned>(i + 1)), "Bitmap eviction omitted a committed page");
  store.begin();
  for (auto id : ids) store.write(id, pattern(40));
  store.rollback();
  store.begin();
  check(store.read(ids.front()) == pattern(1) && store.read(ids.back()) == pattern(5),
        "Reused bitmap windows leaked a previous transaction");
  store.write(ids.back(), pattern(7)); store.commit();
  check(store.read(ids.front()) == pattern(1) && store.read(ids.back()) == pattern(7),
        "Commit after window reset differs");
}

void state_errors() {
  Directory d; PageStore store(d.path);
  rejected([&] { store.commit(); }); rejected([&] { store.rollback(); });
  rejected([&] { store.allocate(); }); rejected([&] { store.read(0); });
  store.begin(); rejected([&] { store.begin(); });
  const auto stale = store.savepoint(); store.rollback(); store.begin();
  rejected([&] { store.rollback_to(stale); });
  rejected([&] { store.write(0, Page{}); }); store.rollback();
}

void commit_interruption(const std::string& fault, bool expected_new) {
  Directory d;
  { PageStore initial(d.path); initial.begin(); initial.allocate(); initial.write(0, pattern(1)); initial.commit(); }
  {
    PageStore store(d.path, [&](std::string_view value) {
      if (value == fault) throw std::runtime_error("Injected interruption");
    });
    store.begin(); store.write(0, pattern(7)); store.allocate(); store.write(1, pattern(8));
    rejected([&] { store.commit(); }); rejected([&] { store.read(0); });
  }
  PageStore recovered(d.path);
  check(recovered.size() == (expected_new ? 2U : 1U), "Recovery page count is not atomic");
  check(recovered.read(0) == pattern(expected_new ? 7 : 1), "Recovery returned mixed state");
  if (expected_new) check(recovered.read(1) == pattern(8), "Recovery lost second page");
}

void randomized_transactions() {
  Directory d; std::mt19937 random(18491); std::vector<Page> committed;
  for (unsigned round = 0; round < 80; ++round) {
    PageStore store(d.path); auto visible = committed; store.begin();
    for (unsigned statement = 0; statement < 8; ++statement) {
      const auto point = store.savepoint(); const auto before = visible;
      if (visible.empty() || (visible.size() < 24 && random() % 3 == 0)) {
        check(store.allocate() == visible.size(), "Allocation sequence differs"); visible.push_back(Page{});
      }
      for (unsigned update = 0; update < 3; ++update) {
        const auto id = random() % visible.size(); visible[id] = pattern(random()); store.write(id, visible[id]);
      }
      if (random() % 4 == 0) { store.rollback_to(point); visible = before; }
      for (std::size_t id = 0; id < visible.size(); ++id)
        check(store.read(id) == visible[id], "Transaction differs from independent in-memory reference");
    }
    if (random() % 4 == 0) store.rollback(); else { store.commit(); committed = visible; }
    check(store.size() == committed.size(), "Committed size differs from reference");
    for (std::size_t id = 0; id < committed.size(); ++id)
      check(store.read(id) == committed[id], "Committed page differs from reference");
  }
}
}  // namespace

int main() {
  unsigned passed = 0;
  auto run = [&](const std::string& name, const std::function<void()>& function) {
    function(); ++passed; std::cout << "PASS " << name << '\n';
  };
  try {
    run("committed_pages_survive_restart", durable_pages);
    run("zero_pages_survive_empty_commit", zero_pages);
    run("page_prefixes_across_transactions_and_reopen", page_prefixes);
    run("rollback_and_uncommitted_close", rollback_and_close);
    run("statement_savepoint_keeps_prior_changes", statement_savepoints);
    run("bitmap_window_eviction_savepoint_and_transaction_reuse", bitmap_window_eviction);
    run("transaction_state_errors", state_errors);
    for (const auto* fault : {"commit.header_written", "commit.frame_written"})
      run(fault, [&] { commit_interruption(fault, false); });
    for (const auto* fault : {"commit.marker_written", "commit.wal_synced", "commit.verified",
                             "commit.applied_page", "commit.data_synced", "commit.wal_cleared"})
      run(fault, [&] { commit_interruption(fault, true); });
    run("randomized_transactions_against_reference", randomized_transactions);
    std::cout << "RESULT passed=" << passed << " failed=0\n";
    return 0;
  } catch (const std::exception& error) { std::cerr << "FAIL " << error.what() << '\n'; return 1; }
}
