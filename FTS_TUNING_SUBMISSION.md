# seekdb 全文索引调优（赛题一）

`FULLTEXT` 写入链路的 ASCII 快路径优化：分词 `ctype`、小写化、停用词判定、
词频聚合与 `ObFTWord` 比较都不再走 Unicode 表；只要 token 里含非 ASCII 字节，
仍走原来的字符集函数，因此 `general_ci` 的重音折叠语义不变。

## 源码

| 项目 | 值 |
| --- | --- |
| 上游 | https://github.com/oceanbase/seekdb |
| 版本 | 1.4.0.0，source revision `de17949d57b9cd2240f141f8116d29bcd495d8d9` |
| 本仓库 | 该 revision 的完整源码树，已应用改动 |
| 改动范围 | `src/storage/fts/`（5 个文件） |

改动的 5 个文件：

```
src/storage/fts/utils/ob_ft_char_utils.h      (新增，ASCII 快路径工具)
src/storage/fts/ob_whitespace_ft_parser.cpp   (分词 ctype)
src/storage/fts/ob_fts_stop_word.cpp          (小写化 / 停用词 / 词频聚合)
src/storage/fts/ob_fts_stop_word.h
src/storage/fts/ob_fts_struct.cpp             (ObFTWord 比较)
```

同一份改动也存成补丁：`seekdb/fts-tuning/kernel/fts_tokenizer_optimization.patch`
（`patch -p1` 或 `git apply` 均可干净应用）。

## 构建入口

```bash
# Ubuntu 24.04 x86_64；依赖 cmake / ninja / g++ / libaio / ncurses / ssl / lz4 / zstd
export CARGO_NET_OFFLINE=true
./build.sh release --make -j$(nproc)

# 产物
build_release/src/observer/seekdb
```

本机（128 核）实测：`--make -j96` 全量构建 **395 s**，产物 1.38 GB。

## 验证结果

统一基准（`seekdb/fts-tuning/bench/bench.py`，100000 行语料、300 条查询、单线程，
同一台机器，原始二进制 vs 优化二进制）：

| 阶段 | 指标 | 基线 | 优化后 | 变化 |
| --- | --- | ---: | ---: | ---: |
| 建索引 | 耗时 | 25.85 s | **21.53 s** | **-16.7%** |
| 建索引 | 吞吐 | 3869 行/s | **4645 行/s** | **+20.1%** |
| 带索引插入 | 耗时 | 41.40 s | **36.35 s** | **-12.2%** |
| 带索引插入 | 吞吐 | 2415 行/s | **2751 行/s** | **+13.9%** |
| 分词（隔离测量） | 20000 文档 | 1.81 s | **0.85 s** | **-53.3%** |
| 全量检索 | p50 / QPS | 329 ms / 3.56 | 325 ms / 3.52 | ±噪声 |
| Top-10 检索 | p50 | 8.51 ms | 8.31 ms | ±噪声 |

语义等价性：

- `verify_semantics.py`：62/62 项探针逐字节一致；
- `verify_corpus_tokens.py`：100000 行 `TOKENIZE()` 摘要相同；
- 300 条检索的 `regress_*.tsv` 与 `.topk` 在基线与优化实例上**逐字节相同**。

## 文档

完整分析、优化说明、复现步骤与部署记录见 `seekdb/fts-tuning/README.md`。
