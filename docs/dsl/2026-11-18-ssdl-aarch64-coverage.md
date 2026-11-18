# SSDL × AArch64：可支持性分析

- date: 2026-11-18
- status: **Evidence（可复算）**
- LLVM：`700e551714cc`（`$SSDL_LLVM_SRC`）
- SSDL：`d766aa1`
- 方法：源码普查（`lib/Target/AArch64/AArch64Sched*.td`，31 个模型）
  + 两轮实测（单模型端到端、31 模型 sweep）
- 复现命令见 §7

## 0. 结论

**能支持。语义层不需要扩展；语法层只差 4 个字段/规则级的小口；真正的阻塞在工具链。**

| 层 | 结论 | 证据 |
|---|---|---|
| 语义层 | 覆盖，且比 RISC-V **更简单** | `ItinRW`/`RegisterFile`/`LoadQueue`/`StoreQueue` 全部为 0；`defm`/`multiclass` 为 0 |
| 语法层 | v2 的 6 个顶层关键字够用 | AArch64 只用到 `set` / `predicate`(sched·tii·prolog) / `feature` / `machine`(model·cpu·alias) |
| 语法层 | 4 个小缺口（见 §4） | `map=`、`MCSchedPredicate<命名TIIPredicate>`、TII 外部方法名、`unsupported_features` 前缀 |
| 工具层 | **主要工作量** | 31 个模型 sweep：2/31 已达 record 级全等；其余 29 个卡在 4 类转写缺口 + RISC-V 硬编码 |

一句话：**SSDL 的领域模型是够的；现在缺的是把 RISC-V 专用的工具链参数化，加 4 条小的 lowering。**

## 1. 普查

### 1.1 规模

| 项 | 数量 |
|---|---:|
| `SchedMachineModel` | **31** |
| `ProcessorModel`（`-mcpu` 名） | **85** |
| `ProcessorAlias` | **19** |
| 调度 TD 总行数 | **57,343** |
| 共享读写词表（`AArch64Schedule.td`） | 34 `SchedWrite` + 12 `SchedRead` |

85 个 CPU 共享 31 个模型（如 `cortex-a53/a34/a35/a65/a65ae` 都用 `CortexA53Model`），
这正是 v2 把 `machine` 拆成 `model` / `cpu` 的现实理由：一个 model 对多个 cpu。

### 1.2 构造频次（按 def 站点统计）

| TableGen 构造 | 次数 | SSDL 落点 |
|---|---:|---|
| `InstRW` | 11,538 | `map`（全部匿名 def） |
| `SchedWriteRes` + `WriteRes` | 3,649 + 604 | `bind` |
| `ReadAdvance` + `SchedReadAdvance` | 305 + 384 | `advance` |
| `SchedAlias` | 440 | `alias` / `variant` 的自动别名 |
| `SchedWriteVariant` | 199 | `variant` |
| `SchedReadVariant` | 13 | `read_variant` |
| `WriteSequence` | 19 | `write_seq` |
| `ProcResource` + `ProcResGroup` | 486 + 220 | `resource` / `res_group` |
| `MCSchedPredicate` / `SchedPredicate` | 48 / 6 | `predicate sched` |
| `TIIPredicate` | 14 | `predicate tii` |
| `FeatureSchedPredicate` | 2 | `feature(X)` 谓词 |
| `PredicateProlog` | 1 | `predicate prolog` |
| `RetireControlUnit` | 1（`AArch64SchedOlympus.td:48`） | `retire_unit` |
| `ItinRW` / `RegisterFile` / `LoadQueue` / `StoreQueue` | **0** | 用不上 |
| `defm` / `multiclass` | **0** | 不需要 |
| `foreach` | 4（都在 `AArch64SchedPredicates.td`） | 展开即可 |

`InstRW` 的指令集合里 `instregex` 占多数（6,278 行 vs 2,154 行 `instrs`），
SSDL 的 `regex("...")` 一一对应；AArch64 不在同一列表里混写两种形式。

### 1.3 机器标量

| 字段 | 分布 |
|---|---|
| `IssueWidth`（31） | 1/2/3/4/5/6/8/10/12/14 |
| `MicroOpBufferSize`（31） | 0（6 个顺序核）、70、128、160、180、192、208、228、256、320、376 |
| `LoopMicroOpBufferSize`（24） | 16/32/40/48/60/128 |
| `LoadLatency`（31） | 3/4/5 |
| `MispredictPenalty`（28） | 8–16 |
| `CompleteModel`（31） | 26 个 `1`、5 个 `0` |
| `FullInstRWOverlapCheck`（9） | 全为 `0` |
| `PostRAScheduler`（9） | 全为 `1` |
| `HighLatency` / `EnableIntervals` | 0 |

`UnsupportedFeatures` 29 处（见 §4 G4）。

## 2. 逐构造映射

| AArch64 TD | SSDL v2 | 判定 |
|---|---|---|
| `def X : SchedWrite/SchedRead`（全局 46 个） | `write X;` / `read X;`（各 machine 内声明） | ✓ 见 §8 第 2 条（emitter 会 hoist 成全局唯一 def） |
| `def : WriteRes<W, [Res]> {...}` | `bind W { resources=[Res]; latency=N; ... }` | ✓ |
| `def X : SchedWriteRes<[Res]>` 直接当 write | `write X; bind X {...}` | ✓ 语义等价，记录数 2 vs 1（§8 第 1 条） |
| `def : InstRW<[W...], (instrs ...)>` | `map [...] { 0 = W; 1 = read_default; }` | ✓ |
| `(instregex "...")` | `regex("...")` | ✓ |
| `def : ReadAdvance<R, N, [W...]>` | `advance R { cycles=N; writes=[W...]; }` | ✓ |
| `def X : SchedReadAdvance<N, [W]>` **直接当 read 用** | `read X; advance X {...}` | ✓ 语义等价；转写需支持（§6） |
| `def : SchedAlias<A, B>` | `alias A -> B;` | ✓ |
| `def X : SchedWriteVariant<[...]>` **直接当 write 用** | `write X; variant X {...}` | ✓ 语义等价；转写需支持（§6） |
| `SchedVar<Pred, [W...]>` | `case Pred: [W...]` | ✓ |
| `NoSchedPred` | `default:` | ✓ |
| `WriteSequence<[W...], N>` | `write_seq X = [...] { repeat = N; }` | ✓ |
| `ProcResource<N> { let Super=...; let BufferSize=...; }` | `resource X { super=...; units=N; buffer=...; }` | ✓ |
| `ProcResGroup<[..]>` | `res_group X = [..] { buffer = N; }` | ✓ |
| `RetireControlUnit` | `retire_unit { buffer = N; max_per_cycle = N; }` | ✓ |
| `ProcessorModel<"n", M, [F...], [T...]>` | `machine cpu n { model = M; features=[...]; tune_features=[...]; }` | ✓（emitter 类名需参数化，§6） |
| `ProcessorAlias<...>` | `machine alias X = Y;` | ✓ |
| `SubtargetFeature<"mattr","Field","val","desc",[implies]>` | `feature plain X { ... }` | ✓ |
| `FeatureSchedPredicate<F>` | `feature(F)` 谓词 | ✓ |
| `PredicateProlog<[code]>` | `predicate prolog = host [...];` | ✓ |
| `CheckFunctionPredicate(WithTII)<a,b>` | `func(a,b)` / `func_tii(a,b)` | ✓ |
| `CheckNonPortable<code>` | `non_portable(host [...])` | ✓（MCA 侧恒为 false，见 §8 第 5 条） |
| `SchedPredicate<[{code}]>`（Cyclone/Falkor） | `sched_code(host [{...}])` | ✓ |
| `CheckImmOperand_s<i,"Enum">` + `FunctionMapper` | `imm_is_enum(i,"Enum", map=...)` | **✗ 未 lowering**（§4 G1） |
| `MCSchedPredicate<命名TIIPredicate>` | — | **✗ 无形式**（§4 G2） |
| `TIIPredicate<"C++名", switch>` | `predicate tii Name { switch ... }` | **△ 名字不可分离**（§4 G3） |
| `UnsupportedFeatures = [...]` | `unsupported_features = [...]` | **✗ Has 前缀**（§4 G4） |

## 3. 谓词层实测

AArch64 的谓词分三类，SSDL 的三种谓词形态正好对上：

1. **MC 谓词**（`CheckOpcode` / `CheckRegOperand` / `CheckSameRegOperand` /
   `CheckImmOperand*` / `CheckNumOperands` / `CheckAll` / `CheckAny` / `CheckNot`）
   → `opcode` / `reg_is` / `same_reg` / `imm_*` / `num_ops` / `all_of` / `any_of` / `not`。
   `TargetInstrPredicate.td` 的 29 个 `Check*` 类里，AArch64 用到的全部落在 SSDL 的封闭原子集内。

2. **函数谓词**（`CheckFunctionPredicateWithTII`，3 个 def，双版本 MC+MI）
   → `func("AArch64_MC::isFpOrNEON", "AArch64InstrInfo::isFpOrNEON")`。
   三个：`isFpOrNEON` / `isHForm` / `isQForm`（`AArch64SchedPredicates.td:68-82`）。

3. **TII 谓词 + 包装**（14 个 `TIIPredicate`，13 个 `MCSchedPredicate<...Fn>`
   包装，约 60 个 `SchedVar` 引用点）。
   这是 AArch64 与 RISC-V 最大的结构差异：**谓词不只是叶子，还要被别的记录引用**。

`FunctionMapper` 是第四个差异点：4 个映射函数
（`getArithExtendType` / `getArithShiftValue` / `getShiftValue` / `getShiftType`）
覆盖 23 个谓词 def，再被 `IsExtWorXPred`、`IsShiftBy*`、Exynos/Olympus 的
组合谓词引用（`AArch64SchedPredicates.td:20-50`）。

## 4. 四个缺口（附实测）

### G1 `map=` 只进签名表，不进 lowering

```
$ python3 tools/ssdl/emit_td.py probe.ssdl --syntax v2
line 3: predicate CheckLSL0: error: imm_is_enum: map= is not lowered yet
```

`tools/ssdl/predicates.py:516` 的 `reject_mapper()` 对 `map=` 一律报错。
上游语义是给谓词记录加 `let FunctionMapper = "..."`，再在 O(1) 内把操作数
映射成枚举值。SSDL 要补的是：遇到 `map=` 时把该原子**物化成具名 def**
（`def ..._m0 : CheckImmOperand_s<3,"..."> { let FunctionMapper = "..."; }`），
因为内联表达式上挂不了 `let`。影响面：23 个谓词 def，4 个映射函数。

### G2 命名 `TIIPredicate` 无法被 SCHED 谓词引用

```
$ ... predicate tii RegShiftedFn { ... } predicate sched P = RegShiftedFn;
line 9: lowering: error: predicate RegShiftedFn: unknown predicate
```

`self.predicates` 只登记 `PredicateDecl`，不登记 `TIIPredicate`
（`tools/ssdl/predicates.py:268 ensure_named`）。上游写法是
`def RegShiftedPred : MCSchedPredicate<RegShiftedFn>;`，AArch64 有 13 处。

**这个缺口不能拿 `non_portable` 绕过**：`CheckNonPortable` 在生成 MCInst
路径时恒展开为 `false`（`include/llvm/Target/TargetInstrPredicate.td:236-240`），
而 `TIIPredicate` 在两条路径上都是真的。用 `non_portable` 替代会**改变
llvm-mca 的变体选择**。

修法很小：`ensure_named` 认识 TII 名字，`mc(TiiName)` → `MCSchedPredicate<TiiName>`。

### G3 `TIIPredicate` 的外部方法名不可指定

SSDL 发的是 `TIIPredicate<"<谓词名>", ...>`（`check.py:662` 的 golden 断言），
但 AArch64 的 14 个 def **全部**是「def 名 ≠ C++ 方法名」：

| def | C++ 方法名 |
|---|---|
| `RegExtendedFn` | `hasExtendedReg` |
| `RegShiftedFn` | `hasShiftedReg` |
| `ScaledIdxFn` | `isScaledAddr` |
| `IsCopyIdiomFn` | `isCopyIdiom` |
| `IsZeroIdiomFn` | `isZeroIdiom` |
| `IsZeroFPIdiomFn` | `isZeroFPIdiom` |
| `Exynos*Fn`（6 个） | `isExynos*` |
| `Neoverse*Fn`（2 个） | `isNeoverse*` |

这个字符串是 `AArch64InstrInfo::hasExtendedReg()` 的 C++ 契约，不能改名。
修法：`predicate tii RegExtendedFn { tii = "hasExtendedReg"; switch ... }`
（或 `method = "..."`），一个字段。

### G4 `unsupported_features` 硬编码 `Has` 前缀

```
$ ... machine model m { unsupported_features = [HasSVE, HasMTE]; }
def mModel : SchedMachineModel { let UnsupportedFeatures = [HasHasSVE, HasHasMTE]; }
```

`tools/ssdl/emit_td.py:1618` 对每个名字拼 `Has`，且**不校验存在性**——
这是 RISC-V 的命名约定（`StdExtZbkb` → `HasStdExtZbkb`）。AArch64 的
`HasSVE` 会被拼成 `HasHasSVE`，TableGen 直接报未定义。

另外 AArch64 的 `UnsupportedFeatures` 常常是**复合列表**：
`!listconcat(SVEUnsupported.F, PAUnsupported.F, SMEUnsupported.F, [HasMTE, ...])`
（`AArch64SchedA53.td:29-31`），29 处引用、24 个不同名字。
SSDL 目前只能在每个 model 里把列表摊平重写一遍。

修法两件事：(a) 按已声明名字直出，`Has` 映射只留给 `extension` kind；
(b) 增加 `set predicates X = [...]`（第 4 个 set kind），让复合列表写一次、
各 model 用 `...X` 展开。这同时消掉 G4 的一半重复。

## 5. 实测

### 5.1 单模型端到端（TSV110）

```text
$ from_records.py aarch64.rec --model TSV110Model --machine tsv110
$ emit_td.py tsv110.ssdl --target-include AArch64.td --records-json tsv110.json
$ equiv.py aarch64.rec tsv110.json --in-model TSV110Model --gen-model tsv110Model

intree:    87 writes, 13 reads, 4 variants, 224 InstRW, 7 resources
generated: 87 writes, 10 reads, 0 variants, 224 InstRW, 7 resources
MISMATCH (7 entries shown)
read TSV110ReadCRC / MAQ / MAW:     only in intree   ← SchedReadAdvance 直接当 read
variant TSV110WrISReg/IEReg(+Br):   only in intree   ← SchedWriteVariant 直接当 write
```

**87/87 writes、224/224 InstRW、7/7 resources 完全一致**；
7 处差异全部是「记录形态」而非「调度语义」，对应 §6 的两个转写缺口。

### 5.2 31 模型 sweep

以 `-print-records` 为 oracle，对每个模型跑
`from_records → emit_td → equiv`：

| 结果 | 数量 | 模型 |
|---|---:|---|
| record 级全等 | **2** | Kryo、Oryon |
| 未达 record 级全等 | 29 | 其余 |

29 个的阻塞原因只有 4 类，全部在 `from_records.py` / `emit_td.py`，与语言无关：

| 阻塞 | 模型数 | 根因 | 修法 |
|---|---:|---|---|
| 变体引用的谓词 def 不转写（`TIIPredicate` / `CheckImmOperand_s` / `CheckSameRegOperand`） | 21 | `from_records.py:111` 直接抛 `unsupported MC predicate` | 先转写谓词块，再转写变体 |
| 引用但未 bind 的全局 write 不声明（`WriteVd`/`WriteVq`/`WriteFImm`/`WriteFCopy`） | 3 | 只收集本模型 bind 过的 write | 收集 InstRW 引用到的全部 RW |
| `let SchedModel` 外的 `ProcResource` 不收集（`A57UnitM`…） | 1 | 按 `SchedModel == M` 过滤 | 按「被本模型 bind 引用」收集 |
| 融合式 read / direct variant 不转写 | 4 | 只认 `ReadAdvance` 的 `ReadType` 字段、只认 `SchedAlias<W,V>` | 识别 `SchedReadAdvance`、`SchedWriteVariant` 作为 write/read 本体 |

结论：**这 29 个不是「SSDL 表达不了」，而是「转写器还没认全上游的两种等价写法」。**

## 6. 与 RISC-V 的差异

| 维度 | RISC-V | AArch64 | 对 SSDL 的含义 |
|---|---|---|---|
| 模型数 | 14（族展开后更多实例） | 31（85 CPU，**无族**） | AArch64 用 `model` + 多个 `cpu`，不用 `family` |
| 共享读写词表 | 231 个，全局 | 46 个，全局 | 两者都靠 emitter 的 `collect_global_vocabulary` hoist |
| 元编程 | `foreach`/`defm` 大量 | `defm`=0，`foreach`=4 | AArch64 对「无元编程」更友好 |
| 行程 | 有 `SchedMachineModel` + `Itineraries` | `ItinRW`=0 | 纯 sched-model 路线，正是 SSDL 主路径 |
| MCA 逃逸项 | `RegisterFile`/`LoadQueue`/`StoreQueue` | **0** | AArch64 用不到 SSDL 的弱项 |
| 谓词 | 基本是叶子，`MCInstPredicate` 树 | **TII 谓词 + 包装 + 复用**（60 处） | 需要 G2 |
| 映射函数 | 无 | 4 个 `FunctionMapper`，23 个 def | 需要 G1 |
| 特性 | `RISCVExtension`/`RISCVTuneFeature` 三个 kind | 只有 `SubtargetFeature` | AArch64 只用 `plain`，`extension`/`tune` 不参与 |
| 处理器类 | `RISCVProcessorModel` | 通用 `ProcessorModel` | emitter 类名需参数化 |

一句话：**AArch64 在「数据量/元编程」上比 RISC-V 简单，在「谓词结构」上比 RISC-V 复杂。**

## 7. 复现

```bash
export SSDL_LLVM_SRC=/path/to/llvm-project
export SSDL_LLVM_BUILD=/path/to/llvm-build
T=$SSDL_LLVM_BUILD/bin/llvm-tblgen
A=$SSDL_LLVM_SRC/lib/Target/AArch64

# 1) oracle
$T -print-records -I $SSDL_LLVM_SRC/include -I $A -o /tmp/aarch64.rec $A/AArch64.td

# 2) 单模型链路
python3 tools/ssdl/from_records.py /tmp/aarch64.rec --model TSV110Model --machine tsv110 -o /tmp/tsv110.ssdl
python3 tools/ssdl/emit_td.py /tmp/tsv110.ssdl --target-include AArch64.td \
    --records-json /tmp/tsv110.json -o /tmp/tsv110.td
python3 tools/ssdl/equiv.py /tmp/aarch64.rec /tmp/tsv110.json \
    --in-model TSV110Model --gen-model tsv110Model

# 3) 全量（31 个模型）
for M in $(grep -h '^def .* : SchedMachineModel' $A/AArch64Sched*.td \
           | sed 's/def \([A-Za-z0-9_]*\).*/\1/'); do
  mach=$(printf '%s' "$M" | sed 's/Model$//' | tr 'A-Z' 'a-z')
  python3 tools/ssdl/from_records.py /tmp/aarch64.rec --model "$M" --machine "$mach" -o "/tmp/$M.ssdl" &&
  python3 tools/ssdl/emit_td.py "/tmp/$M.ssdl" --target-include AArch64.td \
      --records-json "/tmp/$M.json" -o "/tmp/$M.td" &&
  python3 tools/ssdl/equiv.py /tmp/aarch64.rec "/tmp/$M.json" \
      --in-model "$M" --gen-model "${mach}Model"
done
```

`llvm-tblgen -print-records` 对 AArch64 约 1.2 秒 / 63 MB，比 RISC-V 的
151 MB 还小，跑全量 sweep 的机器成本可以忽略。

## 8. 诚实边界

1. **record 级 ≠ 语义级。** `write W; bind W` 发两条记录
   （`SchedWrite` + `SchedWriteRes`/`WriteRes`），上游常用一条
   `def X : SchedWriteRes<...>`。MC 看到的是同一张 write 表，但记录名与数量不同；
   §5.2 的「全等」是 `equiv.py` 按调度键（`WriteType`/`ReadType`/`MatchRW`/资源名）
   比较的结果，不是逐行文本相同。
2. **全局词表 hoist 是已实现的设计**，不是缺口。
   `tools/ssdl/emit_td.py:748 collect_global_vocabulary` 把各 machine 里声明的
   `write`/`read` 去重后只发一次，正好对应
   `AArch64Schedule.td` 的「46 个全局 write + 每模型 604 条 bind」。
3. **SSDL 的 resource 作用域比上游严。** 上游允许 `ProcResource` 写在
   `let SchedModel` 外面（`AArch64SchedA57.td:48`），SSDL 要求写在 machine 内。
   语义不变，但转写器要按「被引用」而不是「带 SchedModel 字段」收集。
4. **匿名 `ProcessorModel` 无法用 `--in-processor` 对比。**
   AArch64 的 85 个处理器全是 `def : ProcessorModel<...>`，没有 def 名；
   `equiv.py` 按 def 名取记录，所以目前只能比 model 侧。
   要比 CPU 侧，得给 `equiv` 加「按 `string Name` 找 ProcessorModel」的入口。
5. **`non_portable` 不是万能逃生口。** 它在 MCInst 路径恒为 false
   （`TargetInstrPredicate.td:236-240`）。AArch64 自己用了 6 次
   `SchedPredicate<[{...}]>`，SSDL 的 `sched_code` 同样会得到「CodeGen 真 / MCA 假」
   的行为——这是**上游原有语义**，SSDL 的 warning 只是把它显式化。
6. **指令/寄存器词汇不在 SSDL 内。** `ADDWrr`、`X0`、`GPR64`、`HasSVE`
   都来自目标 `.td`；SSDL 只查语法与签名。AArch64 有 11,538 个 `InstRW`、
   85 个 CPU 的特性列表，这些数据必须由目标词表提供或由生成器导出。
7. **没有条件表达式。** AArch64 的 `LoadLatency` 等是常量，暂不构成问题；
   但如果要描述「按特性选不同延迟」，仍要靠 host profile 投影。

## 9. 落地顺序（建议）

| 步 | 内容 | 成本 | 产出 |
|---|---|---|---|
| 0 | `paths.py` / `fetch-records.sh` / emitter 类名 / census 参数化 | 半天 | `aarch64.rec` + 可跑 sweep |
| 1 | G1 `map=` lowering（具名 helper def + `FunctionMapper`） | 小 | 23 个谓词可直译 |
| 2 | G2 `mc(TiiName)` + G3 `tii = "..."` | 小 | 13 个包装 + 14 个 TII 可直译 |
| 3 | G4 `unsupported_features`：去 `Has` 前缀 + `set predicates` | 小 | 29 处不再手抄 |
| 4 | `from_records` 4 个转写缺口 | 中 | 31/31 sweep |
| 5 | 把 31 模型 sweep 接进 `check.py` 门禁 | 小 | AArch64 与 RISC-V 同级证据 |

**不建议做的**：为 AArch64 增加新的顶层关键字、引入 `defm`/`foreach` 式的元编程、
或把 `FunctionMapper` 做成字符串模板。这四条都会把「语法融合、语义分层」
重新拆回 TableGen 的样子。
