#include "minivectordb/database.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <queue>
#include <random>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace minivectordb {
namespace {

[[noreturn]] void invalid(const std::string& message) { throw std::runtime_error(message); }
void put64(std::byte* data, std::uint64_t value) {
  for (unsigned i = 0; i < 8; ++i) data[i] = std::byte((value >> (8 * i)) & 255);
}
std::uint64_t get64(const std::byte* data) {
  std::uint64_t value = 0;
  for (unsigned i = 0; i < 8; ++i) value |= std::uint64_t(std::to_integer<unsigned>(data[i])) << (8 * i);
  return value;
}
std::uint32_t get32(const std::byte* data) {
  std::uint32_t value = 0;
  for (unsigned i = 0; i < 4; ++i) value |= std::uint32_t(std::to_integer<unsigned>(data[i])) << (8 * i);
  return value;
}
void append32(std::vector<std::byte>& output, std::uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) output.push_back(std::byte((value >> (8 * i)) & 255));
}
void append64(std::vector<std::byte>& output, std::uint64_t value) {
  for (unsigned i = 0; i < 8; ++i) output.push_back(std::byte((value >> (8 * i)) & 255));
}
void append_string(std::vector<std::byte>& output, const std::string& value) {
  if (value.size() > std::numeric_limits<std::uint32_t>::max()) invalid("String is too large");
  append32(output, static_cast<std::uint32_t>(value.size()));
  for (unsigned char c : value) output.push_back(std::byte(c));
}
struct Decoder {
  const std::vector<std::byte>& data;
  std::size_t offset = 0;
  void need(std::size_t length) const {
    if (offset > data.size() || length > data.size() - offset) invalid("Invalid stored record");
  }
  std::uint32_t u32() { need(4); const auto n = get32(data.data() + offset); offset += 4; return n; }
  std::uint64_t u64() { need(8); const auto n = get64(data.data() + offset); offset += 8; return n; }
  std::string string() {
    const auto length = u32(); need(length);
    std::string value(reinterpret_cast<const char*>(data.data() + offset), length);
    offset += length; return value;
  }
  void complete() const { if (offset != data.size()) invalid("Trailing stored record bytes"); }
};

std::uint64_t hash_key(std::int64_t key) {
  auto x = std::bit_cast<std::uint64_t>(key) + 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

void check_value(const Column& column, const Value& value) {
  if (column.type == Type::bigint) {
    if (!std::holds_alternative<std::int64_t>(value)) invalid("BIGINT value required");
  } else if (column.type == Type::varchar) {
    const auto* text = std::get_if<std::string>(&value);
    if (!text || unicode_length(*text) > column.extent) invalid("VARCHAR value or length mismatch");
  } else if (column.type == Type::vector) {
    const auto* vector = std::get_if<Vector>(&value);
    if (!vector || vector->size() != column.extent) invalid("VECTOR dimension mismatch");
    for (float n : *vector) if (!std::isfinite(n)) invalid("VECTOR must contain finite float32 values");
  } else invalid("Unknown column type");
}

void check_row(const Schema& schema, const Row& row) {
  if (row.size() != schema.columns.size()) invalid("Row column count mismatch");
  for (std::size_t i = 0; i < row.size(); ++i) check_value(schema.columns[i], row[i]);
}

std::vector<std::byte> encode_row(const Row& row) {
  std::vector<std::byte> bytes;
  for (const auto& value : row) {
    if (const auto* integer = std::get_if<std::int64_t>(&value)) append64(bytes, std::bit_cast<std::uint64_t>(*integer));
    else if (const auto* text = std::get_if<std::string>(&value)) append_string(bytes, *text);
    else {
      const auto& vector = std::get<Vector>(value);
      if constexpr (std::endian::native == std::endian::little) {
        const auto raw = std::as_bytes(std::span(vector));
        bytes.insert(bytes.end(), raw.begin(), raw.end());
      } else {
        for (float number : vector) append32(bytes, std::bit_cast<std::uint32_t>(number));
      }
    }
  }
  return bytes;
}

Row decode_row(const Schema& schema, const std::vector<std::byte>& bytes) {
  Decoder input{bytes}; Row row;
  row.reserve(schema.columns.size());
  for (const auto& column : schema.columns) {
    if (column.type == Type::bigint) row.emplace_back(std::bit_cast<std::int64_t>(input.u64()));
    else if (column.type == Type::varchar) row.emplace_back(input.string());
    else {
      const auto length = std::size_t(column.extent) * sizeof(float);
      input.need(length);
      Vector vector(column.extent);
      if constexpr (std::endian::native == std::endian::little) {
        std::memcpy(vector.data(), input.data.data() + input.offset, length);
        input.offset += length;
      } else {
        for (auto& number : vector) number = std::bit_cast<float>(input.u32());
      }
      row.emplace_back(std::move(vector));
    }
  }
  input.complete(); return row;
}

struct Table {
  std::uint64_t id = 0, schema_blob = 0, next = 0, directory = 0, buckets = 0, rows = 0, height = 1;
  std::uint64_t vector_index = 0;
  Schema schema;
};
constexpr std::size_t bucket_capacity = (page_size - 16) / 16;
constexpr std::size_t blob_capacity = page_size - 16;
constexpr std::uint32_t schema_labels_tag = 0x314c424c;
constexpr std::string_view sort_owner = "MiniVectorDB transient sort v1\n";

void cleanup_interrupted_sorts(const std::filesystem::path& parent) {
  for (const auto& entry : std::filesystem::directory_iterator(parent)) {
    if (entry.is_symlink() || !entry.is_directory() ||
        !entry.path().filename().string().starts_with("minivectordb-sort-")) continue;
    const auto marker = entry.path() / "OWNER";
    if (std::filesystem::is_symlink(marker) || !std::filesystem::is_regular_file(marker)) continue;
    std::ifstream owner(marker, std::ios::binary); std::array<char, 64> text{};
    owner.read(text.data(), text.size());
    if (std::string_view(text.data(), static_cast<std::size_t>(owner.gcount())) != sort_owner) continue;
    owner.close(); bool recognized = true;
    for (const auto& file : std::filesystem::directory_iterator(entry.path())) {
      const auto name = file.path().filename().string();
      if (file.is_symlink() || !file.is_regular_file()) { recognized = false; break; }
      if (name == "OWNER") continue;
      if (!name.starts_with("run-") || name.size() < 7) { recognized = false; break; }
      for (char c : name.substr(4)) if ((c < '0' || c > '9') && c != '-') recognized = false;
    }
    if (!recognized) continue;
    for (const auto& file : std::filesystem::directory_iterator(entry.path())) std::filesystem::remove(file.path());
    std::filesystem::remove(entry.path());
  }
}

struct SortWorkspace {
  std::filesystem::path directory;
  explicit SortWorkspace(const std::filesystem::path& parent) {
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
      directory = parent / ("minivectordb-sort-" + std::to_string(std::random_device{}()));
      if (std::filesystem::create_directory(directory)) {
        std::ofstream marker(directory / "OWNER", std::ios::binary); marker << sort_owner; marker.close();
        if (!marker) invalid("Cannot mark sort workspace");
        return;
      }
    }
    invalid("Cannot create sort workspace");
  }
  ~SortWorkspace() {
    std::error_code error;
    for (const auto& entry : std::filesystem::directory_iterator(directory, error))
      if (entry.is_regular_file()) std::filesystem::remove(entry.path(), error);
    std::filesystem::remove(directory, error);
  }
  std::filesystem::path path(std::uint64_t pass, std::uint64_t number) const {
    return directory / ("run-" + std::to_string(pass) + "-" + std::to_string(number));
  }
};

class RunWriter {
 public:
  explicit RunWriter(const std::filesystem::path& path) : output_(path, std::ios::binary) {
    if (!output_) invalid("Cannot open sort output");
  }
  void put(const Row& row) {
    const auto bytes = encode_row(row); std::array<std::byte, 8> length{};
    put64(length.data(), bytes.size());
    output_.write(reinterpret_cast<const char*>(length.data()), length.size());
    output_.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    if (!output_) invalid("Cannot write sort output");
  }
  void finish() { output_.flush(); output_.close(); if (!output_) invalid("Cannot finish sort output"); }
 private:
  std::ofstream output_;
};

class RunReader {
 public:
  RunReader(const std::filesystem::path& path, const Schema& schema) : input_(path, std::ios::binary), schema_(schema) {
    if (!input_) invalid("Cannot open sort input");
  }
  std::optional<Row> next() {
    std::array<std::byte, 8> length{};
    input_.read(reinterpret_cast<char*>(length.data()), length.size());
    if (input_.gcount() == 0 && input_.eof()) return {};
    if (input_.gcount() != static_cast<std::streamsize>(length.size())) invalid("Incomplete sort record");
    const auto count = get64(length.data());
    if (count > static_cast<std::uint64_t>(std::numeric_limits<std::streamsize>::max())) invalid("Sort record too large");
    std::vector<std::byte> bytes(static_cast<std::size_t>(count));
    input_.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    if (!input_) invalid("Incomplete sort record data");
    return decode_row(schema_, bytes);
  }
 private:
  std::ifstream input_;
  const Schema& schema_;
};

std::size_t row_memory(const Row& row) {
  std::size_t bytes = sizeof(Row) * 2 + row.capacity() * sizeof(Value);
  for (const auto& value : row) {
    if (const auto* text = std::get_if<std::string>(&value)) bytes += text->capacity() + 1;
    else if (const auto* vector = std::get_if<Vector>(&value)) bytes += vector->capacity() * sizeof(float);
  }
  return bytes;
}

void external_sort(const std::filesystem::path& directory, const Schema& schema,
                   const std::function<void(const Visitor&)>& source,
                   const std::function<bool(const Row&, const Row&)>& before,
                   std::optional<std::uint64_t> limit, const Visitor& visitor, std::size_t budget) {
  SortWorkspace workspace(directory); std::vector<Row> chunk;
  std::size_t memory = 0; std::uint64_t runs = 0;
  const auto flush = [&] {
    if (chunk.empty()) return;
    std::sort(chunk.begin(), chunk.end(), before);
    RunWriter output(workspace.path(0, runs++));
    for (const auto& row : chunk) output.put(row);
    output.finish(); std::vector<Row>().swap(chunk); memory = 0;
  };
  source([&](const Row& row) {
    const auto size = row_memory(row);
    if (!chunk.empty() && (size > budget || memory > budget - size)) flush();
    memory += size; chunk.push_back(row); return true;
  });
  flush(); std::uint64_t pass = 0;
  while (runs > 1) {
    std::uint64_t next_runs = 0;
    for (std::uint64_t i = 0; i < runs; i += 2) {
      if (i + 1 == runs) {
        std::filesystem::rename(workspace.path(pass, i), workspace.path(pass + 1, next_runs++)); continue;
      }
      {
        RunReader left(workspace.path(pass, i), schema), right(workspace.path(pass, i + 1), schema);
        RunWriter output(workspace.path(pass + 1, next_runs++)); auto a = left.next(), b = right.next();
        while (a || b) {
          if (a && (!b || !before(*b, *a))) { output.put(*a); a = left.next(); }
          else { output.put(*b); b = right.next(); }
        }
        output.finish();
      }
      std::filesystem::remove(workspace.path(pass, i)); std::filesystem::remove(workspace.path(pass, i + 1));
    }
    runs = next_runs; ++pass;
  }
  if (runs == 0) return;
  RunReader input(workspace.path(pass, 0), schema); std::uint64_t emitted = 0;
  while (!limit || emitted < *limit) {
    const auto row = input.next(); if (!row || !visitor(*row)) break; ++emitted;
  }
}

double squared_distance(const Vector& vector, const Vector& query) {
  double distance = 0;
  for (std::size_t i = 0; i < vector.size(); ++i) {
    const double delta = double(vector[i]) - double(query[i]); distance += delta * delta;
  }
  return distance;
}

std::size_t vector_column(const Schema& schema) {
  for (std::size_t i = 0; i < schema.columns.size(); ++i) {
    if (schema.columns[i].type == Type::vector) return i;
  }
  invalid("Table has no vector column");
}

}  // namespace

void validate_value(const Column& column, const Value& value) {
  check_value(column, value);
}

double l2_distance(const Vector& left, const Vector& right) {
  if (left.size() != right.size()) invalid("VECTOR dimension mismatch");
  const auto squared = squared_distance(left, right);
  if (!std::isfinite(squared)) invalid("VECTOR must contain finite float32 values");
  return std::sqrt(squared);
}

std::size_t unicode_length(std::string_view text) {
  std::size_t count = 0;
  for (std::size_t i = 0; i < text.size(); ++count) {
    const auto lead = static_cast<unsigned char>(text[i++]);
    std::uint32_t code = 0; unsigned remaining = 0;
    if (lead < 0x80) code = lead;
    else if (lead >= 0xc2 && lead <= 0xdf) { code = lead & 31; remaining = 1; }
    else if (lead >= 0xe0 && lead <= 0xef) { code = lead & 15; remaining = 2; }
    else if (lead >= 0xf0 && lead <= 0xf4) { code = lead & 7; remaining = 3; }
    else invalid("Invalid UTF-8 lead byte");
    const auto width = remaining + 1;
    if (remaining > text.size() - i) invalid("Incomplete UTF-8 character");
    while (remaining--) {
      const auto byte = static_cast<unsigned char>(text[i++]);
      if ((byte & 0xc0) != 0x80) invalid("Invalid UTF-8 continuation");
      code = (code << 6) | (byte & 63);
    }
    if ((width == 2 && code < 0x80) || (width == 3 && code < 0x800) ||
        (width == 4 && code < 0x10000) || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff))
      invalid("Invalid Unicode scalar value");
  }
  return count;
}

int compare_strings(std::string_view left, std::string_view right) {
  unicode_length(left); unicode_length(right);
  const auto length = std::min(left.size(), right.size());
  for (std::size_t i = 0; i < length; ++i) {
    const auto a = static_cast<unsigned char>(left[i]), b = static_cast<unsigned char>(right[i]);
    if (a != b) return a < b ? -1 : 1;
  }
  return left.size() == right.size() ? 0 : left.size() < right.size() ? -1 : 1;
}

Vector make_vector(std::span<const double> input) {
  static_assert(std::numeric_limits<float>::is_iec559 && std::numeric_limits<float>::digits == 24);
  Vector result; result.reserve(input.size());
  for (double value : input) {
    if (!std::isfinite(value))
      invalid("Value cannot be represented as finite float32");
    const auto magnitude = std::abs(value);
    const double maximum = std::numeric_limits<float>::max();
    if (magnitude > maximum) {
      // Round-to-nearest overflows at the midpoint above the largest finite value.
      // Avoid an out-of-range C++ floating conversion in the finite rounding band.
      const double threshold = std::ldexp(1.0, 128) - std::ldexp(1.0, 103);
      if (magnitude >= threshold) invalid("Value overflows finite float32");
      result.push_back(value < 0 ? -std::numeric_limits<float>::max() : std::numeric_limits<float>::max());
    } else result.push_back(static_cast<float>(value));
  }
  return result;
}

int compare_values(const Value& left, const Value& right) {
  if (left.index() != right.index()) invalid("Comparison operand types differ");
  if (const auto* a = std::get_if<std::int64_t>(&left)) {
    const auto b = std::get<std::int64_t>(right); return *a == b ? 0 : *a < b ? -1 : 1;
  }
  if (const auto* a = std::get_if<std::string>(&left)) return compare_strings(*a, std::get<std::string>(right));
  invalid("Ordinary comparison requires BIGINT or VARCHAR");
}

bool matches(const Row& row, std::span<const Filter> filters) {
  for (const auto& filter : filters) {
    if (filter.column >= row.size()) invalid("Unknown filter column");
    const auto order = compare_values(row[filter.column], filter.value); bool match;
    switch (filter.relation) {
      case Relation::equal: match = order == 0; break;
      case Relation::less: match = order < 0; break;
      case Relation::less_equal: match = order <= 0; break;
      case Relation::greater: match = order > 0; break;
      case Relation::greater_equal: match = order >= 0; break;
      default: invalid("Unsupported comparison operator");
    }
    if (!match) return false;
  }
  return true;
}

struct Database::Impl {
  std::filesystem::path directory;
  PageStore pages;
  explicit Impl(const std::filesystem::path& path, PageStore::FaultHook hook) : directory(path), pages(path, std::move(hook)) {
    cleanup_interrupted_sorts(directory);
    if (pages.size() == 0) {
      pages.begin(); pages.allocate(); Page root{};
      std::memcpy(root.data(), "MVDBRT01", 8); pages.write(0, root); pages.commit();
    }
    if (std::memcmp(pages.read(0).data(), "MVDBRT01", 8) != 0) invalid("Invalid database catalog");
  }

  template<class Action> void statement(Action action) {
    const bool automatic = !pages.in_transaction();
    if (automatic) pages.begin();
    const auto point = pages.savepoint();
    try { action(); }
    catch (...) {
      pages.rollback_to(point);
      if (automatic) pages.rollback();
      throw;
    }
    if (automatic) pages.commit();
  }

  std::uint64_t allocate() {
    auto root = pages.read(0); const auto free = get64(root.data() + 16);
    if (free == 0) return pages.allocate();
    const auto page = pages.read(free); put64(root.data() + 16, get64(page.data()));
    pages.write(0, root); pages.write(free, Page{}); return free;
  }

  void release(std::uint64_t id) {
    if (id == 0) invalid("Cannot release catalog root");
    auto root = pages.read(0); Page page{};
    put64(page.data(), get64(root.data() + 16)); put64(root.data() + 16, id);
    pages.write(id, page); pages.write(0, root);
  }

  std::uint64_t store_blob(std::span<const std::byte> bytes) {
    std::uint64_t next = 0;
    for (std::size_t end = bytes.size(); end > 0;) {
      const auto start = end > blob_capacity ? end - blob_capacity : 0;
      Page page{}; put64(page.data(), next); put64(page.data() + 8, end - start);
      std::copy(bytes.begin() + start, bytes.begin() + end, page.begin() + 16);
      const auto id = allocate(); pages.write(id, page); next = id; end = start;
    }
    return next;
  }

  std::vector<std::byte> blob(std::uint64_t id) {
    std::vector<std::byte> result; std::uint64_t visited = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic record pages");
      const auto page = pages.read(id); const auto used = get64(page.data() + 8);
      if (used > blob_capacity) invalid("Invalid record page length");
      result.insert(result.end(), page.begin() + 16, page.begin() + 16 + static_cast<std::size_t>(used));
      id = get64(page.data());
    }
    return result;
  }

  void release_blob(std::uint64_t id) {
    std::uint64_t visited = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic record pages");
      const auto next = get64(pages.read(id).data()); release(id); id = next;
    }
  }

  Table read_table(std::uint64_t id) {
    const auto page = pages.read(id);
    if (std::memcmp(page.data(), "MVDBTB01", 8) != 0) invalid("Invalid table catalog");
    Table table; table.id = id; table.schema_blob = get64(page.data() + 8);
    table.next = get64(page.data() + 16); table.directory = get64(page.data() + 24);
    table.buckets = get64(page.data() + 32); table.rows = get64(page.data() + 40);
    table.height = get64(page.data() + 48); table.vector_index = get64(page.data() + 56);
    const auto bytes = blob(table.schema_blob); Decoder input{bytes};
    table.schema.name = input.string(); table.schema.primary_key = input.u32(); const auto count = input.u32();
    for (std::uint32_t i = 0; i < count; ++i)
      table.schema.columns.push_back({input.string(), static_cast<Type>(input.u32()), input.u32()});
    // Display labels extend the original schema fields with a tagged payload.
    if (input.offset < bytes.size()) {
      if (input.u32() != schema_labels_tag) invalid("Unknown schema extension");
      for (auto& column : table.schema.columns) column.display_name = input.string();
    }
    input.complete(); return table;
  }

  void write_table(const Table& table) {
    Page page{}; std::memcpy(page.data(), "MVDBTB01", 8);
    put64(page.data() + 8, table.schema_blob); put64(page.data() + 16, table.next);
    put64(page.data() + 24, table.directory); put64(page.data() + 32, table.buckets);
    put64(page.data() + 40, table.rows); put64(page.data() + 48, table.height);
    put64(page.data() + 56, table.vector_index); pages.write(table.id, page);
  }

  Table table(const std::string& name) {
    auto id = get64(pages.read(0).data() + 8); std::uint64_t visited = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic table catalog");
      auto current = read_table(id); if (current.schema.name == name) return current; id = current.next;
    }
    invalid("Unknown table: " + name);
  }

  bool exists(const std::string& name) {
    auto id = get64(pages.read(0).data() + 8); std::uint64_t visited = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic table catalog");
      const auto current = read_table(id); if (current.schema.name == name) return true; id = current.next;
    }
    return false;
  }

  std::uint64_t directory_get(const Table& table, std::uint64_t bucket) {
    auto id = table.directory;
    for (auto level = table.height; level > 0; --level) {
      const auto page = pages.read(id); id = get64(page.data() + ((bucket >> (9 * (level - 1))) & 511) * 8);
      if (id == 0) invalid("Missing hash directory entry");
    }
    return id;
  }

  void directory_set(const Table& table, std::uint64_t bucket, std::uint64_t value) {
    auto id = table.directory;
    for (auto level = table.height; level > 0; --level) {
      auto page = pages.read(id); const auto offset = ((bucket >> (9 * (level - 1))) & 511) * 8;
      if (level == 1) { put64(page.data() + offset, value); pages.write(id, page); return; }
      auto next = get64(page.data() + offset);
      if (next == 0) { next = allocate(); put64(page.data() + offset, next); pages.write(id, page); }
      id = next;
    }
  }

  void new_directory(Table& table, std::uint64_t buckets) {
    table.buckets = buckets; table.height = 1;
    for (auto capacity = std::uint64_t(512); capacity < buckets; capacity *= 512) ++table.height;
    table.directory = allocate();
    for (std::uint64_t i = 0; i < buckets; ++i) directory_set(table, i, allocate());
  }

  std::uint64_t lookup(const Table& table, std::int64_t key) {
    auto id = directory_get(table, hash_key(key) & (table.buckets - 1));
    std::uint64_t visited = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic hash bucket");
      const auto page = pages.read(id); const auto count = get64(page.data() + 8);
      if (count > bucket_capacity) invalid("Invalid hash bucket size");
      for (std::size_t i = 0; i < count; ++i) {
        const auto* entry = page.data() + 16 + i * 16;
        if (std::bit_cast<std::int64_t>(get64(entry)) == key && get64(entry + 8) != 0) return get64(entry + 8);
      }
      id = get64(page.data());
    }
    return 0;
  }

  void index_put(const Table& table, std::int64_t key, std::uint64_t record) {
    auto id = directory_get(table, hash_key(key) & (table.buckets - 1));
    std::uint64_t free_page = 0, last = 0, visited = 0; std::size_t free_slot = 0;
    while (id != 0) {
      if (++visited > pages.size()) invalid("Cyclic hash bucket");
      auto page = pages.read(id); const auto count = get64(page.data() + 8);
      if (count > bucket_capacity) invalid("Invalid hash bucket size");
      for (std::size_t i = 0; i < count; ++i) {
        auto* entry = page.data() + 16 + i * 16;
        const auto pointer = get64(entry + 8);
        if (pointer != 0 && std::bit_cast<std::int64_t>(get64(entry)) == key) {
          put64(entry + 8, record); pages.write(id, page); return;
        }
        if (pointer == 0 && free_page == 0) { free_page = id; free_slot = i; }
      }
      if (count < bucket_capacity && free_page == 0) { free_page = id; free_slot = static_cast<std::size_t>(count); }
      last = id; id = get64(page.data());
    }
    if (record == 0) invalid("Cannot delete absent primary key");
    if (free_page == 0) {
      free_page = allocate(); auto page = pages.read(last);
      put64(page.data(), free_page); pages.write(last, page); free_slot = 0;
    }
    auto page = pages.read(free_page); const auto count = get64(page.data() + 8);
    put64(page.data() + 16 + free_slot * 16, std::bit_cast<std::uint64_t>(key));
    put64(page.data() + 24 + free_slot * 16, record);
    if (free_slot == count) put64(page.data() + 8, count + 1);
    pages.write(free_page, page);
  }

  template<class Function> void each_entry(const Table& table, Function function) {
    for (std::uint64_t bucket = 0; bucket < table.buckets; ++bucket) {
      auto id = directory_get(table, bucket); std::uint64_t visited = 0;
      while (id != 0) {
        if (++visited > pages.size()) invalid("Cyclic hash bucket");
        const auto page = pages.read(id); const auto count = get64(page.data() + 8);
        if (count > bucket_capacity) invalid("Invalid hash bucket size");
        for (std::size_t i = 0; i < count; ++i) {
          const auto* entry = page.data() + 16 + i * 16; const auto record = get64(entry + 8);
          if (record != 0 && !function(std::bit_cast<std::int64_t>(get64(entry)), record)) return;
        }
        id = get64(page.data());
      }
    }
  }

  void release_directory(std::uint64_t id, std::uint64_t height) {
    const auto page = pages.read(id);
    for (std::size_t i = 0; i < 512; ++i) {
      auto child = get64(page.data() + i * 8);
      if (height > 1 && child != 0) release_directory(child, height - 1);
      else while (child != 0) { const auto next = get64(pages.read(child).data()); release(child); child = next; }
    }
    release(id);
  }

  void grow(Table& table, std::uint64_t expected) {
    auto capacity = table.buckets;
    while (expected / 128 >= capacity) {
      if (capacity > (std::numeric_limits<std::uint64_t>::max() >> 1)) invalid("Hash capacity overflow");
      capacity *= 2;
    }
    if (capacity == table.buckets) return;
    const auto old = table; new_directory(table, capacity);
    each_entry(old, [&](std::int64_t key, std::uint64_t record) { index_put(table, key, record); return true; });
    write_table(table); release_directory(old.directory, old.height);
  }

  Table make_table(const Schema& schema) {
    std::vector<std::byte> bytes; append_string(bytes, schema.name); append32(bytes, schema.primary_key);
    append32(bytes, static_cast<std::uint32_t>(schema.columns.size()));
    for (const auto& column : schema.columns) {
      append_string(bytes, column.name); append32(bytes, static_cast<std::uint32_t>(column.type)); append32(bytes, column.extent);
    }
    if (std::any_of(schema.columns.begin(), schema.columns.end(), [](const Column& column) { return !column.display_name.empty(); })) {
      append32(bytes, schema_labels_tag);
      for (const auto& column : schema.columns) append_string(bytes, column.display_name);
    }
    Table table; table.schema = schema; table.schema_blob = store_blob(bytes); table.id = allocate();
    new_directory(table, 16);
    return table;
  }

  std::optional<Table> vector_index(const Table& table) {
    if (table.vector_index == 0) return {};
    return read_table(table.vector_index);
  }

  // The auxiliary table has one live (key, record page, float32 vector) row per key.
  // It stays outside the user table list and shares the owner's page transaction.
  void put_vector(Table& index, std::int64_t key, std::uint64_t record, const Vector& vector) {
    const auto old = lookup(index, key);
    if (old == 0) grow(index, index.rows + 1);
    const Row entry{key, std::bit_cast<std::int64_t>(record), vector};
    const auto replacement = store_blob(encode_row(entry));
    index_put(index, key, replacement);
    if (old != 0) release_blob(old);
    else ++index.rows;
  }

  void erase_vector(Table& index, std::int64_t key) {
    const auto record = lookup(index, key);
    if (record == 0) invalid("Missing vector index entry");
    index_put(index, key, 0); release_blob(record); --index.rows;
  }

  template<class Function>
  void each_vector(const Table& table, SearchMode mode, const Predicate& predicate, Function function) {
    if (mode == SearchMode::exact) {
      const auto column = vector_column(table.schema);
      each_entry(table, [&](std::int64_t key, std::uint64_t record) {
        const auto row = decode_row(table.schema, blob(record));
        if (predicate && !predicate(row)) return true;
        return function(key, record, std::get<Vector>(row[column]));
      });
      return;
    }
    const auto index = vector_index(table);
    if (!index) invalid("ANN mode requires a vector index");
    // Exhaustive flat-index search returns exact results using bounded query memory.
    each_entry(*index, [&](std::int64_t key, std::uint64_t entry) {
      const auto row = decode_row(index->schema, blob(entry));
      const auto record = std::bit_cast<std::uint64_t>(std::get<std::int64_t>(row[1]));
      if (predicate && !predicate(decode_row(table.schema, blob(record)))) return true;
      return function(key, record, std::get<Vector>(row[2]));
    });
  }
};

Database::Database(const std::filesystem::path& path, PageStore::FaultHook hook)
    : impl_(std::make_unique<Impl>(path, std::move(hook))) {}
Database::~Database() = default;
void Database::begin() { impl_->pages.begin(); }
void Database::commit() { impl_->pages.commit(); }
void Database::rollback() { impl_->pages.rollback(); }
Schema Database::schema(const std::string& name) { return impl_->table(name).schema; }
std::uint64_t Database::count(const std::string& name) { return impl_->table(name).rows; }
std::optional<Row> Database::get(const std::string& name, std::int64_t key) {
  auto& s = *impl_; const auto table = s.table(name); const auto record = s.lookup(table, key);
  if (record == 0) return {};
  return decode_row(table.schema, s.blob(record));
}

void validate_schema(const Schema& schema) {
  if (schema.name.empty() || schema.columns.empty() || schema.primary_key >= schema.columns.size() ||
      schema.columns[schema.primary_key].type != Type::bigint) invalid("A BIGINT primary key is required");
  unicode_length(schema.name); std::unordered_set<std::string> names; unsigned vectors = 0;
  for (const auto& column : schema.columns) {
    if (column.name.empty() || !names.insert(column.name).second) invalid("Duplicate or empty column name");
    unicode_length(column.name);
    unicode_length(column.display_name);
    if (column.type != Type::bigint && column.type != Type::varchar && column.type != Type::vector)
      invalid("Unsupported column type");
    if (column.type != Type::bigint && column.extent == 0) invalid("Column extent must be positive");
    if (column.type == Type::vector && ++vectors > 1) invalid("At most one vector column is supported");
  }
  if (schema.columns.size() > std::numeric_limits<std::uint32_t>::max()) invalid("Too many columns");
}

void Database::create_table(const Schema& schema) {
  validate_schema(schema);
  auto& s = *impl_;
  s.statement([&] {
    if (s.exists(schema.name)) invalid("Duplicate table");
    auto table = s.make_table(schema); auto root = s.pages.read(0);
    table.next = get64(root.data() + 8); s.write_table(table);
    put64(root.data() + 8, table.id); s.pages.write(0, root);
  });
}

void Database::create_vector_index(const std::string& name, const std::string& index_name,
                                   std::size_t column) {
  if (index_name.empty()) invalid("Vector index name must not be empty");
  unicode_length(index_name);
  auto& s = *impl_;
  s.statement([&] {
    auto table = s.table(name);
    if (column >= table.schema.columns.size() || table.schema.columns[column].type != Type::vector) {
      invalid("Vector index requires a VECTOR column");
    }
    if (table.vector_index != 0) invalid("Vector column already has an index");
    const Schema schema{index_name, {{"key", Type::bigint}, {"record", Type::bigint},
                                   {"vector", Type::vector, table.schema.columns[column].extent}}, 0};
    auto index = s.make_table(schema);
    s.each_entry(table, [&](std::int64_t key, std::uint64_t record) {
      const auto row = decode_row(table.schema, s.blob(record));
      s.put_vector(index, key, record, std::get<Vector>(row[column]));
      return true;
    });
    s.write_table(index); table.vector_index = index.id; s.write_table(table);
  });
}

void Database::insert(const std::string& name, std::span<const Row> rows) {
  auto& s = *impl_;
  s.statement([&] {
    auto table = s.table(name); std::unordered_set<std::int64_t> keys;
    for (const auto& row : rows) {
      check_row(table.schema, row); const auto key = std::get<std::int64_t>(row[table.schema.primary_key]);
      if (!keys.insert(key).second || s.lookup(table, key) != 0) invalid("Duplicate primary key");
    }
    if (rows.size() > std::numeric_limits<std::uint64_t>::max() - table.rows) invalid("Row count overflow");
    s.grow(table, table.rows + rows.size());
    auto index = s.vector_index(table);
    for (const auto& row : rows) {
      const auto record = s.store_blob(encode_row(row));
      const auto key = std::get<std::int64_t>(row[table.schema.primary_key]);
      s.index_put(table, key, record); ++table.rows;
      if (index) s.put_vector(*index, key, record, std::get<Vector>(row[vector_column(table.schema)]));
    }
    if (index) s.write_table(*index);
    s.write_table(table);
  });
}

std::uint64_t Database::update(const std::string& name, const Predicate& predicate,
                               std::span<const Assignment> assignments) {
  auto& s = *impl_; std::uint64_t changed = 0;
  s.statement([&] {
    const auto table = s.table(name); std::unordered_set<std::size_t> fields;
    for (const auto& assignment : assignments) {
      if (assignment.column >= table.schema.columns.size() || assignment.column == table.schema.primary_key ||
          !fields.insert(assignment.column).second) invalid("Invalid assignment or primary-key update");
      check_value(table.schema.columns[assignment.column], assignment.value);
    }
    auto index = s.vector_index(table);
    s.each_entry(table, [&](std::int64_t key, std::uint64_t record) {
      auto row = decode_row(table.schema, s.blob(record));
      if (predicate && !predicate(row)) return true;
      for (const auto& assignment : assignments) row[assignment.column] = assignment.value;
      const auto replacement = s.store_blob(encode_row(row));
      if (index) s.put_vector(*index, key, replacement, std::get<Vector>(row[vector_column(table.schema)]));
      s.index_put(table, key, replacement); s.release_blob(record); ++changed; return true;
    });
    if (index) s.write_table(*index);
  });
  return changed;
}

std::uint64_t Database::erase(const std::string& name, const Predicate& predicate) {
  auto& s = *impl_; std::uint64_t changed = 0;
  s.statement([&] {
    auto table = s.table(name);
    auto index = s.vector_index(table);
    s.each_entry(table, [&](std::int64_t key, std::uint64_t record) {
      const auto row = decode_row(table.schema, s.blob(record));
      if (predicate && !predicate(row)) return true;
      if (index) s.erase_vector(*index, key);
      s.index_put(table, key, 0); s.release_blob(record); ++changed; return true;
    });
    if (index) s.write_table(*index);
    table.rows -= changed; s.write_table(table);
  });
  return changed;
}

void Database::scan(const std::string& name, const Predicate& predicate, const Visitor& visitor) {
  auto& s = *impl_; const auto table = s.table(name);
  s.each_entry(table, [&](std::int64_t, std::uint64_t record) {
    const auto row = decode_row(table.schema, s.blob(record));
    return predicate && !predicate(row) ? true : visitor(row);
  });
}

void Database::scan_ordered(const std::string& name, const Predicate& predicate, std::size_t column,
                            bool descending, std::optional<std::uint64_t> limit, const Visitor& visitor,
                            std::size_t chunk_budget) {
  auto& s = *impl_; const auto table = s.table(name);
  if (column >= table.schema.columns.size() || table.schema.columns[column].type == Type::vector)
    invalid("ORDER BY requires an ordinary column");
  if (chunk_budget == 0) invalid("Sort chunk budget must be positive");
  if (limit && *limit == 0) return;
  const auto before = [&](const Row& a, const Row& b) {
    const auto order = compare_values(a[column], b[column]);
    if (order != 0) return descending ? order > 0 : order < 0;
    return std::get<std::int64_t>(a[table.schema.primary_key]) < std::get<std::int64_t>(b[table.schema.primary_key]);
  };
  external_sort(s.directory, table.schema, [&](const Visitor& emit) { scan(name, predicate, emit); },
                before, limit, visitor, chunk_budget);
}

std::vector<Neighbor> Database::topk(const std::string& name, const Vector& query,
                                    std::size_t k, const Predicate& predicate) {
  std::vector<Neighbor> result;
  scan_topk(name, query, k, predicate, [&](const Neighbor& value) { result.push_back(value); return true; });
  return result;
}

void Database::scan_topk(const std::string& name, const Vector& query, std::uint64_t k,
                          const Predicate& predicate, const NeighborVisitor& visitor, std::size_t budget,
                          SearchMode mode) {
  auto& s = *impl_; const auto table = s.table(name); const auto column = vector_column(table.schema);
  check_value(table.schema.columns[column], query);
  if (budget == 0) invalid("TopK working budget must be positive");
  if (k == 0) return;
  struct Candidate {
    double squared; std::int64_t key; std::uint64_t record;
    bool operator<(const Candidate& other) const {
      return squared != other.squared ? squared < other.squared : key < other.key;
    }
  };
  if (k > budget / (4 * sizeof(Candidate))) {
    const auto before = [&](const Row& a, const Row& b) {
      const auto da = squared_distance(std::get<Vector>(a[column]), query);
      const auto db = squared_distance(std::get<Vector>(b[column]), query);
      if (da != db) return da < db;
      return std::get<std::int64_t>(a[table.schema.primary_key]) < std::get<std::int64_t>(b[table.schema.primary_key]);
    };
    external_sort(s.directory, table.schema, [&](const Visitor& emit) {
      s.each_vector(table, mode, predicate, [&](std::int64_t, std::uint64_t record, const Vector&) {
        return emit(decode_row(table.schema, s.blob(record)));
      });
    }, before, k,
      [&](const Row& row) {
        return visitor({std::get<std::int64_t>(row[table.schema.primary_key]),
                        l2_distance(std::get<Vector>(row[column]), query), row});
      }, budget);
    return;
  }
  std::priority_queue<Candidate> best;
  s.each_vector(table, mode, predicate, [&](std::int64_t key, std::uint64_t record, const Vector& vector) {
    const double distance = squared_distance(vector, query);
    Candidate candidate{distance, key, record};
    if (best.size() < k) best.push(candidate);
    else if (candidate < best.top()) { best.pop(); best.push(candidate); }
    return true;
  });
  std::vector<Candidate> ordered; ordered.reserve(best.size());
  while (!best.empty()) { ordered.push_back(best.top()); best.pop(); }
  std::reverse(ordered.begin(), ordered.end());
  for (const auto& candidate : ordered) {
    Neighbor neighbor{candidate.key, std::sqrt(candidate.squared), decode_row(table.schema, s.blob(candidate.record))};
    if (!visitor(neighbor)) break;
  }
}

void Database::scan_distance_ordered(const std::string& name, const Vector& query, bool descending,
                                     std::optional<std::uint64_t> limit, const Predicate& predicate,
                                     const NeighborVisitor& visitor, std::size_t budget, SearchMode mode) {
  if (!descending) {
    scan_topk(name, query, limit.value_or(std::numeric_limits<std::uint64_t>::max()),
              predicate, visitor, budget, mode);
    return;
  }
  auto& s = *impl_;
  const auto table = s.table(name);
  const auto column = vector_column(table.schema);
  check_value(table.schema.columns[column], query);
  if (budget == 0) invalid("Distance sort working budget must be positive");
  if (limit && *limit == 0) return;
  const auto before = [&](const Row& a, const Row& b) {
    const auto da = squared_distance(std::get<Vector>(a[column]), query);
    const auto db = squared_distance(std::get<Vector>(b[column]), query);
    if (da != db) return da > db;
    return std::get<std::int64_t>(a[table.schema.primary_key]) < std::get<std::int64_t>(b[table.schema.primary_key]);
  };
  external_sort(s.directory, table.schema, [&](const Visitor& emit) {
    s.each_vector(table, mode, predicate, [&](std::int64_t, std::uint64_t record, const Vector&) {
      return emit(decode_row(table.schema, s.blob(record)));
    });
  },
    before, limit, [&](const Row& row) {
      return visitor({std::get<std::int64_t>(row[table.schema.primary_key]),
                      l2_distance(std::get<Vector>(row[column]), query), row});
    }, budget);
}

}  // namespace minivectordb
