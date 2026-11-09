#pragma once

#include "minivectordb/page_store.hpp"

#include <functional>
#include <optional>
#include <span>
#include <string>
#include <variant>
#include <vector>

namespace minivectordb {

enum class Type : std::uint32_t { bigint = 1, varchar = 2, vector = 3 };
struct Column {
  std::string name;
  Type type;
  std::uint32_t extent = 0;
  std::string display_name = {};
};
struct Schema { std::string name; std::vector<Column> columns; std::uint32_t primary_key = 0; };
using Vector = std::vector<float>;
using Value = std::variant<std::int64_t, std::string, Vector>;
using Row = std::vector<Value>;
using Predicate = std::function<bool(const Row&)>;
using Visitor = std::function<bool(const Row&)>;
struct Assignment { std::size_t column; Value value; };
struct Neighbor { std::int64_t primary_key; double distance; Row row; };
using NeighborVisitor = std::function<bool(const Neighbor&)>;
enum class Relation { equal, less, less_equal, greater, greater_equal };
struct Filter { std::size_t column; Relation relation; Value value; };

Vector make_vector(std::span<const double> input);
void validate_schema(const Schema& schema);
void validate_value(const Column& column, const Value& value);
double l2_distance(const Vector& left, const Vector& right);
std::size_t unicode_length(std::string_view text);
int compare_strings(std::string_view left, std::string_view right);
int compare_values(const Value& left, const Value& right);
bool matches(const Row& row, std::span<const Filter> filters);

// Typed query core. The contest SQL/framing adapter is a separate integration.
class Database {
 public:
  explicit Database(const std::filesystem::path& directory, PageStore::FaultHook hook = {});
  ~Database();
  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;

  void begin();
  void commit();
  void rollback();
  void create_table(const Schema& schema);
  Schema schema(const std::string& table);
  std::uint64_t count(const std::string& table);
  std::optional<Row> get(const std::string& table, std::int64_t primary_key);
  void insert(const std::string& table, std::span<const Row> rows);
  std::uint64_t update(const std::string& table, const Predicate& predicate,
                       std::span<const Assignment> assignments);
  std::uint64_t erase(const std::string& table, const Predicate& predicate);
  void scan(const std::string& table, const Predicate& predicate, const Visitor& visitor);
  void scan_ordered(const std::string& table, const Predicate& predicate, std::size_t order_column,
                    bool descending, std::optional<std::uint64_t> limit, const Visitor& visitor,
                    std::size_t chunk_budget_bytes = 1024 * 1024);
  std::vector<Neighbor> topk(const std::string& table, const Vector& query,
                             std::size_t k, const Predicate& predicate = {});
  void scan_topk(const std::string& table, const Vector& query, std::uint64_t k,
                 const Predicate& predicate, const NeighborVisitor& visitor,
                 std::size_t working_budget_bytes = 1024 * 1024);
  void scan_distance_ordered(const std::string& table, const Vector& query, bool descending,
                             std::optional<std::uint64_t> limit, const Predicate& predicate,
                             const NeighborVisitor& visitor,
                             std::size_t working_budget_bytes = 1024 * 1024);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace minivectordb
