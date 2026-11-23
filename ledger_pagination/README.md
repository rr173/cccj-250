# 台账逻辑分页（ledger_pagination）

把全年领用台账编排成**机器可接收的逻辑页**，交给只能逐页接收、逐页核对的归档系统。
不产出成品 PDF、不做排印、不联网归档——只交付逻辑页。

## 问题模型

输入是确定顺序的明细序列，每条明细带：

| 字段 | 含义 | 约束 |
|---|---|---|
| `group` | 所属一级分组 | 每个分组只出现为一个连续区间 |
| `qty` | 精确带符号数量 | 整数/小数字符串/分数，精确求和 |
| `units` | 行占用单位数 | 整数 ≥ 1 |
| `citations` | 引用的本批引注 ID | 必须在引注目录中声明 |

另有：引注目录 `citation_catalog`（每个引注的占用单位，非负整数）、
不可拆组 `unbreakable_groups`（禁止跨页）、每页固定上限 `capacity`（整数 ≥ 1）。

### 页成本（所有成分一起计入容量）

覆盖明细区间 `[i, j)` 的一页，其占用单位为：

```
  Σ 明细 units                       原行
+ 2 × 页内相交分组数                  每组一行组标题 + 一行控制额，各占 1 单位
+ Σ 页内去重引注的目录单位            同一引注不管跨多少组只发一次
+ (该页续接上页分组 ? 1 : 0)          续接标记
+ (该页分组留待下页继续 ? 1 : 0)      续接标记；首页/末页无外部续接
```

控制额只对该页本组实际明细求和。续接标记一律按**原始完整台账**计算——
包括不可行时输出的前缀页（前缀页不当成已交付）。

不可拆组 ⇔ 页边界不得落在其区间内部。每页至少一条明细，保持原序，不可跳过数据。

### 目标

1. 页数最少；
2. 页数相同时，按页边界位置向量（各页结束位置）字典序取最小。

### 不可行时

- 给出任何合法分页从开头最多能覆盖到的边界 `max_covered_boundary`；
- 给出一条到该边界的合法路径（前缀页，标记 `delivered: false`）；
- 公布阻断证据（不要求最小证明）：
  - `single_detail_exceeds_capacity`：下一条明细独占一页也装不下（附成本分解）；
  - `unbreakable_group_exceeds_capacity`：边界处开始的不可拆组整组装不进一页
    （附组区间、整组页成本、当前可负担的最远页末）。
- 非法输入（分组不连续、units 非正整数、引注未声明、目录单位为负、容量非法等）
  以 `status: "invalid"` 与合法但不可装页（`"infeasible"`）明确区分。

## 算法（不构建所有明细位置对的密集表）

记页成本 `cost(i, j)`：对固定的 `i` 关于 `j` 单调不减，对固定的 `j` 关于 `i` 单调不增
（去掉一条明细至少省 1 单位，足以抵消续接标记 ±1 的波动）。

1. **双指针扫描**求 `max_end[i]`（从 `i` 出发能装下的最远页末）：滑动窗口增量维护
   明细单位和、分组数、引注重复计数。每个位置进出窗口各一次，探测次数 ≤ 2n。
2. **层级数组** `level[p]` = 覆盖 `[p, n)` 的最少页数。`level` 在合法边界上单调不增，
   故 `level[p] = 1 + level[clamp(max_end[p])]`（`clamp` 向下吸附到最近合法边界），
   一次倒序遍历即可。
3. 可行 ⇔ `level[0]` 有限。字典序最小边界向量是贪心的：从 level = r 的位置，
   下一边界取第一个 level 降到 r−1 的合法位置（阈值表一次扫描预计算）。
4. 不可行时，从 0 迭代最远跳跃映射 `p → clamp(max_end[p])`，不动点即最大可覆盖边界，
   迭代序列就是一条合法路径。

时间 O(n + 引注引用总数)，空间 O(n)。**不会**逐一生成每个合法整页区间后再选页：
长清单测试用计数器（`window_probes ≤ 2n`）核对这一点。

## 逻辑页的行顺序约定（每页可独立验证）

```
[续上页标记?]                                  continuation / from_previous
( 组标题 → 本组明细(原序) → 控制额 )*           group_header / detail / control_total
引注行*（按引注目录声明顺序，页内去重）          citation
[留待下页标记?]                                continuation / to_next
```

每页输出：`detail_start/detail_end`、分组段及控制额、引注（含目录单位）、
续接标记、`units_used`、`capacity`、`headroom`、`lines`（上述固定顺序），
归档系统收到单页即可核对行数、物料数量及引注，无需另页查引用。

## 输入 / 输出

```json
{
  "capacity": 12,
  "citation_catalog": {"Q1": 1, "Q2": 2},
  "unbreakable_groups": ["B"],
  "details": [
    {"group": "A", "qty": "10", "units": 1, "citations": ["Q1"]}
  ]
}
```

Python API：

```python
from ledger_pagination import paginate
result = paginate(payload)          # -> dict, status ∈ ok / infeasible / invalid
```

命令行：

```bash
python3 -m ledger_pagination examples/sample_input.json   # 或从 stdin 读
```

## 测试

```bash
python3 tests/run_tests.py          # 无 pytest 依赖；文件同时兼容 pytest
```

- 小数据枚举分割（1200 种子 + 扩展 4000 种子）与暴力枚举对照：组合页容量、
  续行、引注共用、固定决胜（同页数字典序最小）逐一核对；
- 长清单（20 万 / 100 万明细，单页可容纳数千行）：核对线性探测计数与耗时，
  验证没有逐一生成每个合法整页区间后再选页。
