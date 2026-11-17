# SSDL 领域模型：每个语法元素在上游的消费点

- date: 2026-11-16
- status: **分析（非规范）**；规范性修订见 §6，落地状态见 `2026-11-16-ssdl-parser-notes.md` §12
- 证据基线：`$SSDL_LLVM_SRC` @ `700e551714cc`
- 方法：五个并行只读审计，逐条引用上游 `file:line`；本文只写"上游怎么用"，不写"我们希望它怎么用"
- 结论口径：凡是本文说"有消费者"的，都给出调用点；说"无消费者"的，给出搜索范围（`llvm/lib`、`llvm/include`、`llvm/tools`）

---

## 0. 一句话

SSDL 现在描述的是**记录的形状**，而上游真正约束的是**消费者的契约**。两者的差集就是本轮所有"看起来能展开、一接就炸"的问题：`-gen-subtarget` 失败、两机模型撞名、谓词在 llvm-mca 里静默退化、属性 `?` 被当成 `false`。

因此领域建模的输出不是又一张语法表，而是四张表：**所有权（谁拥有这个名字）→ 记录 → 消费者 → 约束**。

---

## 1. 十条结论（按影响排序）

1. **`write`/`read` 是目标级词表，`resource` 是模型级词表。** 上游 `WriteIALU` 定义在 `llvm/lib/Target/RISCV/RISCVSchedule.td:16`，被指令的 `Sched<[WriteIALU, ...]>` 引用（`RISCVInstrInfo.td:786`）；模型只提供 `WriteRes<WriteIALU, [PipeAB]>`（`RISCVSchedSiFive7.td:335`）。SSDL 把两者都放进 `machine`，导致两个 machine 必然冲突；v1.1 已按 R1 修正（词表提升 + 机器级记录前缀）。
2. **部分模型不可行。** 一个模型只要有一条 `WriteRes`，`hasInstrSchedModel` 即为真（`utils/TableGen/Common/CodeGenSchedule.h:279-281`），随后 `findWriteResources` 要求覆盖该指令集可达的**每一个** `SchedWrite`/`SchedRead`，否则 fatal `Processor does not define resources for <Write>`（`utils/TableGen/SubtargetEmitter.cpp:984-989`）。没有 base-model 回退，源码里是显式 TODO（同上 `:983`）。这解释了 §7 的 `-gen-subtarget` 失败。
3. **`complete_model` 只是完整性检查的开关，不是"允许缺资源"的开关。** `CompleteModel=false` 只跳过 `checkCompleteness()`（`CodeGenSchedule.cpp:1921-1922`），不跳过 `findWriteResources`；未建模的打包类会退化成 `InvalidNumMicroOps`（`SubtargetEmitter.cpp:1174-1179`）。
4. **属性的取值域是三态，不是布尔。** `mayLoad`/`mayStore`/`hasSideEffects` 可以是 `?`（未推断）。RISCV 的 `-print-records` 里 `mayLoad = ?` 出现 190 次、`hasSideEffects = ?` 169 次；限定到 Instruction 记录是 51 / 37 条（16966 条 Instruction 中）。把"未知"读成 `false` 会把可能访存的指令判成纯计算。
5. **STI 谓词（`isDependencyBreaking`）只有 llvm-mca 一个消费者。** 唯一调用点是 `llvm/lib/MCA/InstrBuilder.cpp:724`；`TargetSubtargetInfo::isDependencyBreaking`（`include/llvm/CodeGen/TargetSubtargetInfo.h:202`）在树内没有调用者。生成的 MI 版本是死代码。
6. **`TIIPredicate` 是真正被到处调用的。** `RISCVInstrInfo.cpp:849`、`RISCVInsertVSETVLI.cpp:387`、`AArch64MacroFusion.cpp:83` 等直接调生成函数。同为"谓词"，两者语义地位完全不同。
7. **谓词操作数索引有两套空间。** 谓词体用 `getOperand(Index)`（原始下标，含 def），而 `updatesOpcodeMask` 的掩码位用 MCA 的 use 索引（排除 def、追加隐式操作数），且同一份函数体会被同时展开成 `MachineInstr` 与 `MCInst` 两个版本（`utils/TableGen/SubtargetEmitter.cpp:1949` 附近）。DSL 目前只有一个裸下标。
8. **`PredicateProlog` 只进 MI 路径。** 它被拼进 `resolveSchedClass(unsigned, const MachineInstr *MI, const TargetSchedModel *)`（`SubtargetEmitter.cpp:1943-1949`），不在 `resolveVariantSchedClass` 的 MC 路径里。prolog 里引用 `MI`/`SchedModel` 可行，引用 MC 层符号不可行。
9. **`feature kind` 不是运行时边界。** plain/extension/tune 全部编进同一张 `SubtargetFeatureKV` 与同一个 `FeatureBitset`；区分的只有发射路径与元数据表：extension 的 version/bitmask 只存在于 RISC-V TargetParser 表（`RISCVTargetDefEmitter.cpp:245-260`，查询 `RISCVISAInfo.cpp:1173-1181`），MC 层只看布尔位。`Implies` 还有两套独立闭包（MC bitset 与 `RISCVISAInfo::updateImplication`）。
10. **有消费者不等于有语义。** `InstRW.Unsupported` 无任何读取点；`FullInstRWOverlapCheck` 只在 `CodeGenSchedule.cpp:1040` 做重叠校验；`RetireOOO`、`RegisterFile`、`RetireControlUnit`、`LoadQueue`/`StoreQueue` 只进 llvm-mca；`LoopMicroOpBufferSize` 只被 TTI 当展开阈值（`BasicTTIImpl.h:755`）。

---

## 2. 所有权：三类名字，三种命运

上游 TableGen 的 `def` 名是**全局唯一**的，`SchedModel` 字段只是给记录打归属标签。因此"这个名字归谁"决定了能不能写第二个模型。

| 层 | SSDL 现在的声明 | 生成的记录 | 有 `SchedModel` 字段 | def 名要求 | 上游同类 |
|---|---|---|---|---|---|
| target-global | `write W;` | `SchedWrite` | 否 | 全目标唯一，被指令 `Sched<[...]>` 引用 | `RISCVSchedule.td:16` |
| target-global | `read R;` | `SchedRead` | 否 | 同上 | `RISCVSchedule.td` |
| target-global | `write_seq X = [...];` | `WriteSequence` | 有，但 TableGen 显式忽略 | 全局 | `TargetSchedule.td:241` |
| target-global | `func_unit` / `bypass` / `itin_class` | `FuncUnit` / `Bypass` / `InstrItinClass` | 否 | 全局 | `TargetItinerary.td:25-140` |
| machine | `resource` / `res_group` | `ProcResourceUnits` / `ProcResGroup` | 是 | **全局唯一**（上游靠 `defm` 前缀造名字） | `RISCVSchedSiFive7.td:275,302` |
| machine | `bind W -> [R], ...` | `WriteRes` | 是 | 可匿名 | `TargetSchedule.td:310` |
| machine | `advance R : cycles = N` | `ReadAdvance` | 是 | 可匿名 | `TargetSchedule.td:347` |
| machine | `map [...] -> [...]` | `InstRW` | 是 | 可匿名 | `TargetSchedule.td:464` |
| machine | `map_itin [...] -> [...]` | `ItinRW` | 是 | 可匿名 | `TargetSchedule.td:477` |
| machine | `alias SRC -> DST` | `SchedAlias` | 是 | 全局（我们带前缀） | `TargetSchedule.td:490` |
| machine | `variant` / `read_variant` | `SchedWriteVariant` / `SchedReadVariant` | 是（`SchedVar` 自身无） | 全局 | `TargetSchedule.td:426-437` |
| machine | `reg_file` | `RegisterFile` | 是 | 全局 | `TargetSchedule.td:574` |
| machine | `retire_unit` / `load_queue` / `store_queue` | `RetireControlUnit` / `LoadQueue` / `StoreQueue` | 是 | 全局 | `TargetSchedule.td:596-610` |
| processor | `machine` 的 `features` / `tune_features` / `default_march` | `RISCVProcessorModel` / `ProcessorAlias` | — | 全局 | `RISCVProcessors.td:76-87` |
| global | `predicate` / `predicate_prolog` / `sti_decl` / `sti_function` / `tii_predicate` | `MCInstPredicate` 子树 / `PredicateProlog` / `STIPredicateDecl` / `STIPredicate` / `TIIPredicate` | 仅 `STIPredicate` 有 | 全局 | `TargetInstrPredicate.td` 等 |

### 2.1 上游怎么解决"第二个模型"

`SiFive7` 一族有三个模型，共享一份描述，做法是 `multiclass` + `defm`：资源名变成 `SiFive7PipeA`，`WriteRes` 写成匿名 `def : WriteRes<WriteIALU, [...]>`（`RISCVSchedSiFive7.td:1564, 311-322`）。注意它**没有**把 `WriteIALU` 复制成模型私有名字——共享词表 + 模型私有资源，是上游的硬约束，不是风格。

### 2.2 SSDL 的偏差

`write`/`read` 被放进 machine 块，`emit_td.py` 按 machine 逐个发射 `def W_ALU : SchedWrite;`。于是：

```text
$ python3 tools/ssdl/emit_td.py /tmp/twomach.ssdl
machine B: error: emitted def 'W_ALU' collides with 'machine A'
machine B: error: emitted def 'P' collides with 'machine A'
machine B: error: emitted def 'WR_W_ALU' collides with 'machine A'
```

`P` 撞名是**正确**的（资源本就该按模型区分，见 §6 R2）；`W_ALU` 撞名是**建模错误**（词表被私有化）。

---

## 3. 消费层：一个字段只服务一层

| 消费层 | 代表消费者 | 吃哪些字段 | 判定方法 |
|---|---|---|---|
| 指令调度（codegen） | `MachineScheduler.cpp`、`TargetSchedule.cpp`、`ScheduleDAGInstrs.cpp`、`PostRASchedulerList.cpp` | `WriteRes.Latency/NumMicroOps/ReleaseAtCycles/BeginGroup/EndGroup`、`ReadAdvance.Cycles`、`InstRW`、`SchedAlias`、`SchedWriteVariant`、`IssueWidth`、`MicroOpBufferSize`、`PostRAScheduler`、`EnableIntervals` | 只要出现在 `lib/CodeGen` 的 `getSchedModel()`/`TargetSchedModel` 调用链里 |
| llvm-mca | `MCA/InstrBuilder.cpp`、`MCA/Stages/*`、`MCA/HardwareUnits/*` | `ReleaseAtCycles`、`RetireOOO`、`RegisterFile`/cost、`RetireControlUnit`、`LoadQueue`/`StoreQueue`、`Unsupported`、STI dep-breaking 谓词 | 只出现在 `lib/MCA` |
| TTI / 代价模型 | `BasicTTIImpl.h`、`SelectOptimize.cpp`、`EarlyIfConversion.cpp`、`TargetInstrInfo::getInstrLatency` | `LoopMicroOpBufferSize`、`MispredictPenalty`、`LoadLatency`/`HighLatency` | 出现在 `lib/Analysis`、`lib/CodeGen/*TransformInfo*`、代价函数 |
| TableGen 校验 | `CodeGenSchedule.cpp`、`SubtargetEmitter.cpp` | `FullInstRWOverlapCheck`、`UnsupportedFeatures`、`CompleteModel` | 只在 `utils/TableGen` 里读 |
| driver / TargetParser | `RISCVTargetParser.cpp`、clang `RISCV.cpp`、`RISCVISAInfo.cpp` | `DefaultMarch`、tune directive 名、extension version/bitmask | 只在 `clang/lib/Driver`、`lib/TargetParser` |
| isel / asm matcher | `DAGISelMatcherEmitter.cpp`、`AsmMatcherEmitter.cpp` | `Predicate`、`AssemblerPredicate` | 只生成匹配表 |
| 无消费者 | — | `InstRW.Unsupported` | 全树搜索无读取点 |

三条最容易踩的分歧，写进规范前必须知道：

1. **`AcquireAtCycles` 在 MCA 里不读。** MCA 只构造 `CycleSegment(0, ReleaseAtCycle)`（`lib/MCA/InstrBuilder.cpp:99`），把 acquire 当 0。SSDL 的 `bind ... acquire = [...]` 只影响 codegen。
2. **`InstRW.Unsupported` 是死字段。** `WriteRes.Unsupported`/`ReadAdvance.Unsupported` 会发 `InvalidNumMicroOps`（`SubtargetEmitter.cpp:1206,1324`），`InstRW` 的同名字段没有任何读取点（`TargetSchedule.td:469` 只是声明）。
3. **`SchedPredicate<code>` 在 MCA 里静默退化。** MCA 只展开 `MCSchedPredicate`/`FeatureSchedPredicate`（`SubtargetEmitter.cpp:1785` 的 `collectVariantClasses(..., true)`）；含 C++ 代码的 `SchedPredicate` 在 MCA 一律走 default 分支，而 codegen 会正常求值（`:1942`）。同一个 `variant` 在两层的语义不同。

---

## 4. 逐元素契约

### 4.1 `set`

- 编译期构造，**不产生任何记录**；上游没有对应物。
- 唯一的语义来源是宿主 schema。合法属性域见 §4.7 与 D13。
- 结论：`set` 的正确性完全取决于 schema 的保真度，而 `-print-records` 只能给出**解析后、推断前**的值（三态，见 D5）。

### 4.2 `predicate`

| 形态 | 生成的记录 | 消费者 | 约束 |
|---|---|---|---|
| 表达式（MC sort） | `CheckOpcode` / `CheckRegOperand` / `CheckAll` ... | 被 `variant`、`sti_decl.default`、`tii_predicate`、`InstRW` 引用 | 同一函数体同时展开为 MI 与 MCInst 两个版本（`D8`） |
| 表达式（SCHED sort） | `MCSchedPredicate<...>` / `FeatureSchedPredicate<...>` | codegen `resolveSchedClass`；MCA 只认这两类 | 含 C++ 的 `SchedPredicate<code>` 在 MCA 退化为 default（§3-3） |
| 块形式 `selector` | `Predicate<"...">` | isel 的 `CheckPatternPredicate`（`DAGISelMatcherEmitter.cpp:1347`） | 只影响指令选择，不是调度 |
| 块形式 `assembler` | `AssemblerPredicate<(all_of ...), "desc">` | asm matcher 的 `ComputeAvailableFeatures`（`AsmMatcherEmitter.cpp:4013`） | 与 `Predicate` 是两份手写同步的表示，DSL 必须同时发或显式声明只发一份 |

### 4.3 `predicate_prolog`

- 唯一注入点：`resolveSchedClass`（`SubtargetEmitter.cpp:1943-1949`）。
- 可用符号：`MI`（`const MachineInstr *`）、`SchedModel`；`Subtarget` 是该生成类的成员。
- 不可用：MC 层符号、`resolveVariantSchedClass` 路径的任何东西。
- 结论：prolog 是"调度谓词的局部变量"，不是通用函数体前缀。写进规范时应限定它只能被 SCHED sort 的谓词引用。

### 4.4 `feature`

| `kind` | 发射点 | 运行时效果 | 元数据去向 |
|---|---|---|---|
| `plain` | `SubtargetFeature<name, field, value, desc>` | 置位 `FeatureBitset` 中的一位；`Implies` 由 `MCSubtargetInfo` 运行期闭包 | 无 |
| `extension` | 同上 + `RISCVExtension<major,minor,...>` | 同上；MC 只看布尔位 | version/experimental → `SupportedExtensions`；bitmask → `ExtensionBitmask`（`RISCVTargetDefEmitter.cpp:245-260`）；查询 `getRISCVFeaturesBitsInfo`（`RISCVISAInfo.cpp:1173-1181`） |
| `tune` | 同上 + `RISCVTuneFeature<pos,neg,...>` | 同上；`TuneFeatures` 与 `Features` 是同一行的两个掩码（`SubtargetEmitter.cpp:388-390`） | directive 名只被 `parseTuneFeatureString` 读（`RISCVTargetParser.cpp:253-345`），调用方是 clang `-mtune`（`Driver/ToolChains/Arch/RISCV.cpp:433-471`） |

- `SubtargetFeature` 只有 `Name/FieldName/Value/Desc/Implies/InlineBehavior` 六个字段（`TargetSubtarget.td:31-63`），没有 `InitValue`。SSDL 的 `mattr`（E1）对应 `Name`。
- `Desc` 只用于 `-mattr=help` 之类的文本（`MCSubtargetInfo.cpp:113`）。
- `InlineBehavior` 的四个值与上游 def 一一对应：`InlineIgnore/InlineDefault/InlineInverse/InlineMustMatch`（`TargetSubtarget.td:17-26`）；默认值因基类而异（plain 默认 `InlineDefault`，RISC-V tune 默认 `InlineIgnore`，`RISCVFeatures.td:1973`）。
- `Implies` **不是**可互换的两套语义：MC 侧是 bit 闭包（`MCSubtargetInfo.cpp:38-57`），RISC-V ISA 侧是 `updateImplication`（`RISCVISAInfo.cpp:870-893`，单向、不处理 inverse）。
- `default_march` 不是 `ProcessorModel` 字段，只在 `RISCVProcessorModel` 上（`RISCVProcessors.td:76-87`），被 `RISCVTargetDefEmitter.cpp:196-202` 发出，消费者只有 TargetParser 与 clang driver，`RISCVSubtarget` 不读。

### 4.5 `sti_decl` / `sti_function` / `tii_predicate`

| SSDL | TD | 真实消费者 | 备注 |
|---|---|---|---|
| `sti_decl D { default = P; overrides = b; expand_for_mc = b; opcode_mask = b; delegates = [...]; }` | `STIPredicateDecl<name, default, overrides, expandForMC, updatesOpcodeMask, delegates>`（`TargetInstrPredicate.td:387-409`） | 被 `STIPredicate` 引用 | `opcode_mask` 的真实字段名是 `UpdatesOpcodeMask`：声明"这个函数多一个 in/out APInt 掩码参数" |
| `sti_function F : D { dep_breaking_class ... }` | `STIPredicate<D, [classes]>` + `let SchedModel` | **llvm-mca 的 `InstrBuilder.cpp:724`** | MI 侧生成函数在树内无调用者（D7） |
| `dep_breaking_class C([ops], P, [indices])` | `DepBreakingClass<[ops], P, BrokenDeps>`（`TargetInstrPredicate.td:374-376`） | 同上 | `BrokenDeps` 是"每个隐式/显式输入操作数一位"的位集；空集表示"显式输入寄存器互相独立"（`:373`）；掩码**不由谓词推导** |
| `tii_predicate T = return P;` / `switch opcode` | `TIIPredicate<"T", MCReturnStatement<P>>` / `MCOpcodeSwitchStatement<...>` | 目标代码直接调用：`RISCVInstrInfo.cpp:849`、`AArch64MacroFusion.cpp:83` 等 | 这是唯一"生成即被用"的谓词族 |

- `expand_for_mc` 默认 `true`：`CheckPseudo`/`CheckNonPortable` 在 MCA 版里静默变 `false`。
- 操作数索引空间分裂（D8）：谓词体 `getOperand(i)` 是 MI 原始下标；掩码位是 MCA use 索引。`RISCVInstrPredicates.td:12-19` 明文记录了伪指令在两套编号下的差异。
- 同一 opcode 在同一个谓词/CPU 上重复覆盖是 fatal（`CodeGenSchedule.cpp` 的重叠检查路径）。

### 4.6 `machine` 标量

| SSDL 字段 | TD 字段 | 消费者 | 备注 |
|---|---|---|---|
| `issue_width` | `IssueWidth` | `MachineScheduler.cpp:3020`、`VLIWMachineScheduler.cpp`、`ResourcePriorityQueue.cpp:314` | |
| `micro_op_buffer_size` | `MicroOpBufferSize` | `MachineScheduler.cpp:3130,3161,3264,4065` | `0` = 顺序核 |
| `loop_micro_op_buffer` | `LoopMicroOpBufferSize` | 只有 `BasicTTIImpl.h:755` | TTI 展开阈值，不是调度 |
| `load_latency` / `high_latency` | `LoadLatency` / `HighLatency` | `TargetInstrInfo.cpp:1813-1815`（`getInstrLatency`） | 缺省延迟回退 |
| `mispredict_penalty` | `MispredictPenalty` | `SelectOptimize.cpp:1424`、`EarlyIfConversion.cpp:1249` | 代价启发式 |
| `post_ra_scheduler` | `PostRAScheduler` | `TargetSubtargetInfo.cpp:65` | |
| `complete_model` | `CompleteModel` | `CodeGenSchedule.cpp:1921` + 运行期 `TargetSchedule.cpp:221-228` 的 `llvm_unreachable` | 见结论 3 |
| `full_inst_rw_overlap_check` | `FullInstRWOverlapCheck` | 只有 `CodeGenSchedule.cpp:1040` | 纯校验开关 |
| `enable_intervals` | `EnableIntervals` | `TargetSchedule.cpp:345` | |
| `unsupported_features` | `UnsupportedFeatures` | `CodeGenSchedule.cpp:1926,2165` | 只影响完整性检查 |

### 4.7 机器块内的记录

| SSDL | TD | 消费者层 | 关键语义 |
|---|---|---|---|
| `resource R : units = n, buffer = m` | `ProcResourceUnits` | codegen 资源表 + MCA | `BufferSize` 决定乱序窗口建模；有资源但无 `WriteRes` 是 fatal（`SubtargetEmitter.cpp:1531`） |
| `bind W -> [R], latency = n, uops = m, release = [...], acquire = [...], begin_grp, end_grp, single, retire_ooo, unsupported` | `WriteRes` | codegen / MCA | `release` 为空 ⇒ 每个资源各占 **1 拍**，不是 `Latency`（`SubtargetEmitter.cpp:1251-1254`；2026-11-17 更正）；`acquire` 为空 ⇒ 0 拍（`:1256-1260`）；两个数组长度必须相等（`:1263-1266`）；`single` 在发射时折进 `BeginGroup/EndGroup`（`:1214-1215`），无独立运行时字段；`retire_ooo` 只被 MCA 读（`MCA/InstrBuilder.cpp:713`） |
| `advance R : cycles = n, writes = [...], tunables = [...]` | `ReadAdvance` | codegen | `cycles` 可负；`tunables` 长度不得超过 `writes`（`TargetSchedule.td:335-337` 的 assert） |
| `map [...] -> [RW...]` | `InstRW` | codegen | **覆盖**该 CPU 上指令的 `SchedRW`（`SubtargetEmitter.cpp:1150-1166`）；要求指令已有 sched class，否则 fatal `No sched class for instruction.`（`CodeGenSchedule.cpp:1013`）；`Unsupported` 死字段 |
| `map_itin [...] -> [RW...]` | `ItinRW` | codegen | 只在 `Writes.empty()` 时被查（审计结论；属 itinerary 兼容路径） |
| `alias SRC -> DST` | `SchedAlias` | codegen | 逐处理器解析（`SubtargetEmitter.cpp:766-800`）；alias 套 alias 是 fatal，同一 RW 重复 alias 是 fatal |
| `variant W { case P: [...] default: [...] }` | `SchedWriteVariant<[SchedVar<...>]>` + `SchedAlias` | codegen；MCA 仅 MC/Feature 谓词 | 必须显式 `default`；`SchedVar` 自身无 `SchedModel`（`TargetSchedule.td:430` 显式忽略）；subject 是 dispatch 名，分支里的 subject 自引用要实体化成 `SchedWriteRes`/`SchedReadAdvance`（M13） |
| `write_seq X = [...] repeat = n` | `WriteSequence` | codegen | `SchedModel` 字段被显式忽略（`:240`） |
| `reg_file F { num_phys; classes; costs; allow_move_elim; max_move_elim_per_cy; allow_zero_elim_only }` | `RegisterFile` | **只有 MCA**（`MCA/HardwareUnits/RegisterFile.h:212`） | codegen 不读 |
| `retire_unit { buffer; max_per_cycle }` | `RetireControlUnit` | **只有 MCA**（`lib/MCA/HardwareUnits/RetireControlUnit.cpp:22`） | 顺序核上 MCA 直接断言不可用（`:27`） |
| `load_queue` / `store_queue` | `LoadQueue` / `StoreQueue` | **只有 MCA** | codegen 不读 |

### 4.8 `itineraries`

- 记录族：`FuncUnit`/`Bypass`/`InstrStage`/`InstrItinClass`/`InstrItinData`/`ProcessorItineraries`（`TargetItinerary.td:25-140`），经 `SchedMachineModel.Itineraries`（`TargetSchedule.td:86`）挂载。
- 消费者：`InstrItineraryData`（`MCInstrItineraries.h:117-240`）、`TargetSchedModel`（`TargetSchedule.cpp:36-38` 等优先用 itinerary）、hazard recognizer、PostRA、MachinePipeliner。
- **llvm-mca 与 itinerary 互斥**：`tools/llvm-mca/llvm-mca.cpp:446-449` 直接报错，`MCA/InstrBuilder.cpp:556-557` 断言 `hasInstrSchedModel`。
- 结论：itinerary 是"老目标的完整模型"，不是 `WriteRes` 的补充；同一 machine 同时给两套，等于给 MCA 一个不可用配置。

### 4.9 属性宇宙（`set` 的定义域）

- 可信且 load-bearing：`isTerminator`、`isBarrier`、`isBranch`、`isCall`、`isReturn`（+`isIndirectBranch`、`isEHScopeReturn`）、`mayLoad`、`mayStore`、`hasSideEffects`、`isPseudo`、`isMeta`、`isNotDuplicable`、`isConvergent`、`isPredicable`（计算属性）。
- 只影响发射管线：`isCodeGenOnly`（`AsmMatcherEmitter.cpp:1624`）、`isAsmParserOnly`（`DecoderEmitter.cpp:1383`）、`hasNoSchedulingInfo`（`CodeGenSchedule.cpp:867`）、`hasCtrlDep`（`DAGISelMatcherGen.cpp:952`）。
- 寄存器侧：只有 `isConstant`、`isArtificial`、`CoveredBySubRegs` 有语义；`isConstant` **不等于零寄存器**（RISC-V 的 `VLENB` 也是 `isConstant = true`，`RISCVRegisterInfo.td:820`）。
- 上游**没有** zero/SP/FP 的 TableGen 位：零寄存器只是 host C++ 的 `isZeroReg`（`RISCVMCTargetDesc.cpp:187`），SP/FP 是 `TargetRegisterInfo` 的虚函数（`RISCVRegisterInfo.cpp:863`）。
- 因此 SSDL 的 `reg(is_zero)` 一类的属性必须由宿主 schema 提供（`schema.py --merge` 的用途），不能从 TableGen 推导。

---

## 5. 契约缺口（按优先级）

**D1（P0，已闭合）`write`/`read` 所有权错位。** 证据：§2.2 的撞名；上游 `RISCVSchedule.td:16`。修法：R1，已落地（`parser-notes` §13）。

**D2（P0，已按诊断闭合）资源名缺乏模型作用域。** 证据：上游 `defm SiFive7 : SiFive7ProcResources` 造出 `SiFive7PipeA`（`RISCVSchedSiFive7.td:1564`）；我们实测 `P` 撞名。修法：R2，选择"显式唯一 + 改名诊断"，已落地；机器级生成记录同时加了 machine 前缀。

**D3（P0，已闭合）完整性契约缺失。** 证据：结论 2、3。影响：`machine` 被当成"补丁"，实际必须是"完整模型"；`-gen-subtarget` 因缺 `WriteRes` 必炸。修法：R4，已落地——全量 SiFive7 三个模型由 `from_records.py` 生成后与上游逐字段相等（`parser-notes` §17）。

**D4（P0）`map` 的覆盖语义与前置条件。** 证据：`SubtargetEmitter.cpp:1150-1166`、`CodeGenSchedule.cpp:1013`。影响：`map` 不是"给新指令补调度"，而是"覆盖已有 sched class"；没有 `SchedRW` 的指令不能只靠 `map` 救。修法：R10。

**D5（P1，已闭合）属性三态被压成二态。** 证据：`mayLoad = ?` 190 处（Instruction 记录 51 条）、`hasSideEffects = ?` 169 处（37 条）；`getValueAsBitOrUnset`（`CodeGenInstruction.cpp:459-465`）。修法：R5，已落地——`schema.py` 导出 `null`，`sets.py` 用三值逻辑，未知参与比较即错误。

**D6（P1）谓词索引空间未建模。** 证据：D8。影响：`opcode_mask` 场景下谓词与掩码不一致。修法：R6。

**D7（P1，部分闭合）STI 谓词只服务 MCA，但 SSDL 未声明这一事实。** 证据：结论 5。影响：用户以为写了 `sti_decl` 就改了调度器。进展：三个固定 hook（`isZeroIdiom`/`isDependencyBreaking`/`isOptimizableRegisterMove`）现在只绑定上游 `STIPredicateDecl`，字段按 C++ 签名校验，重复声明会报错（`parser-notes` §16.1）。剩余：消费者事实仍需在规范里明说。修法：R13。

**D8（P1）`PredicateProlog` 作用域过宽。** 证据：§4.3。修法：R7。

**D9（P1）`read_default` 是隐式魔法名。** 证据：`emit_td.py:142` 特判 `read_default → ReadDefault`，v1.1 文法没有它。修法：R3。

**D10（P2，部分闭合）`variant` 的 MCA 退化。** 证据：§3-3。进展：`variant`/`read_variant` 现在能生成可被 `-gen-subtarget` 展开的真实记录，MC/Feature 谓词在 codegen 与 MCA 两层一致（`parser-notes` §16.2-16.3）。剩余：含 C++ 代码的 `SchedPredicate` 在 MCA 仍退化为 default。修法：R8。

**D11（P2）`acquire`/`unsupported` 的层间不一致。** 证据：§3-1、§3-2。修法：R11（规范里按字段标注所属层）。

**D12（P2）`feature.implies` 双闭包。** 证据：§4.4。修法：R9。

**D13（P2，已闭合）三态在消费端还没有约束。** 导出端已修：`schema.py` 把 `?` 写成 JSON `null`（不再是静默丢弃），并有 `attribute_schema_exports_tablegen_records` 自检；消费端也已修：`sets.py` 的 `eval_cond` 返回 `bool | None`，`&&`/`||`/`!` 按三值逻辑传播，读取未知属性时直接报错。

**D14（P2）itinerary 与资源模型混用。** 证据：§4.8。修法：R12。

---

## 6. 规范修订建议（R1–R13）

不改 9 个顶层关键字，不改"无 base/无元编程"。

| ID | 修订 | 类型 | 优先级 |
|---|---|---|---|
| R1 ✅ | `write` / `read` / `write_seq` 定义为**目标级词表**：可以写在任意 `machine` 块里，但展开时提升到单一目标命名空间；重复声明必须完全同形，否则报 `conflicting declaration of scheduling write 'W'`。`bind`/`advance`/`map` 仍然是 machine 级。 | 语义 | P0 |
| R2 ✅ | `resource` / `res_group` 的 def 名必须全目标唯一；两个 machine 使用同名资源时给出明确诊断，并建议改名（不做隐式前缀，保持 SSDL↔TD 名字 1:1）。备选：按 machine 加前缀，但那会破坏可对拍性。 | 语义 | P0 |
| R3 | `read_default` 列为保留标识符，文法上等价于"该操作数使用上游 `ReadDefault`"。 | 文法 | P0 |
| R4 | 明确 `machine` 是**完整模型**：`complete_model = false` 只关闭完整性检查，不关闭 `WriteRes` 覆盖要求；展开器在 `--strict` 下对"声明了 resource 但没有任何 `bind`"报错。 | 语义 | P0 |
| R5 ✅ | `set` 的属性取值域改为 `true/false/unknown` 三态；任何比较遇到 `unknown` 都是错误。`schema.py` 输出 `null`（JSON 的 unknown），`sets.py` 用三值逻辑传播。 | 文法/工具 | P1 |
| R6 | 谓词的操作数下标必须声明索引空间：默认 MI 下标；使用 `opcode_mask` 时必须显式给出掩码下标列表，且掩码长度按 MCA use 索引计。 | 文法 | P1 |
| R7 | `predicate_prolog` 限定只对 SCHED sort 生效；`assembler`/`tii_predicate`/isel 谓词引用 prolog 符号是错误。 | 语义 | P1 |
| R8 | `variant` 的 case 谓词含 C++（`SchedPredicate<code>`）时，若该机器 `expand_for_mc = true`，必须报错或要求显式 `mc_only = true` 标注。 | 语义 | P2 |
| R9 | `feature.implies` 按 kind 分派：`plain`/`tune` 走 MC bit 闭包；`extension` 走 `RISCVISAInfo` 单向闭包。禁止混用两种语义。 | 语义 | P2 |
| R10 | `map` 明确为"覆盖"；`--strict` 下要求被映射的指令至少在一个 `variant`/`bind` 或宿主提供的 `SchedRW` 表里有 sched class。 | 语义 | P0 |
| R11 | `bind` 的字段按消费者分层标注：`acquire` 只影响 codegen；`retire_ooo`、`reg_file`、`retire_unit`、队列只影响 llvm-mca；`InstRW.unsupported` 不生成任何东西。 | 文档 | P2 |
| R12 | 同一 `machine` 中 `itineraries` 与 `bind` 互斥（上游 MCA 不支持 itinerary）。 | 语义 | P2 |
| R13 | `sti_decl`/`sti_function` 标注"消费者：llvm-mca 依赖破坏分析"，并说明 MI 侧没有树内调用者。 | 文档 | P1 |
| R14 ✅ | `variant`/`read_variant` 的 subject 是 dispatch 名：分支写回 subject 时，展开器把 `bind` 实体化为 `SchedWriteRes`、把 `advance` 实体化为 `SchedReadAdvance`；read 无 `advance` 时用 `ReadDefault`。subject 与 `alias` 互斥，subject 之间的环必须在展开期诊断（TableGen 会无限展开）。 | 语义 | P0 |

---

## 7. 为什么 `-gen-subtarget` 失败（以及正确的验证路径）

复现：

```bash
llvm-tblgen -gen-subtarget -I llvm/include -I llvm/lib/Target/RISCV /tmp/gen.td
# Assertion `SchedWrites[Idx].isValid() && "invalid SchedWrite"' failed
```

链条：

1. `CodeGenTarget::getSchedModels` 构造 `CodeGenSchedModels` 时跑 `checkCompleteness()`（`CodeGenSchedule.cpp:1917-1972`）。若模型 `CompleteModel=true`，任何缺少 sched class 的指令都会 fatal `Incomplete schedule model`。
2. `-gen-subtarget` 继续走 `genSchedClassTables`；一旦 `hasInstrSchedModel`（模型有任何 `WriteRes` 或 `ItinRW`），每个可达 `SchedWrite` 都必须在 `WriteResMap` 里找到（`SubtargetEmitter.cpp:984-989`），否则 `Processor does not define resources for <Write>`。
3. 我们的 `/tmp/gen.td` 只定义了 `W_ALU`/`W_LD`/`W_MULDIV` 等少数写，而 `RISCV.td` 的指令 `SchedRW` 引用了 `WriteIALU`、`COPY` 的调度等整套词表，所以必然在第 2 步失败。这是**集成形状**问题：把"一个 CPU 的完整模型"和"目标已有指令集"拼在同一个翻译单元里，本来就要求覆盖全集。

正确的验证路径有两条：

- **最小 stub 目标（已落地）**：`tests/ssdl/stub-target/SSDLStub.td` + `tests/ssdl/stub-mini.ssdl`，`check.py` 的 `generated_td_passes_gen_subtarget` 同时跑 `-gen-subtarget` 与 `-gen-instr-info`，并用"去掉 `bind` 必须失败"做反控。它验证形状与完整性契约，能进 CI。注意 stub 要求指令先于调度记录声明且不写 `SchedRW`（调度类由 `map` 的 `InstRW` 提供），并需要 `RemapAllTargetPseudoPointerOperands` 与 `Target.InstructionSet`。
- **完整模型**：把一个真实 CPU 的整套 `WriteRes` 全部用 SSDL 写出来（SiFive7 的量级），再替换在树 `.td` 做完整构建。这条是产品级验收，不是每轮 CI。

当前 fixture 的价值仅限于 `-print-records` 对拍；它不可能通过 `-gen-subtarget`，也不应该为了通过而放松检查。

---

## 8. 复现命令

```bash
# 1. 两机模型的资源撞名（D2；词表已在 R1 中提升为共享）
python3 tools/ssdl/emit_td.py /tmp/twomach.ssdl

# 2. 属性三态（D5/D13）
llvm-tblgen -print-records -I llvm/include -I llvm/lib/Target/RISCV \
    llvm/lib/Target/RISCV/RISCV.td > $SSDL_CACHE/riscv.rec
grep -c 'bit mayLoad = ?;' $SSDL_CACHE/riscv.rec          # 190（Instruction 记录 51 条）
grep -c 'bit hasSideEffects = ?;' $SSDL_CACHE/riscv.rec   # 169（Instruction 记录 37 条）
python3 tools/ssdl/schema.py $SSDL_CACHE/riscv.rec --merge tests/ssdl/attrs.json \
    -o /tmp/schema.json                            # 16962 inst / 398 reg
python3 -c 'import json;s=json.load(open("/tmp/schema.json"));print(s["inst"]["ADD"]);print(s["reg"]["X0"])'

# 3. 完整性门（结论 2）
grep -n "does not define resources for" utils/TableGen/SubtargetEmitter.cpp
grep -n "Incomplete schedule model" utils/TableGen/Common/CodeGenSchedule.cpp

# 4. STI 谓词只有一个消费者（结论 5）
grep -rn "isDependencyBreaking" llvm/lib llvm/include
```
