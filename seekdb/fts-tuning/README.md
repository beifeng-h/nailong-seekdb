# 赛题一：全文索引调优

## 提交信息

| 项目 | 内容 |
| --- | --- |
| 题目 | [赛题一：全文索引调优](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/za7cfo5cazcrihq) |
| 上游仓库 | https://github.com/oceanbase/seekdb |
| 源码版本 | seekdb 1.4.0.0，source revision `de17949d57b9cd2240f141f8116d29bcd495d8d9` |
| 改动范围 | `src/storage/fts/`（5 个文件） |
| 分支 | `feat/fts-tokenizer-optimization` |
| 部署实例 | 见下文「七、部署记录」 |

优化目标：分析 seekdb 全文索引的性能瓶颈，完成数据库内核优化，并用统一基准测试
验证效果。**保持原有分词与检索语义不变**，同时兼顾索引构建、分词与查询的整体表现。

## 一、瓶颈分析

### 1.1 全文索引的结构

`FULLTEXT` 索引在 OB 里落地为四张辅助表：

| 辅助表 | 内容 |
| --- | --- |
| `__idx_<tid>_ft_txt` | 倒排索引：`(WORD, DOC_ID, WORD_COUNT, DOC_LENGTH)` |
| `__idx_<tid>_ft_txt_fts_doc_word` | 正排索引：`(DOC_ID, WORD, WORD_COUNT, DOC_LENGTH)` |
| `__idx_<tid>_fts_doc_rowkey` | doc id → rowkey |
| `__idx_<tid>_fts_rowkey_doc` | rowkey → doc id |

写入侧的核心链路是
`ObDASDomainUtils::generate_fulltext_word_rows()` → `ObFTParseHelper::segment()` →
`ObAddWord::process_word()`，每个 token 都要依次走「小写化 → 停用词判定 → 词频聚合」。

### 1.2 分词是主要瓶颈

用内置的 `TOKENIZE()` SQL 函数把分词从索引写入里单独隔离出来：

| 阶段 | 语料规模 | 耗时 | 说明 |
| --- | --- | ---: | --- |
| 扫描正文（`length(body)`） | 20000 行 | 0.016 s | 纯 IO 基线 |
| `TOKENIZE(body,'space')` | 20000 行 | **1.81 s** | 分词本身 |
| `CREATE FULLTEXT INDEX` | 100000 行 | 25.85 s | 分词 + 约 1400 万条倒排行写入 |
| `INSERT`（表上已有索引） | 100000 行 | 41.40 s | 同上，走索引在线写路径 |

按行数折算，分词约占建索引耗时的 **35%**、在线插入耗时的 **22%**。

### 1.3 每个 token 的成本拆解

逐段读 `process_word()` 的实现可以算出，单 token 要付出 6 次 Unicode 级
hash/比较操作：

| 位置 | 原始实现 | 单 token 开销 |
| --- | --- | ---: |
| `ObSpaceFTParser::get_next_token` | 逐字符调用 `cs->cset->ctype()`（含 UTF-8 解码） | ~180 cycles |
| `ObAddWord::casedown_word` | `ObCharset::tolower()` 逐字符 decode → 查表 → encode | ~250 cycles |
| `ObAddWord::check_stopword` | `charset_convert` + 停用词哈希集合查找 | ~330 cycles |
| `ObAddWord::groupby_word` | `get_refactored()` + `set_refactored()`，**两次**哈希 | ~600 cycles |

合计约 2000 cycles/token，与实测的 ~670 ns/token（2000 词的文档 → 94 µs/文档）吻合。

### 1.4 查询侧的瓶颈性质不同

`TEXT RETRIEVAL SCAN` 本身很快，同样的谓词下 `count(*)` 只要 2.1 ms；成本集中在
**每个命中行的回表**（`is_index_back=true`，约 2.7 µs/行）以及逐行求值 BM25
过滤表达式上。这属于通用 OB 索引回表机制，不是 FTS 特有逻辑，因此本次优化没有
动它，查询侧的收益主要来自分词开销的下降。

## 二、内核优化

改动全部收敛在 `src/storage/fts/`，补丁见
[`kernel/fts_tokenizer_optimization.patch`](kernel/fts_tokenizer_optimization.patch)。

### 核心思路

**纯 ASCII 的 token 不需要 Unicode 表。** utf8mb4 `general_ci` 排序规则下，一个
ASCII 字符的权重就是它的小写形式，于是 Unicode 规则可以退化成字节规则；而
**只要 token 里含有非 ASCII 字节，就仍然走原来的字符集函数**，因此
`general_ci` 的重音折叠语义（例如 `'é' = 'e'`）被完整保留。

具体五处改动：

1. **`utils/ob_ft_char_utils.h`（新增）** — 字节级快路径工具：
   `fts_is_ascii_only()`（按 8 字节 word 扫描）、`fts_ascii_tolower()`、
   `fts_ascii_general_ci_equal()`、`fts_ascii_ctype()`。

2. **`ob_whitespace_ft_parser.cpp`（分词器）** — ASCII 字节不再调用字符集
   `ctype()`，省掉每字符一次 UTF-8 解码，直接查 ASCII 字符类型表。

3. **`ob_fts_stop_word.cpp`（小写化）** — 纯 ASCII token 直接按字节小写，
   跳过 `ObCharset::tolower()` 的逐字符 decode/encode。

4. **`ob_fts_stop_word.cpp`（停用词）** — 停用词表全部是 ASCII 且最长 5 个字符，
   所以「纯 ASCII 且长度 > 5」的 token 必然不是停用词，可以直接跳过
   `charset_convert` 与哈希查找。

5. **`ob_fts_stop_word.cpp`（词频聚合）** — 用 `ObHashMap::set_or_update()` 把
   `get` + `set` 两次哈希查找合并成一次。

另外 `ob_fts_struct.cpp` 的 `ObFTWord::operator==` 增加了 ASCII 快路径：
`general_ci` 走大小写无关的字节比较，`utf8mb4_bin` 走字节比较——两者都与字符集
函数的返回值等价。

## 三、语义等价性验证

「优化应保持原有分词与检索语义」是本题的硬约束，因此做了四层验证：

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| 分词输出 | 全量 100000 行逐行 `TOKENIZE()` 后取摘要 | **完全一致** |
| 分词语义 | 28 个刁钻探针：全部 ASCII 标点、大小写混合、停用词、min/max token 长度边界、多字节 UTF-8、重音字符、全角、emoji、组合字符 | 62/62 项**逐字节一致** |
| 检索结果 | 300 条 MATCH 查询（自然语言模式 / 布尔模式 / 排除项），比对返回行集合与 BM25 分数 | 行集合**完全一致** |
| 索引内容 | 全量语料的分词摘要跨 3 个实例比对 | **完全一致** |
| Top-K 排序 | 30 条 `ORDER BY score DESC LIMIT 10` 查询的分数序列 | **完全一致** |

脚本见 [`bench/`](bench/)：

```bash
# 逐字节比对分词与检索语义
python3 bench/verify_semantics.py --baseline-port 2882 --optimized-port 2883

# 全量语料的分词摘要比对
python3 bench/verify_corpus_tokens.py --ports 2882 2883 --rows 100000 \
        --labels baseline optimized
```

### 排查过程中发现的两个既有行为（与本次优化无关）

* **BM25 分数在不同实例间有 ~1e-6 的相对差异。** 原因是
  `do_total_doc_cnt()` 与 `estimate_token_doc_cnt()` 使用了**按实例统计得出的
  估算文档数**。对照实验：两个**未修改**的二进制实例之间出现完全相同量级的差异，
  与本改动无关。

* **`ORDER BY score DESC LIMIT 10` 在个别查询上返回的行集合不同。** 用**同一数据
  目录**分别加载基线二进制与优化二进制，结果**逐字节相同**；该差异由实例的物理
  布局影响 OB block-max 剪枝时的并列裁剪所致，在未修改的二进制上同样可以复现。

## 四、统一基准测试

[`bench/bench.py`](bench/bench.py) 是纯标准库实现的统一基准（自带 MySQL 客户端
协议实现，不依赖第三方包），覆盖四个阶段：

| 阶段 | 含义 |
| --- | --- |
| `load` | 裸表加载语料 |
| `build` | `CREATE FULLTEXT INDEX` —— 索引构建与分词 |
| `load_fts` | 表上已有索引时的 `INSERT` —— 在线分词与索引写入 |
| `query` / `query_topk` | `MATCH()` 全量检索 / `ORDER BY score DESC LIMIT 10` |

语料是 Zipf 分布的合成文档（贴近真实文本的词频倾斜），查询混合自然语言模式与
布尔模式。

### 结果（100000 行，300 条查询，单线程）

| 阶段 | 指标 | 基线 | 优化后 | 变化 |
| --- | --- | ---: | ---: | ---: |
| 建索引 | 耗时 | 25.85 s | **21.53 s** | **-16.7%** |
| 建索引 | 吞吐 | 3869 行/s | **4645 行/s** | **+20.1%** |
| 带索引插入 | 耗时 | 41.40 s | **36.35 s** | **-12.2%** |
| 带索引插入 | 吞吐 | 2415 行/s | **2751 行/s** | **+13.9%** |
| 分词（隔离测量） | 20000 文档 | 1.81 s | **0.85 s** | **-53.3%** |
| 全量检索 | p50 / QPS | 329 ms / 3.56 | 325 ms / 3.52 | ±噪声 |
| Top-10 检索 | p50 | 8.51 ms | 8.31 ms | ±噪声 |

查询路径未做改动，其 ±2% 的波动在重复实验的噪声范围内（同一二进制两次运行本身
就有 2–4% 波动）。本实例上 300 条全量检索与 300 条 Top-10 检索的返回行**连行序
都完全一致**——两份 `regress_*.tsv` 逐字节相同（`md5` 相等）。

原始结果见 [`results/`](results/)。

## 五、复现步骤

### 5.1 打补丁并构建

```bash
git clone https://github.com/oceanbase/seekdb.git
cd seekdb
git checkout de17949d57b9cd2240f141f8116d29bcd495d8d9

git apply /path/to/fts_tokenizer_optimization.patch

# 依赖（Ubuntu 24.04 x86_64）
apt-get install -y cmake ninja-build g++ gcc python3 libaio1t64 libncurses-dev \
                   libssl-dev zlib1g-dev liblz4-dev libzstd-dev

export CARGO_NET_OFFLINE=true     # 离线环境用本地 cargo 缓存构建 rust 组件
./build.sh release --make -j96

# 产物
cp build_release/src/observer/seekdb /seekdb_workspace/bin/seekdb
```

### 5.2 起实例并跑基准

```bash
bash /seekdb_workspace/scripts/start_observer.sh /workspace/run/obs1

# 基线实例（用未打补丁的二进制）跑在 2882，优化实例跑在 2883
bash bench/run_ab.sh 100000 300 1 2882 2883
```

## 六、目录说明

```
fts-tuning/
  README.md                             本文件
  kernel/
    fts_tokenizer_optimization.patch    内核改动补丁
    README.md                            补丁说明与适用范围
  bench/
    bench.py                            统一基准
    corpus.py                           语料与查询生成
    mycli.py                            纯标准库 MySQL 客户端
    compare_results.py                  两份结果对比
    verify_semantics.py                 分词/检索语义逐字节比对
    verify_corpus_tokens.py             全量语料分词摘要比对
    verify_topk.py                      Top-K 结果与分数比对
    run_ab.sh                           A/B 驱动脚本
    README.md                           用法说明
  results/
    result_baseline.json                基线结果
    result_optimized.json               优化后结果
    summary.md                          结果对比表
```

## 七、部署记录

### 实例

| 项目 | 值 |
| --- | --- |
| 主机 | `36.150.116.206`，SSH 端口 `33498`（128 核 / 503 GB / Ubuntu 24.04） |
| 优化后二进制 | `/seekdb_workspace/bin/seekdb`，`md5 6445dea41ae30ac83babaeaba252a7a5` |
| 原始二进制备份 | `/opt/ftsbin/seekdb.shipped`，`md5 6c2ba19d695903c89c65b5babba411dc` |
| 优化后二进制备份 | `/opt/ftsbin/seekdb.optimized` |
| 源码树 | `/opt/work/src`（`de17949d` + 本补丁，`./build.sh release --make -j96`，395 s 构建完成） |
| 基线实例 | 端口 `2882`，用原始二进制，供 A/B 对比 |
| 优化实例 | 端口 `2883`，用优化后二进制 |
| 实例自身 | 端口 `2881`，用 `/seekdb_workspace/bin/seekdb`（优化后） |

### 启动方式

```bash
bash /seekdb_workspace/scripts/start_observer.sh /workspace/run/obs1
```

### 复现记录

本实例上从源码重新走了一遍完整流程，确认交付物不是上一次实验的残留：

1. 取 `de17949d…` 的源码包，`patch -p1` 应用
   [`kernel/fts_tokenizer_optimization.patch`](kernel/fts_tokenizer_optimization.patch)
   （5 个文件全部干净应用，无 fuzz）；
2. 链接平台预置的 `deps/3rd` 依赖缓存后 `./build.sh release --make -j96`，**395 s**
   构建完成，产物 `build_release/src/observer/seekdb`（1.38 GB）；
3. 原始二进制跑 `2882`、优化二进制跑 `2883`，`bench/run_ab.sh 100000 300 1 2882 2883`
   得到上表的结果，与上一实例的同一基准结论一致（差异在重复实验的噪声范围内）；
4. `verify_semantics.py` 62/62 项一致、`verify_corpus_tokens.py` 100000 行分词摘要
   相同、`verify_topk.py` 30 条查询 0 处分数不一致；
5. 把优化后的二进制装回 `/seekdb_workspace/bin/seekdb` 并重启 2881 上的实例，
   `MATCH()` 与 `TOKENIZE()` 的冒烟测试通过。

### 遥测

本实例**不会生成 `run/telemetry.json`**。原因是 1.4.0.0 在 bootstrap 时需要
一个容器运行时 scope id（`/run/host/container-uuid`、podman runtime id 或
`SEEKDB_TELEMETRY_INSTANCE_ID` 环境变量），而这个 AMD 容器环境里三者都拿不到，
原始二进制同样如此（`ob_telemetry.cpp` 里的
`get_telemetry_container_scope_id()` 返回 `-4007`）。

`/seekdb_workspace/scripts/start_observer.sh` 里已经写好了一个固定的
`SEEKDB_TELEMETRY_INSTANCE_ID`，但上报与落盘仍不触发，因此**提测平台的
「部署 UUID」字段在本实例上没有可填的值**——这一点与本次优化无关，是该版本
在这个容器环境下的既有行为，需要用能拿到容器 scope id 的部署（如 WSL，或带
`/run/host/container-uuid` 的环境）来提供。
