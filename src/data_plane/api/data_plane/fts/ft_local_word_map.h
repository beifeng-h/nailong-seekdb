/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include "lib/container/ob_se_array.h"
#include "data_plane/fts/ob_fts_struct.h"

namespace oceanbase
{
namespace storage
{

// A single-owner word table. Bucket and chain order match ObFTWordMap so that
// callers which serialize tokens see the same order as before.
class FTLocalWordMap final
{
public:
  struct Entry
  {
    Entry() : first(), second(0), next_(-1) {}
    ObFTWord first;
    int64_t second;
    int64_t next_;
  };

private:
  struct Bucket
  {
    Bucket() : generation_(0), head_(-1) {}
    uint64_t generation_;
    int64_t head_;
  };

public:
  class const_iterator
  {
  public:
    const_iterator(const FTLocalWordMap *map, bool end)
        : map_(map), bucket_(0), entry_(-1)
    {
      if (!end) {
        next_bucket();
      }
    }
    const Entry *operator->() const { return &map_->entries_.at(entry_); }
    bool operator!=(const const_iterator &other) const { return entry_ != other.entry_; }
    const_iterator &operator++()
    {
      entry_ = map_->entries_.at(entry_).next_;
      if (entry_ < 0) {
        ++bucket_;
        next_bucket();
      }
      return *this;
    }

  private:
    void next_bucket()
    {
      while (bucket_ < map_->bucket_count_) {
        const Bucket &bucket = map_->buckets_.at(bucket_);
        if (bucket.generation_ == map_->generation_) {
          entry_ = bucket.head_;
          break;
        }
        ++bucket_;
      }
    }
    const FTLocalWordMap *map_;
    int64_t bucket_;
    int64_t entry_;
  };

  FTLocalWordMap() : buckets_(), entries_(), bucket_count_(0), size_(0), generation_(0) {}
  ~FTLocalWordMap() = default;

  int prepare(const int64_t requested_buckets)
  {
    int ret = OB_SUCCESS;
    const int64_t count = requested_buckets > 0
        ? common::hash::cal_next_prime(requested_buckets) : 0;
    if (OB_UNLIKELY(count <= 0)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      while (OB_SUCC(ret) && buckets_.count() < count) {
        if (OB_FAIL(buckets_.push_back(Bucket()))) {
        }
      }
      if (OB_SUCC(ret)) {
        bucket_count_ = count;
        reuse();
      }
    }
    return ret;
  }

  void reuse()
  {
    size_ = 0;
    if (OB_UNLIKELY(++generation_ == 0)) {
      for (int64_t i = 0; i < buckets_.count(); ++i) {
        buckets_.at(i).generation_ = 0;
      }
      generation_ = 1;
    }
  }

  void reset()
  {
    buckets_.reset();
    entries_.reset();
    bucket_count_ = 0;
    size_ = 0;
    generation_ = 0;
  }

  int add(const ObFTWord &word, const int64_t frequency, const bool group,
          const common::ObObjMeta &meta, common::ObIAllocator *copy_allocator = nullptr)
  {
    int ret = OB_SUCCESS;
    uint64_t hash = 0;
    if (OB_UNLIKELY(word.empty() || frequency <= 0)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_UNLIKELY(bucket_count_ <= 0)) {
      ret = OB_NOT_INIT;
    } else if (OB_FAIL(word.hash(hash))) {
    } else {
      Bucket &bucket = buckets_.at(hash % bucket_count_);
      int64_t index = bucket.generation_ == generation_ ? bucket.head_ : -1;
      while (index >= 0 && !(entries_.at(index).first == word)) {
        index = entries_.at(index).next_;
      }
      if (index >= 0) {
        if (!group) {
          ret = OB_HASH_EXIST;
        } else {
          Entry &entry = entries_.at(index);
          ObFTWord retained;
          const common::ObString old_bytes = entry.first.get_word().get_string();
          const common::ObString new_bytes = word.get_word().get_string();
          if (nullptr != copy_allocator && old_bytes == new_bytes) {
            retained = entry.first;
          } else {
            ret = retain_word(word, meta, copy_allocator, retained);
          }
          if (OB_FAIL(ret)) {
          } else {
            entry.first = retained;
            entry.second += frequency;
          }
        }
      } else {
        if (size_ == entries_.count() && OB_FAIL(entries_.push_back(Entry()))) {
        } else {
          ObFTWord retained;
          ret = retain_word(word, meta, copy_allocator, retained);
          if (OB_FAIL(ret)) {
          } else {
            Entry &entry = entries_.at(size_);
            entry.first = retained;
            entry.second = 1;
            entry.next_ = bucket.generation_ == generation_ ? bucket.head_ : -1;
            bucket.head_ = size_++;
            bucket.generation_ = generation_;
          }
        }
      }
    }
    return ret;
  }

  int64_t size() const { return size_; }
  const_iterator begin() const { return const_iterator(this, false); }
  const_iterator end() const { return const_iterator(this, true); }

private:
  static int retain_word(const ObFTWord &word, const common::ObObjMeta &meta,
                         common::ObIAllocator *allocator, ObFTWord &retained)
  {
    int ret = OB_SUCCESS;
    retained = word;
    if (nullptr != allocator) {
      const common::ObString bytes = word.get_word().get_string();
      char *copy = static_cast<char *>(allocator->alloc(bytes.length()));
      if (OB_ISNULL(copy)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        MEMCPY(copy, bytes.ptr(), bytes.length());
        retained = ObFTWord(bytes.length(), copy, meta);
      }
    }
    return ret;
  }

  common::ObSEArray<Bucket, 32> buckets_;
  common::ObSEArray<Entry, 32> entries_;
  int64_t bucket_count_;
  int64_t size_;
  uint64_t generation_;
  DISALLOW_COPY_AND_ASSIGN(FTLocalWordMap);
};

} // namespace storage
} // namespace oceanbase
