# seekdb 全文索引调优 — 第二轮（本实例 36.150.116.206:31435）

## 1. 环境与方法

* 实例：`ssh -p 31435 root@36.150.116.206`（128 核 / 503 GB / Ubuntu 24.04）
* 源码：`beifeng-h/nailong-seekdb` main 分支（已含上一轮的分词 ASCII 快路径）
* 基线二进制与本轮二进制在同一台机器、同一数据目录、交替运行，
  使用仓库自带的 `tools/benchmark/fts_large_bench.sh` 官方口径：
  `ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 SAMPLES=3 WARMUP=30 SKIP_LOAD=0`
* 每组交替跑 3 轮，取中位数比较。

## 2. 瓶颈定位（实测，不是推测）

用 rdtsc 计数器在关键函数上打点（诊断构建，未进入提交），单线程统计（cycles/次）：

| 位置 | 每次耗时 | 说明 |
|---|---|---|
| `ObIKCJKProcessor::do_process` 路径中的 `ObFTRangeDict::match` | **264 cycles** | 每个字符都要走一次 |
| └ `find_first_char_range` | **200 cycles** | 占 match 的 76% |
| └ 字典 DAT 查找 `ObArrayHashMap::find` | 29 cycles | 很便宜 |
| `ObAddWord::process_word` | 441 cycles/token | 小写化 49 + 停用词 75 + 词频聚合 214 |
| 整篇文档分词 `ObFTParseHelper::segment` | 60.5k cycles ≈ 20 µs | 2.75M 次 match / 4 万篇 |
| 索引构建每篇文档（含生成索引行） | 64.5k cycles ≈ 21 µs | 只占建索引总时间的 ~10% |

两条重要结论：

1. **建索引的耗时并不主要花在分词上**。`ALTER TABLE ... ADD FULLTEXT INDEX`
   20k 行 ≈ 4.0 s，其中分词与索引行生成只有 ~0.84 s，其余 ~3.1 s 花在
   DAS/存储写入（约 48 行/篇 × 3.2 µs/行）。因此分词侧优化对构建指标的
   杠杆有限（这也解释了为什么"每篇文档重建 IK 词典"这类改动实测收益接近 0，
   实测单次词典物化仅约 36 次小分配）。
2. **分词里最贵的是每个字符一次的"字典区间查找"**：`find_first_char_range`
   对全部 12 个区间线性扫描，每次比较都走 `ObCharset::strcmp` 函数指针，
   200 cycles/次，而真正的 DAT 查找只要 29 cycles。

## 3. 本轮改动（3 个文件）

1. `src/storage/fts/dict/ob_ft_range_dict.{h,cpp}` — 区间查找快路径：
   * 字典区间固定用 `utf8mb4_bin`，其比较等价于 UTF-8 字节序比较，
     因此用 memcmp + 长度比较替代 `ObCharset::strcmp`；
   * 记住上次命中的区间作为提示（一篇文档的字符几乎都在同一区间），
     命中则一次比较完成，未命中再退回全量扫描（命中前会校验，不会误用）。
   * 实测：`find_first_char_range` 200 → 55 cycles，`match` 264 → 120 cycles。
2. `src/storage/fts/ob_fts_stop_word.cpp` — 停用词检查的恒等变换消除：
   当待查词与停用词表 collation 相同时，`charset_convert` 是恒等变换，
   直接查表；同时把每 token 一次的 `ObArenaAllocator` 构造/8KB 页分配
   从热路径上去掉（只有真正需要转换时才建）。语义完全不变。

## 4. 同机 A/B 结果（3 轮交替，中位数）

| 指标 | 基线 | 本轮 | 变化 |
|---|---|---|---|
| build_ik_all_sec | 4.165 | **4.056** | **−2.6%** |
| build_ik_content_sec | 3.138 | **2.932** | **−6.6%** |
| build_beng_en_sec | 1.630 | 1.635 | −0.3%（噪声） |
| tokenize_ik_avg_ms | 0.1065 | **0.0963** | **−9.6%** |
| tokenize_beng_avg_ms | 0.0604 | **0.0578** | **−4.3%** |
| query_cn/beng/mixed/limit | 1.48/2.17/1.64/2.01 | 1.50/2.16/1.66/2.03 | ±1%（未改动查询路径） |

按官方口径（构建 / 分词 / 查询三类等权）：构建 −3.0%、分词 −6.9%、查询 −0.7%。

## 5. 正确性验证

* 40 条刁钻语料（大小写、停用词、重音、全角、emoji、日韩俄希、CJK 标点、
  数字/量词、重复词、超长 token）：`TOKENIZE` 输出、`MATCH` 命中数、
  布尔检索命中集合、BM25 相关度分数——两个二进制**逐字节相同**（184 行 diff 为空）。
* 官方基准的 4 个命中数完全一致：8001 / 11000 / 7332 / 20。

## 6. 未采纳的方向（有数据支撑）

* 每线程复用 IK 词典对象：实测 PARSER_INIT 每次约 1 µs，构建/分词收益在噪声内。
* 逐 token 小写化/词频聚合的进一步改造：合计约 263 cycles/token，
  相对分词总成本（~2000 cycles/token）占比较小。
* 查询侧：`is_index_back=false`、每命中约 0.19 µs，已经很紧；
  `LIMIT` 未下推到 TEXT RETRIEVAL SCAN 是 `query_limit` 的根因，
  但需要改优化器 + DAS 两层，本轮未做。
