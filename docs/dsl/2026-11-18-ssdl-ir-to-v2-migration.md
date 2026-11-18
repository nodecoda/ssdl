# IR → v2 迁移表与工具

**状态**：Phase 6 完成。v2 是唯一表面语法，v1.3 是语义基线。
自 2026-11-18 起，下表左列（原 v1）不再是可写语法，只作为 IR 文本存在；
`--syntax ir` 读它，`to_v2.py` 把它翻成表面源。见
`2026-11-18-ssdl-one-surface.md`。
**工具**：`tools/ssdl/to_v2.py`（v1 → v2），`syntax_v2.to_v1`（v2 → v1）。
**证据**：14/14 模型 `family_sweep.py --syntax v2` records match；
`tests/ssdl/surface/sifive7-mini.ssdl` 与手写 `.td` 逐字段一致；
11 对 v1/v2 fixture 的 `SemProgram` 相同。

---

## 1. 两条命令

```bash
# 单个文件
python3 tools/ssdl/to_v2.py old.ssdl -o new.ssdl
python3 tools/ssdl/elaborate.py new.ssdl --syntax v2

# 14 模型门禁（mining 仍产出 v1，再迁移到 v2）
python3 tools/ssdl/family_sweep.py --syntax v2 --out /tmp/ssdl-sweep-v2
```

`validate.py` / `emit_td.py` / `elaborate.py` / `family_sweep.py` 都有
`--syntax v2|ir`；`records.json` 与 `llvm-tblgen -print-records` 是唯一外部真值。

---

## 2. 迁移表

| v1 | v2 | lowering |
|---|---|---|
| `set D = [...] : kind = dimension, units = [...];` | `set dimension D = [...] { units = [...]; }` | 展开为成员名 |
| `set D = ... : kind = dimension, div_factor = [...];` | `set dimension D = ... { projections = { div_factor = [...]; } }` | `D.div_factor` 查表 |
| `set R : kind = relation = [...];` | `set relation R = [...];` | 编译期关系 |
| `set I = [A, B];` | `set instructions I = [A, B];` | 编译期集合 |
| `machine F(p: INT) { ... }` | `machine family F(p: INT) { ... }` | 族模板 |
| `machine X : F(512) { ... }` | `machine instance X : F(512) { ... }` | 实例 |
| `machine X { ... }` | `machine both X { ... }` | model + processor（v1 默认） |
| `machine X = Y;` | `machine alias X = Y;` | `ProcessorAlias` |
| `resource P : units = 2, buffer = 4;` | `resource P { units = 2; buffer = 4; }` | `ProcResource` |
| `res_group G = [A, B] : buffer = 2;` | `res_group G = [A, B] { buffer = 2; }` | `ProcResGroup` |
| `bind W -> [P], latency = 2, release = [3];` | `bind W { resources = [P]; latency = 2; release = [3]; }` | `WriteRes` |
| `advance R : cycles = 1 : writes = [W];` | `advance R { cycles = 1; writes = [W]; }` | `ReadAdvance` |
| `map [ADD] -> [W, R];` | `map [ADD] { 0 = W; 1 = R; }` | `InstRW`（下标即操作数位置） |
| `write_seq S = [W] repeat = 2;` | `write_seq S = [W] { repeat = 2; }` | 保留（未 lowering） |
| `load_queue : resource = P;` | `load_queue { resource = P; }` | `LoadQueue` |
| `feature X : kind = extension, version = "2.1";` | `feature extension X { version = "2.1"; }` | `SubtargetFeature` + 扩展表 |
| `feature X : ..., bitmask = { group = 0, bit = 8 };` | `feature X { ... bitmask { group = 0; bit = 8; } }` | 位图探测 |
| `feature X : kind = plain, field = "F", value = "true";` | `feature plain X { bool F = true; }` 或 `host { field = "F"; value = "true"; }` | `SubtargetFeatureKV` |
| `predicate P = all_of(...);` | `predicate P = all_of(...);` | `MCInstPredicate` |
| `predicate P = sched_code([{...}]);` | `predicate P = sched_code(host [{...}]);` | C++ 代码片 |
| `predicate P { selector = cxx_expr([{...}]); assembler = ...; }` | `predicate select P { when = host [{...}]; during = ...; }` | `Predicate` + `AssemblerPredicate` |
| `predicate_prolog([{...}]);` | `predicate prolog = host [{...}];` | `PredicateProlog` |
| `sti_decl X { ... }` + `sti_function F : X { sched_model = M; ... }` | `predicate sti X { ... impl F : M { ... } }` | `STIPredicateDecl` + `STIPredicate` |
| `tii_predicate X = switch opcode { ... };` | `predicate tii X { switch opcode { ... } }` | `TIIPredicate` |
| `itineraries : sched_model = M : packetizer_namespace = "N" { ... }` | `itineraries : packetizer_namespace = "N" { sched_model = M; ... }` | `ProcessorItineraries` |
| `stage(1, [FU], 2, reserved)` | `stage { cycles = 1; units = [FU]; timeinc = 2; kind = reserved; }` | `InstrItinData` |

---

## 3. 迁移器的诚实边界

1. **注释不保留**。`to_v2` 以语句为单位（复用 `family_expand` 的扫描器），
   `#` / `/* */` 在输出中丢失。语义不变；需要注释的源请手工补回。
2. **`machine both` 会显式化默认值**。v1 的裸 `machine X { ... }` 在 v2
   里必须写角色；回到 v1 时会多一条 `role = both;`。它是默认值，records
   不受影响，但逐字节比较展开文本时要去掉这一行。
3. **TII 裸 `return` 没有 v2 写法**。`tii_predicate X = return P;` 无法迁移；
   `tests/ssdl/ir-only/errata.ssdl` 因此保留为 IR 套件。
4. **只搬语法，不做语义解析**。`to_v2` 不解析谓词、不求值表达式、不展开
   维度；它是 `to_v1` 的逆变换。任何需要“理解”的构造都应先扩 v2 文法。
5. **行号不保留**。迁移后的错误定位指向 v2 文件，不是原 v1 行。

---

## 4. 门禁

| 门禁 | 结果 |
|---|---|
| `family_sweep.py --syntax v2`（14 模型 × 两条对拍路径） | 14/14 `records match` |
| `to_v2` → `to_v1` → 展开，14 个 sweep 源逐字节相同 | 14/14 |
| 8 个 family fixture 往返展开相同 | 8/8 |
| 11 对 v1/v2 fixture 的 `SemProgram` | 全部相同 |
| `docs/examples/sifive7-family-v2.ssdl` 与 v1 展开逐字节相同 | 一致 |

复现：

```bash
python3 tools/ssdl/family_sweep.py --syntax v2 --out /tmp/ssdl-sweep-v2 --jobs 4
python3 tools/ssdl/check.py
```
