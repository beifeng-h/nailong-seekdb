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

#include "data_plane/fts/ob_fts_struct.h"

#include "lib/charset/ob_charset.h"
#include "share/datum/ob_datum_funcs.h"
#include "storage/ob_storage_util.h"
#include "storage/fts/utils/ob_ft_char_utils.h"

namespace oceanbase
{
namespace storage
{

int ObFTWord::hash(uint64_t &hash_val) const
{
  int ret = OB_SUCCESS;
  common::ObDatumBasicFuncs *funcs = ObDatumFuncs::get_basic_func(meta_.get_type(), meta_.get_collation_type());
  if (OB_ISNULL(funcs)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (funcs->default_hash_ == nullptr) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    ret = funcs->default_hash_(word_, 0, hash_val, nullptr);
  }
  return ret;
}
bool ObFTWord::operator==(const ObFTWord &other) const
{
  bool is_equal = false;
  int ret = OB_SUCCESS;
  int cmp_ret = 0;
  const ObCollationType lhs_collation = meta_.get_collation_type();
  const ObCollationType rhs_collation = other.meta_.get_collation_type();
  // Fast path for pure ASCII tokens.  The utf8mb4 general_ci comparison of two
  // ASCII strings is the case insensitive byte comparison and the utf8mb4_bin
  // comparison is the plain byte comparison; both are exactly what the
  // collation functions compute, only without the Unicode table lookups.
  // A token holding a non ASCII byte keeps using the collation functions, so
  // the accent folding of general_ci (e.g. U+00E9 equals 'e') is preserved.
  if (OB_LIKELY(lhs_collation == rhs_collation)
      && (CS_TYPE_UTF8MB4_GENERAL_CI == lhs_collation || CS_TYPE_UTF8MB4_BIN == lhs_collation)) {
    const ObString &lhs = word_.get_string();
    const ObString &rhs = other.word_.get_string();
    if (OB_LIKELY(fts_is_ascii_only(lhs.ptr(), lhs.length())
                  && fts_is_ascii_only(rhs.ptr(), rhs.length()))) {
      if (CS_TYPE_UTF8MB4_GENERAL_CI == lhs_collation) {
        is_equal = fts_ascii_general_ci_equal(lhs.ptr(), lhs.length(), rhs.ptr(), rhs.length());
      } else {
        is_equal = (lhs.length() == rhs.length())
                   && (0 == MEMCMP(lhs.ptr(), rhs.ptr(), lhs.length()));
      }
      return is_equal;
    }
  }
  ObDatumCmpFuncType func = get_datum_cmp_func(meta_, other.meta_);
  if (func == nullptr) {
    ob_abort();
  } else if (OB_FAIL(func(word_, other.word_, cmp_ret, nullptr))) {
    ob_abort();
  } else {
    is_equal = (cmp_ret == 0);
  }
  return is_equal;
}
} // namespace storage
} // namespace oceanbase
