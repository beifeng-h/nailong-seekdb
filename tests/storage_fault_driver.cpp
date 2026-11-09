#include "minivectordb/page_store.hpp"

#include <chrono>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using minivectordb::Page;
using minivectordb::PageStore;

Page pattern(unsigned seed) {
  Page page{};
  for (std::size_t i = 0; i < page.size(); ++i) page[i] = std::byte((seed * 31 + i * 17) & 255);
  return page;
}

// Test driver only. The parent kills the blocked process at the named hook.
int main(int argc, char** argv) {
  if (argc < 3) { std::cerr << "usage: storage_fault_driver MODE DIRECTORY [STAGE|PAGES]\n"; return 2; }
  try {
    const std::string mode = argv[1], stage = argc > 3 ? argv[3] : "";
    auto hook = [&](std::string_view value) {
      if (value != stage) return;
      std::cout << "FAULT_READY " << value << std::endl;
      for (;;) std::this_thread::sleep_for(std::chrono::seconds(1));
    };
    PageStore store(std::filesystem::path(argv[2]), hook);
    if (mode == "seed") {
      store.begin(); store.allocate(); store.write(0, pattern(1)); store.commit();
    } else if (mode == "commit" || mode == "uncommitted") {
      store.begin(); store.write(0, pattern(7)); store.allocate(); store.write(1, pattern(8));
      if (mode == "commit") store.commit();
      else hook("transaction.uncommitted");
    } else if (mode == "old" || mode == "new") {
      const bool next = mode == "new";
      if (store.size() != (next ? 2U : 1U) || store.read(0) != pattern(next ? 7 : 1) ||
          (next && store.read(1) != pattern(8))) throw std::runtime_error("Database contains mixed state");
    } else if (mode == "recover") {
      if (store.size() != 2 || store.read(0) != pattern(7) || store.read(1) != pattern(8))
        throw std::runtime_error("Repeated recovery differs");
    } else if (mode == "stream-write") {
      const auto count = std::stoull(stage); store.begin();
      for (std::uint64_t i = 0; i < count; ++i) { const auto id = store.allocate(); store.write(id, pattern(static_cast<unsigned>(i))); }
      store.commit();
    } else if (mode == "stream-read") {
      const auto count = std::stoull(stage);
      if (store.size() != count) throw std::runtime_error("Stream count mismatch");
      for (std::uint64_t i = 0; i < count; ++i)
        if (store.read(i) != pattern(static_cast<unsigned>(i))) throw std::runtime_error("Stream page mismatch");
    } else { throw std::runtime_error("Unknown test-driver mode"); }
    std::cout << "PASS " << mode << '\n'; return 0;
  } catch (const std::exception& error) { std::cerr << "FAIL " << error.what() << '\n'; return 1; }
}
