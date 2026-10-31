# 统一基准与验证脚本

全部脚本只用 Python 标准库（`mycli.py` 是自己实现的 MySQL 客户端协议），
不需要安装任何第三方包。

## 目录

| 文件 | 作用 |
| --- | --- |
| `bench.py` | 统一基准：裸表加载 / 建索引 / 带索引插入 / MATCH 检索 / Top-K 检索 |
| `corpus.py` | Zipf 分布的合成语料与查询生成（固定随机种子，可复现） |
| `mycli.py` | 纯标准库 MySQL 客户端 |
| `compare_results.py` | 把两份基准结果并排对比并算出变化幅度 |
| `verify_semantics.py` | 分词与检索语义的**逐字节**比对 |
| `verify_corpus_tokens.py` | 全量语料分词摘要比对 |
| `verify_topk.py` | Top-K 结果行与 BM25 分数比对 |
| `run_ab.sh` | A/B 驱动：起两个实例、跑基准、出对比表 |

## 统一基准

```bash
python3 bench.py \
  --host 127.0.0.1 --port 2881 --user root \
  --rows 100000 --queries 300 --threads 1 \
  --tag run --out result.json --regress regress.tsv
```

阶段由 `--phases` 控制，默认全跑：

```
setup,load,build,load_fts,query,query_topk
```

* `load` —— 把语料写进没有索引的 `bench_stage` 表；
* `build` —— `bench_main` 表上 `CREATE FULLTEXT INDEX`（纯建索引 + 分词）；
* `load_fts` —— 把同一份语料 `INSERT SELECT` 进**已带索引**的 `bench_fts` 表
  （在线分词 + 索引写入）；
* `query` —— `MATCH()` 全量检索；
* `query_topk` —— `ORDER BY score DESC LIMIT 10` 的 Top-K 检索。

`--regress` 会把每条查询的返回行数、结果摘要写到文件，用于比对语义。

> 批量导入会超过默认的 10 秒语句超时，脚本已经在每个连接上设置
> `ob_query_timeout` / `ob_trx_timeout`。

## A/B 驱动

```bash
bash run_ab.sh 100000 300 1 2882 2883
```

参数依次是：行数、查询数、并发线程数、基线实例端口、优化实例端口。
脚本会依次在两个实例上跑基准，然后输出对比表与结果集一致性检查。

## 语义验证

```bash
# 逐字节比对分词与检索语义（小而刁钻的语料，62 项检查）
python3 verify_semantics.py --baseline-port 2882 --optimized-port 2883

# 全量语料的分词摘要比对
python3 verify_corpus_tokens.py --ports 2882 2883 --rows 100000 \
        --labels baseline optimized
```

`verify_semantics.py` 的探针覆盖：全部 ASCII 标点、大小写混合、停用词、
min/max token 长度边界、多字节 UTF-8、重音字符、全角字符、emoji、组合字符。

`verify_topk.py` 比较 Top-K 结果的分数序列；因为 BM25 用到**按实例统计得出的
估算文档数**，不同实例之间允许 ~1e-6 的相对偏差，脚本按相对容差比较。
