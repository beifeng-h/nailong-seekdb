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

#include <cstdint>

namespace oceanbase
{
namespace share
{

class AiColumnContext
{
public:
  static bool is_worker_sql() { return worker_sql_; }
  static bool is_retry_sql() { return retry_sql_; }
  static bool is_retry_column(uint64_t table_id, uint64_t column_id)
  {
    return retry_sql_ && retry_table_id_ == table_id && retry_column_id_ == column_id;
  }

  // This process-local scope is entered only by the background task service.
  // It is not a session variable and cannot be enabled by user SQL.
  class WorkerScope
  {
  public:
    WorkerScope() : previous_(worker_sql_) { worker_sql_ = true; }
    ~WorkerScope() { worker_sql_ = previous_; }
    WorkerScope(const WorkerScope &) = delete;
    WorkerScope &operator=(const WorkerScope &) = delete;
  private:
    bool previous_;
  };

  // RETRY is ordinary transactional DML with one explicitly selected AI column.
  // It must enqueue new work, unlike worker publication.
  class RetryScope
  {
  public:
    RetryScope(uint64_t table_id, uint64_t column_id)
        : previous_(retry_sql_), previous_table_(retry_table_id_), previous_column_(retry_column_id_)
    {
      retry_sql_ = true;
      retry_table_id_ = table_id;
      retry_column_id_ = column_id;
    }
    ~RetryScope()
    {
      retry_sql_ = previous_;
      retry_table_id_ = previous_table_;
      retry_column_id_ = previous_column_;
    }
    RetryScope(const RetryScope &) = delete;
    RetryScope &operator=(const RetryScope &) = delete;
  private:
    bool previous_;
    uint64_t previous_table_;
    uint64_t previous_column_;
  };

private:
  inline static thread_local bool worker_sql_ = false;
  inline static thread_local bool retry_sql_ = false;
  inline static thread_local uint64_t retry_table_id_ = 0;
  inline static thread_local uint64_t retry_column_id_ = 0;
};

} // namespace share
} // namespace oceanbase
