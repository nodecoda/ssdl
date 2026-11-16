# SSDL 解析器与最小展开器：实现记录

- date: 2026-11-16
- 上游文档：`2026-11-16-ssdl-v1.0-final.md`（未改动）
- 上游评审：`2026-11-16-ssdl-review.md`（未改动）
- 代码：`tools/ssdl/{parser,emit_td,compare,check}.py`
- 测试：`tests/ssdl/`
- 结论：**第 5 节示例已能完整解析（19 个顶层声明）；`feature` / `machine` / `resource` / `write` / `read` / `bind` / `advance` / `map` 已能展开成真实 LLVM TableGen 记录，并与手写 `.td` 的 `-print-records` 输出逐字段一致。** 其余构造仍只解析、不展开，并给出显式诊断。

---

## 1. 一句话

这一轮把“可解析性”从断言变成了可执行检查，把“可展开性”从 `norm` 散文变成了 8 个自检；但它没有、也不应该声称 SSDL 1.0 已经语义闭合。评审里的 S1–S5 现在都有对应解析路径，M1/M4/M9/P1-10/P1-11 在**展开器范围内**闭合，其余 M/P 项仍然开放。

---

## 2. 运行方式与结果

```bash
# 8 项自检
python3 tools/ssdl/check.py
# ok   spec_example_checked_in_copy_matches_the_document
# ok   spec_example_parses
# ok   spec_example_is_not_lowerable_as_written
# ok   fixture_lowers_to_the_handwritten_records
# ok   comparator_is_not_vacuous
# ok   errata_syntax_parses
# ok   unlowered_constructs_are_reported_not_dropped
# ok   negative_controls
# 8/8 checks passed

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

不修改上游文档，只记录“解析器为了接受文档自己的示例所做的文法修正”。S1–S5 来自评审；S6 是本轮新发现。

| ID | 位置 | 文档写法 | 文档 EBNF | 解析器接受的修正 |
|---|---|---|---|---|
| S1 | 3.3/3.6 | `predicate IsZeroReg(i) = reg_is(i, ZeroRegs);` | 没有参数列表；`reg_is` 首参是 `INT` | `predicate IDENT [ "(" [IDENT {"," IDENT} [","]] ")" ] ...`；操作数下标允许 `INT | IDENT` |
| S2 | 3.6 | `all_of(..., IsZeroReg(1), ...)` | 没有“命名谓词调用” | `predicate_expr` 增加 `IDENT "(" [args] ")"`，参数按被调用谓词的形参绑定 |
| S3 | 3.5 | `feature X : kind = extension, version = "2.1", ...;` | 字段之间无逗号 | 字段之间允许可选的 `,` |
| S4 | 3.12 | `resource LSU : units = 1, buffer = 4;` | 每个属性前独立 `":"` | `resource`/`res_group` 的属性分隔符接受 `":"` 或 `","` |
| S5 | 2 | `machine generic-rv64`、`siFive7-alias` | `IDENT` 不含 `-` | `IDENT` 首字符后允许 `-`；`->` 优先于“负号 + 数字”识别 |
| S6 | 3.9 | `sti_function ... { ... }` 后没有 `;` | `"}" ";"` | 尾随 `;` 可选（示例两处都省略） |

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
| S6 | `sti_function` 尾随 `;` 可选 | `errata.ssdl` 最后一个声明 |
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
tools/ssdl/parser.py       # 词法 + 递归下降，覆盖 9 个顶层关键字与六条 errata
tools/ssdl/emit_td.py      # 最小展开器：feature/machine/resource/write/read/bind/advance/map
tools/ssdl/compare.py      # -print-records 按 def 名对拍
tools/ssdl/check.py        # 8 项自检（正向、负控、errata、确定性、非空比较）
tests/ssdl/spec-example.ssdl          # 文档第 5 节逐字副本
tests/ssdl/sifive7-mini.ssdl          # 可展开的 SiFive7 子集
tests/ssdl/sifive7-mini.expected.td   # 手写等价物（真实 LLVM 类）
tests/ssdl/errata.ssdl                # S1–S6 语法用例
```
