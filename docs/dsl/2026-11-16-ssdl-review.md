# SSDL 1.0 Final 评审：可解析性与可展开性都还没闭合

- date: 2026-11-16
- 评审对象：`history/2026-11-16-ssdl-v1.0-final.md`
- 证据基线：`/home/ubuntu/llvm/trees/explore` @ `700e551714cc`
- 方法：只做静态评审。逐条把文档的 EBNF 与示例对推，再把 `norm` 规则对照 LLVM 的 TableGen 类定义；没有运行 SSDL 解析器（目前也不存在），因此不声称示例“能解析”。相反，下面列出的是按文档自己的文法**推不出来**的位置。
- 结论：**设计方向成立，但“1.0 Final / 语义完整的替代品”不成立。** 至少有 5 处示例无法由 EBNF 推导，10 处语义无法唯一展开成 LLVM TableGen；旗舰例子还把 AArch64 的指令/寄存器/零寄存器习语写进了 RISC-V SiFive7，并把一个 in-order 核写成了 OoO 核。

---

## 0. 一句话

这不是“再润色一下”的问题。SSDL 现在同时缺三样东西：

1. **可解析的源语言**：`predicate` 的参数、命名谓词引用、`feature` 字段分隔、资源属性分隔、连字符标识符，五处直接和 EBNF 冲突；
2. **可唯一展开的中间语义**：`feature` 名字到 `-mattr` 名字、`predicate_expr` 的两个 sort、`machine` 融合后的记录拆分、`set` 的属性模型，都没有定义；
3. **可执行的验收**：没有解析器、没有 TD 产物对拍、没有负控。没有这三样，“final”只是一个断言。

---

## 1. 先说成立的部分

这些点值得保留，不应该在修语法时一起丢掉：

- **9 个平级顶层关键字**的边界清楚：`set` / `predicate` / `predicate_prolog` / `feature` / `sti_decl` / `sti_function` / `tii_predicate` / `machine` / `itineraries`。
- **`feature` 用 `kind` 承载形态**，而不是引入 base/extension：`plain` / `extension` / `tune` 与 LLVM 的 `SubtargetFeature`、`RISCVExtension`、`RISCVTuneFeature` 基本能一一对应。
- **`machine` 把 `SchedMachineModel` 与 `ProcessorModel` 放在一个块里**，对用户是合理的抽象；问题只在展开规则没有写清。
- **`bind` / `map` / `advance` / `variant` / `reg_file` / `retire_unit` / `load_queue` / `store_queue`** 这些关系的命名和 LLVM 的 `WriteRes` / `InstRW` / `ReadAdvance` / `SchedWriteVariant` / `RegisterFile` / `RetireControlUnit` / `LoadQueue` / `StoreQueue` 很接近，说明领域模型是抓准了的。
- **`sti_decl` / `sti_function` / `tii_predicate`** 的骨架与 `STIPredicateDecl` / `STIPredicate` / `TIIPredicate` 对得上。
- **`itineraries` 的 `stage(cycles, units, timeinc, kind)`** 与 `InstrStage` 的四个字段对得上。

---

## 2. P0-S：示例按 EBNF 直接不可解析

这五条不需要讨论语义，拿文档的 3.x 节逐字推导就能复现。

| ID | 文档位置 | 示例写法 | 文法要求 | 结论 |
|---|---|---|---|---|
| S1 | 3.3 + 3.6 | `predicate IsZeroReg(i) = reg_is(i, ZeroRegs);` | `predicate IDENT "=" predicate_expr`；`reg_is` 首参是 `INT` | 两处冲突：没有参数列表产生式；`i` 不是 `INT` |
| S2 | 3.6 | `all_of(opcode([ORR]), IsZeroReg(1), IsZeroReg(2))` | `predicate_expr` 只有 `true/false`、`not`、`all_of`、`any_of`、`feature`、`atom_pred`；没有“命名谓词调用” | `IsZeroReg(1)` 无法推导；`variant`、`sti_function`、`tii_predicate` 都受影响 |
| S3 | 3.5 | `feature StdExtI : kind = extension, version = "2.1", ...` | `feature_decl = "feature" IDENT ":" { feature_field } ";"`，字段之间没有逗号 | 示例里的逗号不在文法里；第 6 节速查表也用了逗号，说明是系统性的 |
| S4 | 3.12 | `resource LSU : units = 1, buffer = 4;` | `resource_decl` 的两个可选属性各自带 `":"`，不是逗号分隔 | `, buffer = 4` 无法推导；应写 `: buffer = 4` |
| S5 | 2 + 3.11 | `machine generic-rv64 { ... }`、`machine siFive7-alias = siFive7;` | `IDENT = letter { letter | digit | "_" }` | `-` 不在 `IDENT` 里；两个名字都非法 |

**最小复现**（把示例中的五行单独拿出来，按文档文法都不成立）：

```text
predicate IsZeroReg(i) = reg_is(i, ZeroRegs);          # S1
all_of(opcode([ORR]), IsZeroReg(1), IsZeroReg(2))      # S2
feature StdExtI : kind = extension, version = "2.1";   # S3
resource LSU : units = 1, buffer = 4;                  # S4
machine generic-rv64 { }                                # S5
```

> 这五条不是“示例写错了一个符号”，而是**文法没有覆盖自己在示例和速查表里反复使用的写法**。修法只有两种：改文法，或改示例；不能靠“宿主预处理”掩盖，因为那会把语言边界推给实现者。

---

## 3. P0-M：语义不能唯一展开

### M1. `feature` 的标识符与 TD 的 `Name` 字符串没有映射

SSDL 的 `feature X` 是一个 `IDENT`；LLVM 的 `SubtargetFeature` 真正的名字是 `Name` 字符串，而且三种 kind 的命名规则不同：

| SSDL | 实际 TD | `Name` | 证据 |
|---|---|---|---|
| `feature Feature64Bit` | `Feature64Bit : SubtargetFeature<"64bit", "IsRV64", "true", ...>` | `"64bit"` | `RISCVFeatures.td:1991` |
| `feature StdExtI` | `FeatureStdExtI : RISCVExtension<2, 1, ...>` | `"i"`（由 record 名去前缀再小写） | `RISCVFeatures.td:29-46,73` |
| `feature TunePostRA` | `TunePostRAScheduler : RISCVSimpleTuneFeature<"use-postra-scheduler", ...>` | `"use-postra-scheduler"` | `RISCVFeatures.td:2078` |

而 `norm` 只写 `emit_subtarget_feature(X)`，没有说 `X` 是 `Name`、是 record 名、还是逻辑名。结果是：按字面展开，`-mattr=Feature64Bit` 而不是 `-mattr=64bit`，`-mattr=StdExtI` 而不是 `-mattr=i`。这不是命名风格问题，是**命令行契约错误**。

**最小修法**：在 `feature` 里增加显式 `name = STRING`（或 `mattr = STRING`），并为 `plain/extension/tune` 各写一条命名规则；`X` 只作为 SSDL 内部引用名。

### M2. `predicate_expr` 混了两个 sort：`MCInstPredicate` 与 `SchedPredicateBase`

LLVM 里这是两条不同的继承链：

- `MCInstPredicate` 及其子类：`CheckOpcode`、`CheckRegOperand`、`CheckAll`、`CheckAny`、`CheckNonPortable` …（`TargetInstrPredicate.td:94`）
- `SchedPredicateBase` 及其子类：`MCSchedPredicate<MCInstPredicate>`、`FeatureSchedPredicate<SubtargetFeature>`、`SchedPredicate<code>`、`AllOfSchedPreds<...>` …（`TargetSchedule.td:371-407`）

`feature(IDENT)` 对应的是 `FeatureSchedPredicate`，**不是** `MCInstPredicate`；`opcode(...)` 对应的是 `CheckOpcode`，**不是** `SchedPredicateBase`。SSDL 的 `predicate_expr` 把两者都塞进同一个 `all_of`，于是：

- `all_of(opcode([ORR]), IsZeroReg(1))` 在 MC 层应变成 `CheckAll<[CheckOpcode<...>, ...]>`；
- 在 `variant` 的 `SchedVar` 里应变成 `AllOfSchedPreds<[MCSchedPredicate<CheckAll<...>>, ...]>`；
- `feature(X)` 在 MC 层根本没有对应物，只能在外层 `AllOfSchedPreds` 里出现。

RISC-V 自己就有混合两 sort 的真实例子：

```tablegen
def VLDSX0Pred
    : AllOfSchedPreds<[FeatureSchedPredicate<TuneOptimizedZeroStrideLoad>,
                       MCSchedPredicate<CheckRegOperand<3, X0>>]>;
```

（`RISCVInstrPredicates.td:18-20`）

SSDL 没有 `MCSchedPredicate` 包装、没有 sort 标注、也没有“在 `variant` 上下文里自动包一层”的规则。**后果**：`variant` / `sti_decl default` / `tii_predicate` 的展开不是唯一的，宿主只能猜。

**最小修法**：把谓词分成两个语法层，例如 `mc_pred` 与 `sched_pred`；或者显式写 `sched(all_of(...))` / `mc(all_of(...))`，并在 `norm` 里给出提升规则。

### M3. 4.4 把“表达式谓词”展开成了 `Predicate<...>`，与 3.3 自相矛盾

文档 3.3 说表达式形式“对应 TD 的 `MCInstPredicate`”，但 4.4 写：

```text
predicate P = expr;
  → Predicate<展开后的 C++ 表达式>            [调度用]
```

`Predicate<code>` 是**指令选择**谓词，定义在 `TargetSubtarget.td:78`；调度侧的对应物是 `MCInstPredicate`（可包成 `MCSchedPredicate`）或 `SchedPredicate<code>`（`TargetSchedule.td:392`）。`Predicate<...>` 既不是调度谓词，也没有 `SchedModel`/`MI` 的作用域。4.4 与 3.3 必须二选一，不能同时成立。

**最小修法**：把 4.4 改成 `MCInstPredicate<...>`（或 `SchedPredicate<...>`，取决于是否允许 C++ 代码），并在 3.3 引用同一句。

### M4. `machine` 的“融合”没有定义记录拆分

`machine` 块里的字段分属三个 TD 记录：

| SSDL 字段 | 实际记录 | 证据 |
|---|---|---|
| `issue_width`、`micro_op_buffer_size`、`load_latency`、`mispredict_penalty`、`complete_model`、`enable_intervals` | `SchedMachineModel` | `TargetSchedule.td:76-124` |
| `features`、`tune_features` | `ProcessorModel` / `RISCVProcessorModel` | `TargetSubtarget.td:172-178`；`RISCVProcessors.td:76-87` |
| `default_march` | `RISCVProcessorModel.DefaultMarch` | `RISCVProcessors.td:76-87` |
| `machine X = Y;` | `ProcessorAlias` | `TargetSubtarget.td:180` |

`norm` 只写“所有块内实体/关系绑定 `SchedModel = X`”，没有说：

- `features`/`tune_features`/`default_march` 要不要同时生成一个 `ProcessorModel`？
- `machine X` 到底是 `SchedMachineModel`、`ProcessorModel`，还是两者？
- `machine X = Y` 生成 `ProcessorAlias` 后，`sti_function` 里的 `sched_model = X` 还能不能把 `X` 当 `SchedMachineModel` 用？
- `itineraries` 是挂到 `SchedMachineModel.Itineraries`，还是 `ProcessorModel.ProcItin`？

**后果**：宿主可以实现出两种都“看起来对”的展开，但 `-mcpu`/`-mattr`/`llc` 行为不同。这不是“实现细节”，是语言的核心语义。

**最小修法**：在 `norm` 里加一张字段到记录的映射表，并明确 `machine` 生成 `SchedMachineModel` + `ProcessorModel`（或引入 `processor`/`sched_model` 两个概念）。

### M5. `set` 的 `inst(attr_cond)` / `reg(attr_cond)` 没有属性模型

文档说“属性抽象分层：`set` 声明具名集合”，但只给了 `attr_expr` 的语法，没有给：

- 指令/寄存器的属性 universe 是什么；
- `category`、`logical`、`is_zero_reg` 从哪来；
- 属性类型（bool / int / string / enum）；
- 缺失属性怎么处理；
- 展开后的顺序、重复项、循环引用。

示例直接用了 `inst(category == logical)` 和 `reg(is_zero_reg)`，但 LLVM TableGen 的 `Instruction`/`Register` 并没有这两个通用字段。`CheckOpcode` 需要的是 `list<Instruction>`（`TargetInstrPredicate.td:226`），不是属性查询。**没有属性模型，4.1 的 `inst(attr_cond) → [满足条件的指令列表]` 无法实现。**

**最小修法**：定义一份 `set` 可见的属性 schema（字段名、类型、取值域、来源），并规定展开的确定性排序、去重、循环检测和缺失属性错误。

### M6. 顶层 `itineraries` 没有绑定到任何 `machine`

3.14 的顶层 `itineraries_decl` 没有 `sched_model` / `machine` 字段；4.2 的展开只覆盖 `machine X { ... }` 内部的 `itineraries`。因此：

- 顶层写 `itineraries { ... }` 是**孤立记录**，无法挂到 `SchedMachineModel.Itineraries`（`TargetSchedule.td:86`）；
- 实际 LLVM 的 `ProcessorItineraries` 需要被 `SchedMachineModel` 或 `ProcessorModel` 引用（`TargetItinerary.td:125`）；
- 9 个顶层关键字里的第 9 个，只有在 `machine` 内部才有用。

另外，`itin_class` 实体和 `itin` 声明都在创建 `InstrItinClass`，但文档没有说两者的关系；`map_itin` 只把 itinerary class 映射到 sched writes，没有把**指令**映射到 itinerary class。示例中的 `IIC_iALU`/`IIC_iLoad` 没有任何指令使用，属于装饰性声明。

**最小修法**：给顶层 `itineraries` 增加 `sched_model = IDENT`（或 `machine = IDENT`），并说明 `itin_class` 与 `itin` 的等价/覆盖规则；补上指令到 itinerary class 的绑定方式（或明确由宿主指令定义提供）。

### M7. `sti_function` 里的 `delegates` 没有落点

3.9 允许：

```text
sti_function X : Decl {
    delegates = [A, B];
    ...
}
```

但实际 `STIPredicate` 只有 `Declaration`、`Classes`、`SchedModel` 三个字段（`TargetInstrPredicate.td:415-420`）；`Delegates` 在 `STIPredicateDecl` 上（`TargetInstrPredicate.td:387-405`）。所以 `sti_function` 里的 `delegates` 要么静默丢弃，要么去改声明记录——两种都不是“声明语言”的局部语义。

**最小修法**：从 `sti_function_item` 删除 `delegates`，只保留在 `sti_decl`；如果确实需要函数级委托，新增一个明确的 `emit` 规则并说明它改的是哪个 record。

### M8. 缺少 `SchedPredicate<code>`，`non_portable` 不能替代

`non_portable(CODE_BLOCK)` 在 4.1 没有展开，3.6 对应的是 `CheckNonPortable`（`TargetInstrPredicate.td:239`）。但调度侧还有另一个东西：

```tablegen
SchedVar<SchedPredicate<[{ TII->isVRegCopy(MI, /*LMUL=*/1) }]>, [SMX60WriteVCOPY_M1]>
```

（`RISCVSchedSpacemitX60.td:1054-1057`）

`SchedPredicate<code>` 是 `SchedPredicateBase`（`TargetSchedule.td:392`），作用域是 `MI` + `SchedModel`；`CheckNonPortable` 是 `MCInstPredicate`，在 MC 层恒为 false（`TargetInstrPredicate.td:230-233`）。两者不能互换。SSDL 没有 `sched_predicate` / `sched_predicate_expr` 形式，因此**无法表达这类真实调度谓词**。

**最小修法**：新增 `sched_predicate(CODE_BLOCK)`（或 `sched_predicate { code = ...; }`），与 `non_portable` 分开；在 `norm` 里分别落到 `SchedPredicate<...>` 与 `CheckNonPortable<...>`。

### M9. `unsupported_features` 的类型写错了

3.11 写：

```text
unsupported_features = ident_list;
```

但 `SchedMachineModel.UnsupportedFeatures` 是 `list<Predicate>`（`TargetSchedule.td:117`），实际写法如：

```tablegen
let UnsupportedFeatures = [HasStdExtZbkb, HasStdExtZbkc, ...];
```

（`RISCVSchedSiFive7.td:1615-1618`）

`ident_list` 会让人以为这里放 feature 名；但 `UnsupportedFeatures` 要的是 predicate 名。**类型不对，生成会失败或语义相反。**

**最小修法**：改名为 `unsupported_predicates = predicate_ref_list`，或在 `norm` 里明确“这里写 feature 名，宿主自动取对应的 `HasX` predicate”。

### M10. 裸 `IDENT` 同时表示“集合名”和“单条指令/寄存器”

文法里没有命名空间：

```text
inst_ref = IDENT ;
reg_ref  = IDENT ;
opcode   = "opcode" "(" ( IDENT | set_expr ) ")" ;
reg_is   = "reg_is" "(" INT "," ( IDENT | reg_ref | set_expr ) ... ")" ;
equiv_class_def = ( "equiv_class" | "dep_breaking_class" ) IDENT
                  "(" ( IDENT | set_expr ) "," ... ")" ;
```

于是：

- `opcode(ZeroRegs)` 里 `ZeroRegs` 是集合名；
- `equiv_class OrrXzrXzr([ORR], ...)` 里 `[ORR]` 是集合；
- `equiv_class OrrXzrXzr(MySet, ...)` 里 `MySet` 又可能是单条指令；
- `reg_is(1, R1)` 里 `R1` 是寄存器，`reg_is(1, MySet)` 里 `MySet` 是集合；
- `opcode(inst(attr_cond))` 和 `opcode(reg(attr_cond))` 在语法上都合法，但后者是类型错误。

**没有 declared namespace / type-directed resolution 规则**，宿主无法在解析期决定 `IDENT` 的 sort。这与“语义完整”直接冲突。

**最小修法**：要么把引用写成 `set(Name)` / `inst(Name)` / `reg(Name)`，要么在 `set` 声明里记录元素 sort，并规定解析器在遇到裸 `IDENT` 时按“已声明集合优先 / 否则单条指令”解析，同时给出冲突错误。

---

## 4. P1：LLVM 事实与完整性缺口

这一节不是“语法不能解析”，而是“能解析，但展开后不是 LLVM 想要的东西”。

| ID | 问题 | 文档 | 实际 LLVM | 证据 |
|---|---|---|---|---|
| P1-1 | `inline_behavior` 对 `tune` 不可写 | 3.5 表：`tune` 不可写 `inline_behavior` | `RISCVTuneFeature` 有 `InlineBehavior ib = InlineIgnore` 参数 | `RISCVFeatures.td:1969-1978` |
| P1-2 | `version` 是字符串 | `version = "2.1"` | `RISCVExtension<int major, int minor, ...>` 是两个整数 | `RISCVFeatures.td:29` |
| P1-3 | `bitmask` 的 group 是任意 `INT` | `bitmask = { group = INT, bit = INT }` | `RISCVExtensionBitmask<bits<3> groupID, int bitPos>`；重复 group/bit 还会 assert | `RISCVFeatures.td:56`；`RISCVTargetDefEmitter.cpp:247-260` |
| P1-4 | `experimental` 没有名字前缀 | 只写 `experimental = true` | `RISCVExperimentalExtension` 会把 `-mattr` 名字改成 `experimental-...` | `RISCVFeatures.td:29-46,67-70` |
| P1-5 | `positive_directive` / `negative_directive` 可选 | 3.5 表：`tune` 支持写 | `RISCVTuneFeature` 构造函数要求两者 | `RISCVFeatures.td:1969-1978` |
| P1-6 | `desc` 在 `predicate` 里不是“描述” | `desc = "'I' (...)"` | `Predicate.PredicateName` / `AssemblerPredicate` 的 `name` 是诊断名 | `TargetSubtarget.td:78-118,127-133` |
| P1-7 | 只有 `AssemblerPredicate` 的谓词写不出来 | 块形式 `selector` 必写 | Mips `UseTCCInDIV : AssemblerPredicate<...>` 没有 `Predicate` 基类 | `MipsInstrInfo.td:246` |
| P1-8 | `reg_is(i, SetName) → reg_is(i, [list])` 没有对应类 | 4.1 展开为列表 | `CheckRegOperand<int, Register>` 只收**一个**寄存器 | `TargetInstrPredicate.td:147` |
| P1-9 | `pseudo` 展开成 `opcode` 会丢掉 MC 语义 | 4.1 `pseudo → opcode [非 MC 层]` | `CheckPseudo` 在 MC 层恒为 false | `TargetInstrPredicate.td:230-233` |
| P1-10 | `inst_set` 允许 `instrs` 与 `regex` 混写 | `inst_item = IDENT | regex(STRING)` | `InstRW` 的 `Instrs` 是单个 `dag`，`instrs` 与 `instregex` 是两个 operator；`MCOpcodeSwitchCase` 只收 `list<Instruction>` | `CodeGenSchedule.cpp:45-52,186-191`；`TargetInstrPredicate.td:280` |
| P1-11 | `advance` 的 `cycles` 可选，且 `INT` 不能为负 | 3.13 `[ ":" "cycles" = INT ]`；2 `INT = digit` | `ReadAdvance` 的 `cycles` 是必填 `int`；负 advance 有语义 | `TargetSchedule.td:337-347` |
| P1-12 | `variant` 强制 `default` | 3.13 要求 `default` | `SchedWriteVariant<list<SchedVar>>` 没有强制 default；`NoSchedPred` 才是默认谓词 | `TargetSchedule.td:400,447` |
| P1-13 | `retire_unit` 的 `max_per_cycle` 可选 | 3.12 可选 | `RetireControlUnit<int bufferSize, int retirePerCycle>` 两个都必填 | `TargetSchedule.td:596` |
| P1-14 | `RISCVProcessorModel` 的字段少了 | 只有 `features`/`tune_features`/`default_march` | 还有 `MVendorID`、`MArchID`、`MImpID`、`ConfigurableTuneFeatures` | `RISCVProcessors.td:76-87` |
| P1-15 | `default_march` 省略时的推导没写 | 4.5 只说“影响 -mcpu=X 且未指定 -march” | emitter 在 `DefaultMarch` 为空时从 features 计算 | `RISCVTargetDefEmitter.cpp:196` |
| P1-16 | 没有 `include` / 宿主 prelude | 9 个顶层关键字没有 include | 示例依赖 `ADD`/`LD`/`ORR`/`GPR64`/`FU_ALU`/`read_default` 等外部符号 | 见第 5 节 |
| P1-17 | 没有 forward-ref / duplicate / cycle / error 规则 | 全文没有 | 示例有 `StdExtZicsr` 被引用但未声明；`set` 可以用 `...` 递归 | 示例 + 3.2 |
| P1-18 | “一套 `[...]`”与文法不符 | 原则 6 说空列表、尾逗号、`...`、范围都支持 | 只有 `int_set` 有 `[a..b]`；`...` 只在 `set_expr` 与 `int_set` 里有；`ident_list`/`int_list`/`bit_list`/`sched_rw_list` 都没有；`inst_set` 有 `regex` 但没有范围 | 3.15 |
| P1-19 | `all_of` 有两套调用形状 | 3.6 `all_of(a, b, ...)`；3.7 `all_of(ident_list)`；示例两者都用 | 语法上可区分，但“n 元组合子”的用户模型不统一 | 3.6/3.7 |
| P1-20 | `attr_expr` 是左递归且没有优先级 | 3.16 | 需要 `&&`/`||`/`!` 的优先级和结合性，否则语法歧义 | 3.16 |

> P1 不是“可以后补”的清单。P0-M 修完后，这些会决定生成的 `.td` 是否和现有后端逐字节/逐行为一致。

---

## 5. 旗舰例子：这不是 RISC-V SiFive7，而是 AArch64 + OoO + RISC-V 的混合体

这一节单独列，因为“完整例子”是文档最有说服力的部分，也是目前最容易被社区一眼看穿的部分。

### 5.1 指令、寄存器、零寄存器习语是 AArch64 的

| 示例 | 归属 | 实际 |
|---|---|---|
| `ORR`、`EOR`、`OrrXzrXzr` | AArch64 | RISC-V 定义的是 `OR`/`XOR`（`RISCVInstrInfo.td:927,933`），没有 `ORR`/`EOR` |
| `GPR64`、`GPR32` | AArch64 | `AArch64RegisterInfo.td:202,207` 定义的 class；RISC-V 的寄存器类是 `GPR`（`RISCVRegisterInfo.td:318`） |
| `IsZeroReg` / `OrrXzrXzr` | AArch64 的 `XZR` 零寄存器习语 | RISC-V 的零寄存器是 `X0`，不是 `ORR XZR` 形式 |

所以 `set LogicalInsts = [ORR, EOR, AND, ...]`、`map [ORR]`、`variant ... opcode([ORR])`、`sti_function ... [ORR]/[EOR]`、`tii_predicate case [ORR]/[EOR]` 都不是 RISC-V。**这不是“示例用了伪指令”，而是把 AArch64 的零寄存器优化原样搬到了 RISC-V 标题下。**

### 5.2 SiFive7 的实际参数与示例相反

| 项 | 示例 | 实际 `SiFive7SchedMachineModel` | 证据 |
|---|---|---|---|
| `IssueWidth` | `issue_width = 4` | `2` | `RISCVSchedSiFive7.td:1610` |
| `MicroOpBufferSize` | `micro_op_buffer_size = 192` | `0`，注释明确“SiFive7 is in-order” | `RISCVSchedSiFive7.td:1609` |
| `LoadLatency` | `load_latency = 3` | `3` | `:1611` |
| `MispredictPenalty` | `mispredict_penalty = 9` | `3` | `:1612` |
| `CompleteModel` | `complete_model = false` | `0` | `:1613` |
| `EnableIntervals` | 未写 | `true` | `:1614` |

更严重的是：`micro_op_buffer_size = 192` + `reg_file`（180 个物理寄存器）+ `allow_move_elim` + `retire_unit`（320/6）描述的是一个 **OoO 核**；而实际 SiFive7 的 `MicroOpBufferSize = 0`，注释直接说它是 in-order。把这两组参数放在同一个 `machine siFive7` 里，**模型自相矛盾**。

### 5.3 SiFive7 的实际处理器记录也不是这样

实际 RISC-V 的 `SIFIVE_7` 是 tune processor：

```tablegen
defvar SiFive7TuneFeatures = [TuneSiFive7, TuneNoDefaultUnroll,
                              TuneShortForwardBranchIALU,
                              TunePostRAScheduler];
def SIFIVE_7 : RISCVTuneProcessorModel<"sifive-7-series",
                                       SiFive7Model, SiFive7TuneFeatures>;
```

（`RISCVProcessors.td:159-163`）

它**没有** `features` 列表；示例却给了 `StdExtI/M/A/F/D/C` 一整套架构特性，并且 tune 只写了两个（少了 `TuneShortForwardBranchIALU`、`TunePostRAScheduler`）。`generic-rv64` 实际是：

```tablegen
def GENERIC_RV64 : RISCVProcessorModel<"generic-rv64",
                                       SpacemitX60Model,
                                       [Feature64Bit, FeatureStdExtI],
                                       GenericTuneFeatures>;
```

（`RISCVProcessors.td:108-115`）

示例却加了 `StdExtZifencei`/`StdExtZicsr`，并且没有 `SpacemitX60Model`。

### 5.4 SiFive7 不使用 itinerary

RISC-V 后端没有 `InstrItinData` 定义（`grep -rn 'InstrItinData' llvm/lib/Target/RISCV` 为空），`SiFive7SchedMachineModel` 也没有设置 `Itineraries`。示例却在 `machine siFive7` 里写了两条 `itin`，而且没有任何指令通过 `itin_class` / `map_itin` 使用它们。**这两条 itinerary 是装饰性的，不会影响调度。**

### 5.5 示例里的未声明符号

即使不讨论目标事实，示例也不是“完整”的：

```text
ORR, EOR                     # AArch64，RISC-V 无
GPR64, GPR32                 # AArch64 寄存器类
FeatureStdExtZifencei        # 被引用，未声明
StdExtZicsr                  # 被引用，未声明
StdExtM/A/F/D/C              # 被引用，未声明
TuneSiFive7                  # 被引用，未声明
TuneNoDefaultUnroll          # 被引用，未声明
read_default                 # 未声明；TD 里是 ReadDefault
no_bypass                    # 未声明；TD 里是 NoBypass
FU_ALU, FU_LSU               # 未用 func_unit 声明
Bypass_ALU_LD                # 未用 bypass 声明
```

有些可以是宿主 prelude（`read_default`/`no_bypass`），有些必须是 `feature`/`entity_decl`（`FU_ALU`/`Bypass_ALU_LD`），但文档没有给出 prelude 清单，也没有给出“未声明符号”的报错规则。**“完整例子”目前不能作为验收语料。**

---

## 6. 哪些是真正的 P0，哪些可以后补

如果要把 SSDL 从“设计稿”推进到“可实现的规范”，建议按下面顺序：

### P0（不修就无法实现）

1. **补 S1–S5 的文法**：`predicate` 参数与命名谓词引用、`feature` 字段分隔、`resource` 属性分隔、带连字符的名字（或字符串名）。
2. **定义谓词的两个 sort**：`MCInstPredicate` vs `SchedPredicateBase`，以及 `MCSchedPredicate` 包装/提升规则（M2）。
3. **修正 4.4**：表达式谓词落到 `MCInstPredicate` 或 `SchedPredicate`，不是 `Predicate<...>`（M3）。
4. **定义 `feature` 的 `Name`/`-mattr` 映射**（M1）。
5. **定义 `machine` 到 `SchedMachineModel`/`ProcessorModel`/`ProcessorAlias` 的拆分**（M4）。
6. **定义 `set` 的属性 schema 与确定性展开**（M5）。
7. **给顶层 `itineraries` 加绑定**，并说明 `itin_class`/`itin`/`map_itin` 的关系（M6）。
8. **处理 `sti_function.delegates`**（M7）。
9. **补 `SchedPredicate<code>`**，与 `non_portable` 分开（M8）。
10. **修正 `unsupported_features` 类型**，并解决裸 `IDENT` 的 sort 歧义（M9/M10）。

### P1（决定“语义等价”是否成立）

- 按第 4 节的表逐项对齐 `RISCVExtension` / `RISCVTuneFeature` / `SubtargetFeature` 的字段与默认值。
- 给 `set`/`feature`/`machine` 加 forward-ref、duplicate、cycle、类型错误的规则。
- 给 `all_of`/`any_of`/`none_of` 明确规定空参数语义（true/false），因为 LLVM 的 `CheckAll<[]>`/`CheckAny<[]>`/`AllOfSchedPreds<[]>` 需要唯一答案。
- 给 `INT` 加符号，或明确 `advance` 不支持负值并说明这是有意的限制。
- 统一 `[...]` 的能力表：哪些 list 支持 `...`、`[a..b]`、`regex`、尾逗号；不要再声称“一套走天下”而文法只有 `int_set` 支持全部。
- 给 `predicate` 的 `desc` 改名（`predicate_name`）或说明它映射到 `PredicateName` 而不是描述。

### P2（文档质量）

- `CODE_BLOCK` 的 `any_char` 终止条件、`STRING` 的 escape、`COMMENT` 的 trivia 规则。
- `attr_expr` 的优先级/结合性。
- `retire_unit`/`variant` 的默认值。
- `bind` 的 `release`/`acquire` 与资源列表的长度校验。

---

## 7. 建议的验收方式（没有这个，“final”无法证伪）

SSDL 目前最大的问题不是“有 bug”，而是**没有可执行判据**。建议最小验收链：

1. 写一个 SSDL parser（哪怕只覆盖 1.0 的文法），把 `history/2026-11-16-ssdl-v1.0-final.md` 第 5 节作为唯一输入；
2. 解析失败即 P0，不能靠“宿主预处理”绕过；
3. 解析成功后，按 `norm` 生成 `.td`；
4. 对同一个目标，把生成的 `.td` 与手写 `.td` 分别跑 `llvm-tblgen`，比较：
   - `-gen-subtarget` 的 `SubtargetFeatureKV` / `ProcessorModel` / `SchedMachineModel` 表；
   - `-gen-sched-model` 的 `WriteRes`/`InstRW`/`ReadAdvance`/`SchedWriteVariant`；
   - `-gen-instr-info` 的 `TIIPredicate` / `STIPredicate`；
5. 至少加三个负控：
   - 未知 feature 名字必须报错；
   - `opcode(SetName)` 的 set 元素不是指令必须报错；
   - `feature` 的 `kind` 与字段不匹配必须报错。

没有第 1 步，第 2–5 步都是空谈；没有第 4 步，就无法证明“语义完整的替代品”。

---

## 8. 最终判断

| 维度 | 判断 |
|---|---|
| 作为设计提案 | **强**。9 个关键字、`kind` 分层、`machine` 融合、关系式 `bind`/`map`/`advance` 都值得继续。 |
| 作为 1.0 Final 规范 | **不成立**。5 处示例不可解析，10 处语义不可唯一展开，旗舰例子与 LLVM 事实不符。 |
| 作为“语义完整的替代品” | **目前不成立**。没有属性模型、没有 sort 系统、没有 `Name` 映射、没有验收链。 |
| 最应该先做的一件事 | 把第 5 节示例改成**一个真实目标**（要么 AArch64，要么 RISC-V；不要混合），并让它通过一个最小 parser。 |

**建议的下一步**：不要继续加关键字。先写一个 200–400 行的 parser + 一个只覆盖 `feature`/`machine`/`bind`/`map` 的 emitter，把示例跑通；跑通后再谈 `sti`/`tii`/`itineraries`。parser 会把上面一半的争议直接变成编译错误，剩下的一半才是设计问题。
