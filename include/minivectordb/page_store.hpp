#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <string_view>

namespace minivectordb {

inline constexpr std::size_t page_size = 4096;
using Page = std::array<std::byte, page_size>;

// Single-client disk pages. A successful commit is durable before returning.
// A commit I/O error makes the handle unusable; reopen to resolve its outcome.
class PageStore {
 public:
  using FaultHook = std::function<void(std::string_view)>;
  struct Savepoint {
    std::uint64_t generation;
    std::uint64_t undo_size;
    std::uint64_t page_count;
  };

  explicit PageStore(const std::filesystem::path& directory, FaultHook hook = {});
  ~PageStore();
  PageStore(const PageStore&) = delete;
  PageStore& operator=(const PageStore&) = delete;

  void begin();
  std::uint64_t allocate();
  Page read(std::uint64_t page_id, std::size_t prefix_bytes = page_size);
  void write(std::uint64_t page_id, const Page& page);
  Savepoint savepoint() const;
  void rollback_to(Savepoint point);
  void commit();
  void rollback();
  std::uint64_t size() const;
  bool in_transaction() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace minivectordb
