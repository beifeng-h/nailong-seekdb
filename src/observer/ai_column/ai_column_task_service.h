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

#include "share/ob_thread_pool.h"
#include "lib/container/ob_array.h"
#include "lib/string/ob_sql_string.h"
#include "lib/allocator/ob_allocator.h"

namespace oceanbase
{
namespace observer
{

// The outbox is written in the business transaction. Workers see only committed
// groups, perform external work without locks, then publish under a row lock.
class AiColumnTaskService : public share::ObThreadPool
{
public:
  AiColumnTaskService();
  ~AiColumnTaskService();
  int init();
  int start() override;
  void stop() override;
  void wait() override;
  void destroy();

protected:
  void run1() override;

private:
  struct Task;
  int process_one_group(const common::ObString &owner, bool &found);
  int claim_group(common::ObIAllocator &allocator, const common::ObString &owner,
                  common::ObArray<Task> &tasks, bool &found);
  int execute_task(common::ObArenaAllocator &allocator, Task &task);
  int save_task_result(const Task &task, const common::ObString &owner, int execution_ret);
  int publish_group(common::ObIAllocator &allocator, const common::ObArray<Task> &tasks,
                    const common::ObString &owner);
  bool is_inited_;
  DISALLOW_COPY_AND_ASSIGN(AiColumnTaskService);
};

} // namespace observer
} // namespace oceanbase
