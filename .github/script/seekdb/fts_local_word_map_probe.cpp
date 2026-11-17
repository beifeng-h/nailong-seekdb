// Exercise the production container with substituted allocation and word types.
// Real collation and parser behavior are checked by the separate SQL replay.
#include <algorithm>
#include <cassert>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <list>
#include <random>
#include <string>
#include <vector>

#define OB_SUCCESS 0
#define OB_INVALID_ARGUMENT -1
#define OB_NOT_INIT -2
#define OB_HASH_EXIST -3
#define OB_ALLOCATE_MEMORY_FAILED -4
#define OB_UNLIKELY(x) (x)
#define OB_ISNULL(x) (nullptr == (x))
#define OB_SUCC(x) (OB_SUCCESS == (x))
#define OB_FAIL(x) (OB_SUCCESS != (ret = (x)))
#define MEMCPY std::memcpy
#define DISALLOW_COPY_AND_ASSIGN(T) T(const T &) = delete; T &operator=(const T &) = delete

int array_budget = -1;
namespace oceanbase {
namespace common {
struct ObObjMeta { bool fold = false; };
class ObString {
public:
  ObString() : length_(0), data_(nullptr) {}
  ObString(int64_t n, const char *s) : length_(n), data_(s) {}
  int64_t length() const { return length_; }
  const char *ptr() const { return data_; }
  bool operator==(const ObString &other) const
  {
    return length_ == other.length_ && 0 == std::memcmp(data_, other.data_, length_);
  }
private:
  int64_t length_;
  const char *data_;
};
class ObIAllocator {
public:
  bool fail = false;
  int allocations = 0;
  std::vector<void *> blocks;
  ~ObIAllocator() { for (void *p : blocks) std::free(p); }
  void *alloc(int64_t n)
  {
    if (fail) return nullptr;
    void *p = std::malloc(n);
    blocks.push_back(p);
    ++allocations;
    return p;
  }
};
template <typename T, int64_t N> class ObSEArray {
public:
  int64_t count() const { return data.size(); }
  T &at(int64_t i) { return data.at(i); }
  const T &at(int64_t i) const { return data.at(i); }
  int push_back(const T &item)
  {
    if (array_budget == 0) return OB_ALLOCATE_MEMORY_FAILED;
    if (array_budget > 0) --array_budget;
    data.push_back(item);
    return OB_SUCCESS;
  }
  void reset() { data.clear(); }
  std::vector<T> data;
};
namespace hash {
int64_t cal_next_prime(int64_t n)
{
  while (n > 1) {
    bool prime = true;
    for (int64_t i = 2; i * i <= n; ++i) if (n % i == 0) prime = false;
    if (prime) break;
    ++n;
  }
  return n;
}
}
}
namespace storage {
class ObFTWord {
public:
  struct Datum {
    common::ObString value;
    common::ObString get_string() const { return value; }
  };
  ObFTWord() = default;
  ObFTWord(int64_t n, const char *s, const common::ObObjMeta &meta)
      : datum{common::ObString(n, s)}, fold(meta.fold) {}
  const Datum &get_word() const { return datum; }
  bool empty() const { return datum.value.length() == 0; }
  std::string key() const
  {
    std::string s(datum.value.ptr(), datum.value.length());
    while (!s.empty() && s.back() == ' ') s.pop_back();
    if (fold) for (char &c : s) c = std::tolower(static_cast<unsigned char>(c));
    return s;
  }
  int hash(uint64_t &out) const
  {
    out = 0;
    for (unsigned char c : key()) out = out * 131 + c;
    return OB_SUCCESS;
  }
  bool operator==(const ObFTWord &other) const { return key() == other.key(); }
  Datum datum;
  bool fold = false;
};
}
}
#include "map-under-test.h"

using namespace oceanbase;
using namespace oceanbase::storage;
std::vector<std::pair<std::string, int64_t>> snapshot(const FTLocalWordMap &map)
{
  std::vector<std::pair<std::string, int64_t>> out;
  for (auto it = map.begin(); it != map.end(); ++it) {
    auto s = it->first.get_word().get_string();
    out.emplace_back(std::string(s.ptr(), s.length()), it->second);
  }
  assert(out.size() == static_cast<size_t>(map.size()));
  return out;
}
int main()
{
  FTLocalWordMap map;
  common::ObObjMeta meta;
  std::mt19937 rng(12345);
  uint64_t checked = 0;
  for (int document = 0; document < 400; ++document) {
    meta.fold = document % 2;
    int64_t requested = document % 3 == 0 ? 2 : document % 3 == 1 ? 37 : 997;
    assert(map.prepare(requested) == OB_SUCCESS);
    int64_t buckets = common::hash::cal_next_prime(requested);
    std::vector<std::list<std::pair<std::string, int64_t>>> expected(buckets);
    std::vector<std::string> words;
    words.reserve(1000);
    for (int i = 0; i < 1000; ++i) {
      words.push_back("term" + std::to_string(rng() % 300));
      if (rng() % 2) words.back()[0] = 'T';
      if (rng() % 4 == 0) words.back() += " ";
      const std::string &bytes = words.back();
      ObFTWord word(bytes.size(), bytes.data(), meta);
      uint64_t hash = 0;
      word.hash(hash);
      auto &chain = expected[hash % buckets];
      auto found = std::find_if(chain.begin(), chain.end(), [&](const auto &e) {
        return ObFTWord(e.first.size(), e.first.data(), meta) == word;
      });
      int64_t frequency = 1 + rng() % 7;
      bool group = rng() % 4 != 0;
      int wanted = OB_SUCCESS;
      if (found == chain.end()) chain.emplace_front(bytes, 1);
      else if (!group) wanted = OB_HASH_EXIST;
      else { found->first = bytes; found->second += frequency; }
      assert(map.add(word, frequency, group, meta) == wanted);
      if (i % 23 == 0 || i == 999) {
        std::vector<std::pair<std::string, int64_t>> flat;
        for (const auto &bucket : expected) flat.insert(flat.end(), bucket.begin(), bucket.end());
        assert(snapshot(map) == flat);
        ++checked;
      }
    }
    map.reuse();
    assert(snapshot(map).empty());
  }
  common::ObIAllocator allocator;
  meta.fold = true;
  assert(map.prepare(2) == OB_SUCCESS);
  char buffer[] = "Alpha";
  assert(map.add(ObFTWord(5, buffer, meta), 7, true, meta, &allocator) == OB_SUCCESS);
  buffer[0] = 'x';
  assert(snapshot(map)[0] == std::make_pair(std::string("Alpha"), int64_t(1)));
  assert(map.add(ObFTWord(5, "ALPHA", meta), 3, true, meta, &allocator) == OB_SUCCESS);
  int prior_allocations = allocator.allocations;
  assert(map.add(ObFTWord(5, "ALPHA", meta), 2, true, meta, &allocator) == OB_SUCCESS);
  assert(allocator.allocations == prior_allocations);
  auto before = snapshot(map);
  allocator.fail = true;
  assert(map.add(ObFTWord(5, "alpha", meta), 2, true, meta, &allocator) == OB_ALLOCATE_MEMORY_FAILED);
  assert(snapshot(map) == before);
  assert(map.add(ObFTWord(4, "beta", meta), 1, true, meta, &allocator) == OB_ALLOCATE_MEMORY_FAILED);
  assert(snapshot(map) == before);
  map.generation_ = UINT64_MAX;
  map.reuse();
  assert(snapshot(map).empty());
  map.reset();
  assert(map.add(ObFTWord(4, "word", meta), 1, true, meta) == OB_NOT_INIT);
  array_budget = 3;
  assert(map.prepare(37) == OB_ALLOCATE_MEMORY_FAILED);
  array_budget = -1;
  assert(map.prepare(37) == OB_SUCCESS);
  array_budget = 0;
  assert(map.add(ObFTWord(4, "word", meta), 1, true, meta) == OB_ALLOCATE_MEMORY_FAILED);
  assert(snapshot(map).empty());
  array_budget = -1;
  assert(map.add(ObFTWord(4, "word", meta), 1, true, meta) == OB_SUCCESS);
  assert(map.prepare(0) == OB_INVALID_ARGUMENT);
  std::cout << "container_checks " << checked << " operations 400000\n";
}
