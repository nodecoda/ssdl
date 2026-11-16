# SSDL 解析器与最小展开器：实现记录

- date: 2026-11-16
- 上游文档：`2026-11-16-ssdl-v1.0-final.md`（未改动）
- 上游评审：`2026-11-16-ssdl-review.md`（未改动）
- 代码：`tools/ssdl/{parser,emit_td,compare,check}.py`
- 测试：`tests/ssdl/`
- 结论：**第 5 节示例已能完整解析（19 个顶层声明）；`feature` / `machine` / `resource` / `write` / `read` / `bind` / `advance` / `map` 已能展开成真实 LLVM TableGen 记录，并与手写 `.td` 的 `-print-records` 输出逐字段一致。** 其余构造仍只解析、不展开，并给出显式诊断。

---

## 1. 一句话

这一轮把“可解析性”从断言变成了可执行检查，把“可展开性”从 `norm` 散文变成了 13 个自检；但它没有、也不应该声称 SSDL 1.0 已经语义闭合。评审里的 S1–S7 现在都有对应解析路径，M1/M4/M9/P1-10/P1-11 在**展开器范围内**闭合；v1.1 又把 M2/M3/M6/M8/M10 做成了可执行的 sort/绑定校验（仍不等于 lowering）。

---

## 2. 运行方式与结果

```bash
# 13 项自检
python3 tools/ssdl/check.py
# ok   spec_example_checked_in_copy_matches_the_document
# ok   spec_example_parses
# ok   spec_example_is_not_lowerable_as_written
# ok   fixture_lowers_to_the_handwritten_records
# ok   comparator_is_not_vacuous
# ok   errata_syntax_parses
# ok   unlowered_constructs_are_reported_not_dropped
# ok   negative_controls
# 13/13 checks passed

# 只展开
python3 tools/ssdl/emit_td.py tests/ssdl/sifive7-mini.ssdl -o /tmp/generated.td

# 单次对拍
python3 tools/ssdl/compare.py /tmp/generated.td tests/ssdl/sifive7-mini.expected.td
# records match: /tmp/generated.td == tests/ssdl/sifive7-mini.expected.td
```

工具默认从 `/home/ubuntu/llvm/builds/explore/bin/llvm-tblgen` 找 `llvm-tblgen`，从 `/home/ubuntu/llvm/trees/explore/llvm` 找 LLVM 源码树；可用 `LLVM_TBLGEN` 和 `SSDL_LLVM_SRC` 覆盖。

### 2.1 第 5 节示例确实能解析

`tests/ssdl/spec-example.ssdl` 是从文档第 5 节逐字抽出的副本；自检会把文档重新抽一遍并比对，防止两边漂移。解析结果是 19 个顶层声明，顺序与文档一致：

```text
SetDecl ZeroRegs, SetDecl LogicalInsts,
PredicateDecl IsZeroReg / HasStdExtI / NoStdExtZbb,
PredicateProlog,
FeatureDecl StdExtI / StdExtZicntr / StdExtZibi / Feature64Bit / TunePostRA,
STIDecl isZeroIdiom / isDependencyBreaking,
MachineDecl generic-rv64 / siFive7 / siFive7-alias,
STIFunction SiFive7ZeroIdiom / SiFive7DepBreaking,
TIIPredicate isDependencyBreaking
```

### 2.2 示例能解析 ≠ 能展开

`--strict` 下展开器会拒绝第 5 节示例，并按声明列出原因。实际输出（节选）：

```text
feature Feature64Bit: error: kind = plain requires field 'mattr'
feature StdExtZicntr: error: unknown feature 'StdExtZicsr'
machine generic-rv64: error: unknown feature 'StdExtZifencei'
machine siFive7.W_ALU: error: SchedWriteVariant is not lowered by the minimal emitter
machine siFive7.GPRFile: error: reg_file is not lowered by the minimal emitter
machine siFive7.itineraries: error: itineraries are not lowered by the minimal emitter
set ZeroRegs: error: attribute-filtered sets are not lowered (norm 4.1)
predicate IsZeroReg: error: predicates are not lowered by the minimal emitter
sti_decl isZeroIdiom: error: STI declarations are not lowered by the minimal emitter
tii_predicate isDependencyBreaking: error: TII predicates are not lowered by the minimal emitter
```

这不是实现缺陷，而是评审结论的可执行版本：示例缺 `-mattr` 名、引用了未声明特性、并使用了 AArch64 指令名；同时 `set`/谓词/STI/TII/`variant` 的语义在 1.0 里还没有唯一展开规则。

---

## 3. 语法 errata：实现接受的六处修正

不修改上游文档，只记录“解析器为了接受文档自己的示例所做的文法修正”。S1–S5 来自评审；S6/S7 是后续实现中新发现的。这些修正已经写进 `2026-11-16-ssdl-v1.1-draft.md`。

| ID | 位置 | 文档写法 | 文档 EBNF | 解析器接受的修正 |
|---|---|---|---|---|
| S1 | 3.3/3.6 | `predicate IsZeroReg(i) = reg_is(i, ZeroRegs);` | 没有参数列表；`reg_is` 首参是 `INT` | `predicate IDENT [ "(" [IDENT {"," IDENT} [","]] ")" ] ...`；操作数下标允许 `INT | IDENT` |
| S2 | 3.6 | `all_of(..., IsZeroReg(1), ...)` | 没有“命名谓词调用” | `predicate_expr` 增加 `IDENT "(" [args] ")"`，参数按被调用谓词的形参绑定 |
| S3 | 3.5 | `feature X : kind = extension, version = "2.1", ...;` | 字段之间无逗号 | 字段之间允许可选的 `,` |
| S4 | 3.12 | `resource LSU : units = 1, buffer = 4;` | 每个属性前独立 `":"` | `resource`/`res_group` 的属性分隔符接受 `":"` 或 `","` |
| S5 | 2 | `machine generic-rv64`、`siFive7-alias` | `IDENT` 不含 `-` | `IDENT` 首字符后允许 `-`；`->` 优先于“负号 + 数字”识别 |
| S6 | 3.9 | `sti_function ... { ... }` 后没有 `;` | `"}" ";"` | 尾随 `;` 可选（示例两处都省略） |
| S7 | 3.10 | `tii_predicate X = return P;` | `statement ";"`，而 `return` 已带 `;` | 尾随 `;` 可选；`return` 形式写一个，`switch` 形式写一个 |

S1 的“参数”目前只是**语法**绑定：`IsZeroReg(i)` 能解析，`IsZeroReg(1)` 也能解析；参数作用域、实参个数与类型检查属于语义层，尚未实现。S2 同理，只保证语法形状，不保证 `i` 会被替换。

S5 有词法副作用，必须在下一版文法里写清：`a-b` 是一个 `IDENT`，不是减法；负数仍由 `-` 紧跟数字识别；`->` 必须先于 `IDENT` 匹配。解析器的匹配顺序是 `...` / `..` / `->` / 比较符 → 负整数 → `IDENT`。

另有一条**只属于展开器**的 errata（E1）：

```text
feature Feature64Bit : kind = plain, mattr = "64bit", field = "IsRV64", value = "true";
```

`mattr` 不在 1.0 的 `feature_field` 里，但 M1 已经证明 `plain` 特性的 TD `Name`（`-mattr` 名）无法从 SSDL 标识符推导（`Feature64Bit` → `"64bit"`）。展开器要求 `plain` 必须写 `mattr`；`extension`/`tune` 按确定性规则推导，见下一节。

---

## 4. 展开器的命名规则（必须稳定，否则无法对拍）

### 4.1 `feature`

| kind | TD def 名 | `Name`（`-mattr`） | 证据 |
|---|---|---|---|
| `plain` | SSDL 名原样 | 显式 `mattr` | `SubtargetFeature<string n, ...>`（`TargetSubtarget.td:31`） |
| `extension` | `Feature` + SSDL 名（已以 `Feature` 开头则不重复） | `RISCVExtension` 自动推导：去掉 `FeatureStdExt`/`FeatureVendor`，小写；`experimental` 前加 `experimental-` | `RISCVFeatures.td:29-53,63-68` |
| `tune` | SSDL 名原样 | `positive_directive` | `RISCVTuneFeature<string name, string pos_directive, ...>`（`RISCVFeatures.td:1969`） |

`tune` 的 `negative_directive` 省略时按 `"no-" # positive_directive` 生成（与 `RISCVSimpleTuneFeature` 一致，`RISCVFeatures.td:1980`）。

`implies` 的 kind 也必须匹配：`extension` 只能隐式启用 extension，`tune` 只能隐式启用 tune。`RISCVExperimentalExtension` 的 implies 是 `list<RISCVExtension>`，比 `RISCVExtension` 的 `list<SubtargetFeature>` 更严格，所以这一条同时是类型约束。

`extension` 的 `version = "2.1"` 被拆成 `RISCVExtension<2, 1, ...>`；`experimental = true` 改用 `RISCVExperimentalExtension`；`bitmask` 追加 `RISCVExtensionBitmask<group, bit>`，并检查 `group` 落在 `bits<3>` 的 0..7。

实测记录（`tests/ssdl/sifive7-mini`）：

```tablegen
def FeatureSSDL64Bit { string Name = "ssdl-64bit"; string FieldName = "IsRV64"; ... }
def FeatureStdExtXssdl { string Name = "xssdl"; int MajorVersion = 2; int MinorVersion = 1; int GroupID = 0; int BitPos = 8; }
def FeatureStdExtZssdlexp { string Name = "experimental-zssdlexp"; bit Experimental = 1; }
def TuneSSDLPostRA { string Name = "use-ssdl-postra-scheduler"; string NegativeDirectiveName = "no-use-ssdl-postra-scheduler"; }
```

### 4.2 `machine`

```text
machine X { ... }   →  def XModel : SchedMachineModel { ... }
                       def X      : RISCVProcessorModel<"X", XModel, [features], [tune], "march">
                       （features 为空时用 RISCVTuneProcessorModel）
machine X = Y;      →  def X : ProcessorAlias<"X", "Y">
```

`-` 不能出现在 TableGen def 名里，统一替换为 `_`：`ssdl-sifive-7` → `ssdl_sifive_7` / `ssdl_sifive_7Model`。处理器 `Name` 字符串保持原文，所以 `-mcpu` 名字不受影响。

字段到记录的映射即评审 M4 的拆分：

| SSDL | 记录 |
|---|---|
| `issue_width`、`micro_op_buffer_size`、`load_latency`、`high_latency`、`mispredict_penalty`、`post_ra_scheduler`、`complete_model`、`full_inst_rw_overlap_check`、`enable_intervals` | `SchedMachineModel` |
| `unsupported_features` | `SchedMachineModel.UnsupportedFeatures`，`list<Predicate>` |
| `features`、`tune_features`、`default_march` | `RISCVProcessorModel` / `RISCVTuneProcessorModel` |

`unsupported_features = [StdExtZbkb]` 展开为 `let UnsupportedFeatures = [HasStdExtZbkb];`，由 `-print-records` 按 `list<Predicate>` 检查类型（M9）。这一步只验证类型与名字，**不验证** `HasStdExtZbkb` 一定存在——它由宿主特性表提供；测试里它来自真实的 `RISCVFeatures.td`。

### 4.3 调度记录

| SSDL | TD | 命名 |
|---|---|---|
| `write W` / `read R` | `SchedWrite` / `SchedRead` | 原样 |
| `resource P : units = n` | `ProcResource<n>` | 原样；`buffer` → `let BufferSize` |
| `res_group G = [...]` | `ProcResGroup<[...]>` | 原样 |
| `bind W -> [R...], ...` | `WriteRes<W, [R...]>` + `let Latency/NumMicroOps/ReleaseAtCycles/...` | `WR_<W>` |
| `advance R : cycles = n : writes = [W...]` | `ReadAdvance<R, n, [W...]>` | `RA_<R>` |
| `map [I...] -> [W..., R...]` | `InstRW<[...], (instrs I...)>` | `IRW_<I...>` |
| `map [regex("P")] -> [...]` | `InstRW<..., (instregex "P")>` | `IRW_REGEX_<P>` |

`read_default` / `no_read_advance` / `write_none` 分别映射到 `ReadDefault` / `NoReadAdvance` / `NoWrite`。`bind` 省略 `latency`/`uops` 时沿用 LLVM 类默认值（`Latency = 1`、`NumMicroOps = 1`），不额外生成 `let`。

明确拒绝（而不是猜测）的输入：`advance` 缺 `cycles`（`ReadAdvance` 无默认值，P1-11）；同一条 `write` 的重复 `bind`；同一条 `read` 的重复 `advance`；空 `map`；`map` 里 `IDENT` 与 `regex(...)` 混写（P1-10）；`implies` 的 kind 不匹配；重复的 `map` 生成名；跨 machine 引用 `write`/`read`/`resource`；未声明的特性、别名目标、`res_group` 成员。

---

## 5. 仍然只解析、不展开的构造

`--strict` 会把下面每一项变成错误；默认模式则打印 `skip` 诊断并继续展开其余部分。测试同时验证两种模式（`unlowered_constructs_are_reported_not_dropped`）。

| 构造 | 状态 | 阻塞项 |
|---|---|---|
| `set`（含 `inst(...)` / `reg(...)` / `...` / 范围） | 只解析 | M5：没有属性 schema、排序、去重、环检测 |
| `predicate`（表达式形与块形） | 只解析 | M2/M3/M8：两个 predicate sort 未分层 |
| `predicate_prolog` | 只解析 | 依赖 predicate 展开 |
| `sti_decl` / `sti_function` / `tii_predicate` | 只解析 | M2/M7：`STIPredicate` 与声明/委托的落点未定 |
| `variant` / `read_variant` / `map_itin` / `alias` | 只解析 | M2/M8：`SchedVar` 谓词 sort 未定 |
| `reg_file` / `retire_unit` / `load_queue` / `store_queue` | 只解析 | 需要寄存器类/资源引用策略；P1-13 |
| `itineraries`（顶层或 machine 内） | 只解析 | M6：顶层没有 `sched_model` 绑定 |

这些不是“以后再补的格式问题”。在 M2/M3/M5/M6 关闭之前，任何展开都会是宿主自创语义，而不是 SSDL 1.0 的语义。

---

## 6. 与评审的逐项状态

### 6.1 已闭合（在实现/展开器范围内）

| 项 | 状态 | 证据 |
|---|---|---|
| S1 | 语法接受 `predicate P(i)`，`reg_is(i, ...)` 可解析 | `tests/ssdl/errata.ssdl`；`errata_syntax_parses` |
| S2 | 语法接受命名谓词调用 `IsZeroReg(1)` | 同上 |
| S3 | 特性字段允许逗号分隔 | 示例与 fixture |
| S4 | `resource` 属性允许 `:` 与 `,` | fixture 的 `LSU : units = 1, buffer = 4` |
| S5 | `IDENT` 允许 `-` | `ssdl-sifive-7`、`errata-machine-alias` |
| S6 | `sti_function` 尾随 `;` 可选 | `errata.ssdl` 的 `ErrataHook` |
| S7 | `tii_predicate` 的 `return` 形式只需一个 `;` | `errata.ssdl` 的 `ErrataTII`；`v1_1_tii_return_semicolon_forms` |
| M1 | 展开器要求 `plain.mattr`，并确定性推导 `extension`/`tune` 的 `Name` | 第 4.1 节；`-print-records` 的 `Name` 字段 |
| M4（部分） | `machine` 拆成 `XModel` + `X` + 别名 | 第 4.2 节；记录对拍 |
| M9 | `unsupported_features` 展开为 `list<Predicate>` | `UnsupportedFeatures = [HasStdExtZbkb]` |
| P1-10 | `map` 拒绝 `instrs`/`instregex` 混写 | 负控 `cannot mix instruction names and regex` |
| P1-11 | `advance` 强制 `cycles` | 负控 `cycles is required` |
| P1-17（部分） | 未声明引用、重复特性/机器/绑定/映射报错；特性允许前向引用 | 负控与两遍收集 |

### 6.2 仍开放

| 项 | 为什么仍开放 |
|---|---|
| S1/S2 的语义 | 参数作用域、实参个数、参数替换未实现 |
| M1 的规范层 | 展开器用 `mattr` 闭合，但 1.0 文法仍无该字段，E1 必须进下一版 |
| M2/M3/M8 | `MCInstPredicate` 与 `SchedPredicateBase` 未分层；4.4 仍与 3.3 冲突 |
| M5 | `set` 无属性模型 |
| M6 | 顶层 `itineraries` 无绑定；`itin_class` 与 `itin` 的关系未定义 |
| M7 | `sti_function.delegates` 无落点 |
| M10 | 裸 `IDENT` 的 sort 歧义（特性？谓词？集合？） |
| P1-1..9、P1-12..16、P1-18..20 | 见评审；本轮未触及 |
| 全量 SiFive7 等价 | 只做了二十余条记录的 fixture，不是整核模型 |
| 生成代码行为 | `-print-records` 只证明记录字段一致，不证明 `-gen-subtarget`/调度行为一致 |

---

## 7. 对拍方法为什么可信

`tools/ssdl/compare.py` 做三件事：

1. 从两个 `.td` 源文件里取出**各自文本声明的 `def` 名**，只比较这些记录；
2. 用 `llvm-tblgen -print-records` 让 TableGen 自己解析类默认值、模板参数与多重继承；
3. 逐名字提取完整记录块，字段与顺序都必须一致，并给出 unified diff。

因此 `sifive7-mini.expected.td` 是“手写的、使用真实 LLVM 类的等价物”，而不是由生成器再生成一遍。`comparator_is_not_vacuous` 会把期望文件里的 `Latency = 3` 改成 `4`，确认比较器确实报错并指出 `WR_W_LOAD`。

fixture 里的特性名带 `ssdl` 前缀，是因为生成文件与真实的 `RISCVFeatures.td` 同时加载；真实迁移应当**替换**在树内的 `def FeatureStdExt...`，而不是并存。这一点是测试夹具的约束，不是 SSDL 的语义。

`machine ssdl-sifive-7` 使用真实的 SiFive7 调度参数（`IssueWidth=2`、`MicroOpBufferSize=0`、`LoadLatency=3`、`MispredictPenalty=3`、`CompleteModel=0`、`EnableIntervals=true`，见 `RISCVSchedSiFive7.td:1608-1625`），但只保留三条 `map` 和两个资源，不是完整模型。第 5 节示例里 AArch64 指令名与 OoO 参数的问题仍然存在，评审第 5 节继续有效。

---

## 8. 建议的下一轮顺序

1. **改文法**：把第 3 节的 S1–S6 与 E1（`mattr`）写进规范；这是纯文本修改，但必须先做，否则后面每个实现都要自带 errata。
2. **先定 predicate sort**：M2/M3/M8 是 `variant`、`sti_decl default`、`tii_predicate` 的共同前置；不定这个，展开 `variant` 只会制造第二套语义。
3. **`set` 属性 schema**：M5 不解决，4.1 的 `inst(attr_cond)` 无法实现；先给出字段名、类型、来源、排序与缺失属性策略。
4. **顶层 `itineraries` 绑定**：M6 只需一个 `sched_model = IDENT` 与 `itin_class`/`itin` 的等价规则。
5. **扩大对拍**：把 SiFive7 的 `WriteRes`/`InstRW` 子集逐条搬进 fixture，直到 `-print-records` 与在树记录一致；再尝试 `-gen-subtarget` 级别的差异。
6. **接入方式**：不要生成一个 `include "RISCV.td"` 的独立文件去替换在树 `.td`；更现实的落点是 `RISCVTargetDefEmitter` 或一个生成 `def` 片段、由 `RISCV.td` 包含的构建步骤。本轮只验证记录形状，不验证接入。

---

## 9. 文件清单

```text
tools/ssdl/parser.py       # 词法 + 递归下降，覆盖 9 个顶层关键字与七条 errata
tools/ssdl/emit_td.py      # 展开器：feature/machine/调度/reg_file/itineraries + 谓词接线
tools/ssdl/predicates.py   # 谓词/STI/TII lowering（MC/SCHED/SEL）
tools/ssdl/sets.py         # set 求值器 + 宿主属性 schema
tools/ssdl/tdutil.py       # TableGen 文本工具
tools/ssdl/compare.py      # -print-records 按 def 名对拍（匿名记录规范化）
tools/ssdl/check.py        # 18 项自检（正向、负控、errata、sort、lowering、非空比较）
tools/ssdl/validate.py     # v1.1 predicate sort + itinerary binding 校验
tests/ssdl/spec-example.ssdl          # 文档第 5 节逐字副本
tests/ssdl/sifive7-mini.ssdl          # 可展开的 SiFive7 子集
tests/ssdl/sifive7-mini.expected.td   # 手写等价物（真实 LLVM 类）
tests/ssdl/errata.ssdl                # S1–S7 语法用例
tests/ssdl/v1.1.ssdl                  # MC/SCHED 排序与 sched_model 绑定用例
tests/ssdl/attrs.json                 # set 求值用的 inst/reg 属性 schema
```

---

## 10. 2026-11-16 v1.1 增量

v1.1 的规范文本在 `2026-11-16-ssdl-v1.1-draft.md`。本轮只实现其中可判定的部分：

| v1.1 项 | 实现 | 证据 |
|---|---|---|
| S1/S2 语法 | 解析器 | `errata.ssdl`、13/13 自检 |
| S3–S7 | 解析器 | 同上；S7 的 `return`/`switch` 两种形式都有用例 |
| 谓词 MC/SCHED/SEL | `validate.py` 推断、提升、上下文检查 | `v1_1_predicate_sorts`、`v1_1_sort_negative_controls` |
| 命名谓词 | 未知引用、参数个数、循环、保留名 | 同上 |
| `variant` 的 MC 自动提升 | 校验通过；lowering 仍未实现 | `v1.1.ssdl` |
| `tii_predicate`/STI 的 MC 约束 | 校验 | 负控 `requires an MC predicate` |
| `mc(...)` / `sched(...)` / `sched_code(...)` | 解析 + sort | `v1.1.ssdl` 的 `SchedPred` |
| 顶层 `itineraries.sched_model` | 解析 + 绑定校验（必须是 machine 定义，别名不行） | `v1_1_itineraries_binding_parses`、负控 |
| machine 内禁止 `sched_model` | 校验 | 负控 `must not set sched_model` |
| `itin` / `itin_class` 重名 | 校验 | 负控 `duplicate itin`、`duplicate itinerary class` |
| 混合 `map` | 拆成 `(instrs ...)` + `(instregex ...)` 两条 `InstRW` | `v1_1_mixed_map_is_split`、fixture 对拍 |
| `advance.cycles` 必写/可负 | 展开器 | 负控 `cycles is required` |
| `attr_expr` 优先级 | 解析器（`!` > 比较 > `&&` > `||`） | `parse_attr_or/and/not/cmp` |
| `set` 属性契约 | **仅规范**，求值器未实现 | v1.1 §5 |

注意：§10 记录的是“校验先行”的中间状态。`set` 求值与谓词 lowering 已在 §11 落地；§10 中“仍未实现/只解析”的表述由 §11 取代。

---

## 11. 2026-11-16 v1.1 lowering 落地

§11 关闭 §10 留下的 lowering 缺口：`set` 求值、谓词/STI/TII lowering、`variant`、寄存器文件、retire/queue、itineraries 都已生成真实记录，并由 `llvm-tblgen -print-records` 对拍。

### 11.1 新增文件与职责

```text
tools/ssdl/sets.py        # set 求值器 + 宿主属性 schema（JSON）
tools/ssdl/predicates.py  # 谓词/STI/TII lowering（MC/SCHED/SEL 三路）
tools/ssdl/tdutil.py      # 共享的 TableGen 文本转义/命名工具
tools/ssdl/compare.py     # -print-records 对拍
tools/ssdl/schema.py      # 从 -print-records 导出属性 schema（--merge 叠宿主语义）
tests/ssdl/attrs.json     # fixture 的属性 schema（inst/reg 顺序、属性值）
```

`emit_td.py` 的 `emit()` 新增 `attrs=` 与 `sorts=`；命令行新增 `--attrs PATH`。不传 schema 时，`set` 仍按“未求值”报告 skip，谓词里引用 `set` 是错误；`--strict` 下两者都是错误。

### 11.2 `set` 求值契约（v1.1 §5）

输入 schema：

```json
{"order": {"inst": ["ADD", "..."], "reg": ["X0", "..."]},
 "inst": {"ADD": {"category": "alu", "...": "..."}},
 "reg":  {"X0":  {"is_zero": true}}}
```

实现要点：

- `inst(expr)` / `reg(expr)` 按 `order` 稳定遍历，只保留条件为真的实体；
- 比较必须同型：`==`/`!=` 同型，`< > <= >=` 仅 int；未知属性、缺失属性、未知实体都是错误；
- 结果按宿主顺序稳定去重；`...Set` 拼接保持“先出现者在前”；
- 裸列表混用 inst/reg 是错误；set 循环引用报告完整链（`A -> B -> A`）；
- 空裸列表无法推断 kind，是错误；`[a..b]` 的 `a>b` 是错误。

### 11.3 谓词 lowering 与命名

| SSDL | 生成的 TableGen |
|---|---|
| `predicate P = MC 表达式;` | `def P : CheckAll<...>` 等 `MCInstPredicate` 子类 |
| `predicate P(i) = ...;` | 宏；调用处内联，不生成记录 |
| `predicate P = SCHED 表达式;` | `FeatureSchedPredicate` / `SchedPredicate` / `AllOfSchedPreds` 等 |
| `predicate P { selector = ...; }` | `def P : Predicate<"...">` |
| `predicate P { selector = ...; assembler = ...; }` | 再加 `AssemblerPredicate<(all_of ...), "desc">` |
| `predicate_prolog([{...}]);` | `def SSDLPredicateProlog : PredicateProlog<[{...}]>`（多个时加数字后缀） |
| `sti_decl D { ... }` | `def DDecl : STIPredicateDecl<"D", default, overrides, expandForMC, opcode_mask, [delegates]>` |
| `sti_function F : D { ... }` | `def F : STIPredicate<DDecl, [classes]>`，有 `sched_model` 时前置 `let SchedModel = <machine>Model in` |
| `dep_breaking_class C(...)` | `def C : DepBreakingClass<[opcodes], pred[, operands]>` |
| `equiv_class C(...)` | `def C : InstructionEquivalenceClass<[opcodes], pred[, operands]>` |
| `tii_predicate T = return P;` | `def T : TIIPredicate<"T", MCReturnStatement<P>>` |
| `tii_predicate T = switch opcode {...}` | `MCOpcodeSwitchStatement<[...], default>` |

实现细节：

- 表达式谓词不会生成 `Predicate<...>`；只有块形式 `selector` 才走指令选择谓词（v1.0 §4.4 的修正）；
- `true`/`false` 作为**命名谓词**时生成 `CheckAll<[TruePred]>` / `CheckAll<[FalsePred]>`，因为 `TruePred`/`FalsePred` 是 def 不是 class，`def P : TruePred;` 会被 TableGen 拒绝；
- `variant` case 用 `to_sched`：MC 自动包成 `MCSchedPredicate<...>`，SCHED 直接使用，SEL 是错误；
- MC 上下文（`sti_decl.default`、`tii_predicate.return`、`equiv_class` 谓词）拒绝 SCHED/SEL，lowering 与 `validate.py` 双重检查；
- `func(...)`/`func_tii(...)` 映射到 `CheckFunctionPredicate` / `CheckFunctionPredicateWithTII`；`map=` 仍显式报“not lowered yet”。

### 11.4 `variant` / 寄存器文件 / 队列 / itineraries

```text
variant W { case P: [RW]; default: [RW]; }
  -> def WVariant : SchedWriteVariant<[SchedVar<to_sched(P), [RW]>,
                                      SchedVar<NoSchedPred, [RW]>]>;
     def SSDLVariantAlias_W : SchedAlias<W, WVariant>;
read_variant R { ... }
  -> SchedReadVariant + SchedAlias
```

- `default` 必须显式写出（P1-12），否则报错；
- `variadic = true` 生成 `let Variadic = true;`；
- `reg_file F { ... }` → `RegisterFile<num_phys, [classes], [costs], [allow_move_elim], max_move_elim_per_cy, allow_zero_elim_only>`；
- `retire_unit { buffer; max_per_cycle; }` → `<Machine>RetireUnit : RetireControlUnit<b, m>`，两者都必写（P1-13）；
- `load_queue`/`store_queue` → `LoadQueue_<Resource>` / `StoreQueue_<Resource>`；
- `func_unit` / `bypass` / `itin_class` → `FuncUnit` / `Bypass` / `InstrItinClass`；
- `itineraries { func_units; bypasses; itin ... }`：为列表中的名字补 `FuncUnit`/`Bypass`（机器已声明则不重复），`itin X` 生成 `IIC_X`（已带 `IIC_` 前缀则不重复加）与内联 `InstrItinData`，最后生成 `<Machine>Itineraries : ProcessorItineraries<...>` 并加 `let PacketizerNamespace`；
- 顶层 `itineraries : sched_model = M` 生成 `<M>Itineraries`，通过 `let Itineraries = <M>Itineraries;` 挂到 `MModel`；机器内外同时声明同一模型的 itineraries 是错误。

### 11.5 证据

```bash
python3 tools/ssdl/check.py                 # 24/24
python3 tools/ssdl/emit_td.py tests/ssdl/sifive7-mini.ssdl \
    --strict --attrs tests/ssdl/attrs.json -o /tmp/gen.td
python3 tools/ssdl/compare.py /tmp/gen.td tests/ssdl/sifive7-mini.expected.td
```

fixture 现在覆盖：`set` 求值与展开去重、MC/SCHED/SEL 谓词、`predicate_prolog`、`sti_decl` + 两条 `sti_function`（`SchedModel` 绑定、`DepBreakingClass`）、`tii_predicate` 的 switch、`variant`/`read_variant`、`RegisterFile`、`RetireControlUnit`、`LoadQueue`/`StoreQueue`、机器内与顶层两套 itineraries。`v1.1.ssdl` 还验证 MC→SCHED 提升与顶层 itinerary 绑定。

### 11.6 仍然开放

- **M5 属性 schema 来源**：`schema.py` 已能从 `llvm-tblgen -print-records` 导出指令/寄存器属性（RISCV：16962 inst / 398 reg），并用 `--merge` 叠加宿主语义属性（如零寄存器）。三态已保留为 JSON `null`（`mayLoad = ?` 在 Instruction 记录上 51 条）；缺口转到消费端：`sets.py` 还没有把"与 null 比较"升级为错误。见 `2026-11-16-ssdl-domain-model.md` D5/D13。
- **`map=`**：`reg_is`/`imm_is` 等的函数映射没有对应 SSDL 语义，lowering 直接报错。
- **`combo_func`**：`ProcessorItineraries` 上没有挂载点，仍然 skip（`--strict` 报错）。
- **`sti_function.delegates`（M7）**：`STIPredicate` 本身没有 delegates 字段；`delegates` 属于 `STIPredicateDecl`（`TargetInstrPredicate.td:405-408`），因此只能在 `sti_decl` 上表达。
- **验证深度**：仍只做 `-print-records`。`-gen-subtarget`/`-gen-instr-info` 已实测，失败原因是模型完整性而非展开器形状，见 §12 与领域模型 §7。

### 11.7 本轮关闭的项

| 项 | 状态 | 证据 |
|---|---|---|
| SchedModel 作用域 | 关闭 | 输出分三类桶：prelude（FuncUnit/Bypass/InstrItinClass/ProcessorItineraries）、unscoped（SchedWrite/SchedRead/ProcessorModel/ProcessorAlias）、scoped（`let SchedModel = ... in { ... }`）；`-print-records` 确认 `PipeA`/`WR_W_ALU`/`IRW_LD`/`SchedAlias`/`RegisterFile` 等都带 `SchedModel` |
| `write_seq` | 关闭 | `WriteSequence<[...], N>`；名字先收集，支持前向引用 |
| 调度 `alias` | 关闭 | `SchedAlias_<SRC>_<DST> : SchedAlias<SRC, DST>` |
| `map_itin` | 关闭 | `ItinRW<[IIC...], [RW...]>`，名字对 `itin_class` 与机器内 `itin` 解析 |
| 宏参数传集合/特性 | 关闭 | `IsALUAndZero([ADD,SUB], ZeroRegs, 1)` → `CheckAll<[CheckOpcode<[ADD,SUB]>, CheckRegOperand<1, X0>]>` |
| 属性 schema 导出 | 关闭（导出）/ 部分（消费） | `schema.py` 导出 + `--merge`；三态写 `null`；`check.py` 的 `attribute_schema_exports_tablegen_records` 覆盖；`sets.py` 的 null 比较待做 |

## 12. 2026-11-16 领域模型审计

规范增量见 `2026-11-16-ssdl-domain-model.md`。§11 的 lowering 是"记录形状正确"，本轮补的是"消费者契约"。对新实现的直接影响：

| 发现 | 对实现的含义 |
|---|---|
| `write`/`read` 是目标级词表（`RISCVSchedule.td:16`），`resource` 是模型级 | 两机模型必然撞名（已实测）；`emit_td.py` 需要把 `write`/`read`/`write_seq` 提升到单一目标命名空间 |
| 有 `WriteRes` 就必须覆盖全部可达 `SchedWrite`（`SubtargetEmitter.cpp:984-989`） | `machine` 是完整模型，不是补丁；`complete_model = false` 不解除该要求 |
| `WriteRes` 的 `release`/`acquire` 数组默认值与长度约束（`SubtargetEmitter.cpp:1254-1266`） | 展开器应校验长度相等；`acquire` 只影响 codegen |
| `InstRW` 是覆盖且要求已有 sched class（`CodeGenSchedule.cpp:1013`） | `map` 不能给"没有 SchedRW 的指令"补调度 |
| STI 谓词只有 llvm-mca 消费者（`MCA/InstrBuilder.cpp:724`） | `sti_decl`/`sti_function` 必须在规范中标注消费层 |
| 属性三态（`mayLoad = ?` 190 处） | `set` 的比较语义需要 `unknown`，`schema.py` 需要保留 `?` |
| `PredicateProlog` 只注入 `resolveSchedClass`（`SubtargetEmitter.cpp:1943-1949`） | prolog 只能被 SCHED sort 引用 |
| itinerary 与 MCA 互斥（`tools/llvm-mca/llvm-mca.cpp:446-449`） | 同一 machine 不应同时给 `bind` 与 `itineraries` |

上述八条都还是**规范/实现缺口**，不在本轮代码范围内；下一轮的顺序应为 R1（词表所有权）→ R4/R10（完整性/覆盖）→ R5（三态）→ R2（资源命名）。

## 13. 2026-11-16 调度词表所有权（M11/M12）

领域模型（`2026-11-16-ssdl-domain-model.md`）的 D1/D2 落地结果。

### 13.1 改了什么

| 项 | 之前 | 现在 |
|---|---|---|
| `write`/`read` | 每个 machine 各发一份 `SchedWrite`/`SchedRead` | 收集到目标命名空间，只发一次；同形重复声明允许，异形/异 kind 报 `conflicting declaration of scheduling ...` |
| `write_seq` | 发在 `let SchedModel` 里 | 发在目标级（`WriteSequence.SchedModel` 被 SubtargetEmitter 忽略，`?` 是正确形状） |
| `func_unit`/`bypass`/`itin_class` | machine 内 prelude | 目标级，只发一次 |
| 机器级记录名 | `WR_W_ALU` / `RA_R_ALU` / `IRW_*` / `SchedAlias_*` / `*Variant` / `LoadQueue_*` | 加 machine 前缀：`<M>_WR_<W>` 等（等价上游 `defm` 前缀，如 `SiFive7PipeA`） |
| 资源重名 | 泛化报错 `emitted def 'P' collides with 'machine A'` | `resource 'P' is already defined by 'machine A'; resource names are global TableGen defs, ...` |
| 引用解析 | 只看本机声明的写/读 | 目标命名空间（B 机可不重复声明直接 `bind W_ALU`） |

### 13.2 证据

```bash
python3 tools/ssdl/check.py    # 24/24
```

新增自检：

- `scheduling_vocabulary_is_target_global`：两机共享词表，`def W_ALU : SchedWrite;` 只出现一次，两个 `let SchedModel = ... in` 各自独立；
- `scheduling_vocabulary_conflict_is_reported`：`write W` 与 `read W` 跨机冲突；
- `resource_collision_names_the_rule`：资源重名给出"改名"诊断；
- `machine_scoped_records_are_namespaced`：`A_WR_W`/`B_LoadQueue_Q` 等带前缀，未前缀的 `WR_W` 不存在。

fixture 对拍仍通过（`compare.py` records match）；`tests/ssdl/sifive7-mini.expected.td` 相应改名为 `ssdl_sifive_7_WR_*` 等，并把 `W_MULDIV_SEQ` 移到目标级。

### 13.3 仍然开放

- 用户命名的 `resource`/`res_group`/`reg_file` 仍要求全目标唯一（R2 选择"显式唯一 + 诊断"，不做隐式前缀）；
- 属性三态的比较语义（D5）与 `-gen-subtarget` 的完整模型验收（§12）未动。
