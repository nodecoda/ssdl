# SSDL 2.0 文法评审（编译器视角）

> 本文写于 2026-11-17，是 v2 文法的评审记录。文中的 `to_v1` 现名
> `to_ir`（2026-11-18 命名收口），内容保留当时的判断。


- date: 2026-11-17
- reviewer: compiler-engineering review
- base: `0e961fb` + 本轮修复
- 验收：`check.py` 118/118、`golden.py --check` 23/23、
  `family_sweep.py --syntax v2` 14/14

## 0. 总评

v2 的方向是对的：独立 DSL、语义优先、`kind` 进入声明、字段体统一、
host 边界显式。它已经不再是"TD 的可读包装"，而是可以独立演进的领域语言。

仍然"变味"的地方集中在两类：

1. **类型只停留在最外层**。实体字段、谓词参数、`to_v1` 边界都在用字符串，
   类型检查发生在很晚的 lower/parse 阶段，错误位置还会跳回 v1 行号。
2. **同一语义有多个语法位置**。`kind` 有四种放法，嵌套块有两种拼法，
   `feature` 的 `kind` 还能写两遍。

下面按"本轮已修 / 已撤回 / 仍存在"分开写，避免把旧结论当现状。

---

## 1. 本轮已修（有回归测试）

| # | 问题 | 修法 | 证据 |
|---|---|---|---|
| 1 | 无维度的 family 不展开：`machine family f(n: INT)` 被当成普通 machine | `FAMILY_SOURCE` 改为识别参数化 `machine` 头与 `machine N : F(...)`，不再依赖 `kind = dimension` | `family_without_dimensions_is_still_expanded` |
| 2 | 规范 §1.1 的 `-` 规则自相矛盾（规则说"后接 ident_start"，例子却把 `sifive-7-series` 当单标识符） | 确认实现（`-` 恒为标识符字符）才是可用的上下文无关规则；重写 §1.1，并说明"名字位置 vs 值位置" | `hyphen_is_an_identifier_character_not_a_lexical_operator` |
| 3 | `map` 的值接受任意表达式，错误拖到 v1 parse | 值位置只接受 `sched_rw`（write/read 名 + 可选维索引），前端报 v2 位置 | `v2_map_and_retire_unit_are_checked` |
| 4 | `retire_unit` 可重复，会发射两个同名 def | 每个 machine 至多一个 | 同上 |
| 5 | `stage.index` / `stage.latency` 名不符实 | 改名为 `cycles` / `timeinc`，与 `InstrStage` 和 v1 对齐；旧名报"已改名"错误 | `v2_stage_fields_are_named_and_checked` |
| 6 | 谓词位置的 `none_of` 前端放行、validate 才报 unknown predicate | 前端直接拒绝，并提示改用 `all_of(not(P))`；`select during` 不受影响 | `v2_predicate_forms_are_checked` |
| 7 | §6.2 表头重复、表线少一列；§7.1 漏 `<=`/`>=` | 修文档 | 文档 diff |
| 8 | `itin.cycles` 语义未写明，容易被误读成"每级 stage 的周期" | 在 §6.6 写明它是 operand cycles：元素 0 是结果可用周期，长度与 stages 无关 | 文档 diff |

第 5 条是本轮最重要的命名修正。字段名必须描述**语义**，不能描述**位置**：
`index` 听起来像序号，实际是这一级占用的周期数；`latency` 听起来像结果延迟，
实际是进入下一级前的 time increment。这类错误不会让测试变红，但会让 DSL
重新长出"靠注释才能读懂"的 TD 味道。

---

## 2. 撤回的两条旧结论（本轮复验为误报）

诚实记录，避免后续规划建立在错误前提上。

1. **`resource.name` 不是死字段**。`resource P { name = "X"; }` 会改名并
   重写所有引用（`bind W -> [X]`）。这是 v1.3 的 name template 语义，已实现。
2. **`itin.cycles` 与 `stages` 数量不必相等**。`TargetItinerary.td` 的
   `OperandCycles` 是"结果/操作数可用周期"，与 stage 数无关；`[3, 1]` 配
   1 个 stage 合法。曾短暂加入的"数量必须相等"不变量已撤回。

这两条说明：评审结论必须回到 LLVM 的类定义和真实 fixture，不能只看 v2 文档。

---

## 3. 仍存在的问题

### P1：类型没有进入中间表示

类型的"存在感"目前只在三处：`predicate` 参数 `INT/BOOL`、`feature` 的
`bool/int/string`、以及 `bind` 等字段的 `int_expr`。再往里走就是字符串：

- 实体字段的值是渲染后的文本。`resource P { units = "two"; }` 能通过 v2
  前端，变成 `resource P : units = "two";`，然后在 v1 parse 时报
  `expected integer`，行号还是 v1 的。
- `validate.validate()` 只做 sort 检查。`is_reg(true)`、`num_ops("x")` 返回
  空列表；真正的 arity/type 检查在 `predicates.py` 的 lower 阶段，报错位置
  是 `lowering`，参数以 Python tuple 形式出现（`('bool', True)`）。
- `to_v1()` 返回纯文本，没有 source map，所以任何落到 v1 的错误都无法映射
  回 v2 行列。

建议：建立一张**实体字段类型表**（`int / int_list / bool / ident / enum`），
由 `syntax_v2` 和 `validate` 共用；字段值在进入 `Decl.text` 前完成类型检查。
谓词的签名表同理，直接从 `predicates.py` 的 lower 分支抽出来，前置到前端。

### P1：`to_v1` 是文本接缝

管线是 `v2 → v1 文本 → AST → emitter`。这带来三个具体后果：

1. 错误位置漂移（见上）。
2. `SemProgram` 的 `Decl.text` 是实际契约，`Symbol`/`Phase` 只是旁路标注。
3. 任何 v2 的语法信息（可选字段来自哪里、默认值是否显式写出）在接缝处丢失。

短期：让 `to_v1` 返回 `(text, line_map)`，或在 `parse_file(syntax="v2")`
里直接产出带 v2 位置的 AST。
长期：让 `emit`/`diagnose` 消费 `SemProgram` 而不是重新 parse 文本，
文本只作为调试输出。

### P2：`kind` 有四种放法

现状：

```text
set instructions X = ...;      # 关键字后
machine family F(...) { ... }  # 关键字后
predicate P : select { ... }   # 名字后，冒号
feature F : extension { ... }  # 名字后，冒号
feature F { kind = extension; ... }  # 体里，还能与头部重复
```

同一个"声明种类"占用了两个语法位置、两种标点，`feature` 还有第三种写法。
建议统一为 `<keyword> <kind> NAME`（与 `machine family` 一致），并删除
`feature` 体内的 `kind =`。这样读到第二个 token 就知道这是什么声明。

### P2：嵌套块有两种拼法

`projections = { ... }`（字段 = 块）与 `bitmask { ... }`（字段 块）并存。
按"字段 = 值"的总规则，统一成 `bitmask = { group = 0; bit = 1; }`。

### P2：`predicate` 承担五个概念

`sched` / `select` / `sti` / `tii` / `prolog` 共用一个关键字。Phase 5 的
records 对比已通过，说明**语义上可行**；但它与"一个概念一个关键字"相冲突。
两个选项：

- 接受这是有意的"谓词家族"，但把关键字改名为 `hook`，减少"predicate 还能
  定义 STI 实现"的意外感；
- 或拆回 `predicate` / `select` / `sti` / `tii` 四个顶层关键字。

不建议现在改；等其余语法稳定后再做一次性迁移。

### P2：剩余规范/实现漂移

- `set` 范围允许负端点（`-2..2` 可解析），规范写的是无符号 `INT`。要么
  收紧实现，要么在 §3 写明"端点可带负号"。
- `is_reg(-1)` 能通过 lowering；操作数索引是否允许负数应明确（建议拒绝）。
- §7.1 的属性比较已补 `<=`/`>=`；实现与文档现在一致。

### P3：`feature` 的类型只在前端

`bool IsRV64 = true;` 与 `value = "true"` 在 `Decl.text` 里不可区分。对 TD
没有影响（值本来就是字符串），但应明确写"类型只在 v2 前端做检查"，避免
读者误以为中间表示保留了类型。

---

## 4. 建议路线

**短期（低风险，已具备条件）**

1. 实体字段类型表 + 前端检查（P1）。
2. 谓词签名表进 `validate`，错误位置用 v2 行列（P1）。
3. `bitmask = { ... }` 统一（P2）。
4. 范围负号、`is_reg(-1)` 二选一（P2）。

**中期（一次语法决策）**

5. `kind` 位置统一为 `<keyword> <kind> NAME`，删除 `feature` 体内 `kind`。
6. `to_v1` 带 source map，或 v2 直接产 AST。
7. 决定 `predicate` 是改名 `hook` 还是拆关键字。

**长期（重新权衡，不必现在做）**

8. `SemProgram` 取代 `Decl.text` 成为 emit/diagnose 的输入。
9. 若未来要生成非 TD 后端，类型化 IR 是前提；现在不做也可以，但要
   诚实标注"文本是接缝"。

**明确不做**

- 重新引入 TD 语法噪音（`let`、`!cast`、`NAME #` 拼接）。
- 参数参与标识符拼接。
- 把 RUNTIME 值放进结构位置。
- 为"上游兼容"牺牲语义表达。

---

## 5. 验证记录

```text
check.py                 118/118
golden.py --check        23 expansions match (0 model entries skipped)
family_sweep --syntax v2 14/14 match
```

新增回归：family 无维度展开、`-` 词法、`map` 值类型、`retire_unit`
唯一性、stage 字段改名、`none_of` 前端拒绝。
