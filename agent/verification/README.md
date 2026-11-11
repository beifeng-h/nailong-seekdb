# 本地差分验证报告

这个目录是**本地差分验证**的原始输出：用**组织方未修改**的公开题包
（`mysqltest/`）与**组织方未修改**的驱动（`protocol_check/run.py`、
`_assertions.py`、`config.json`）去驱动**另一套独立实现**，做跨实现差分。

## 这里跑的是什么

不是提测实现本身（C++ 容器），而是本机的 Python 参考实现。两者代码
完全不同、语言也不同，被同一份组织方断言审判——差异立刻暴露。

## 为什么报告在这里而不是 CI 里

提测实现侧的验证由 CI 完成，见
`[agent/learning_record.md](../learning_record.md)` 的可核验清单。
这份本地报告回答的是另一个问题：**同一份外部断言，能不能让第二套
独立实现也站住**。它抓到了参考实现里的真实原子性缺陷，也暴露了
"通过的用例可能通过了错的原因"。

## 唯一改动的地方

题包自带的 `protocol_check/_transport.py` 用 `selectors` + `select()`
读子进程管道。`select()` 在 POSIX 上能监视管道，在 Windows 上只能
监视 socket，管道直接报 `WinError 10093`；`os.killpg` 在 Windows 上
也不存在。

本地复现时只把这一层换成 Windows 兼容实现（后台线程读管道 +
`proc.kill()`）。以下文件**一行未改**，直接从题包目录加载：

- `protocol_check/run.py`——用例选取、顺序、重启与模式切换、报告生成；
- `protocol_check/_assertions.py`——比较口径（浮点容差、多重集合、
  错误码断言）；
- `protocol_check/config.json`——判题资源与比较参数；
- `mysqltest/`——全部公开用例与断言。

所以"通过"的含义与组织方自测工具完全一致。

## 结果

| 轮次 | 通过 / 总数 | 说明 |
| --- | --- | --- |
| 首轮 | 14 / 20 | 6 项失败，其中 `transaction_failed_insert` 是真实原子性缺陷 |
| 修复后 | 19 / 20 | 剩余 1 项是 `vector_index_maintenance`——参考实现明确只做精确 TopK，不实现 ANN 索引（SPEC-006 已登记为不在契约范围内） |

## 复现方式

两个仓库角色：

- **参考实现仓库**——含 `minivectordb/` 与 `scripts/minivectordb_jsonl.py`
  （JSONL 协议适配器）、`scripts/protocol_check_win/`（Windows 传输层与
  启动器）；
- **题包仓库**——`2026-competition-round2` 的检出，含 `mysqltest/` 与
  `protocol_check/`。

在参考实现仓库里执行（路径按实际位置替换）：

```powershell
$env:PYTHONPATH = "<参考实现仓库>"
$env:PYTHONUTF8 = "1"
$py = "<python>"
$cmd = '["' + ($py -replace '\\','/') + '", "<参考实现仓库>/scripts/minivectordb_jsonl.py", "--data-dir", "{data_dir}"]'
& $py scripts\protocol_check_win\run_protocol_check.py `
    "<题包仓库>/mysqltest" --command $cmd --output <新的输出目录>
```

`run_protocol_check.py` 会把题包的 `run.py` 原样加载（`runpy.run_path`），
只替换 `_transport` 的解析位置。输出目录必须是新目录。

`PYTHONUTF8=1` 是必需的：题包用例文件是 UTF-8，而 Windows 上
`Path.read_text()` 默认用 GBK，会在读中文用例时报解码错误。

## 文件

- `python_public_check.first_run.txt`——首轮差分的原始控制台输出（逐字记录）；
- `python_public_check.result.json`——组织方 `result.json` 原样拷贝；
- `python_public_check.cases.json`——逐用例状态与耗时。

