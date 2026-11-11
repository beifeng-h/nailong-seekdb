#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winioctl.h>
BOOL WINAPI io_test_read_file(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED);
#define ReadFile io_test_read_file
#else
#include <unistd.h>
ssize_t io_test_pread(int, void*, std::size_t, off_t);
#define pread io_test_pread
#endif

// Exercise the production file implementation with controlled native reads.
#include "../src/page_store.cpp"

#ifdef _WIN32
#undef ReadFile
#else
#undef pread
#endif

namespace {

std::size_t read_limit = std::numeric_limits<std::size_t>::max();
unsigned native_calls = 0;
bool inject_error = false;
#ifndef _WIN32
unsigned interrupted_reads = 0;
#endif

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

std::uint32_t reference_crc_update(std::uint32_t value, const void* data, std::size_t length) {
  const auto* bytes = static_cast<const unsigned char*>(data);
  for (std::size_t index = 0; index < length; ++index) {
    value ^= bytes[index];
    for (unsigned bit = 0; bit < 8; ++bit)
      value = (value >> 1) ^ (0xedb88320U & (0U - (value & 1U)));
  }
  return value;
}

void check_crc_bytes(const void* data, std::size_t length) {
  const auto* bytes = static_cast<const unsigned char*>(data);
  for (const auto seed : {0U, 0xffffffffU, 0x12345678U}) {
    const auto expected = reference_crc_update(seed, data, length);
    require(minivectordb::crc_update(seed, data, length) == expected,
            "CRC lookup differs from the bitwise reference");
    for (const auto split : {std::size_t{0}, length / 2, length}) {
      auto actual = minivectordb::crc_update(seed, data, split);
      actual = minivectordb::crc_update(actual, bytes + split, length - split);
      require(actual == expected, "Incremental CRC differs across chunk boundaries");
    }
  }
  require(minivectordb::crc(data, length) == ~reference_crc_update(0xffffffffU, data, length),
          "CRC initialization or final complement differs");
}

void crc_contracts() {
  require(minivectordb::crc(nullptr, 0) == 0, "Empty CRC differs");
  require(minivectordb::crc_update(0x12345678U, nullptr, 0) == 0x12345678U,
          "Empty CRC update changed the seed");
  constexpr char standard[] = "123456789";
  require(minivectordb::crc(standard, 9) == 0xcbf43926U, "Standard CRC-32 check value differs");
  check_crc_bytes(standard, 9);
  std::array<unsigned char, 256> values{};
  for (std::size_t index = 0; index < values.size(); ++index)
    values[index] = static_cast<unsigned char>(index);
  check_crc_bytes(values.data(), values.size());
  minivectordb::Page page{};
  for (std::size_t index = 0; index < page.size(); ++index)
    page[index] = std::byte((index * 37 + 11) & 255);
  check_crc_bytes(page.data(), page.size());
}

struct TestDirectory {
  std::filesystem::path path;
  explicit TestDirectory(const char* name) : path(std::filesystem::absolute(name).lexically_normal()) {
    require(std::filesystem::create_directory(path), "Fresh test directory required");
  }
  ~TestDirectory() {
    std::error_code error;
    std::filesystem::remove(path / "mixed.bin", error);
    std::filesystem::remove(path / "large.bin", error);
    std::filesystem::remove(path, error);
  }
};

}  // namespace

#ifdef _WIN32
BOOL WINAPI io_test_read_file(HANDLE handle, LPVOID data, DWORD length, LPDWORD transferred, LPOVERLAPPED request) {
  ++native_calls;
  if (inject_error) {
    *transferred = 0;
    SetLastError(ERROR_READ_FAULT);
    return FALSE;
  }
  const auto count = static_cast<DWORD>(std::min<std::size_t>(length, read_limit));
  return ::ReadFile(handle, data, count, transferred, request);
}
#else
ssize_t io_test_pread(int descriptor, void* data, std::size_t length, off_t offset) {
  ++native_calls;
  if (interrupted_reads != 0) {
    --interrupted_reads;
    errno = EINTR;
    return -1;
  }
  if (inject_error) {
    errno = EIO;
    return -1;
  }
  return ::pread(descriptor, data, std::min(length, read_limit), offset);
}
#endif

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  try {
    TestDirectory directory(argv[1]);
    unsigned passed = 0;
    const auto pass = [&](const char* name) { ++passed; std::cout << "PASS " << name << '\n'; };
    crc_contracts();
    pass("crc32_matches_reference_and_incremental_updates");
    std::array<std::byte, 37> expected{};
    for (std::size_t index = 0; index < expected.size(); ++index) expected[index] = std::byte((index * 7 + 3) & 255);
    std::array<std::byte, 37> actual{};
    minivectordb::File file(directory.path / "mixed.bin");
    file.write(0, expected.data(), expected.size());
    file.read(0, actual.data(), actual.size());
    require(actual == expected, "Immediate read missed an unbuffered write");
    const std::array patch{std::byte{41}, std::byte{42}, std::byte{43}};
    file.write(9, patch.data(), patch.size());
    std::copy(patch.begin(), patch.end(), expected.begin() + 9);
    file.read(19, actual.data() + 19, 7);
    file.read(0, actual.data(), actual.size());
    require(actual == expected, "Interleaved cursor positions changed data");
    file.resize(11);
    require(file.size() == 11, "File resize length differs");
    pass("interleaved_write_read_and_resize");

    bool rejected = false;
    try { file.read(0, actual.data(), actual.size()); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "Partial EOF was accepted as a full read");
    file.write(0, expected.data(), expected.size());
    file.read(0, actual.data(), actual.size());
    require(actual == expected, "File could not be reused after EOF");
    pass("partial_eof_rejected_and_handle_reused");

    read_limit = 7;
    const auto before = native_calls;
    file.read(0, actual.data(), actual.size());
    require(actual == expected && native_calls - before >= 6, "Short reads were not completed at the correct offsets");
    read_limit = std::numeric_limits<std::size_t>::max();
    pass("short_reads_reassembled");

#ifndef _WIN32
    interrupted_reads = 2;
    file.read(0, actual.data(), actual.size());
    require(actual == expected && interrupted_reads == 0, "Interrupted reads were not retried");
    pass("interrupted_reads_retried");
#endif

    inject_error = true;
    rejected = false;
    try { file.read(0, actual.data(), actual.size()); }
    catch (const std::runtime_error&) { rejected = true; }
    inject_error = false;
    require(rejected, "Native I/O error was ignored");
    file.read(0, actual.data(), actual.size());
    require(actual == expected, "Read after reported I/O error differs");
    pass("native_io_error_reported");

    rejected = false;
    try { file.read(static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) + 1, actual.data(), 1); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "Out-of-range file offset was accepted");
    rejected = false;
    try { file.read(static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()), actual.data(), 1); }
    catch (const std::runtime_error&) { rejected = true; }
    require(rejected, "Out-of-range file extent was accepted");
    file.read(0, nullptr, 0);
    pass("offset_range_and_zero_length");

    const auto large_path = directory.path / "large.bin";
#ifdef _WIN32
    const auto handle = CreateFileW(large_path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                                    CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    require(handle != INVALID_HANDLE_VALUE, "Cannot create sparse offset fixture");
    DWORD returned = 0;
    const bool sparse = DeviceIoControl(handle, FSCTL_SET_SPARSE, nullptr, 0, nullptr, 0, &returned, nullptr) != 0;
    CloseHandle(handle);
    require(sparse, "Sparse file support required for large-offset fixture");
#endif
    minivectordb::File large(large_path);
    constexpr std::uint64_t offset = (std::uint64_t{1} << 32) + 73;
    large.write(0, expected.data(), expected.size());
    large.write(offset, expected.data(), expected.size());
    large.read(offset, actual.data(), actual.size());
    require(actual == expected, "64-bit positional read differs");
    large.read(0, actual.data(), actual.size());
    require(actual == expected, "High-offset read disturbed a later write/read position");
    large.sync();
    require(large.size() == offset + expected.size(), "Large sparse file length differs");
    pass("large_sparse_offsets_roundtrip");
    std::cout << "RESULT passed=" << passed << " failed=0\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
