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

#ifndef _OCEANBASE_STORAGE_FTS_UTILS_CHAR_UTILS_H_
#define _OCEANBASE_STORAGE_FTS_UTILS_CHAR_UTILS_H_

#include "lib/charset/ob_ctype.h"
#include "lib/utility/alloc_assist.h"
#include "lib/utility/ob_macro_utils.h"

#define true_word_char(ctype, character) ((ctype) & (_MY_U | _MY_L | _MY_NMR) || (character) == '_')

namespace oceanbase
{
namespace storage
{

// Building a full text index lowercases, hashes and compares every token it
// produces.  The utf8mb4 general_ci collation implements those operations
// with Unicode tables (decode, weight lookup, re-encode), and that work
// dominates the cost of tokenizing a document.
//
// A pure ASCII token does not need any of it: the utf8mb4 general_ci weight
// of an ASCII character is its lower case form, so the Unicode rules collapse
// into plain byte rules.  The helpers below implement exactly those byte
// rules.
//
// They are only used when the whole token is ASCII, so the general_ci
// semantics are preserved.  In particular the accent folding that maps
// e.g. U+00E9 onto 'e' still applies, because a token containing a non ASCII
// byte never takes the fast path.

// Longest entry of the stop word list, in characters.
static constexpr int64_t FTS_MAX_STOP_WORD_LENGTH = 5;

// True when every byte of [str, str + len) is a 7 bit ASCII character.
OB_INLINE bool fts_is_ascii_only(const char *str, const int64_t len)
{
  int64_t i = 0;
  const uint64_t high_bits = 0x8080808080808080ULL;
  for (; i + 8 <= len; i += 8) {
    uint64_t chunk = 0;
    MEMCPY(&chunk, str + i, sizeof(chunk));
    if (0 != (chunk & high_bits)) {
      return false;
    }
  }
  for (; i < len; ++i) {
    if (0 != (static_cast<unsigned char>(str[i]) & 0x80)) {
      return false;
    }
  }
  return true;
}

// Lower case form of an ASCII character, matching the utf8mb4 general_ci weight.
OB_INLINE char fts_ascii_tolower(const char c)
{
  return (c >= 'A' && c <= 'Z') ? static_cast<char>(c + ('a' - 'A')) : c;
}

// Compare two ASCII strings the way utf8mb4 general_ci compares them.
OB_INLINE bool fts_ascii_general_ci_equal(const char *lhs, const int64_t lhs_len,
                                          const char *rhs, const int64_t rhs_len)
{
  if (lhs_len != rhs_len) {
    return false;
  }
  for (int64_t i = 0; i < lhs_len; ++i) {
    if (fts_ascii_tolower(lhs[i]) != fts_ascii_tolower(rhs[i])) {
      return false;
    }
  }
  return true;
}

// Character type of an ASCII byte, matching the utf8mb4 character type table.
OB_INLINE int fts_ascii_ctype(const unsigned char c)
{
  if (c >= 'A' && c <= 'Z') {
    return _MY_U;
  } else if (c >= 'a' && c <= 'z') {
    return _MY_L;
  } else if (c >= '0' && c <= '9') {
    return _MY_NMR;
  }
  return 0;
}

} // end namespace storage
} // end namespace oceanbase

#endif // _OCEANBASE_STORAGE_FTS_UTILS_CHAR_UTILS_H_
