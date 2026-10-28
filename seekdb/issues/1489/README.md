# 赛题二：修复 Issue #1489

## 选题

| 项目 | 内容 |
| --- | --- |
| Issue | [#1489 [Bug]: Performance bug: Equivalent OR-FALSE identity filter causes up to 71.38x search latency difference](https://github.com/oceanbase/seekdb/issues/1489) |
| 标签 | `2026-competition` |
| 上游仓库 | https://github.com/oceanbase/seekdb |
| Fork 仓库 | https://github.com/beifeng-h/seekdb |
| PR | 待填 |
| 部署实例 | UUID `1c461b9d-973e-8c7e-9a3c-29099836cf43`（WSL / Linux 部署，端口 2882） |

## 为什么选它

`2026-competition` 标签下共 21 个 Issue，其中 13 个属于同一族：*两个语义等价的过滤条件，
在向量检索（`APPROXIMATE`）路径下延迟相差 N 倍*。这一族共享同一条根因链路，且
Issue #1489 下已经有开发者给出根因定位与一行代码指针，复现路径和根因都已明确，
剩下的工作是验证、修复与补回归测试。

其余 8 个是功能或增强类任务（表函数注册、PL 复合输出编码、TraceLog 状态清理等），
改动面和不确定性都大得多。

## 根因

`src/sql/rewrite/ob_transformer_impl.cpp` 的 `choose_rewrite_rules()`：

```cpp
if (func.contain_geometry_values_ || func.contain_fulltext_search_ || func.contain_vec_index_approx_) {
  disable_list = ObTransformRule::ALL_TRANSFORM_RULES;
}
```

`contain_vec_index_approx_` 由 `stmt->has_vec_approx()` 置位，也就是 SQL 里带
`ORDER BY l2_distance(...) APPROXIMATE` 的近似向量检索。这类语句此前会**禁用全部
rewrite 规则**，于是谓词保持用户书写的原样：

- 重复谓词不会被合并（`SIMPLIFY_EXPR` 的 `remove_duplicate_exprs` 不执行）；
- `OR` 条件不会被拆成按索引取范围扫描（`OR_EXPANSION` 不执行）。

直接后果是向量索引自适应扫描无法走 `idx_category` 的范围扫描，退化成整表扫描 +
逐行求值残余谓词。

## 复现

脚本：[`../../wsl/repro_ann_rewrite.py`](../../wsl/repro_ann_rewrite.py)

```powershell
wsl -d seekdb-ubuntu -u root -- bash -lc `
  "python3 '/mnt/d/数据库大赛/seekdb/wsl/repro_ann_rewrite.py' --port 2882 --rows 30000 --repeats 128 --runs 5"
```

表结构与 Issue 一致：`category` 与 `age` 各有一个二级索引，`age` 取值域 `[1,1000]，
因此 `age > 1000` 恒为假，`category = 1 OR age > 1000` 与 `category = 1` 语义等价。

seekdb 1.4.0.0（部署实例，30,000 行）实测：

| 过滤条件 | exact 中位延迟 | ANN 中位延迟 |
| --- | ---: | ---: |
| `category = 1` | 1.053 ms | 0.594 ms |
| `category = 1 OR age > 1000` | 1.349 ms | 10.218 ms |
| `category = 1 OR age > 1000` ×128 | 1.864 ms | 114.640 ms |

exact 路径三种写法几乎一致（`OR_EXPANSION` 把 OR 拆成 `idx_category` 与 `idx_age`
两个范围扫描分支），ANN 路径则退化成整表扫描。

`EXPLAIN` 证据（修复前）：

```text
-- category = 1
|0 |VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)|t(idx_category)|60 |159 |
    access([t.__pk_increment], [t.category], [t.id])
    filter_before_indexback[true]
    range_key([t.category]), range(1,MIN ; 1,MAX)

-- category = 1 OR age > 1000
|0 |VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)|t   |63 |295 |
    access([t.__pk_increment], [t.category], [t.age], [t.id])
    filter_before_indexback[false]
    range_key([t.__pk_increment]), range(MIN ; MAX)always true
```

## 修复

待填。

## 验证

待填。

## 提交

待填。
