/**
 * Copyright (c) 2026 OceanBase.
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

/**
 * Deterministic type inference for the file table engine.
 *
 * The inference never looks at a sample: the whole file is scanned and every
 * value contributes one candidate type.  The candidates are merged with a
 * fixed widening lattice so that the result only depends on the file content
 * and not on the order in which the values are seen.
 */

#ifndef OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_TYPE_INFER_H_
#define OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_TYPE_INFER_H_

#include "sql/engine/file_table/ob_file_table_def.h"

namespace oceanbase
{
namespace sql
{
namespace file_table
{

/**
 * Merge two candidate types into the type that holds both of them.
 *
 * NULL widens into every other type, BOOLEAN widens into BIGINT, BIGINT
 * widens into DOUBLE, DATE widens into DATETIME and every type widens into
 * VARCHAR.
 */
InferredType merge_types(InferredType left, InferredType right);

/**
 * Detect the candidate type of a text value.
 *
 * The text is classified as NULL, BOOLEAN, BIGINT, DOUBLE, DATE, DATETIME or
 * VARCHAR.  An integer that does not fit into a signed 64 bit integer widens
 * into DOUBLE, everything else that is not recognised stays VARCHAR.
 */
InferredType detect_text_type(const std::string &text);

/** Whether the text is a valid calendar date in the "YYYY-MM-DD" form. */
bool is_valid_date(const std::string &text);

/** Whether the text is a valid timestamp in the "YYYY-MM-DD HH:MM:SS" form. */
bool is_valid_datetime(const std::string &text);

/**
 * Accumulates the candidate types of one column and reports the inferred
 * type.  The accumulator is order independent: merging is commutative.
 */
class TypeInferrer
{
public:
  TypeInferrer() : type_(InferredType::NULL_TYPE), seen_(false) {}

  /** Feed one raw text cell. */
  void observe_text(const std::string &text, const ReadOptions &options);
  /** Feed one already typed cell. */
  void observe_type(InferredType type);
  /** The inferred type, NULL_TYPE when no value was seen. */
  InferredType result() const { return seen_ ? type_ : InferredType::NULL_TYPE; }
  /** Whether any value was observed at all. */
  bool empty() const { return !seen_; }
  /** Whether a NULL value was observed. */
  bool has_null() const { return has_null_; }

private:
  InferredType type_;
  bool seen_;
  bool has_null_ = false;
};

} // namespace file_table
} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_ENGINE_FILE_TABLE_OB_FILE_TABLE_TYPE_INFER_H_