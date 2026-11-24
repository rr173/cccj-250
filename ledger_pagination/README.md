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
python3 -m ledger_pagination paginate examples/sample_input.json   # 显式子命令
```

原有入口与页格式不变；不写子命令时仍按分页处理。

## 测试

```bash
python3 tests/run_tests.py          # 无 pytest 依赖；文件同时兼容 pytest
```

- 小数据枚举分割（1200 种子 + 扩展 4000 种子）与暴力枚举对照：组合页容量、
  续行、引注共用、固定决胜（同页数字典序最小）逐一核对；
- 长清单（20 万 / 100 万明细，单页可容纳数千行）：核对线性探测计数与耗时，
  验证没有逐一生成每个合法整页区间后再选页。

- `trace-sources`：与 `tests/sources_ref.py` 的显式列表参考实现随机对照 1500 例；
  批量调拨（2 万收货批次 × 200 次整批换仓）核对没有逐批遍历/复制。

---

# 领退料来源明细（`trace-sources`）

从仓库单据版本重建一份可供关账核对的**领退料来源明细**：每张领用单/退料单
各一行，列出数量来自哪些**收货批次**。分页代码与页格式完全不受影响。

```bash
python3 -m ledger_pagination trace-sources examples/sample_sources_input.json
```

## 输入

```json
{
  "business_cutoff": 100,
  "entry_cutoff": 100,
  "versions": [
    {"doc_id": "R1", "entry_time": 10, "type": "receipt", "business_time": 10,
     "material_id": "M", "warehouse_id": "W1", "quantity": 5},
    {"doc_id": "T1", "entry_time": 30, "type": "transfer", "business_time": 30,
     "material_id": "M", "warehouse_id": "W1", "to_warehouse_id": "W2", "quantity": 6},
    {"doc_id": "X1", "entry_time": 70, "type": "return", "business_time": 50,
     "material_id": "M", "warehouse_id": "W2", "quantity": 3, "issue_doc_id": "I1"},
    {"doc_id": "R3", "entry_time": 95, "void": true}
  ]
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `business_cutoff` | 业务截止时点 | 整数，**包含**该时点 |
| `entry_cutoff` | 录入截止时点 | 整数，**包含**该时点 |
| `versions` | 单据版本列表 | 可乱序 |

普通版本：`doc_id`（非空字符串，稳定单据 ID）、`entry_time`（整数录入时点）、
`type`（`receipt` / `transfer` / `issue` / `return`）、`business_time`（整数业务时点）、
`material_id`、`warehouse_id`（非空字符串）、`quantity`（**正整数**）。
`transfer` 另需 `to_warehouse_id`（且必须不同于 `warehouse_id`）；`return` 另需
`issue_doc_id`。撤销版本只需 `doc_id`、`entry_time` 与标记：`"void": true`
（也接受 `cancelled` / `canceled` / `revoked` 布尔标记；或 `"type": "void"`）。

### 版本选择

1. 同一单据同一 `entry_time`：内容相同只算一次；内容不同即 `version_conflict`。
2. 只看 `entry_time <= entry_cutoff` 的版本，取 `entry_time` 最大的那个为准（最新版）。
3. 若最新版是撤销版本，该单据整个不参与。
4. 选定版本若 `business_time > business_cutoff` 也不参与——**即使该单据更旧的版本
   在业务截止之前**。
5. 因此迟录的更正只影响录入截止在其之后的查询，不影响更早录入截止的结果。

`entry_time > entry_cutoff` 的版本根本不可见，不做字段校验。

### 参与与顺序

参与单据按 `(business_time, doc_id)` 升序处理，`doc_id` 按字符码点比较。
库存从零开始；材料必须在本输入中有**参与的**收货单作为依据，否则报错。

## 规则

- 收货单本身就是来源批次，进入本仓可用库存。
- 调拨从发出仓按到达顺序依次扣足，扣出的各批按原次序进入目的仓；**调拨号不是来源批次**。
- 领用按到达顺序 FIFO 扣货，并保留扣货顺序。
- 退料必须引用仍然有效、排在它之前、同仓同物料的领用单；从该领用单尚未退回的
  扣货序列**末尾**取数量（同一领用单可分多次退）。退回的批次保留原扣货序列中的
  相对顺序，接到当前仓可用库存的**末尾**：不挑当前货架上的批次，也不插回原收货位置。
- 输出行的来源按收货单 ID 排序、同一收货单合并；领用为正、退料为负，来源数量之和
  等于该行 `quantity`。退料另列 `issue_doc_id`，**不回改**原领用行；旧单据的来源
  明细永不因后续领用而改变。

## 结果

成功（`status: "ok"`）：

```json
{
  "status": "ok",
  "business_cutoff": 100,
  "entry_cutoff": 100,
  "details": [
    {
      "doc_id": "I1", "business_time": 40, "entry_time": 80, "type": "issue",
      "warehouse_id": "W2", "material_id": "M", "quantity": 5,
      "sources": [{"receipt_doc_id": "R1", "quantity": 5}]
    },
    {
      "doc_id": "X1", "business_time": 50, "entry_time": 70, "type": "return",
      "warehouse_id": "W2", "material_id": "M", "quantity": -3,
      "issue_doc_id": "I1",
      "sources": [{"receipt_doc_id": "R1", "quantity": -3}]
    }
  ],
  "stats": {"documents": 8, "receipts": 2, "transfers": 1, "issues": 3,
            "returns": 2, "nodes_created": 10, "split_calls": 6,
            "merge_calls": 3, "leaf_visits": 4}
}
```

`details` 只含参与的领用/退料单，按 `(business_time, doc_id)` 排序；没有则为 `[]`。
完整可复跑样例：`examples/sample_sources_input.json` →
`examples/sample_sources_expected.json`。

失败（退出码 1，**不输出任何部分明细**）：

- `status: "invalid"` + `errors: [...]`：字段非法（`input_index` 定位输入位置；
  能读出身份时附 `doc_id`/`entry_time`；身份本身读不出时给 `input_index`）、
  `version_conflict`。
- `status: "error"` + `error: {...}`：重放失败。`error` 含 `code`、`message`、
  `doc_id`、`business_time`、`entry_time`、`input_index`，按需附
  `warehouse_id`/`material_id`/`requested`/`available`/`referenced_doc_id`/`reason`。
  代码：`insufficient_stock`（出库不足）、`no_receipt_evidence`、
  `invalid_return_reference`、`over_return`（累计超退）。

## 算法与复杂度

每个 `(仓库, 物料)` 的可用库存是一条**到达序 FIFO 队列**，用隐式 treap 存
"批次贡献块"，每块挂节点数量、子树数量合计：

- 领用/调拨按数量切分 treap（`split` 按子树数量定位），**不逐件展开**；
  一次切分只在边界批次处拆出一个新块，长度与批次数量无关。
- 整批换仓时目的仓为空则直接链接根节点，`O(1)`；否则 `merge` 为 `O(log 批次数)`，
  **不遍历、不复制整份批次清单**——这正是"混有许多收货批次连续整批换仓、末端才领用"的场景。
- 退料按数量从领用单扣货序列末尾取（同样是按数量取尾），再以 `O(尾块数)` 重建
  一个小 treap 接到库存末尾。
- 只有实际产生来源明细的领用/退料行才遍历其批次；`stats.leaf_visits` 可核对这一点。

总代价 `O((单据数 + 批次块数) log 批次数)`。

## 生成式验证（不提交大数据）

```bash
python3 examples/verify_sources.py --bulk          # 批量调拨开销
python3 examples/verify_sources.py --random 2000   # 随机对照 tests/sources_ref.py
```

`--bulk` 生成"多批次 + 整批连续换仓 + 末端领用"，打印耗时与
`leaf_visits`/`split_calls`：跳数从 0 增到 400，`leaf_visits` 恒等于批次数，
证明每多一道调拨不会遍历或复制批次清单。随机样例由内置生成器现场产生，
仓库中只保留生成脚本，不提交大数据文件。

---

# 盘点差异折算（`reconcile-counts`）

财务拿到的是**同一关账时点**的账面余额，而实盘是各库在不同时刻分别盘出来的。
两者之间货物还在两库间流转，所以不能拿各库当时的实盘数直接减账面数——要先把实盘数
折算到关账时点，再作差。`reconcile-counts` 读取移动证据（`journeys`），给出
逐仓逐物料、逐物料的盘点差异表。分页与 `trace-sources` 的输入、输出完全不变。

```bash
python3 -m ledger_pagination reconcile-counts examples/sample_reconcile_input.json
python3 -m ledger_pagination reconcile-counts < examples/sample_reconcile_input.json
```

## 输入

```json
{
  "close_time": 100,
  "counts": [
    {"warehouse_id": "E", "material_id": "M", "count_time": 80, "counted": 14, "book": 10}
  ],
  "journeys": [
    {"journey_id": "J1", "material_id": "M", "quantity": 4,
     "route": ["E", "W"], "windows": [[90, 110]]}
  ]
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `close_time` | 关账时点 | 整数，全表共用 |
| `counts[].warehouse_id` / `material_id` | 要核对的仓、物料 | 非空字符串；同一组合只能出现一次 |
| `counts[].count_time` | 该库实盘时刻 | 整数（可早于或晚于 `close_time`） |
| `counts[].counted` | 实盘数 | 非负整数 |
| `counts[].book` | 同一关账时点的账面数 | 非负整数 |
| `journeys[].journey_id` | 移动证据 ID | 非空字符串、唯一 |
| `journeys[].material_id` | 物料 | 非空字符串 |
| `journeys[].quantity` | 该批实物数量 | 正整数，一路不变 |
| `journeys[].route` | 按发生顺序经过的站，至少两项 | 仓库 ID 或 `null`（在途/库外），相邻两项不同；允许回到经过的库 |
| `journeys[].windows[i]` | 第 i 次换站的时刻窗口 | 含两端的整数区间 `[earliest, latest]`；项数恰为 `len(route)-1` |

两个数组的记录不必按时间排序；输入顺序不影响结果。

## 折算与差异

一次换站有一个整数实际时刻；同一条记录的换站时刻**不得逆序**，可以同刻。
同刻的换站按路线顺序全部完成后，才认定该刻的实物所在地。首次换站前在起点，
最后一次以后在终点。不同记录的时刻各自独立取值。

给定一套实际时刻，一条盘点行的关账实物数为

```
reconciled = counted + quantity × (关账时是否在该库 − 实盘时是否在该库)
```

实盘发生在关账之后时，同一公式向回折算。差异 `= reconciled − book`。
折算数为负照实保留；实盘与移动证据对不上时，也不据此删掉任何符合路线与窗口的时刻。

只有时间范围、没有确切时刻的换站**不替仓库猜**：报告给出所有可行时刻下能达到的
最小值和最大值。若某行的最小值与最大值来自不同的时刻组合，这是正常的——它们各自都
有可行的时刻可以取到。

## 输出

```json
{
  "status": "ok",
  "close_time": 100,
  "rows": [
    {
      "warehouse_id": "E", "material_id": "M",
      "count_time": 80, "counted": 14, "book": 10,
      "reconciled_min": 10, "reconciled_max": 14,
      "difference_min": 0,  "difference_max": 4
    }
  ],
  "material_totals": [
    {
      "material_id": "M",
      "counted_total": 38, "book_total": 30,
      "reconciled_min": 34, "reconciled_max": 34,
      "difference_min": 4,  "difference_max": 4
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `rows[]` | 每个盘过的「仓库 × 物料」一行；按物料 ID、仓库 ID 的字符码点升序 |
| `rows[].reconciled_min` / `_max` | 折算到关账时点的实物数的最小值 / 最大值 |
| `rows[].difference_min` / `_max` | 折算实物数减 `book` 的最小值 / 最大值 |
| `material_totals[]` | 每个**有盘点行**的物料一行；按物料 ID 字符码点升序 |
| `material_totals[].counted_total` / `book_total` | 本次实盘各仓实盘数合计 / 账面数合计 |
| `material_totals[].reconciled_min` / `_max` | **同一套移动时刻**下合计折算数的最小值 / 最大值 |
| `material_totals[].difference_min` / `_max` | 合计折算数减 `book_total` 的最小值 / 最大值 |

关键点：合计行按同一套移动时刻求边界，**不把各仓各自的上下界相加**。例如一批 4 件可能
在关账前或关账后由东库转去西库，东库在它离开前盘了 14 件、西库在它到达后盘了 24 件：
两库折算数分别为 10–14、20–24，但合计一定是 34。没有盘点行的仓库不会凭空生成一行。
同一物料若有多批移动，各批时刻互相独立，但同一批在一次合计中只取一个时刻。

完整可复跑样例：`examples/sample_reconcile_input.json` →
`examples/sample_reconcile_expected.json`（上表即该样例 M 物料的实际结果）。

## 失败

非法输入（字段类型/范围不对、组合重复、`journey_id` 重复、路线或窗口结构不对）返回
`status: "invalid"` 与 `errors`，每条带 `input_index`（能读出身份时附
`warehouse_id`/`material_id`/`journey_id`）；重复组合/重复 ID 给出全部 `input_indices`。

只要有**任一条整条路线**找不到一组符合先后关系与全部窗口的实际时刻（即使它不产生任何
差异），命令返回 `status: "error"`、`error.code = "infeasible_route"`，并给出
`journey_id`、`input_index`、`route`、`windows` 与受阻的换站序号。两种情况都不输出部分
差异表，进程退出码为 1。

## 算法与复杂度

把窗口的左端取前缀最大得到 `E[i]`（第 i 次换站最早可能的时刻），右端取后缀最小得到
`B[i]`（最晚可能时刻）。整条路线可行 ⇔ 每个 i 都有 `E[i] ≤ B[i]`；当刻已完成换站数的
可达范围是区间

```
L(t) = #{ i : B[i] ≤ t },   P(t) = #{ i : E[i] ≤ t }
```

且任意满足 `L(t) ≤ ℓ(t) ≤ P(t)` 的单调选择都能由某套可行时刻实现。于是对一条记录，
逐仓折算的极值只需比较「早时刻」「晚时刻」两个位置的有无（常数次二分查找）；合计极值
则对一个位置数（换站数 + 1）的区间做一次单调动态规划：

```
dp_i(p) = 权重_i(p) + 前一台时刻在 p' ≤ p 上的最优值
```

- 不按刻扫描时间窗口（窗口可以长到 10^15）；
- 不枚举时刻组合（换站多时组合是指数级）；
- 不按件展开数量（数量是整数，只参与加减）。

单条记录的合计代价为 `O(换站数 × 查询时刻数)`，且查询时刻只是各行实盘时刻与关账时刻，
去重后不超过「该物料的盘点行数 + 1」。`rows` 只遍历「确实经过该仓」的记录，与全表
记录总数无关。

## 测试与生成式验证

```bash
python3 tests/run_tests.py test_reconcile       # 单元与随机对照测试
python3 examples/verify_reconcile.py --random 1500 --scale
```

`--random` 与 `tests/reconcile_ref.py` 的穷举参考实现随机对照（参考实现逐套时刻枚举、
逐物料取笛卡尔积）；`--scale` 用 10^15 宽的时间窗口、2000 次换站的长路线核对：
不按刻扫描、不枚举时刻组合，耗时与窗口宽度无关。

---

# 交接装箱清单（`handoff-manifests`）

发货台把装箱、拆封和交接的扫描记录整理成收货方可逐件点收的清单。外箱里还能套周转箱；
只扫了外箱换到另一托盘，不代表里面的每件货都重新扫过。本命令按实际交接时的装载关系和
封签生成 JSON 清单。分页、`trace-sources`、`reconcile-counts` 的入口与格式完全不变。

```bash
python3 -m ledger_pagination handoff-manifests examples/sample_manifests_input.json
python3 -m ledger_pagination handoff-manifests < examples/sample_manifests_input.json
```

## 输入

```json
{
  "objects": [
    {"id": "T1", "kind": "container"},
    {"id": "I1", "kind": "item", "material_id": "M-01"}
  ],
  "events": [
    {"seq": 2, "type": "move", "changes": [{"id": "I1", "parent_id": "T1"}]},
    {"seq": 5, "type": "seal", "id": "T1", "seal_id": "S-100"},
    {"seq": 9, "type": "handoff", "id": "T1"}
  ]
}
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `objects[].id` | 对象 ID | 非空字符串，目录内唯一 |
| `objects[].kind` | 对象种类 | `"container"` 或 `"item"` |
| `objects[].material_id` | 货件物料 | 非空字符串；`kind` 为 `item` 时必填 |
| `events[].seq` | 扫描序号 | 非负整数，全表唯一；**以 `seq` 值为准**，数组位置不代表先后 |
| `events[].type` | 记录类型 | `move` / `seal` / `open` / `handoff` |

每个货件 ID 对应一件实物；容器可装货件或其他容器，货件不能再装东西。起初所有对象都在
现场、没有上级容器，也没有封签。

### `move`：一组同时生效的改挂

`changes` 为非空数组，每项 `{id, parent_id}`；`parent_id: null` 表示落地独立放置。
组内每个对象最多改挂一次，且确实要换上级。整组表示**同一次搬运**：只承认整组之前和之后
的装载关系，最终不能出现循环——例如 B 原装在 A 里，同一条记录让 B 落地、A 装进 B，只要
没有触碰封签，就不应因 `changes` 的排列方式被判成循环。

- 移出的旧容器或其任何上级在搬运**前**若已封签，就不能从那里取物（`sealed_source`）；
- 装入的新容器或其任何上级在搬运**后**若已封签，就不能往那里装物（`sealed_destination`）；
- 已封签的箱子本身可以作为整体搬动，不得因此改变箱内关系。

### `seal` / `open`：加封与拆封

带容器 `id` 及 `seal_id`。加封时该容器须未加封；开封必须与当前封签相符，开封后不再有
活动封签。如果容器在另一只已封签容器内，两种操作都做不了（`sealed_ancestor`）。

### `handoff`：交接离场

带容器 `id`。只有独立放置且仍带活动封签的容器能交接。交接后，箱及当时里面所有对象均已
离场，后面的扫描不得再操作它们，包括作为改挂目的地（`departed_object`）。

## 输出

成功（`status: "ok"`）：

```json
{
  "status": "ok",
  "manifests": [
    {
      "seq": 9,
      "id": "T1",
      "objects": [
        {"id": "I1", "kind": "item", "parent_id": "T1", "material_id": "M-01"},
        {"id": "T1", "kind": "container", "parent_id": null, "seal_id": "S-100"}
      ]
    }
  ],
  "stats": {"objects": 2, "events": 3, "moves": 1, "seals": 1, "opens": 0,
            "handoffs": 1, "change_details": 1, "manifest_objects": 2,
            "accesses": 5, "links": 1, "cuts": 0, "root_queries": 1,
            "splays": 11, "splay_rotations": 0}
}
```

| 字段 | 含义 |
|---|---|
| `manifests[]` | 每份交接清单；按交接 `seq` 递增；没有交接时为 `[]` |
| `manifests[].seq` / `id` | 交接记录的 `seq` 与交接容器 ID |
| `manifests[].objects[]` | 交接容器连同当时全部后代；按 `id` 字符码点升序 |
| `objects[].parent_id` | 交接当时的上级容器；交接根为 `null` |
| `objects[].material_id` | 货件的物料 ID |
| `objects[].seal_id` | 容器当时的活动封签；开着的容器为 `null` |

完整可复跑样例：`examples/sample_manifests_input.json` →
`examples/sample_manifests_expected.json`（样例里事件数组故意不按 `seq` 排列，
并含"B 出 A、A 进 B"的同组互换与已封签整箱搬动）。

## 失败

失败时退出码为 1，**不输出任何部分清单**：

- `status: "invalid"` + `errors: [...]`：目录或记录结构非法（字段类型不对、`kind`
  未知、货件缺 `material_id`、对象 ID 重复、`seq` 缺失/非法/重复、`changes` 为空、
  `parent_id` 键缺失等）。每条带 `input_index`；`seq` 读得出时一并给出，重复 ID/seq
  给出全部 `input_indices`。
- `status: "error"` + `error: {...}`：记录结构合法但违反搬运规则。`error` 含 `code`、
  `message`、`seq`、`input_index`，并按需附 `object_id` / `parent_id` /
  `container_id` / `sealed_container_id` / `seal_id` / `current_seal_id`。代码：
  `unknown_object`、`departed_object`、`not_a_container`、`item_as_parent`、
  `duplicate_change`、`no_change`、`sealed_source`、`sealed_destination`、`cycle`、
  `already_sealed`、`not_sealed`、`seal_mismatch`、`sealed_ancestor`、`not_standalone`。

## 算法与复杂度

装载关系用**无翻转 link-cut 树**维护（只把整棵子树的根挂到新父节点下，无需 lazy 翻转），
在每条优选路径上聚合"已封签容器数"：

- 一次整箱改挂 = 一次 `cut` + 一次 `link`，箱内成千上万件货**不展开、不复制**；
- "上级链上是否有封签"是一次路径聚合查询（`access` 后读根聚合值），**不回查整条祖先链**，
  与嵌套深度无关；
- 同组多条改挂：先按改挂前状态逐条查源链封签，再全部 `cut`、逐条 `link`（挂接目标已在
  被搬子树内即成环），最后按改挂后状态逐条查目标链封签；任一步失败即整体回滚该组；
- 交接时枚举该箱当时全部后代并标记离场——只有**实际列进清单的对象**才有这次枚举。

每条改挂明细与每次封签操作的摊还成本为 `O(log n)`（优于要求的对数平方），
`stats.splay_rotations` 可核对：深度从 2 千增到 3.2 万时，每条明细的旋转数恒约 2.1。
整个输入按 `seq` 排序不计入该项。

## 测试与生成式验证

```bash
python3 tests/run_tests.py test_manifests       # 单元、样例与随机对照测试
python3 examples/verify_manifests.py --random 2000
python3 examples/verify_manifests.py --scale
```

`--random` 与 `tests/manifests_ref.py` 的显式参考实现（父指针数组 + 逐链回查）随机对照，
合法会话逐清单全等、非法会话错误码一致；`--scale` 生成"深嵌套 + 单箱两万货件 +
连续整箱改挂"的场景，核对每条改挂明细的摊还成本不随嵌套深度增长。
