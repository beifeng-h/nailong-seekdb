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

void new_page_undo_boundaries() {
  Directory d; PageStore store(d.path);
  store.begin(); const auto empty = store.savepoint();
  const auto first = store.allocate(); store.write(first, pattern(1)); store.write(first, pattern(2));
  check(std::filesystem::file_size(d.path / "txn.undo") == 0, "Unprotected allocation wrote unnecessary undo bytes");
  const auto kept = store.savepoint();
  store.write(first, pattern(3));
  const auto extra = store.allocate(); store.write(extra, pattern(4));
  store.rollback_to(kept);
  check(store.size() == 1 && store.read(first) == pattern(2), "Savepoint after a new-page write was not preserved");
  check(store.allocate() == extra && store.read(extra) == Page{}, "Dropped allocation retained dirty stage bytes");
  store.write(extra, pattern(5));
  const auto later = store.savepoint(); store.write(extra, pattern(6)); store.rollback_to(later);
  check(store.read(extra) == pattern(5), "Reused page was not protected by a later savepoint");
  store.rollback_to(empty);
  check(store.size() == 0, "Older savepoint retained new pages");
  check(store.allocate() == first && store.read(first) == Page{}, "Older rollback exposed stale page data");
  store.write(first, pattern(7)); store.commit();
  check(store.size() == 1 && store.read(first) == pattern(7), "Commit after dropped-page cleanup failed");
  store.begin(); const auto committed = store.savepoint();
  store.write(first, pattern(8)); store.rollback_to(committed);
  check(store.read(first) == pattern(7), "Committed-page undo was skipped"); store.commit();
}

void cached_page_visibility() {
  Directory d;
  {
    PageStore store(d.path); store.begin();
    for (unsigned i = 0; i < 64; ++i) { store.allocate(); store.write(i, pattern(i)); }
    store.commit();
    for (unsigned i = 0; i < 64; ++i) {
      check(store.read(i) == pattern(i) && store.read(i) == pattern(i), "Repeated cached read differs");
    }
    store.begin(); const auto point = store.savepoint();
    for (unsigned i = 0; i < 48; ++i) store.write(i, pattern(i + 100));
    for (unsigned i = 0; i < 64; ++i)
      check(store.read(i) == pattern(i < 48 ? i + 100 : i), "Cached staged page differs after eviction");
    store.rollback_to(point);
    for (unsigned i = 0; i < 64; ++i) check(store.read(i) == pattern(i), "Savepoint rollback left a stale cached page");
    store.write(3, pattern(77)); store.commit();
    check(store.read(3) == pattern(77), "Commit did not retain the current cached page");
    store.begin(); store.write(3, pattern(88)); check(store.read(3) == pattern(88), "Cache missed own write");
    store.rollback(); check(store.read(3) == pattern(77), "Rollback left a stale cached page");
    store.begin(); const auto allocation_point = store.savepoint(); const auto extra = store.allocate();
    store.write(extra, pattern(9)); check(store.read(extra) == pattern(9), "New-page cache differs");
    store.rollback_to(allocation_point);
    check(store.allocate() == extra && store.read(extra) == Page{}, "Reallocated page retained cached bytes");
    store.write(extra, pattern(11)); store.commit();
  }
  PageStore reopened(d.path);
  check(reopened.read(3) == pattern(77) && reopened.read(64) == pattern(11), "Cached state differed from durable state");
}

void repeated_page_undo_epochs() {
  Directory d; PageStore store(d.path);
  store.begin();
  for (unsigned i = 0; i < 64; ++i) { store.allocate(); store.write(i, pattern(i)); }
  store.commit(); store.begin(); const auto first = store.savepoint();
  store.write(0, pattern(80)); const auto one = std::filesystem::file_size(d.path / "txn.undo");
  store.write(0, pattern(81)); store.write(0, pattern(82));
  check(std::filesystem::file_size(d.path / "txn.undo") == one, "Repeated page writes duplicated the same savepoint image");
  const auto second = store.savepoint(); store.write(0, pattern(90));
  const auto two = std::filesystem::file_size(d.path / "txn.undo");
  check(two > one, "New savepoint did not protect its current page image");
  store.write(0, pattern(91));
  check(std::filesystem::file_size(d.path / "txn.undo") == two, "Second savepoint repeated the same undo image");
  store.rollback_to(second); check(store.read(0) == pattern(82), "Later savepoint restored the wrong version");
  store.write(0, pattern(92)); store.rollback_to(second);
  check(store.read(0) == pattern(82), "Repeated rollback skipped the replacement undo image");
  for (unsigned i = 1; i < 64; ++i) { store.read(i); store.write(i, pattern(i + 100)); }
  store.write(0, pattern(93)); store.rollback_to(first);
  for (unsigned i = 0; i < 64; ++i) check(store.read(i) == pattern(i), "Undo eviction or older savepoint lost an image");
  store.write(0, pattern(70)); store.commit();
  store.begin(); const auto next_transaction = store.savepoint(); store.write(0, pattern(71));
  store.rollback_to(next_transaction); check(store.read(0) == pattern(70), "Undo epoch leaked across transactions");
  store.rollback();
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
    run("new_page_undo_protection_and_reallocation", new_page_undo_boundaries);
    run("cached_pages_eviction_commit_rollback_and_reuse", cached_page_visibility);
    run("repeated_page_undo_savepoints_eviction_and_epoch_reset", repeated_page_undo_epochs);
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
