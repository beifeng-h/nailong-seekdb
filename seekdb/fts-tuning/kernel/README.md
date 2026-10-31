# 内核改动补丁

`fts_tokenizer_optimization.patch` 是本次全文索引调优的全部内核改动。

## 适用范围

| 项目 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/oceanbase/seekdb |
| 源码版本 | seekdb 1.4.0.0，source revision `de17949d57b9cd2240f141f8116d29bcd495d8d9` |
| 改动目录 | `src/storage/fts/` |
| 改动文件数 | 5 |

## 应用方法

```bash
git clone https://github.com/oceanbase/seekdb.git
cd seekdb
git checkout de17949d57b9cd2240f141f8116d29bcd495d8d9

git apply ../kernel/fts_tokenizer_optimization.patch
```

补丁也可以用 `patch -p1` 应用：

```bash
patch -p1 < ../kernel/fts_tokenizer_optimization.patch
```

## 改动清单

| 文件 | 改动 |
| --- | --- |
| `src/storage/fts/utils/ob_ft_char_utils.h` | 新增字节级快路径工具（ASCII 检测、大小写、比较、字符类型） |
| `src/storage/fts/ob_whitespace_ft_parser.cpp` | 分词器对 ASCII 字节不再调用字符集 `ctype()` |
| `src/storage/fts/ob_fts_stop_word.cpp` | 小写化走 ASCII 快路径；停用词前置过滤；词频聚合由两次哈希合并为一次 |
| `src/storage/fts/ob_fts_stop_word.h` | `ObAddWord` 增加 `is_ascii_word_` 成员 |
| `src/storage/fts/ob_fts_struct.cpp` | `ObFTWord::operator==` 增加 ASCII 快路径 |

## 不变性说明

所有快路径都只在 **token 全部由 ASCII 字节组成** 时启用；含非 ASCII 字节的
token 一律走原来的字符集函数。因此：

* 分词切分结果不变；
* utf8mb4 `general_ci` 的大小写不敏感与重音折叠语义不变
  （`'é' = 'e'` 这类比较仍然成立）；
* `utf8mb4_bin` 的逐字节比较语义不变；
* 停用词表判定结果不变（表内全部是 ASCII 且最长 5 字符，长于它的 ASCII token
  必然不是停用词）；
* 词频统计结果不变（新词计数 1，已知词累加 `word_freq`）。

验证脚本与结果见 [`../bench/`](../bench/) 与 [`../results/`](../results/)。
