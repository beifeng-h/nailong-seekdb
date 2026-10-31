# AI 函数增强与 AI 列：当前候选状态

日期：2026-10-30（北京时间）。

本目录基于官方 `2026-competition` 提交 `5bce9e06d1281347ad61344e4b01fab9158c58a2`，在独立本地检出中开发。当前是未提交候选，没有编译，也没有运行数据库或比赛 SQL，不能作为两道赛题已完成或通过的证据。归档用逐文件摘要保存候选；本仓库尚未配置 Git 作者身份，未代填身份或创建提交。

## 题面与范围

- [AI 函数增强](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/psu9fks8dexldoum)：六个子题，60 分。
- [AI 列](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/qenx4sy5s09h2t07)：内核异步生成、事务、重试、严格读取与向量索引，50 分。
- [官方仓库与比赛分支](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/brxxufkegusa86hl)。

官方公开 SQL、期望结果及 Mock 文件未修改。没有使用真实模型密钥，没有推送或向比赛平台提测。

## 已写入的实现

AI 函数代码覆盖 JSON 与长文本 Prompt、重复占位符、原生 JSON/NULL 配置、Completion 响应校验、Ollama 协议和免 Key、Embedding 维度与有限数校验、响应 index 重排、跨行批量调用，以及 Rerank 的跨批次稳定全局排序。新增六项 C++ 回归及显式 CMake/CTest 入口；尚未配置、编译或运行测试目标。

公共 HTTP 客户端已改为让 libcurl 复制并持有请求体，避免局部 JSON 缓冲区析构后仍使用其指针。批次调用保留初始请求与独立的重试预算，耗尽后返回真实错误；每个请求完成、传输和 HTTP 状态及整批 JSON 全部验证后才发布响应，失败重试不会混入上一轮结果。客户端释放批次缓冲区时保留有效的分配器。上述改动只有源码与静态检查证据，尚未实际编译或运行 HTTP 场景。

RETRY 已纳入通用 ALTER action 解析；单独使用时继续生成原生 UPDATE 计划，与其他 ALTER action 组合时通过解析器的不支持错误退出。代码已修，Bison 生成、语法冲突检查及真实 SQL 的 1235 错误码仍待验证。

AI 列代码覆盖 CREATE/ADD/MODIFY 的解析及 Schema 元数据、三个隐藏物理列、冻结输入的 stored generated JSON 表达式、当前与历史元数据、DML 同事务任务投递、后台分组领取、租约和尝试预算、类型校验、请求及定义版本防覆盖、分组事务发布、SHOW 和原生 RETRY 目标传递。

`ai_column_strict_read` 默认为 OFF。开启后，解析阶段在 QueryCtx 保存用户实际引用的 AI 列，再复制到物理计划，执行前检查该列所有行的状态；即使优化器消去恒假子查询，原始引用仍保留。内部隐式赋值和后台发布不作为用户引用；重试目标由计划中的持久化列 ID 表达。

严格状态检查通过同步内部查询显式继承外层完整事务快照，避免 READ COMMITTED 下检查到新 READY 而实际查询仍读取旧 PENDING/NULL 的竞态。含原始 AI 引用的计划即使被优化为空集也保留获取事务快照的要求，兼容缓存计划后开启 strict 的情况；EXPLAIN 保留原生无事务行为。这是源码层修复，尚未在数据库复现竞态或验证修复。

任务表仅保存本实例的数据，不重新引入已从基线移除的多租户接口。模型请求在业务事务之外执行，结果通过普通 DML 写回，以使用现有索引维护机制。

普通 AI 表写入暂时关闭 PDML 与 parallel DAS 写入器，保留源查询的并行执行，让 INSERT SELECT、UPDATE、DELETE 等使用已接入事务任务投递的原生写入路径。内部 offline DDL 继续使用其原生复制与 swap 投递。整数任务的目标转换保留列定义的 signed/unsigned 区别；这些路径均待运行验证。

## 已补入、尚未运行的 ALTER 路径

ADD/MODIFY AI 列走原生 offline DDL。copy 投影重置定义变更列的结果与状态，并为每行生成共享请求标识；目标表的 stored generated `result_spec` 物化新输入。未变 READY 列保留结果，未变 PENDING 列重新生成请求。swap 的两个入口在各自现有事务内重建目标任务并清理旧任务，保存最终公开表名。worker 在领取、调用和发布前检查已发布 Schema，避免 swap 提交与 Schema 发布之间误删新任务。

checksum 排除预期重算的字段，包括普通列转换为 AI 列；未变结果仍使用原生校验。原生 DDL 的 INSERT SELECT 同时满足内部 session 与 DDL 标志时可写入 AI 结果投影，普通用户的显式结果赋值仍拒绝。索引副本清除 AI 所有权标志，删除列时在元数据事务中清理对应任务。

以上是候选代码覆盖范围，不能证明 ALTER 成功、事务正确或向量索引正确；Bison、C++ 和数据库验收仍未执行。

## 已执行与未执行的检查

已执行的本地检查包括 Git 空白检查、SQL 源码及头文件归属检查、严格依赖检查、Share 头文件归属检查、CMake inventory 导出、系统表生成，以及新增文件的 UTF-8 检查。Windows 路径归一化已修正；Share inventory 中两处已有头文件归属遗漏按真实 include 补正。系统变量生成器在 Python 3 中按等价的 `items()` 接口执行；已有初始化块的顺序及枚举文本保持基线原样。错误定义与对应头、消息表已经同步，仍需用原生生成器复核。

额外执行的 Share terminal 架构检查未通过：`src/share/ob_telemetry.cpp:1080` 使用隐藏的全局 context locator。该文件与官方基线一致，本任务未修改其实现；这项检查不能记为通过，也不扩展到遥测模块重构。

新增 `unittest/sql/ai_column/supplemental_regression.py` 以真实 SQL 覆盖已有数据 ADD、READY 后 MODIFY、普通列转 AI、PENDING 跨 offline DDL、带向量索引的 ADD/MODIFY、NULL 与 strict read，以及两组并行 hint 下的 INSERT SELECT/UPDATE/DELETE。strict 断言包含恒假 WHERE 与被优化为空的 EXISTS。驱动语法、CLI 和隔离 Mock HTTP 生命周期已检查；七个数据库场景均未运行。驱动复用未改动的官方 AI 列 Mock，不能替代赛题一指定的外部 Mock 服务。

未执行：Bison/Flex 生成、任何 C++ 编译或链接、C++ 回归、官方 AI 函数和 AI 列 SQL、重启恢复、并发及故障注入、真实向量索引检索、平台提测。

云端原实例已结束，当前没有活跃实例。此前宿主依赖安装成功，首次 `build.sh init` 因 Windows 归档换行转换而在脚本解析阶段退出，未进入工具链初始化或 C++ 编译。规范 LF 基线归档已逐文件与 Git blob 核对（9,176 个导出文件匹配）；旧云解压树的字节修复包尚未上传成功，上传接口返回过 HTTP 403。用户已明确选择先不启动新实例，等待其提供环境；尚未新建或重启实例，也未消耗新的云积分。

## 后续验证入口

先在独立、可丢弃的 Linux 环境恢复精确基线、应用最新 overlay，复核 RETRY 解析并完成构建，再部署测试实例。构建遵循本分支的 `docs/developer-guide/en/toolchain.md` 与 `build-and-run.md`；SQL 测试使用 `tools/deploy/obd.sh`，具体参数以 `docs/developer-guide/en/mysqltest.md` 为准。

直接相关的公开用例：

- `ai_function/ai_function_enhancement`：需要题目指定的 Mock AI Service，端口 8000；该服务不能用真实模型代替。
- `ai_function/ai_prompt`。
- `ai_column/ai_column_ddl`。
- `ai_column/ai_column_t1_dml`。
- `ai_column/ai_column_worker`。
- `ai_column/ai_column_retry_strict`。
- `ai_column/ai_column_vector_index`。

必须使用本候选真实编译出的 seekdb，保留完整构建日志、测试输出与源码摘要。公开测试通过、补充回归通过和正式平台成绩分别记录。

补充入口（均待在候选构建上执行）：

```bash
cmake --build build_release --target ai_function_contracts
ctest --test-dir build_release -R '^ai_function_contracts$' --output-on-failure
python3 unittest/sql/ai_column/supplemental_regression.py \
  --host 127.0.0.1 --port 2881 --user root --disposable \
  --obclient /absolute/path/to/obclient --report /tmp/ai-column-extra.json
```
