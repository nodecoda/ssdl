# SSDL 语法盘点（v1 → v2 迁移前）

- date: 2026-11-17
- 基线：`8b09d0c`（v1.3 完成）
- 回归基线：`check.py` 102/102；`golden.py --check` 22/22（8 fixture + 14 模型）
- 目的：在改语法之前，把“文档写的”“实现接受的”“领域需要的”三张表对齐

本文件是 v2 规划的 Phase 0 产物。它不定义新语法，只记录现状与目标差异。

## 1. 方法

三个来源逐项对照：

1. **规范**：`2026-11-16-ssdl-v1.0-final.md`（9 关键字）、
   `2026-11-17-ssdl-v1.2-draft.md`（族/维度）、`2026-11-17-ssdl-v1.3-semantic-ir.md`
   （relation/extern）。
2. **实现**：`parser.py` 的分派（`parse_machine_item:793`、`parse_feature:635`、
   `parse_predicate:587`、`parse_itineraries:998`）与 `family_expand.py` 的扫描器
   （`_scan_logical_lines:407`、`_expand_body:1202`、`_lower_runtime_bind:1128`）。
3. **兼容**：`2026-11-16-ssdl-parser-notes.md:79` 的 S1–S7 与 E1。

## 2. 顶层声明

| 构造 | 当前写法 | 规范 | 实现实际接受 | v2 目标 |
|---|---|---|---|---|
| `set`（集合） | `set X = [A, B];` / `set X = inst(cond);` / `set X = reg(cond);` | v1.0 3.2 | 三种 set_expr + `...` 展开 + range | `set instructions X = ...` |
| `set`（维度） | `set LMUL = [M1, M2] : kind = dimension, name_format = "_{value}";` | v1.2 C2 | 同上；`INT..INT` 形式 | `set dimension LMUL = [...] { name_format = "..."; }` |
| `set`（关系） | `set DivFactor : kind = relation = [(8, 1), (16, 15)];` | v1.3 AC9 | n 元键；重复/空表报错 | `set relation DivFactor = [...]` |
| `predicate`（调度） | `predicate P = all_of(...);` | v1.0 3.3 | 表达式；`true/false/not/all_of/any_of/feature/atom` | `predicate sched P = ...;` |
| `predicate`（选择/汇编） | `predicate P { selector = cxx_expr([{...}]); assembler = all_of([...]); desc = "..."; }` | v1.0 3.3 | 块形式 + 可选 `recompute_per_function` | `predicate select P { when = host {...}; during = ...; }` |
| `predicate_prolog` | `predicate_prolog([{ ... }]);` | v1.0 3.4 | 全局 C++ 片段 | `host { ... }` 统一 |
| `feature`（plain） | `feature F : kind = plain, field = "IsRV64", value = "true";` | v1.0 3.5 + E1 | 额外接受 `mattr`；未知字段按字符串/整数/布尔宽容接受 | `feature plain F { bool IsRV64 = true; mattr = "64bit"; }` |
| `feature`（extension） | `feature E : kind = extension, version = "2.1", bitmask = { group = 0, bit = 8 };` | v1.0 3.5 | 同上 + `experimental`/`implies` | `feature extension E { version = "..."; bitmask = { group = 0; bit = 8; }; }` |
| `feature`（tune） | `feature T : kind = tune, field = "...", value = "...";` | v1.0 3.5 | `positive_directive`/`negative_directive` | `feature tune T { ... }` |
| `sti_decl` | `sti_decl X { opcode_mask = true; delegates = [...]; }` | v1.0 3.8 | 空声明 `sti_decl X;` 也可 | 并入 `predicate : sti`（P2 门禁） |
| `sti_function` | `sti_function X : Decl { sched_model = M; dep_breaking_class ...; }` | v1.0 3.9 | 尾随 `;` 可选（S6） | 并入 `predicate : sti { impl ... }` |
| `tii_predicate` | `tii_predicate X = switch opcode { ... };` | v1.0 3.10 | `return` 形式分号歧义（S7） | 并入 `predicate : tii` |
| `machine` | `machine X { ... }` / `machine X = Y;` / `machine F(p: INT) { ... }` / `machine X : F(args) { ... }` | v1.0 3.11 + v1.2 C1 | 四种形态由形状决定；`IDENT` 允许 `-`（S5） | `machine model\|cpu\|family\|instance\|alias` |
| `itineraries` | `itineraries : packetizer_namespace = "X" { ... }` | v1.0 3.14 | 顶层或 machine 内；顶层必须 `sched_model` | 统一字段体 + 命名 stage 参数 |
| `extern fn` | `extern fn cycles(lmul: INT, sew: INT) -> INT : cycles;` | v1.3 AC9 | 只在族源中生效；签名进 profile lookup | 保留，语法不变 |

## 3. machine 块：属性

| 属性 | 当前写法 | 值类型 |
|---|---|---|
| `features` | `features = [F1, F2];` | ident list |
| `tune_features` | `tune_features = [...];` | ident list |
| `unsupported_features` | `unsupported_features = [...];` | ident list |
| `default_march` | `default_march = "rv64i2p1";` | string |
| `issue_width` | `issue_width = 4;` | int |
| `micro_op_buffer_size` | `micro_op_buffer_size = 192;` | int |
| `loop_micro_op_buffer` | `loop_micro_op_buffer = 8;` | int |
| `load_latency` | `load_latency = 3;` | int |
| `high_latency` | `high_latency = 10;` | int |
| `mispredict_penalty` | `mispredict_penalty = 9;` | int |
| `post_ra_scheduler` | `post_ra_scheduler = true;` | bool |
| `complete_model` | `complete_model = false;` | bool |
| `full_inst_rw_overlap_check` | `full_inst_rw_overlap_check = true;` | bool |
| `enable_intervals` | `enable_intervals = true;` | bool |

实现风险：`parse_machine_item` 对未知 key 走“machine 属性 + `expect_int`”
（`parser.py:965`），所以拼错一个实体关键字会得到 `expected int` 而不是
“unknown machine item”。v2 解析器应改为白名单 + 明确错误。

## 4. machine 块：实体

| 实体 | 当前写法 | 规范 | 实现备注 |
|---|---|---|---|
| `resource` | `resource P : super = S, units = 2, buffer = 4;` | v1.0 3.12 | 属性分隔符 `:` 与 `,` 都接受（S4） |
| `res_group` | `res_group G = [P, Q] : buffer = 4;` | v1.0 3.12 | 同上 |
| `write` / `read` | `write W;` / `read R;` | v1.0 3.12 | 可带维度索引 `write W[D];` |
| `write_seq` | `write_seq S = [W1, W2] repeat = 2;` | v1.0 3.12 | 不参与族维度展开 |
| `func_unit` / `bypass` / `itin_class` | `func_unit FU;` | v1.0 3.12 | 只有声明 |
| `combo_func` | `combo_func F = [FU1, FU2];` | v1.0 3.12 | — |
| `load_queue` / `store_queue` | `load_queue : resource = LSU;` | v1.0 3.12 | `:` 只在这里出现一次 |
| `reg_file` | `reg_file GPR { num_phys = 180; ... }` | v1.0 3.12 | 字段已用 `;` |
| `retire_unit` | `retire_unit { buffer = 320; max_per_cycle = 6; }` | v1.0 3.12 | 匿名单例 |

## 5. machine 块：关系

| 关系 | 当前写法 | 规范 | 实现备注 |
|---|---|---|---|
| `bind` | `bind W -> [P, Q], latency = 1, release = [3];` | v1.0 3.13 | 属性用 `,`；`bind W[D]` 可维度化；runtime 条件在此降级 |
| `advance` | `advance R : cycles = 1 : writes = [W];` | v1.0 3.13 | 属性用 `:`；实现也接受 `,`；`cycles` 必需 |
| `map` | `map [ADD, SUB] -> [W, read_default];` | v1.0 3.13 | 指令列表 + 位置操作数；`unsupported` 用 `:` |
| `map_itin` | `map_itin [IIC_iALU] -> [W];` | v1.0 3.13 | — |
| `alias` | `alias W -> V;` | v1.0 3.13 | — |
| `variant` | `variant W { case P: [W1]; default: [W2]; }` | v1.0 3.13 | 可维度化 `variant W[D]`；case 选择支持 `Sel[A]` |
| `read_variant` | 同 `variant` | v1.0 3.13 | 与 `variant` 只差 subject 类型 |

## 6. itineraries

| 构造 | 当前写法 | 实现备注 |
|---|---|---|
| 顶层/内联 | `itineraries [ : packetizer_namespace = "X" ] { ... }` | 顶层必须 `sched_model = M;` |
| `func_units` / `bypasses` | `func_units = [FU_ALU];` | ident list |
| `itin` | `itin IIC_iALU { stages = [...]; cycles = [1, 1]; }` | 可带 `bypasses`/`uops` |
| `stage` | `stage(1, [FU_ALU], 1, required)` | 位置参数：index, units, latency, kind |

## 7. 族/维度层（v1.2 + v1.3）

| 构造 | 当前写法 | 实现备注 |
|---|---|---|
| 族声明 | `machine F(vlen: INT, has_fast: BOOL) { ... }` | 参数只有 INT/BOOL |
| 实例 | `machine m512 : F(512, true) { ... }` | 实例 body 与族 body 合并，实例优先 |
| 维度 | `set D = [A, B] : kind = dimension, name_format = "_{value}";` | 成员名 + 可选 units/projections |
| 索引名 | `write W[D];` | 只对 `write/read/bind/advance` 头与 `variant` 生效 |
| 子集 | `Base[D in [A, B]]` | 维度子集 |
| worst | `Base[worst(D)]` | 伪成员 |
| 名称模板 | `resource P : ..., name = "PipeA_{vlen}";` | 只允许 `{param}`，展开期单射检查 |
| 数值表达式 | `latency = vlen / 128` | `+ - * / // %`、`cdiv/max/min/log2`、比较、布尔、`?:` |
| 条件折叠 | `latency = has_fast ? 4 : 7` | ELAB 条件完全折叠 |
| runtime 降级 | `latency = opcode([ADD]) ? 4 : 7` | 生成 aux writes + `variant` |
| relation 查表 | `latency = DivFactor(sew)` | 有限表；miss 报错并列出域 |
| profile 投影 | `cycles(1, sew)` | `extern fn` 签名 + JSON 钉值 |

### 7.1 支持面不对称（必须记录）

`_parse_item` 的 `ITEM_HEAD_RE` 只匹配 `write|read|bind|advance` 且要求 `[` 索引
（`family_expand.py:63`）。因此：

- `write/read/bind/advance` 可维度化；
- `variant/read_variant` 由 `VARIANT_HEAD_RE` 单独处理，可维度化；
- `map`、`write_seq`、`resource`、`reg_file`、`itineraries` 等**不能**在族 body 中
  使用 `Base[D]` 索引；它们只做数值字段替换（`_subst_fields`）。
- 这不是语义限制，是前端支持面的不一致；v2 应统一“哪些位置允许维度索引”。

## 8. 表达式与阶段

| 构造 | 写法 | 阶段 |
|---|---|---|
| 算术 | `+ - * / // %`，`/` 必须整除 | ELAB |
| 函数 | `cdiv/max/min/log2` | ELAB |
| 比较/布尔 | `== != < > <= >= && \|\| !` | ELAB |
| 三元 | `cond ? a : b` | 条件决定：ELAB 折叠，RUNTIME 仅标量字段降级 |
| 谓词原子 | `opcode/pseudo/is_reg/.../same_reg/func/func_tii/non_portable` | RUNTIME（结构性位置拒绝） |
| 组合子 | `all_of/any_of/not`（n 元，允许空） | 随上下文 |

## 9. 语法 errata（v1 兼容，v2 清零）

| ID | 旧写法 | v2 目标 |
|---|---|---|
| S1 | `predicate P(i)` 参数列表 | 显式 `predicate P(i: INT)` 或删除参数语法 |
| S2 | 命名谓词调用 `IsZeroReg(1)` | 保留为谓词调用，但写进 EBNF 并做参数检查 |
| S3 | `feature` 字段间可选 `,` | 统一字段体后自然消失 |
| S4 | `resource` 属性 `:` 与 `,` 混用 | 统一字段体 |
| S5 | `IDENT` 允许 `-` | 词法明确允许（并定义与减号的优先级） |
| S6 | `sti_function` 尾随 `;` 可选 | 语句统一以 `;` 结束 |
| S7 | `tii_predicate` 分号数量不一致 | 语句统一 |
| E1 | `feature` 需要 `mattr` 但 v1.0 未写 | `mattr` 进入正式字段 |

## 10. v2 目标映射（摘要）

| v1 | v2 |
|---|---|
| `set X = [...]` | `set instructions X = [...]` |
| `set X = [...] : kind = dimension` | `set dimension X = [...] { name_format = "..."; }` |
| `set X : kind = relation = [...]` | `set relation X = [...]` |
| `machine X { ... }`（模型+处理器） | `machine model X { ... }` + `machine cpu Y { model = X; ... }` |
| `machine F(p: INT)` / `machine X : F(...)` | `machine family F(p: INT)` / `machine instance X : F(...)` |
| `machine X = Y;` | `machine alias X = Y;` |
| `resource P : units = 2, buffer = 4;` | `resource P { units = 2; buffer = 4; }` |
| `bind W -> [P], latency = 1;` | `bind W { resources = [P]; latency = 1; }` |
| `advance R : cycles = 1 : writes = [W];` | `advance R { cycles = 1; writes = [W]; }` |
| `map [ADD] -> [W, read_default];` | `map ADD { 0 = W; 1 = read_default; }` |
| `feature F : kind = plain, field = "IsRV64", value = "true";` | `feature F { bool IsRV64 = true; }` |
| `stage(1, [FU_ALU], 1, required)` | `stage { cycles = 1; units = [FU_ALU]; timeinc = 1; kind = required; }` |
| `predicate` / `sti_*` / `tii_*` | `predicate P : sched\|select\|sti\|tii`（P2 门禁） |
| `[{ ... }]` | `host { ... }`（保留旧写法到 v1 删除） |

## 11. Phase 0 结论

1. v1 语法不是“一个文法”，而是三层叠加：v1.0 TD 类投影、v1.2 族增量、v1.3
   契约增量，外加 7 条 errata。
2. 领域概念的压缩有两处过头：`machine`（4 概念）与 `set`（4 概念）。
3. 领域概念的拆分有一处过头：谓词被拆成 5 个顶层关键字，反映的是 TD 类层次。
4. 字段分隔符有 4 种写法；v2 必须收敛为 1 种。
5. 族层支持面不一致：只有部分实体可维度化；v2 需要统一规则。
6. 语义内核（IR/阶段/拼写/runtime lowering）与语法问题无关，可以原样复用。
