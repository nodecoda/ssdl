# 后端 TD 关键字参考（调度 / 子目标 / 汇编匹配）

> 自动生成，勿手改。重新生成：
>
> ```bash
> python3 tools/ssdl/td_schema.py --reference > docs/dsl/2026-11-16-ssdl-keyword-reference.md
> ```
>
> `check.py` 的 `keyword_reference_is_up_to_date` 比对这份文件与生成结果，所以后端改了绑定而没重新生成会直接变红。

统计：8 个 emitter 源文件、493 处绑定、245 个不同的 `(形态, 名字)`（class 62，def 3，field 152，record 28）。

## 怎么读

后端不认识 TD 语法，只按字符串去 record 里取值。每一行就是一个这样的关键字：

- **形态**：`field` = `getValueAs*("…")` / `emitProcessorProp` / `addFieldExpander` / `getValue`；`class` = `isSubClassOf`；`record` = `getAllDerivedDefinitions`；`def` = `getDef`。
- **声明**：该名字在 TD 里由谁提供；字段带类型与默认值。
- **读取点**：C++ 里的 `文件:行`（最多列 3 处，全量用 `--census`）。

## 字段

同一字段名可能被多个类声明（如 `Name`）；声明列会给出所有声明类，类型一致时只写一次。

| 字段 | 声明 | 读取点 |
|---|---|---|
| `Name` | `AsmOperandClass`、`AsmParserVariant`、`Processor`、`ProcessorAlias`、`STIPredicateDecl`、`SubtargetFeature`：`string`（默认值不同） | SubtargetEmitter.cpp:213, 226, 275 …（共 18 处） |
| `SuperClasses` | `AsmOperandClass`：`list<AsmOperandClass>` = `[ ]` | AsmMatcherEmitter.cpp:1475 |
| `AllowDuplicateRegisterNames` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:2913, 2930 |
| `AsmParserClassName` | `AsmParser`：`string` = `"AsmParser"` | AsmMatcherEmitter.cpp:3333, 3549 |
| `AsmParserInstCleanup` | `AsmParser`：`string` = `""` | AsmMatcherEmitter.cpp:4416 |
| `CallCustomParserForAllOperands` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:1559 |
| `EmitTokenDiagnosticTypes` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:1196, 2734, 3744 |
| `HasMnemonicFirst` | `AsmParser`：`bit` = `true` | AsmMatcherEmitter.cpp:1595, 3609 |
| `OperandParserMethod` | `AsmParser`：`string` = `""` | AsmMatcherEmitter.cpp:3315 |
| `PreferSmallerInstructions` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:398 |
| `PrioritizeFeatureInMultiMismatchFallback` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:3614 |
| `ReportMultipleNearMisses` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:1597, 3612 |
| `ShouldEmitMatchRegisterAltName` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:3697 |
| `ShouldEmitMatchRegisterAltNameWithIndex` | `AsmParser`：`bit` = `false` | AsmMatcherEmitter.cpp:2928, 3698 |
| `ShouldEmitMatchRegisterName` | `AsmParser`：`bit` = `true` | AsmMatcherEmitter.cpp:3694 |
| `BreakCharacters` | `AsmParserVariant`：`string` = `""` | AsmMatcherEmitter.cpp:1613 |
| `CommentDelimiter` | `AsmParserVariant`：`string` = `""` | AsmMatcherEmitter.cpp:1606 |
| `RegisterPrefix` | `AsmParserVariant`：`string` = `""` | AsmMatcherEmitter.cpp:1608 |
| `SeparatorCharacters` | `AsmParserVariant`：`string` = `" \t,"` | AsmMatcherEmitter.cpp:1612 |
| `TokenizingCharacters` | `AsmParserVariant`：`string` = `"[]*!"` | AsmMatcherEmitter.cpp:1610 |
| `Variant` | `AsmParserVariant`、`AsmWriter`：`int` = `0` | AsmMatcherEmitter.cpp:1615, 3128, 3396 …（共 6 处） |
| `AssemblerCondDag` | `AssemblerPredicate`、`Predicate`：`dag`（默认值不同） | SubtargetFeatureInfo.cpp:181, 203, 224 |
| `AssemblerMatcherPredicate` | `AssemblerPredicate`、`Predicate`：`bit`（默认值不同） | SubtargetFeatureInfo.cpp:32, 169, 220 |
| `PredicateName` | `AssemblerPredicate`、`Predicate`：`string`（默认值不同） | AsmMatcherEmitter.cpp:3004 |
| `MCInstFnName` | `CheckFunctionPredicate`、`CheckFunctionPredicateWithTII`：`string` = `MCInstFn` | PredicateExpander.cpp:458, 464 |
| `MachineInstrFnName` | `CheckFunctionPredicate`、`CheckFunctionPredicateWithTII`：`string` = `MachineInstrFn` | PredicateExpander.cpp:459, 465 |
| `TIIPtrName` | `CheckFunctionPredicateWithTII`：`string` = `TIIPointer` | PredicateExpander.cpp:466 |
| `ImmVal` | `CheckImmOperand` `int` = `Imm`；`CheckImmOperandGE` `int` = `Imm`；`CheckImmOperandGT` `int` = `Imm`；`CheckImmOperandLE` `int` = `Imm`；`CheckImmOperandLT` `int` = `Imm`；`CheckImmOperand_s` `string` = `Value` | PredicateExpander.cpp:398, 403, 408 …（共 6 处） |
| `EndVal` | `CheckImmOperandRange`：`int` = `End` | PredicateExpander.cpp:429 |
| `StartVal` | `CheckImmOperandRange`：`int` = `Start` | PredicateExpander.cpp:428 |
| `CodeBlock` | `CheckNonPortable`：`string` = `Code` | PredicateExpander.cpp:470 |
| `Pred` | `CheckNot`、`MCReturnStatement`、`MCSchedPredicate`：`MCInstPredicate`（默认值不同） | SubtargetEmitter.cpp:1641, 1647；PredicateExpander.cpp:347 …（共 4 处） |
| `NumOps` | `CheckNumOperands`：`int` = `Num` | PredicateExpander.cpp:440 |
| `ValidOpcodes` | `CheckOpcode`：`list<Instruction>` = `Opcodes` | PredicateExpander.cpp:443, 446 |
| `FunctionMapper` | `CheckOperandBase`：`string` = `Fn` | PredicateExpander.cpp:387, 391, 399 …（共 10 处） |
| `Predicates` | `CheckPredicateSequence` `list<MCInstPredicate>` = `Preds`；`CompressPat` `list<Predicate>` = `predicates`；`HwMode` `list<Predicate>` = `Ps`；`InstAlias` `list<Predicate>` = `[ ]`；`Instruction` `list<Predicate>` = `[ ]`；`InstructionEncoding` `list<Predicate>` = `[ ]`；`MnemonicAlias` `list<Predicate>` = `[ ]`；`Requires` `list<Predicate>` = `preds`；`SchedPredicateCombiner` `list<SchedPredicateBase>` = `predicates` | SubtargetEmitter.cpp:1664, 1769；CodeGenSchedule.cpp:2168 …（共 7 处） |
| `Reg` | `CheckRegOperand`：`Register` = `R` | PredicateExpander.cpp:386 |
| `FirstIndex` | `CheckSameRegOperand`：`int` = `First` | PredicateExpander.cpp:436 |
| `SecondIndex` | `CheckSameRegOperand`：`int` = `Second` | PredicateExpander.cpp:437 |
| `FuncList` | `ComboFuncData`：`list<FuncUnit>` = `funclist` | DFAPacketizerEmitter.cpp:156 |
| `TheComboFunc` | `ComboFuncData`：`FuncUnit` = `ComboFunc` | DFAPacketizerEmitter.cpp:154 |
| `CFD` | `ComboFuncUnits`：`list<ComboFuncData>` = `cfd` | DFAPacketizerEmitter.cpp:144 |
| `ComplexDeprecationPredicate` | `ComplexDeprecationPredicate`：`string` = `dep` | AsmMatcherEmitter.cpp:986 |
| `DeprecatedFeatureMask` | `Deprecated`：`SubtargetFeature` = `dep` | AsmMatcherEmitter.cpp:984 |
| `Feature` | `FeatureSchedPredicate`：`SubtargetFeature` = `SF` | SubtargetEmitter.cpp:1649 |
| `SchedModel` | `FeatureSchedPredicate`、`InstRW`、`ItinRW`、`MCSchedPredicate`、`MemoryQueue`、`ProcReadAdvance`、`ProcResGroup`、`ProcResourceUnits`、`ProcWriteResources`、`Processor`、`RegisterFile`、`RetireControlUnit`、`STIPredicate`、`SchedAlias`、`SchedPredicate`、`SchedPredicateCombiner`、`SchedVar`、`SchedVariant`、`WriteSequence`：`SchedMachineModel`（默认值不同） | SubtargetEmitter.cpp:950, 1009, 1151 …（共 31 处） |
| `FunctionName` | `FunctionPredicateBase`：`string` = `name` | CodeGenSchedule.cpp:449；PredicateExpander.cpp:473 |
| `Code` | `HwModePredicateProlog`、`PredicateProlog`：`code` = `c` | SubtargetEmitter.cpp:1634, 2015 |
| `AsmVariantName` | `InstAlias`、`Instruction`、`MnemonicAlias`：`string`（默认值不同） | AsmMatcherEmitter.cpp:1628, 1656, 3043 |
| `Instrs` | `InstRW`：`dag` = `instrlist` | CodeGenSchedule.cpp:186, 1047, 1082 |
| `OperandReadWrites` | `InstRW`、`ItinRW`：`list<SchedReadWrite>` = `rw` | SubtargetEmitter.cpp:1160, 1169；CodeGenSchedule.cpp:606 …（共 9 处） |
| `Unsupported` | `InstRW`、`ProcReadAdvance`、`ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1206, 1324 |
| `Bypasses` | `InstrItinData`：`list<Bypass>` = `bypasses` | SubtargetEmitter.cpp:487 |
| `NumMicroOps` | `InstrItinData`、`ProcWriteResources`：`int`（默认值不同） | SubtargetEmitter.cpp:640, 1211 |
| `OperandCycles` | `InstrItinData`：`list<int>` = `operandcycles` | SubtargetEmitter.cpp:471 |
| `Stages` | `InstrItinData`：`list<InstrStage>` = `stages` | SubtargetEmitter.cpp:426；DFAPacketizerEmitter.cpp:183 |
| `TheClass` | `InstrItinData`：`InstrItinClass` = `Class` | CodeGenSchedule.cpp:1121 |
| `Cycles` | `InstrStage`、`ProcReadAdvance`：`int` = `cycles` | SubtargetEmitter.cpp:435, 1350 |
| `Kind` | `InstrStage` `int` = `kind . Value`；`ProcResourceUnits` `ProcResourceKind` = `kind` | SubtargetEmitter.cpp:452；CodeGenSchedule.cpp:2052 |
| `TimeInc` | `InstrStage`：`int` = `timeinc` | SubtargetEmitter.cpp:449 |
| `Units` | `InstrStage`：`list<FuncUnit>` = `units` | SubtargetEmitter.cpp:439；DFAPacketizerEmitter.cpp:185 |
| `AsmMatchConverter` | `Instruction`：`string` = `""` | AsmMatcherEmitter.cpp:1119, 2142 |
| `HasPositionOrder` | `Instruction`：`bit` = `false` | AsmMatcherEmitter.cpp:669, 671 |
| `Itinerary` | `Instruction`：`InstrItinClass` = `NoItinerary` | CodeGenSchedule.cpp:838 |
| `Namespace` | `Instruction`、`RegAltNameIndex`、`Register`、`RegisterClass`、`SubRegIndex`、`ValueType`：`string`（默认值不同） | SubtargetEmitter.cpp:807, 808；PredicateExpander.cpp:120 …（共 9 处） |
| `SchedRW` | `Instruction`、`Sched`：`list<SchedReadWrite>`（默认值不同） | CodeGenSchedule.cpp:593, 841 |
| `Size` | `Instruction`、`InstructionEncoding`、`RegisterClass`、`SubRegIndex`、`SubRegRange`、`ValueType`：`int`（默认值不同） | AsmMatcherEmitter.cpp:572, 577 |
| `TwoOperandAliasConstraint` | `Instruction`：`string` = `""` | AsmMatcherEmitter.cpp:1726 |
| `Opcodes` | `InstructionEquivalenceClass`、`MCOpcodeSwitchCase`：`list<Instruction>` = `opcodes` | CodeGenSchedule.cpp:269, 314, 344 …（共 4 处） |
| `OperandIndices` | `InstructionEquivalenceClass`：`list<int>` = `operands` | CodeGenSchedule.cpp:347 |
| `Predicate` | `InstructionEquivalenceClass` `MCInstPredicate` = `pred`；`SchedPredicate` `code` = `pred`；`SchedVar` `SchedPredicateBase` = `pred` | SubtargetEmitter.cpp:1699；CodeGenSchedule.cpp:310, 350 …（共 7 处） |
| `MatchedItinClasses` | `ItinRW`：`list<InstrItinClass>` = `iic` | SubtargetEmitter.cpp:1167；CodeGenSchedule.cpp:1204, 1983 |
| `CaseStmt` | `MCOpcodeSwitchCase`：`MCStatement` = `caseStmt` | PredicateExpander.cpp:313 |
| `Cases` | `MCOpcodeSwitchStatement`：`list<MCOpcodeSwitchCase>` = `cases` | PredicateExpander.cpp:341 |
| `DefaultCase` | `MCOpcodeSwitchStatement`：`MCStatement` = `default` | PredicateExpander.cpp:342 |
| `OpIndex` | `MCOperandPredicate`：`int` = `Index` | PredicateExpander.cpp:376, 379, 382 …（共 14 处） |
| `QueueDescriptor` | `MemoryQueue`：`ProcResourceKind` = `PR` | SubtargetEmitter.cpp:842, 851 |
| `FromMnemonic` | `MnemonicAlias`：`string` = `From` | AsmMatcherEmitter.cpp:3046 |
| `ToMnemonic` | `MnemonicAlias`：`string` = `To` | AsmMatcherEmitter.cpp:3070, 3071, 3083 …（共 5 处） |
| `ParserMatchClass` | `Operand`、`RegisterOperand`：`AsmOperandClass`（默认值不同） | AsmMatcherEmitter.cpp:1217, 1251, 1796 …（共 4 处） |
| `CondString` | `Predicate`：`code` = `cond` | SubtargetFeatureInfo.cpp:39, 113, 158 |
| `RecomputePerFunction` | `Predicate`：`bit` = `false` | SubtargetFeatureInfo.h:51 |
| `CycleTunables` | `ProcReadAdvance`：`list<int>` = `tunables` | SubtargetEmitter.cpp:1331 |
| `ValidWrites` | `ProcReadAdvance`：`list<SchedWrite>` = `writes` | SubtargetEmitter.cpp:1329；CodeGenSchedule.cpp:2130 |
| `BufferSize` | `ProcResGroup`、`ProcResourceUnits`：`int` = `- 1` | SubtargetEmitter.cpp:900 |
| `Resources` | `ProcResGroup`：`list<ProcResource>` = `resources` | SubtargetEmitter.cpp:743, 902, 1064 …（共 7 处） |
| `NumUnits` | `ProcResourceUnits`：`int` = `num` | SubtargetEmitter.cpp:746, 903, 904 …（共 4 处） |
| `Super` | `ProcResourceUnits`：`ProcResourceKind` = `?` | SubtargetEmitter.cpp:909, 1076；CodeGenSchedule.cpp:2098 |
| `AcquireAtCycles` | `ProcWriteResources`：`list<int>` = `[ ]` | SubtargetEmitter.cpp:1224 |
| `BeginGroup` | `ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1212 |
| `EndGroup` | `ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1213 |
| `Latency` | `ProcWriteResources`：`int` = `1` | SubtargetEmitter.cpp:1210 |
| `ProcResources` | `ProcWriteResources`：`list<ProcResourceKind>` = `resources` | SubtargetEmitter.cpp:1219；CodeGenSchedule.cpp:2121 |
| `ReleaseAtCycles` | `ProcWriteResources`：`list<int>` = `[ ]` | SubtargetEmitter.cpp:1221 |
| `RetireOOO` | `ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1216 |
| `SingleIssue` | `ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1214, 1215 |
| `Features` | `Processor`：`list<SubtargetFeature>` = `f` | SubtargetEmitter.cpp:376 |
| `ProcItin` | `Processor`：`ProcessorItineraries` = `pi` | CodeGenSchedule.h:475 |
| `TuneFeatures` | `Processor`：`list<SubtargetFeature>` = `tunef` | SubtargetEmitter.cpp:378 |
| `Alias` | `ProcessorAlias`：`string` = `alias` | SubtargetEmitter.cpp:322 |
| `BP` | `ProcessorItineraries`：`list<Bypass>` = `bp` | SubtargetEmitter.cpp:530 |
| `FU` | `ProcessorItineraries`：`list<FuncUnit>` = `fu` | SubtargetEmitter.cpp:516；DFAPacketizerEmitter.cpp:113 |
| `IID` | `ProcessorItineraries`：`list<InstrItinData>` = `iid` | CodeGenSchedule.cpp:550, 1112；CodeGenSchedule.h:276 …（共 4 处） |
| `PacketizerNamespace` | `ProcessorItineraries`：`string` = `""` | DFAPacketizerEmitter.cpp:223 |
| `ReadType` | `ReadAdvance`：`SchedRead` = `read` | CodeGenSchedule.cpp:1899, 2147 |
| `AltNames` | `Register`：`list<string>` = `altNames` | AsmMatcherEmitter.cpp:2935 |
| `AsmName` | `Register`：`string` = `n` | AsmMatcherEmitter.cpp:2901 |
| `RegAltNameIndices` | `Register`：`list<RegAltNameIndex>` = `[ ]` | AsmMatcherEmitter.cpp:2936 |
| `Classes` | `RegisterCategory` `list<RegisterClass>` = `classes`；`STIPredicate` `list<InstructionEquivalenceClass>` = `classes` | CodeGenSchedule.cpp:308, 337 |
| `AllowMoveElimination` | `RegisterFile`：`list<bit>` = `AllowMoveElim` | CodeGenSchedule.cpp:1804 |
| `AllowZeroMoveEliminationOnly` | `RegisterFile`：`bit` = `AllowZeroMoveElimOnly` | CodeGenSchedule.cpp:1791 |
| `MaxMovesEliminatedPerCycle` | `RegisterFile`：`int` = `MaxMoveElimPerCy` | CodeGenSchedule.cpp:1789 |
| `NumPhysRegs` | `RegisterFile`：`int` = `numPhysRegs` | CodeGenSchedule.cpp:1795 |
| `RegClasses` | `RegisterFile`：`list<RegisterClass>` = `Classes` | CodeGenSchedule.cpp:1801 |
| `RegCosts` | `RegisterFile`：`list<int>` = `Costs` | CodeGenSchedule.cpp:1802 |
| `RegClass` | `RegisterOperand`：`RegisterClassLike` = `regclass` | AsmMatcherEmitter.cpp:1230, 2620 |
| `Value` | `ReservationKind` `int` = `val`；`SubtargetFeature` `string` = `v` | SubtargetEmitter.cpp:181, 2112 |
| `MaxRetirePerCycle` | `RetireControlUnit`：`int` = `retirePerCycle` | SubtargetEmitter.cpp:762 |
| `ReorderBufferSize` | `RetireControlUnit`：`int` = `bufferSize` | SubtargetEmitter.cpp:760 |
| `Declaration` | `STIPredicate`：`STIPredicateDecl` = `declaration` | CodeGenSchedule.cpp:411 |
| `DefaultReturnValue` | `STIPredicateDecl`：`MCInstPredicate` = `default` | CodeGenSchedule.h:422 |
| `Delegates` | `STIPredicateDecl`：`list<STIPredicateDecl>` = `delegates` | PredicateExpander.cpp:511 |
| `ExpandForMC` | `STIPredicateDecl`：`bit` = `expandForMC` | PredicateExpander.cpp:607 |
| `OverridesBaseClassMember` | `STIPredicateDecl`：`bit` = `overrides` | PredicateExpander.cpp:499 |
| `UpdatesOpcodeMask` | `STIPredicateDecl`：`bit` = `updatesOpcodeMask` | PredicateExpander.cpp:491, 507, 568 |
| `AliasRW` | `SchedAlias`：`SchedReadWrite` = `alias` | SubtargetEmitter.cpp:948, 1007；CodeGenSchedule.cpp:635 …（共 7 处） |
| `MatchRW` | `SchedAlias`：`SchedReadWrite` = `match` | CodeGenSchedule.cpp:634, 672 |
| `CompleteModel` | `SchedMachineModel`：`bit` = `true` | SubtargetEmitter.cpp:1556；CodeGenSchedule.cpp:1921 |
| `EnableIntervals` | `SchedMachineModel`：`bit` = `false` | SubtargetEmitter.cpp:1562 |
| `FullInstRWOverlapCheck` | `SchedMachineModel`：`bit` = `true` | CodeGenSchedule.cpp:1040 |
| `HighLatency` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1546 |
| `IssueWidth` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1542 |
| `Itineraries` | `SchedMachineModel`：`ProcessorItineraries` = `NoItineraries` | CodeGenSchedule.cpp:546 |
| `LoadLatency` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1545 |
| `LoopMicroOpBufferSize` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1544 |
| `MicroOpBufferSize` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1543 |
| `MispredictPenalty` | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1547 |
| `NoModel` | `SchedMachineModel`：`bit` = `false` | CodeGenSchedule.h:477 |
| `PostRAScheduler` | `SchedMachineModel`：`bit` = `false` | SubtargetEmitter.cpp:1550 |
| `UnsupportedFeatures` | `SchedMachineModel`：`list<Predicate>` = `[ ]` | CodeGenSchedule.cpp:1169 |
| `Selected` | `SchedVar`：`list<SchedReadWrite>` = `selected` | CodeGenSchedule.cpp:572, 1474 |
| `Variadic` | `SchedVariant`：`bit` = `false` | CodeGenSchedule.h:71 |
| `Variants` | `SchedVariant`：`list<SchedVar>` = `variants` | CodeGenSchedule.cpp:570, 1328, 1395 …（共 4 处） |
| `Desc` | `SubtargetFeature`：`string` = `d` | SubtargetEmitter.cpp:228 |
| `FieldName` | `SubtargetFeature`：`string` = `f` | SubtargetEmitter.cpp:180, 2113 |
| `Implies` | `SubtargetFeature`：`list<SubtargetFeature>` = `!sort ( f , i , f . Name )` | SubtargetEmitter.cpp:250 |
| `InlineBehavior` | `SubtargetFeature`：`InlineBehavior` = `ib` | SubtargetEmitter.cpp:2137 |
| `FromToken` | `TokenAlias`：`string` = `From` | AsmMatcherEmitter.cpp:1754 |
| `ToToken` | `TokenAlias`：`string` = `To` | AsmMatcherEmitter.cpp:1755 |
| `WriteType` | `WriteRes`：`SchedWrite` = `write` | CodeGenSchedule.cpp:1892, 2112 |
| `Repeat` | `WriteSequence`：`int` = `rep` | CodeGenSchedule.cpp:756, 796 |
| `Writes` | `WriteSequence`：`list<SchedWrite>` = `writes` | CodeGenSchedule.cpp:566, 665 |

## 字段消费者语义

> 来源：`tools/ssdl/effects.py`。`check.py` 的 `effect_*` 检查校验键、证据行和角色一致性；全部条目审计于 commit `700e551714cc`。

- **层**：`sched` / `predicate` / `feature` / `itinerary`。
- **角色**：`effect` 运行时可见；`selector` 只选择记录；`validation` 仅 TableGen 期；`dead` 无消费者；`declaration` 只改生成代码形状。
- **后端读取**：该字段名是否出现在 emitter 的字符串绑定里（`否` 即死字段候选）。

| 字段 | 层 | 角色 | 运行时消费者 | 后端读取 | 默认语义 | 证据 |
|---|---|---|---|---|---|---|
| `Processor.Features` | feature | effect | codegen, targetparser | 是 | required | llvm/utils/TableGen/SubtargetEmitter.cpp:388 |
| `Processor.Name` | feature | effect | asm, codegen, targetparser | 是 | required; the -mcpu name | llvm/utils/TableGen/SubtargetEmitter.cpp:388 |
| `Processor.ProcItin` | feature | effect | codegen | 是 | NoItineraries unless supplied | llvm/utils/TableGen/SubtargetEmitter.cpp:1050 |
| `Processor.SchedModel` | feature | selector | — | 是 | NoSchedModel unless supplied | llvm/utils/TableGen/SubtargetEmitter.cpp:388 |
| `Processor.TuneFeatures` | feature | effect | codegen, targetparser | 是 | [] | llvm/utils/TableGen/SubtargetEmitter.cpp:388 |
| `ProcessorAlias.Alias` | feature | effect | codegen, targetparser | 是 | required; the canonical CPU name | llvm/utils/TableGen/SubtargetEmitter.cpp:315 |
| `ProcessorAlias.Name` | feature | effect | codegen, targetparser | 是 | required; the alternate -mcpu name | llvm/utils/TableGen/SubtargetEmitter.cpp:315 |
| `RISCVExtension.Experimental` | feature | effect | targetparser | 否 | false; true requires -menable-experimental-extensions and an explicit version | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:41 |
| `RISCVExtension.MajorVersion` | feature | effect | targetparser | 否 | required; part of the TargetParser extension table | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:45 |
| `RISCVExtension.MinorVersion` | feature | effect | targetparser | 否 | required; part of the TargetParser extension table | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:45 |
| `RISCVExtensionBitmask.BitPos` | feature | effect | targetparser | 否 | required; selects the bit inside the group | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:250 |
| `RISCVExtensionBitmask.GroupID` | feature | effect | targetparser | 否 | required; selects the __riscv_feature_bits group | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:249 |
| `RISCVProcessorModel.ConfigurableTuneFeatures` | feature | effect | targetparser | 否 | []; tune features the driver may toggle | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:357 |
| `RISCVProcessorModel.DefaultMarch` | feature | effect | targetparser | 否 | empty; synthesized from Features when absent | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:196 |
| `RISCVTuneFeature.NegativeDirectiveName` | feature | effect | targetparser | 否 | required; accepted by -mtune | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:290 |
| `RISCVTuneFeature.PositiveDirectiveName` | feature | effect | targetparser | 否 | required; accepted by -mtune | llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:289 |
| `SubtargetFeature.Desc` | feature | declaration | — | 是 | required; help text only | llvm/lib/MC/MCSubtargetInfo.cpp:113 |
| `SubtargetFeature.FieldName` | feature | effect | codegen | 是 | required; maps the feature to a Subtarget field | llvm/utils/TableGen/SubtargetEmitter.cpp:2107 |
| `SubtargetFeature.Implies` | feature | effect | codegen, targetparser | 是 | []; emitted as a direct list, not transitively unrolled | llvm/utils/TableGen/SubtargetEmitter.cpp:252 |
| `SubtargetFeature.InlineBehavior` | feature | effect | tti | 是 | InlineDefault for plain features; RISCVTuneFeature defaults to InlineIgnore | llvm/utils/TableGen/SubtargetEmitter.cpp:2126 |
| `SubtargetFeature.Name` | feature | effect | asm, codegen, targetparser | 是 | required; the -mattr name and the key in SubtargetFeatureKV | llvm/utils/TableGen/SubtargetEmitter.cpp:206 |
| `SubtargetFeature.Value` | feature | effect | codegen | 是 | required; non-bool values become enum/int with max-wins codegen | llvm/utils/TableGen/SubtargetEmitter.cpp:2107 |
| `ComboFuncData.FuncList` | itinerary | effect | codegen | 是 | required; members of the combination | llvm/utils/TableGen/DFAPacketizerEmitter.cpp:156 |
| `ComboFuncData.TheComboFunc` | itinerary | effect | codegen | 是 | required; combined unit identity | llvm/utils/TableGen/DFAPacketizerEmitter.cpp:154 |
| `ComboFuncUnits.CFD` | itinerary | effect | codegen | 是 | required; combination table | llvm/utils/TableGen/DFAPacketizerEmitter.cpp:144 |
| `InstrItinData.Bypasses` | itinerary | effect | codegen | 是 | []; per-operand forwarding paths | llvm/utils/TableGen/SubtargetEmitter.cpp:487 |
| `InstrItinData.NumMicroOps` | itinerary | effect | codegen | 是 | 1; 0 means a dynamically determined number | llvm/utils/TableGen/SubtargetEmitter.cpp:640 |
| `InstrItinData.OperandCycles` | itinerary | effect | codegen | 是 | []; per-operand cycle counts | llvm/utils/TableGen/SubtargetEmitter.cpp:471 |
| `InstrItinData.Stages` | itinerary | effect | codegen | 是 | required; reservation stages | llvm/utils/TableGen/SubtargetEmitter.cpp:426 |
| `InstrItinData.TheClass` | itinerary | selector | — | 是 | required; the InstrItinClass being described | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1121 |
| `InstrStage.Cycles` | itinerary | effect | codegen | 是 | required; stage length in cycles | llvm/utils/TableGen/SubtargetEmitter.cpp:435 |
| `InstrStage.Kind` | itinerary | effect | codegen | 是 | Required (0); Reserved (1) | llvm/utils/TableGen/SubtargetEmitter.cpp:452 |
| `InstrStage.TimeInc` | itinerary | effect | codegen | 是 | -1 -> Cycles | llvm/utils/TableGen/SubtargetEmitter.cpp:449 |
| `InstrStage.Units` | itinerary | effect | codegen | 是 | required; functional units usable by the stage | llvm/utils/TableGen/SubtargetEmitter.cpp:440 |
| `ProcessorItineraries.BP` | itinerary | effect | codegen | 是 | required; bypass table | llvm/utils/TableGen/SubtargetEmitter.cpp:530 |
| `ProcessorItineraries.FU` | itinerary | effect | codegen | 是 | required; functional unit table | llvm/utils/TableGen/SubtargetEmitter.cpp:516 |
| `ProcessorItineraries.IID` | itinerary | effect | codegen | 是 | required; per-class itinerary data | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1112 |
| `ProcessorItineraries.PacketizerNamespace` | itinerary | effect | codegen | 是 | empty; one shared packetizer automaton | llvm/utils/TableGen/DFAPacketizerEmitter.cpp:223 |
| `AssemblerPredicate.AssemblerCondDag` | predicate | effect | asm | 是 | required; all_of/any_of/none_of over features | llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:187 |
| `AssemblerPredicate.AssemblerMatcherPredicate` | predicate | declaration | — | 是 | true | llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:25 |
| `AssemblerPredicate.PredicateName` | predicate | effect | asm | 是 | empty; diagnostics text | llvm/utils/TableGen/AsmMatcherEmitter.cpp:3004 |
| `InstructionEquivalenceClass.Opcodes` | predicate | effect | codegen, mca | 是 | required; a switch case per opcode | llvm/utils/TableGen/Common/PredicateExpander.cpp:527 |
| `InstructionEquivalenceClass.OperandIndices` | predicate | effect | mca | 是 | [] -> zero-width mask, meaning all explicit uses are independent | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:282 |
| `InstructionEquivalenceClass.Predicate` | predicate | effect | codegen, mca | 是 | required; the body of the opcode case | llvm/utils/TableGen/Common/PredicateExpander.cpp:527 |
| `Predicate.AssemblerCondDag` | predicate | effect | asm | 是 | required when used as an assembler predicate | llvm/utils/TableGen/AsmMatcherEmitter.cpp:4013 |
| `Predicate.AssemblerMatcherPredicate` | predicate | declaration | — | 是 | false; true opts the predicate into the asm matcher | llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:25 |
| `Predicate.CondString` | predicate | effect | isel | 是 | required; raw C++ expression in DAGISel scope | llvm/utils/TableGen/DAGISelMatcherEmitter.cpp:1347 |
| `Predicate.PredicateName` | predicate | effect | asm | 是 | empty; diagnostics text | llvm/utils/TableGen/AsmMatcherEmitter.cpp:3004 |
| `Predicate.RecomputePerFunction` | predicate | effect | isel | 是 | false; SelectionDAG ignores it, GlobalISel honors it | llvm/utils/TableGen/Common/GlobalISel/GlobalISelMatchTableExecutorEmitter.cpp:36 |
| `PredicateProlog.Code` | predicate | effect | codegen | 是 | required; spliced into resolveSchedClass only | llvm/utils/TableGen/SubtargetEmitter.cpp:1949 |
| `STIPredicateDecl.DefaultReturnValue` | predicate | effect | codegen, mca | 是 | FalsePred (unmatched opcode returns false) | llvm/utils/TableGen/Common/PredicateExpander.cpp:565 |
| `STIPredicateDecl.Delegates` | predicate | effect | codegen, mca | 是 | []; delegated predicates run first and may set the mask | llvm/utils/TableGen/Common/PredicateExpander.cpp:504 |
| `STIPredicateDecl.ExpandForMC` | predicate | declaration | — | 是 | true; emits an MC-layer equivalent | llvm/utils/TableGen/Common/PredicateExpander.cpp:489 |
| `STIPredicateDecl.Name` | predicate | declaration | — | 是 | required; names the generated function | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:294 |
| `STIPredicateDecl.OverridesBaseClassMember` | predicate | declaration | — | 是 | true; declares a virtual override in TargetSubtargetInfo | llvm/include/llvm/Target/TargetInstrPredicate.td:395 |
| `STIPredicateDecl.UpdatesOpcodeMask` | predicate | declaration | — | 是 | false; adds an in/out APInt mask to the generated signature | llvm/utils/TableGen/Common/PredicateExpander.cpp:489 |
| `TIIPredicate.Body` | predicate | effect | codegen, mca | 否 | required; expanded into both MachineInstr and MCInst helpers | llvm/utils/TableGen/InstrInfoEmitter.cpp:662 |
| `InstRW.Instrs` | sched | effect | codegen, mca | 是 | required; opcode or regex DAG expanded at emit | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:186 |
| `InstRW.OperandReadWrites` | sched | effect | codegen, mca | 是 | required; replaces the instruction's SchedRW for that CPU | llvm/utils/TableGen/SubtargetEmitter.cpp:1150 |
| `InstRW.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:1150 |
| `InstRW.Unsupported` | sched | dead | — | 是 | false; no consumer | llvm/include/llvm/Target/TargetSchedule.td:469 |
| `ItinRW.MatchedItinClasses` | sched | effect | codegen, mca | 是 | required; only consulted when the class has no Writes | llvm/utils/TableGen/SubtargetEmitter.cpp:1178 |
| `ItinRW.OperandReadWrites` | sched | effect | codegen, mca | 是 | required; same expansion as InstRW once matched | llvm/utils/TableGen/SubtargetEmitter.cpp:1178 |
| `ItinRW.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:1178 |
| `ProcReadAdvance.CycleTunables` | sched | effect | codegen, mca | 是 | []; folded into Cycles at emit time (Cycles + tunable) | llvm/utils/TableGen/SubtargetEmitter.cpp:1337 |
| `ProcReadAdvance.Cycles` | sched | effect | codegen, mca | 是 | required; negative values increase latency | llvm/utils/TableGen/SubtargetEmitter.cpp:1345 |
| `ProcReadAdvance.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:1319 |
| `ProcReadAdvance.Unsupported` | sched | effect | codegen, mca | 是 | false; true invalidates the whole sched class | llvm/utils/TableGen/SubtargetEmitter.cpp:1324 |
| `ProcReadAdvance.ValidWrites` | sched | effect | codegen, mca | 是 | [] -> WriteResourceID 0, applies to every write (no bypass) | llvm/utils/TableGen/SubtargetEmitter.cpp:1329 |
| `ProcResGroup.BufferSize` | sched | effect | codegen, mca | 是 | -1 -> same semantics as ProcResourceUnits.BufferSize | llvm/utils/TableGen/SubtargetEmitter.cpp:919 |
| `ProcResGroup.Resources` | sched | effect | codegen, mca | 是 | required; group NumUnits is derived by summing members | llvm/utils/TableGen/SubtargetEmitter.cpp:735 |
| `ProcResGroup.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:937 |
| `ProcResourceUnits.BufferSize` | sched | effect | codegen, mca | 是 | -1 -> OoO port (no dispatch hazard); 0 barrier; 1 in-order; >1 station | llvm/utils/TableGen/SubtargetEmitter.cpp:900 |
| `ProcResourceUnits.Kind` | sched | selector | — | 是 | required; also the name base of ProcResource<num> | llvm/utils/TableGen/SubtargetEmitter.cpp:908 |
| `ProcResourceUnits.NumUnits` | sched | effect | codegen, mca | 是 | required; 0 yields a zero resource factor | llvm/utils/TableGen/SubtargetEmitter.cpp:913 |
| `ProcResourceUnits.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:937 |
| `ProcResourceUnits.Super` | sched | effect | codegen, mca | 是 | unset -> SuperIdx 0 (no super-resource) | llvm/utils/TableGen/SubtargetEmitter.cpp:896 |
| `ProcWriteResources.AcquireAtCycles` | sched | effect | codegen | 是 | [] -> 0 | llvm/utils/TableGen/SubtargetEmitter.cpp:1259 |
| `ProcWriteResources.BeginGroup` | sched | effect | codegen, mca | 是 | false; OR-reduced over all writes of the class | llvm/utils/TableGen/SubtargetEmitter.cpp:1212 |
| `ProcWriteResources.EndGroup` | sched | effect | codegen, mca | 是 | false; OR-reduced over all writes of the class | llvm/utils/TableGen/SubtargetEmitter.cpp:1213 |
| `ProcWriteResources.Latency` | sched | effect | codegen, mca | 是 | 1; summed across a WriteSequence; -1 means invalid/infinite | llvm/utils/TableGen/SubtargetEmitter.cpp:1210 |
| `ProcWriteResources.NumMicroOps` | sched | effect | codegen, mca | 是 | 1; summed over every write in the class | llvm/utils/TableGen/SubtargetEmitter.cpp:1211 |
| `ProcWriteResources.ReleaseAtCycles` | sched | effect | codegen, mca | 是 | [] -> 1 cycle per resource, NOT Latency | llvm/utils/TableGen/SubtargetEmitter.cpp:1254 |
| `ProcWriteResources.RetireOOO` | sched | effect | mca | 是 | false | llvm/utils/TableGen/SubtargetEmitter.cpp:1216 |
| `ProcWriteResources.SchedModel` | sched | selector | — | 是 | unset -> applies to every model | llvm/utils/TableGen/SubtargetEmitter.cpp:937 |
| `ProcWriteResources.SingleIssue` | sched | effect | codegen, mca | 是 | false; syntactic sugar for BeginGroup\|EndGroup | llvm/utils/TableGen/SubtargetEmitter.cpp:1214 |
| `ProcWriteResources.Unsupported` | sched | effect | codegen, mca | 是 | false; true emits InvalidNumMicroOps instead of deleting the class | llvm/utils/TableGen/SubtargetEmitter.cpp:1206 |
| `ReadAdvance.ReadType` | sched | selector | — | 是 | required when used as ReadAdvance | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:2145 |
| `SchedAlias.AliasRW` | sched | selector | — | 是 | required; the replacement (sequence or variant allowed) | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:661 |
| `SchedAlias.MatchRW` | sched | selector | — | 是 | required; the SchedReadWrite being replaced | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:628 |
| `SchedAlias.SchedModel` | sched | selector | — | 是 | unset -> generic alias for every model | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:628 |
| `SchedMachineModel.CompleteModel` | sched | validation | — | 是 | true; TableGen completeness gate, runtime check is debug-only | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1919 |
| `SchedMachineModel.EnableIntervals` | sched | effect | codegen | 是 | false; ForceEnableIntervals overrides it | llvm/lib/CodeGen/MachineScheduler.cpp:2907 |
| `SchedMachineModel.FullInstRWOverlapCheck` | sched | validation | — | 是 | true; only downgrades a TableGen error | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1040 |
| `SchedMachineModel.HighLatency` | sched | effect | codegen | 是 | -1 -> DefaultHighLatency (10) | llvm/lib/CodeGen/TargetInstrInfo.cpp:1815 |
| `SchedMachineModel.IssueWidth` | sched | effect | codegen, mca | 是 | -1 -> DefaultIssueWidth (1); 0 is a real value and divides by zero | llvm/utils/TableGen/SubtargetEmitter.cpp:1542 |
| `SchedMachineModel.Itineraries` | sched | effect | codegen | 是 | NoItineraries -> null; a second, legacy scheduling mechanism | llvm/utils/TableGen/SubtargetEmitter.cpp:1050 |
| `SchedMachineModel.LoadLatency` | sched | effect | codegen | 是 | -1 -> DefaultLoadLatency (4); fallback when the sched class has no latency | llvm/lib/CodeGen/TargetInstrInfo.cpp:1813 |
| `SchedMachineModel.LoopMicroOpBufferSize` | sched | effect | tti | 是 | -1 -> DefaultLoopMicroOpBufferSize (0) | llvm/include/llvm/CodeGen/BasicTTIImpl.h:755 |
| `SchedMachineModel.MicroOpBufferSize` | sched | effect | codegen, mca | 是 | -1 -> DefaultMicroOpBufferSize (0, in-order); 0/1/>1 are distinct modes | llvm/utils/TableGen/SubtargetEmitter.cpp:1543 |
| `SchedMachineModel.MispredictPenalty` | sched | effect | codegen | 是 | -1 -> DefaultMispredictPenalty (10) | llvm/lib/CodeGen/EarlyIfConversion.cpp:1249 |
| `SchedMachineModel.NoModel` | sched | selector | — | 是 | false; TableGen bookkeeping for the synthetic NoSchedModel | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:525 |
| `SchedMachineModel.PostRAScheduler` | sched | effect | codegen | 是 | false | llvm/lib/CodeGen/TargetSubtargetInfo.cpp:65 |
| `SchedMachineModel.UnsupportedFeatures` | sched | validation | — | 是 | []; filters instructions out of the completeness check only | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1165 |
| `SchedVar.Predicate` | sched | effect | codegen, mca | 是 | required | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679 |
| `SchedVar.SchedModel` | sched | dead | — | 是 | ignored | llvm/include/llvm/Target/TargetSchedule.td:430 |
| `SchedVar.Selected` | sched | effect | codegen, mca | 是 | required | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679 |
| `SchedVariant.SchedModel` | sched | dead | — | 是 | ignored | llvm/include/llvm/Target/TargetSchedule.td:437 |
| `SchedVariant.Variadic` | sched | effect | codegen, mca | 是 | false; true distributes writes over variadic operands, latency not additive | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1486 |
| `SchedVariant.Variants` | sched | effect | codegen, mca | 是 | required | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679 |
| `WriteSequence.Repeat` | sched | effect | codegen, mca | 是 | 1; re-emits the whole list N times | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:749 |
| `WriteSequence.SchedModel` | sched | dead | — | 是 | ignored | llvm/include/llvm/Target/TargetSchedule.td:244 |
| `WriteSequence.Writes` | sched | effect | codegen, mca | 是 | required; expanded and summed into the class | llvm/utils/TableGen/Common/CodeGenSchedule.cpp:749 |

## 类与记录

| 名字 | 形态 | 声明 | 读取点 |
|---|---|---|---|
| `AllOfSchedPreds` | class | `class AllOfSchedPreds : SchedPredicateCombiner` | SubtargetEmitter.cpp:1669 |
| `AnyOfSchedPreds` | class | `class AnyOfSchedPreds : SchedPredicateCombiner` | SubtargetEmitter.cpp:1671 |
| `AsmOperandClass` | record | `class AsmOperandClass : —` | AsmMatcherEmitter.cpp:1462 |
| `CheckAll` | class | `class CheckAll : CheckPredicateSequence` | PredicateExpander.cpp:448 |
| `CheckAny` | class | `class CheckAny : CheckPredicateSequence` | PredicateExpander.cpp:452 |
| `CheckFunctionPredicate` | class | `class CheckFunctionPredicate : MCInstPredicate` | PredicateExpander.cpp:456 |
| `CheckFunctionPredicateWithTII` | class | `class CheckFunctionPredicateWithTII : MCInstPredicate` | PredicateExpander.cpp:462 |
| `CheckImmOperand` | class | `class CheckImmOperand : CheckOperandBase` | PredicateExpander.cpp:396 |
| `CheckImmOperandGE` | class | `class CheckImmOperandGE : CheckOperandBase` | PredicateExpander.cpp:421 |
| `CheckImmOperandGT` | class | `class CheckImmOperandGT : CheckOperandBase` | PredicateExpander.cpp:411 |
| `CheckImmOperandLE` | class | `class CheckImmOperandLE : CheckOperandBase` | PredicateExpander.cpp:416 |
| `CheckImmOperandLT` | class | `class CheckImmOperandLT : CheckOperandBase` | PredicateExpander.cpp:406 |
| `CheckImmOperandRange` | class | `class CheckImmOperandRange : CheckOperandBase` | PredicateExpander.cpp:426 |
| `CheckImmOperandSimple` | class | `class CheckImmOperandSimple : CheckOperandBase` | PredicateExpander.cpp:431 |
| `CheckImmOperand_s` | class | `class CheckImmOperand_s : CheckOperandBase` | PredicateExpander.cpp:401 |
| `CheckInvalidRegOperand` | class | `class CheckInvalidRegOperand : CheckOperandBase` | PredicateExpander.cpp:393 |
| `CheckIsImmOperand` | class | `class CheckIsImmOperand : MCOperandPredicate` | PredicateExpander.cpp:381 |
| `CheckIsRegOperand` | class | `class CheckIsRegOperand : MCOperandPredicate` | PredicateExpander.cpp:375 |
| `CheckIsVRegOperand` | class | `class CheckIsVRegOperand : MCOperandPredicate` | PredicateExpander.cpp:378 |
| `CheckNonPortable` | class | `class CheckNonPortable : MCInstPredicate` | PredicateExpander.cpp:469 |
| `CheckNot` | class | `class CheckNot : MCInstPredicate` | PredicateExpander.cpp:368 |
| `CheckNumOperands` | class | `class CheckNumOperands : MCInstPredicate` | PredicateExpander.cpp:439 |
| `CheckOpcode` | class | `class CheckOpcode : MCInstPredicate` | PredicateExpander.cpp:445 |
| `CheckPseudo` | class | `class CheckPseudo : CheckOpcode` | PredicateExpander.cpp:442 |
| `CheckRegOperand` | class | `class CheckRegOperand : CheckOperandBase` | PredicateExpander.cpp:384 |
| `CheckRegOperandSimple` | class | `class CheckRegOperandSimple : CheckOperandBase` | PredicateExpander.cpp:389 |
| `CheckSameRegOperand` | class | `class CheckSameRegOperand : MCInstPredicate` | PredicateExpander.cpp:435 |
| `ComboFuncUnits` | record | `class ComboFuncUnits : —` | DFAPacketizerEmitter.cpp:239 |
| `FeatureSchedPredicate` | class | `class FeatureSchedPredicate : SchedPredicateBase` | SubtargetEmitter.cpp:1648, 1763 |
| `HwModePredicateProlog` | record | `class HwModePredicateProlog : —` | SubtargetEmitter.cpp:2012 |
| `InstAlias` | record | `class InstAlias : —` | AsmMatcherEmitter.cpp:1647 |
| `InstRW` | record | `class InstRW : —` | CodeGenSchedule.cpp:603, 849 |
| `Instruction` | class | `class Instruction : InstructionEncoding` | AsmMatcherEmitter.cpp:668, 670 |
| `InstructionEncoding` | class | `class InstructionEncoding : —` | SubtargetEmitter.cpp:2000 |
| `InstructionEquivalenceClass` | record | `class InstructionEquivalenceClass : —` | CodeGenSchedule.cpp:268 |
| `ItinRW` | record | `class ItinRW : —` | CodeGenSchedule.cpp:616, 1151 |
| `LoadQueue` | class | `class LoadQueue : MemoryQueue` | CodeGenSchedule.cpp:478 |
| `MCFalse` | class | `class MCFalse : MCInstPredicate` | PredicateExpander.cpp:362 |
| `MCOpcodeSwitchStatement` | class | `class MCOpcodeSwitchStatement : MCStatement` | PredicateExpander.cpp:340 |
| `MCReturnStatement` | class | `class MCReturnStatement : MCStatement` | PredicateExpander.cpp:346 |
| `MCSchedPredicate` | class | `class MCSchedPredicate : SchedPredicateBase` | SubtargetEmitter.cpp:1640, 1646, 1762 |
| `MCTrue` | class | `class MCTrue : MCInstPredicate` | SubtargetEmitter.cpp:1641；PredicateExpander.cpp:356 |
| `MemoryQueue` | record | `class MemoryQueue : —` | CodeGenSchedule.cpp:476 |
| `MnemonicAlias` | record | `class MnemonicAlias : —` | AsmMatcherEmitter.cpp:3119 |
| `NotSchedPred` | class | `class NotSchedPred : SchedPredicateCombiner` | SubtargetEmitter.cpp:1673, 1690 |
| `Operand` | class | `class Operand : DAGOperand` | AsmMatcherEmitter.cpp:1250, 1795 |
| `Predicate` | class | `class Predicate : —` | AsmMatcherEmitter.cpp:833 |
| `Predicate` | record | `class Predicate : —` | SubtargetFeatureInfo.cpp:27 |
| `PredicateProlog` | record | `class PredicateProlog : —` | SubtargetEmitter.cpp:1633 |
| `ProcResGroup` | class | `class ProcResGroup : ProcResourceKind` | SubtargetEmitter.cpp:741, 901, 1063 …（共 9 处） |
| `ProcResGroup` | record | `class ProcResGroup : ProcResourceKind` | CodeGenSchedule.cpp:1822, 1867 |
| `ProcResourceUnits` | class | `class ProcResourceUnits : —` | CodeGenSchedule.cpp:2044 |
| `ProcResourceUnits` | record | `class ProcResourceUnits : —` | CodeGenSchedule.cpp:1821, 1876 |
| `Processor` | record | `class Processor : —` | SubtargetEmitter.cpp:300；CodeGenSchedule.cpp:517 |
| `ReadAdvance` | class | `class ReadAdvance : ProcReadAdvance` | CodeGenSchedule.cpp:1898, 2145 |
| `ReadAdvance` | record | `class ReadAdvance : ProcReadAdvance` | CodeGenSchedule.cpp:1854 |
| `RegInfo` | class | `class RegInfo : —` | SubtargetEmitter.cpp:1995 |
| `Register` | class | `class Register : —` | SubtargetEmitter.cpp:1996；PredicateExpander.cpp:107 |
| `RegisterByHwMode` | class | `class RegisterByHwMode : HwModeSelect, RegisterOperand` | AsmMatcherEmitter.cpp:2313 |
| `RegisterClass` | class | `class RegisterClass : RegisterClassLike` | AsmMatcherEmitter.cpp:1244 |
| `RegisterClassLike` | class | `class RegisterClassLike : DAGOperand` | SubtargetEmitter.cpp:1998；AsmMatcherEmitter.cpp:1236, 1254 …（共 4 处） |
| `RegisterFile` | record | `class RegisterFile : —` | CodeGenSchedule.cpp:1782 |
| `RegisterOperand` | class | `class RegisterOperand : DAGOperand` | AsmMatcherEmitter.cpp:1214 |
| `RegisterOperand` | record | `class RegisterOperand : DAGOperand` | AsmMatcherEmitter.cpp:2612 |
| `RetireControlUnit` | record | `class RetireControlUnit : —` | CodeGenSchedule.cpp:463 |
| `STIPredicate` | record | `class STIPredicate : —` | CodeGenSchedule.cpp:410 |
| `STIPredicateDecl` | record | `class STIPredicateDecl : —` | CodeGenSchedule.cpp:256 |
| `SchedAlias` | record | `class SchedAlias : —` | CodeGenSchedule.cpp:632 |
| `SchedMachineModel` | class | `class SchedMachineModel : —` | CodeGenSchedule.cpp:545 |
| `SchedPredicateCombiner` | class | `class SchedPredicateCombiner : SchedPredicateBase` | SubtargetEmitter.cpp:1662, 1766 |
| `SchedRead` | class | `class SchedRead : SchedReadWrite` | CodeGenSchedule.cpp:597, 610, 623 …（共 9 处） |
| `SchedReadAdvance` | class | `class SchedReadAdvance : SchedRead, ProcReadAdvance` | SubtargetEmitter.cpp:1000, 1021；CodeGenSchedule.cpp:2005 |
| `SchedReadAdvance` | record | `class SchedReadAdvance : SchedRead, ProcReadAdvance` | CodeGenSchedule.cpp:1859 |
| `SchedReadWrite` | record | `class SchedReadWrite : —` | CodeGenSchedule.cpp:690 |
| `SchedVar` | class | `class SchedVar : —` | CodeGenSchedule.cpp:1369, 1438, 1470 |
| `SchedVariant` | class | `class SchedVariant : —` | CodeGenSchedule.cpp:568；CodeGenSchedule.h:69 |
| `SchedWrite` | class | `class SchedWrite : SchedReadWrite` | CodeGenSchedule.cpp:594, 607, 620 …（共 6 处） |
| `SchedWriteRes` | class | `class SchedWriteRes : SchedWrite, ProcWriteResources` | SubtargetEmitter.cpp:942, 962；CodeGenSchedule.cpp:2002 |
| `SchedWriteRes` | record | `class SchedWriteRes : SchedWrite, ProcWriteResources` | CodeGenSchedule.cpp:1850 |
| `StoreQueue` | class | `class StoreQueue : MemoryQueue` | CodeGenSchedule.cpp:488 |
| `SubRegRange` | class | `class SubRegRange : —` | SubtargetEmitter.cpp:1997 |
| `SubtargetFeature` | class | `class SubtargetFeature : —` | SubtargetFeatureInfo.cpp:136, 253 |
| `SubtargetFeature` | record | `class SubtargetFeature : —` | SubtargetEmitter.cpp:176, 209, 2089 …（共 4 处） |
| `TIIPredicate` | class | `class TIIPredicate : FunctionPredicateBase, MCInstPredicate` | PredicateExpander.cpp:472 |
| `TIIPredicate` | record | `class TIIPredicate : FunctionPredicateBase, MCInstPredicate` | CodeGenSchedule.cpp:448 |
| `TokenAlias` | record | `class TokenAlias : —` | AsmMatcherEmitter.cpp:1753 |
| `ValueType` | class | `class ValueType : —` | SubtargetEmitter.cpp:1993 |
| `WriteRes` | class | `class WriteRes : ProcWriteResources` | CodeGenSchedule.cpp:1891, 2110 |
| `WriteRes` | record | `class WriteRes : ProcWriteResources` | CodeGenSchedule.cpp:1846 |
| `WriteSequence` | class | `class WriteSequence : SchedWrite` | CodeGenSchedule.cpp:565, 1477；CodeGenSchedule.h:76 |

## def

| 名字 | 声明 | 读取点 |
|---|---|---|
| `NoItineraries` | `def NoItineraries : ProcessorItineraries` | CodeGenSchedule.cpp:527 |
| `NoItinerary` | `def NoItinerary : InstrItinClass` | CodeGenSchedule.cpp:832 |
| `NoSchedModel` | `def NoSchedModel : SchedMachineModel` | CodeGenSchedule.cpp:526 |
