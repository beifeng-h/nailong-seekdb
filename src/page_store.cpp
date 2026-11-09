#include "minivectordb/page_store.hpp"

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#ifdef _WIN32
#include <io.h>
#else
#include <unistd.h>
#endif

namespace minivectordb {
namespace {

constexpr std::uint64_t wal_header_size = 44;
constexpr std::uint64_t frame_size = 16 + page_size;
constexpr std::uint64_t footer_size = 32;
constexpr std::uint64_t undo_record_size = 16 + page_size;

[[noreturn]] void fail(const std::string& message) {
  throw std::runtime_error(message);
}

std::uint64_t checked_offset(std::uint64_t page_id, bool data_header = false) {
  const auto extra = data_header ? page_size : 0;
  const auto maximum = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
  if (page_id > (maximum - extra) / page_size) fail("Page offset overflow");
  return page_id * page_size + extra;
}

class File {
 public:
  File(const std::filesystem::path& path, bool reset = false) : path_(path) {
#ifdef _WIN32
    auto open_error = _wfopen_s(&stream_, path.c_str(), reset ? L"w+b" : L"r+b");
    if (open_error == ENOENT && !reset) open_error = _wfopen_s(&stream_, path.c_str(), L"w+b");
#else
    stream_ = std::fopen(path.c_str(), reset ? "w+b" : "r+b");
    if (!stream_ && !reset && errno == ENOENT) stream_ = std::fopen(path.c_str(), "w+b");
    const auto open_error = errno;
#endif
    if (!stream_) fail("Cannot open " + path.string() + ": " + std::generic_category().message(open_error));
    if (std::setvbuf(stream_, nullptr, _IONBF, 0) != 0) fail("Cannot disable file buffering");
  }
  ~File() { if (stream_) std::fclose(stream_); }
  File(const File&) = delete;
  File& operator=(const File&) = delete;

  std::uint64_t size() const { return std::filesystem::file_size(path_); }

  void seek(std::uint64_t offset) {
    if (offset > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
      fail("File offset overflow");
#ifdef _WIN32
    const int result = _fseeki64(stream_, static_cast<__int64>(offset), SEEK_SET);
#else
    const int result = fseeko(stream_, static_cast<off_t>(offset), SEEK_SET);
#endif
    if (result != 0) fail("File seek failed");
  }

  void read(std::uint64_t offset, void* data, std::size_t length) {
    seek(offset);
    if (length != 0 && std::fread(data, 1, length, stream_) != length)
      fail("Incomplete file read: " + path_.string());
  }

  void write(std::uint64_t offset, const void* data, std::size_t length) {
    seek(offset);
    if (length != 0 && std::fwrite(data, 1, length, stream_) != length)
      fail("Incomplete file write: " + path_.string());
  }

  void resize(std::uint64_t size) {
    if (size > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
      fail("File size overflow");
#ifdef _WIN32
    const int result = _chsize_s(_fileno(stream_), size);
#else
    const int result = ftruncate(fileno(stream_), static_cast<off_t>(size));
#endif
    if (result != 0) fail("File resize failed: " + path_.string());
  }

  void sync() {
#ifdef _WIN32
    const int result = _commit(_fileno(stream_));
#else
    int result;
    do { result = fsync(fileno(stream_)); } while (result != 0 && errno == EINTR);
#endif
    if (result != 0) fail("File synchronization failed: " + path_.string());
  }

 private:
  std::filesystem::path path_;
  std::FILE* stream_ = nullptr;
};

std::uint32_t crc_update(std::uint32_t value, const void* data, std::size_t length) {
  const auto* bytes = static_cast<const unsigned char*>(data);
  for (std::size_t i = 0; i < length; ++i) {
    value ^= bytes[i];
    for (int bit = 0; bit < 8; ++bit)
      value = (value >> 1) ^ (0xedb88320U & (0U - (value & 1U)));
  }
  return value;
}

std::uint32_t crc(const void* data, std::size_t length) {
  return ~crc_update(0xffffffffU, data, length);
}

void put64(std::byte* data, std::uint64_t value) {
  for (unsigned i = 0; i < 8; ++i) data[i] = std::byte((value >> (8 * i)) & 255);
}
void put32(std::byte* data, std::uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) data[i] = std::byte((value >> (8 * i)) & 255);
}
std::uint64_t get64(const std::byte* data) {
  std::uint64_t result = 0;
  for (unsigned i = 0; i < 8; ++i) result |= std::uint64_t(std::to_integer<unsigned>(data[i])) << (8 * i);
  return result;
}
std::uint32_t get32(const std::byte* data) {
  return static_cast<std::uint32_t>(get64((std::array<std::byte, 8>{data[0], data[1], data[2], data[3]}).data()));
}
bool magic(const std::byte* data, const char* expected) { return std::memcmp(data, expected, 8) == 0; }

Page make_data_header(std::uint64_t pages, std::uint64_t transaction) {
  Page result{};
  std::memcpy(result.data(), "MVDBPG01", 8);
  put32(result.data() + 8, 1);
  put32(result.data() + 12, page_size);
  put64(result.data() + 16, pages);
  put64(result.data() + 24, transaction);
  put32(result.data() + 32, crc(result.data(), 32));
  return result;
}

}  // namespace

struct PageStore::Impl {
  std::filesystem::path directory;
  std::unique_ptr<File> data, wal, stage, bitmap, undo;
  FaultHook hook;
  std::uint64_t committed_pages = 0, logical_pages = 0, transaction = 0;
  std::uint64_t generation = 0, dirty_count = 0;
  bool active = false, poisoned = false;

  explicit Impl(const std::filesystem::path& path, FaultHook callback)
      : directory(path), hook(std::move(callback)) {
    std::filesystem::create_directories(directory);
    data = std::make_unique<File>(directory / "pages.bin");
    wal = std::make_unique<File>(directory / "redo.wal");
    if (data->size() == 0 && wal->size() == 0) {
      const auto header = make_data_header(0, 0);
      data->write(0, header.data(), header.size());
      data->sync();
    }
    recover();
    Page header{};
    data->read(0, header.data(), header.size());
    if (!magic(header.data(), "MVDBPG01") || get32(header.data() + 8) != 1 ||
        get32(header.data() + 12) != page_size || get32(header.data() + 32) != crc(header.data(), 32))
      fail("Invalid data header");
    committed_pages = logical_pages = get64(header.data() + 16);
    transaction = get64(header.data() + 24);
    if (data->size() != checked_offset(committed_pages, true)) fail("Data file length mismatch");
    remove_staging();
  }

  void usable() const { if (poisoned) fail("Commit outcome uncertain; reopen the database"); }
  void require_transaction() const { usable(); if (!active) fail("No active transaction"); }
  void point(std::string_view value) { if (hook) hook(value); }

  void remove_staging() {
    stage.reset(); bitmap.reset(); undo.reset();
    for (const char* name : {"txn.pages", "txn.bits", "txn.undo"})
      std::filesystem::remove(directory / name);
  }

  bool dirty(std::uint64_t id) {
    unsigned char byte = 0;
    if (id / 8 < bitmap->size()) bitmap->read(id / 8, &byte, 1);
    return (byte & (1U << (id % 8))) != 0;
  }

  void set_dirty(std::uint64_t id, bool value) {
    unsigned char byte = 0;
    if (id / 8 < bitmap->size()) bitmap->read(id / 8, &byte, 1);
    const bool previous = (byte & (1U << (id % 8))) != 0;
    if (previous == value) return;
    if (value) { byte |= static_cast<unsigned char>(1U << (id % 8)); ++dirty_count; }
    else { byte &= static_cast<unsigned char>(~(1U << (id % 8))); --dirty_count; }
    bitmap->write(id / 8, &byte, 1);
  }

  template<class Callback>
  void each_dirty(Callback callback) {
    std::array<unsigned char, page_size> bits{};
    const auto length = bitmap->size();
    for (std::uint64_t offset = 0; offset < length; offset += bits.size()) {
      const auto count = static_cast<std::size_t>(std::min<std::uint64_t>(bits.size(), length - offset));
      bitmap->read(offset, bits.data(), count);
      for (std::size_t i = 0; i < count; ++i) {
        if (bits[i] == 0) continue;
        for (unsigned bit = 0; bit < 8; ++bit) {
          if ((bits[i] & (1U << bit)) == 0) continue;
          const auto id = (offset + i) * 8 + bit;
          if (id >= logical_pages) fail("Dirty page outside transaction");
          callback(id);
        }
      }
    }
  }

  void clear_wal() { wal->resize(0); wal->sync(); }

  void recover(bool committing = false) {
    const auto length = wal->size();
    if (length == 0) return;
    if (length < wal_header_size) { clear_wal(); return; }
    std::array<std::byte, wal_header_size> header{};
    wal->read(0, header.data(), header.size());
    if (!magic(header.data(), "MVDBWL01") || get32(header.data() + 8) != 1 ||
        get32(header.data() + 12) != page_size || get32(header.data() + 40) != crc(header.data(), 40))
      fail("Invalid WAL header");
    const auto tx = get64(header.data() + 16);
    const auto pages = get64(header.data() + 24);
    const auto frames = get64(header.data() + 32);
    checked_offset(pages, true);
    if (frames > (std::numeric_limits<std::uint64_t>::max() - wal_header_size - footer_size) / frame_size)
      fail("WAL frame count overflow");
    const auto expected = wal_header_size + frames * frame_size + footer_size;
    if (length < expected) { clear_wal(); return; }
    if (length != expected) fail("Unexpected bytes after WAL commit");
    std::uint32_t rolling = crc_update(0xffffffffU, header.data(), header.size());
    std::array<std::byte, frame_size> frame{};
    for (std::uint64_t i = 0; i < frames; ++i) {
      wal->read(wal_header_size + i * frame_size, frame.data(), frame.size());
      if (get64(frame.data()) >= pages || get32(frame.data() + 8) != page_size ||
          get32(frame.data() + 12) != crc(frame.data() + 16, page_size))
        fail("Invalid committed WAL frame");
      rolling = crc_update(rolling, frame.data(), frame.size());
    }
    std::array<std::byte, footer_size> footer{};
    wal->read(expected - footer.size(), footer.data(), footer.size());
    if (!magic(footer.data(), "COMMIT01") || get64(footer.data() + 8) != tx ||
        get64(footer.data() + 16) != frames || get32(footer.data() + 24) != ~rolling ||
        get32(footer.data() + 28) != crc(footer.data(), 28)) fail("Invalid WAL commit marker");
    point(committing ? "commit.verified" : "recover.verified");
    // Validate the complete transaction before changing any persistent pages.
    data->resize(checked_offset(pages, true));
    for (std::uint64_t i = 0; i < frames; ++i) {
      wal->read(wal_header_size + i * frame_size, frame.data(), frame.size());
      data->write(checked_offset(get64(frame.data()), true), frame.data() + 16, page_size);
      point(committing ? "commit.applied_page" : "recover.applied_page");
    }
    const auto data_header = make_data_header(pages, tx);
    data->write(0, data_header.data(), data_header.size());
    data->sync();
    point(committing ? "commit.data_synced" : "recover.data_synced");
    clear_wal();
    point(committing ? "commit.wal_cleared" : "recover.wal_cleared");
  }
};

PageStore::PageStore(const std::filesystem::path& path, FaultHook hook)
    : impl_(std::make_unique<Impl>(path, std::move(hook))) {}
PageStore::~PageStore() = default;

void PageStore::begin() {
  auto& s = *impl_; s.usable();
  if (s.active) fail("Nested transactions are unsupported");
  s.remove_staging();
  s.stage = std::make_unique<File>(s.directory / "txn.pages", true);
  s.bitmap = std::make_unique<File>(s.directory / "txn.bits", true);
  s.undo = std::make_unique<File>(s.directory / "txn.undo", true);
  s.logical_pages = s.committed_pages; s.dirty_count = 0;
  s.active = true; ++s.generation;
}

std::uint64_t PageStore::allocate() {
  auto& s = *impl_; s.require_transaction();
  if (s.logical_pages == std::numeric_limits<std::uint64_t>::max()) fail("Page count overflow");
  checked_offset(s.logical_pages + 1, true);
  return s.logical_pages++;
}

Page PageStore::read(std::uint64_t id) {
  auto& s = *impl_; s.usable();
  if (id >= s.logical_pages) fail("Page ID outside database");
  Page page{};
  if (s.active && s.dirty(id)) s.stage->read(checked_offset(id), page.data(), page.size());
  else if (id < s.committed_pages) s.data->read(checked_offset(id, true), page.data(), page.size());
  return page;
}

void PageStore::write(std::uint64_t id, const Page& page) {
  auto& s = *impl_; s.require_transaction();
  const auto before = read(id);
  std::array<std::byte, undo_record_size> record{};
  put64(record.data(), id); record[8] = std::byte(s.dirty(id) ? 1 : 0);
  std::copy(before.begin(), before.end(), record.begin() + 16);
  s.undo->write(s.undo->size(), record.data(), record.size());
  s.stage->write(checked_offset(id), page.data(), page.size());
  s.set_dirty(id, true);
}

PageStore::Savepoint PageStore::savepoint() const {
  const auto& s = *impl_; s.require_transaction();
  return {s.generation, s.undo->size(), s.logical_pages};
}

void PageStore::rollback_to(Savepoint point) {
  auto& s = *impl_; s.require_transaction();
  const auto end = s.undo->size();
  if (point.generation != s.generation || point.undo_size > end ||
      point.undo_size % undo_record_size != 0 || point.page_count > s.logical_pages)
    fail("Invalid savepoint");
  std::array<std::byte, undo_record_size> record{};
  for (auto offset = end; offset > point.undo_size; offset -= record.size()) {
    s.undo->read(offset - record.size(), record.data(), record.size());
    const auto id = get64(record.data());
    const bool was_dirty = std::to_integer<unsigned>(record[8]) != 0;
    if (was_dirty) s.stage->write(checked_offset(id), record.data() + 16, page_size);
    s.set_dirty(id, was_dirty);
  }
  s.undo->resize(point.undo_size);
  s.logical_pages = point.page_count;
}

void PageStore::rollback() {
  auto& s = *impl_; s.require_transaction();
  s.logical_pages = s.committed_pages; s.active = false;
  s.remove_staging();
}

void PageStore::commit() {
  auto& s = *impl_; s.require_transaction();
  if (s.transaction == std::numeric_limits<std::uint64_t>::max()) fail("Transaction ID overflow");
  try {
    std::array<std::byte, wal_header_size> header{};
    std::memcpy(header.data(), "MVDBWL01", 8);
    put32(header.data() + 8, 1); put32(header.data() + 12, page_size);
    put64(header.data() + 16, s.transaction + 1);
    put64(header.data() + 24, s.logical_pages); put64(header.data() + 32, s.dirty_count);
    put32(header.data() + 40, crc(header.data(), 40));
    s.wal->resize(0); s.wal->write(0, header.data(), header.size());
    s.point("commit.header_written");
    std::uint32_t rolling = crc_update(0xffffffffU, header.data(), header.size());
    std::uint64_t position = header.size(), written = 0;
    s.each_dirty([&](std::uint64_t id) {
      std::array<std::byte, frame_size> frame{};
      put64(frame.data(), id); put32(frame.data() + 8, page_size);
      s.stage->read(checked_offset(id), frame.data() + 16, page_size);
      put32(frame.data() + 12, crc(frame.data() + 16, page_size));
      s.wal->write(position, frame.data(), frame.size());
      rolling = crc_update(rolling, frame.data(), frame.size());
      position += frame.size(); ++written;
      s.point("commit.frame_written");
    });
    if (written != s.dirty_count) fail("Transaction dirty count mismatch");
    std::array<std::byte, footer_size> footer{};
    std::memcpy(footer.data(), "COMMIT01", 8);
    put64(footer.data() + 8, s.transaction + 1); put64(footer.data() + 16, written);
    put32(footer.data() + 24, ~rolling); put32(footer.data() + 28, crc(footer.data(), 28));
    s.wal->write(position, footer.data(), footer.size());
    s.point("commit.marker_written");
    s.wal->sync(); s.point("commit.wal_synced");
    s.recover(true);
    s.committed_pages = s.logical_pages; ++s.transaction;
    s.active = false; s.remove_staging();
  } catch (...) { s.poisoned = true; throw; }
}

std::uint64_t PageStore::size() const { impl_->usable(); return impl_->logical_pages; }
bool PageStore::in_transaction() const { impl_->usable(); return impl_->active; }

}  // namespace minivectordb
