# 调度模型：组成、元素与关系

- 日期：2026-11-19
- 读者：要读/写 SSDL 或 TableGen 调度模型的人
- 一句话：调度模型是 **opcode →（资源占用 + 时间）的映射表**，不描述指令语义。

两个消费者读同一张表：**MachineScheduler**（CodeGen 里改指令顺序）与
**llvm-mca**（静态分析流水线）。

```text
                 ┌──────────────────────────────┐
  .td / SSDL ──► │ llvm-tblgen -gen-subtarget    │
                 └──────────────┬───────────────┘
                                ▼
        SubtargetInfo 表 + MCSchedModel 表（只读）
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
        MachineScheduler                  llvm-mca
        （改指令顺序）                    （算吞吐/瓶颈）
```

调度器只回答四个问题：

| 问题 | 模型里的字段 |
|---|---|
| 一条指令占几个发射槽、占哪些执行单元？ | `IssueWidth`、`NumMicroOps`、`ProcResource<units>` |
| 结果多久可用？ | `WriteRes.Latency` |
| 执行单元什么时候释放？ | `ReleaseAtCycles` / `AcquireAtCycles` |
| 读操作数要等多久？能否旁路？ | `ReadAdvance<read, cycles, [writes]>` |

---

## 1. 四层组成

```text
① 目标/特性层   ProcessorModel + Feature
        │  决定“这是哪颗 CPU、开了哪些扩展”
        ▼
② 机器模型层    SchedMachineModel
        │  IssueWidth / 资源树 / buffer / complete_model
        ▼
③ 读写词表层    SchedWrite / SchedRead + WriteRes / ReadAdvance
        │  “一次写/读要占什么、花多久”
        ▼
④ 指令绑顶层    SchedClass ⇐ InstRW / ItinRW / SchedAlias / Variant
        │  “哪条指令用哪组读写”
        ▼
   运行时：MCInstrDesc.SchedClass → MCSchedModel 查表
```

| 层 | 回答的问题 | 典型记录 |
|---|---|---|
| ① 目标/特性 | 这是哪颗 CPU？开哪些特性？ | `ProcessorModel`、`SubtargetFeature` |
| ② 机器模型 | 机器有多宽、有哪些资源、完整度如何？ | `SchedMachineModel`、`ProcResource` |
| ③ 读写词表 | 一次写/读的资源与时间是多少？ | `WriteRes`、`ReadAdvance` |
| ④ 指令绑定 | 哪条指令对应哪组写/读？ | `InstRW`、`SchedAlias`、`SchedWriteVariant` |
| 消费层 | 指令最终查到什么？ | `MCSchedClassDesc`（聚合结果） |

---

## 2. 元素清单

### 2.1 目标/特性层

| 元素 | 作用 | 关键字段 |
|---|---|---|
| `ProcessorModel` | 把 CPU 名字接到（特性集 + 调度模型 + 行程） | `Features`、`TuneFeatures`、`SchedModel`、`Itineraries`、`DefaultMarch` |
| `SubtargetFeature` | 一个可开关的特性 | `Field`/`Value`（写入 Subtarget）、`Implies`、`AssemblerMatcher` |
| `ProcessorAlias` | CPU 别名 | 指向另一个 `ProcessorModel` |

### 2.2 机器模型层

`SchedMachineModel` 是“机器的全局参数”：

| 字段 | 含义 | 谁读 |
|---|---|---|
| `IssueWidth` | 每周期发射槽数 | 两者 |
| `MicroOpBufferSize` | 乱序窗口；0=顺序 | 两者 |
| `NumMicroOps`（默认 1） | 一条指令占几个微操作 | 两者 |
| `LoadLatency` / `HighLatency` | CodeGen 缺省延迟回退 | CodeGen |
| `MispredictPenalty` | 分支代价启发式 | CodeGen |
| `CompleteModel` | 是否要求每条指令都有 sched class | TableGen 校验 |
| `LoopMicroOpBufferSize` | 循环体最适微操作数 | TTI（循环展开/交错） |
| `PostRAScheduler` | 是否在寄存器分配后再排一次 | post-RA 调度器（见 §6.1） |
| `EnableIntervals` | 资源占用是否用区间跟踪 | MachineScheduler（`ResourceSegments`） |
| `UnsupportedFeatures`、`FullInstRWOverlapCheck` | 只影响完整性检查 | 仅校验 |

资源是一棵树：

| 元素 | 语义 |
|---|---|
| `ProcResource<units>` | 一个可计数的执行单元（如 2 个 ALU 端口） |
| `ProcResGroup` | 若干资源的集合（“任一 ALU 都可以”） |
| `Super` 关系 | 子资源属于父资源（`PipeA : Super = ALUGroup`） |
| `BufferSize` | 资源自带的排队深度 |

### 2.3 读写词表层

| 元素 | 语义 |
|---|---|
| `SchedWrite` | “一次写”的类型，本身不带时间 |
| `SchedRead` | “一次读”的类型 |
| `WriteRes<Write, [Res...]>` | 绑定：写 → 资源 + `Latency` + `ReleaseAtCycles` / `AcquireAtCycles` + `NumMicroOps` |
| `ReadAdvance<Read, Cycles, [Writes]>` | 绑定：读相对生产者写，可以少等几个周期（旁路） |

时间语义：

```text
       issue
         │
         ▼
  ───────●───────────────────────────► 时间
         │◄── Acquire ──►│◄── 占用 ──►│
         │              │            │
         │           Release       下一个占用者
         │
         └── Latency：结果对消费者可见的时刻
```

- `Latency`：生产者结果可用的周期数（依赖边的原始权重）。
- `ReleaseAtCycles`：资源在第几个周期释放（默认 1，即占一个周期）。
- `AcquireAtCycles`：资源从第几个周期开始被占用（默认 0）。
- `ReadAdvance`：消费者实际等待 = `Latency − ReadAdvance`。

### 2.4 指令绑顶层

| 元素 | 语义 |
|---|---|
| `SchedClass` | 一组“资源与时间完全相同”的指令共享的描述符 |
| `InstRW<[Writes], (instrs...), [Reads...]>` | 按 opcode 覆盖读写列表 |
| `ItinRW` | 行程（legacy）版本的同上 |
| `SchedAlias<A, B>` | 名字级替换：`A` 在索引时指向 `B` |
| `SchedWriteVariant` / `SchedReadVariant` | 同一写/读按谓词分叉（如“是不是 zero-reg idiom”） |
| `WriteSequence` | 一个写展开成多段（`WriteSequence<[W1, W2], [1, 1]>`） |

---

## 3. 元素之间的关系

### 3.1 主链路：指令 → 类 → 读写 → 资源与时间

```text
  指令 ADD
     │  MCInstrDesc.SchedClass
     ▼
  SchedClass #12
     │  OperandReadWrites / WriteType
     ├── writes: [W_ALU]
     └── reads : [R_ALU, R_ALU]
             │                │
             ▼                ▼
        WriteRes          ReadAdvance
        (PipeA, 1)        (R_ALU, 1)
             │                │
             ▼                ▼
    MCWriteProcResEntry   MCReadAdvanceEntry
    (PipeA, 0, 1)         (1 cycle)
```

调度器拿到的就是两组表项：

- `MCWriteProcResEntry { ProcResourceIdx, Acquire, Release }`：资源占用区间；
- `MCReadAdvanceEntry { UseIdx, WriteResourceID, Cycles }`：依赖边折扣。

### 3.2 读写列表的表达方式：谁覆盖谁

```text
                 默认：指令自己的 Sched<> 列表
                            │
        ┌───────────────────┼────────────────────┐
        ▼                   ▼                    ▼
     InstRW             SchedAlias          SchedWriteVariant
   （按 opcode 覆盖）   （名字替换）        （运行时按谓词选）
        │                   │                    │
        └───────────► 同一个 SchedClass 描述符 ◄─┘
```

- `InstRW` 是“这条指令不用默认列表，改用我给的”；
- `SchedAlias` 是“索引时把 A 当成 B”，**不产生新类**；
- `Variant` 是“同一个写名字，在运行时按谓词选不同列表”，产生变体类。

### 3.3 资源之间的关系

```text
        ALUGroup (Super)
        ┌──────┴──────┐
     PipeA          PipeB        ← 各自 units = 2
        │
     ALUUnit?        ← 子资源（可选）

  bind W -> [PipeA]         直接占一个单元
  bind W -> [ALUGroup]      占组内任一单元
  release/acquire 按“声明的资源列表”逐项配对，再向上展开 super/group
```

展开规则（`SubtargetEmitter::expandProcResources`）：一个单元贡献“自己 + super
链”，组贡献“自己 + 成员全在组内的组”。但 `ReleaseAtCycles` 的长度检查发生在
**展开之前**，对齐的是声明列表——这是建模时最容易踩的坑之一。

### 3.4 特性与处理器

```text
  Feature（-mattr 可开关）
      │ implies
      ▼
  ProcessorModel ── SchedModel ──► SchedMachineModel
      │                              │
      └── Itineraries（legacy）      └── 资源树 / 词表 / 绑定
```

---

## 4. 从源到运行时：编译期做了什么

```text
① 收集 SchedReadWrite 词表
   collectSchedRW 只从四处收集：
     - 指令 Sched<> 列表
     - InstRW.OperandReadWrites
     - ItinRW.OperandReadWrites
     - SchedAlias.AliasRW（目标，不含 MatchRW）
② 合并每个指令的 sched class
     默认列表 → InstRW/ItinRW 覆盖 → 去重、编号
③ 展开资源
     ProcResource → 单元 + super + group
     release/acquire 按声明列表配对，再随资源展开
④ 生成只读表
     MCSchedModel { IssueWidth, Resources[], WriteProcResTable[], ReadAdvanceTable[] }
     MCInstrDesc.SchedClass → MCSchedClassDesc
```

关键约束：**没有进入 ① 的名字，运行时查不到**。`SchedAlias<A, B>` 的 `A`
如果只做别名主语、没有指令引用它，`getSchedWrite(A)` 会命中索引 0 并断言
`invalid SchedWrite`。这不是语法问题，是索引可达性问题
（`CodeGenSchedule.cpp` 的 `collectSchedRW`）。

---

## 5. 一个例子走完

SSDL：

```ssdl
machine model siFive7 {
    issue_width = 4;
    micro_op_buffer_size = 192;

    resource PipeA { units = 2; }

    write W_ALU;
    read  R_ALU;

    bind W_ALU { resources = [PipeA]; latency = 1; release = [1]; }
    advance R_ALU { cycles = 1; }

    map [ADD, SUB] { 0 = W_ALU; 1 = read_default; 2 = read_default; }
}
```

展开成 TableGen：

```tablegen
def SiFive7PipeA : ProcResource<2>;
def W_ALU : SchedWrite;
def R_ALU : SchedRead;
def : WriteRes<W_ALU, [SiFive7PipeA]> { let Latency = 1; let ReleaseAtCycles = [1]; }
def : ReadAdvance<R_ALU, 1>;
def : InstRW<[W_ALU, ReadDefault, ReadDefault], (instrs ADD, SUB)>;
```

运行时，`ADD` 得到：

```text
SchedClass #k:
  NumMicroOps = 1
  WriteProcRes = [(PipeA, acquire=0, release=1)]
  OperandLatency: op1 = 1 − 0 = 1, op2 = 1 − 0 = 1
```

调度器于是知道：`ADD` 占 1 个发射槽、1 个 ALU 单元 1 个周期、结果 1 周期后可用。

---

## 6. 建模时必须知道的规则

1. **可达性决定一切**：没被 `Sched<>`/`InstRW`/`ItinRW`/`SchedAlias` 目标索引的
   读写，运行时不存在。
2. **`release`/`acquire` 与声明资源逐项配对**，空列表合法（默认每资源 1 / 0），
   非空必须等长；且只对可达 write 检查。
3. **`Latency` 是依赖边权重，不是资源占用**；资源占用由 `release/acquire` 决定，
   两者可以差很多（如除法）。
4. **`NumMicroOps` 与 `IssueWidth` 决定吞吐上限**；`MicroOpBufferSize` 决定乱序窗口。
5. **`CompleteModel` 只影响校验**：部分模型在真实 target 上会停在
   `does not define resources`，不是模型错误。
6. **`Itineraries` 与 sched model 是两套**：CodeGen 优先看行程，llvm-mca 只看
   sched model；只有行程的 CPU 会被 mca 拒绝。
7. **`SchedWriteVariant` 的谓词在运行时解析**：谓词必须能被 `MCInst` 判断；
   `PredicateProlog` 的变量在变体解析器里不可见。

### 6.1 “旧调度器”指什么：`PostRAScheduler` 与三条调度器血脉

LLVM 里同时存在三代调度器，读模型字段时最容易混淆：

| 调度器 | 阶段 | 实现 | 现代地位 |
|---|---|---|---|
| SelectionDAG 调度器 | 指令选择内、按基本块 | `ScheduleDAGSDNodes` / `ScheduleDAGRRList` | 仍用于 SelectionDAG 路径 |
| MachineScheduler | 寄存器分配前（pre-RA） | `lib/CodeGen/MachineScheduler.cpp` | 现代默认 |
| post-RA 调度器 | 寄存器分配后 | 两个实现：`PostRASchedulerList`（旧列表调度器）与 `PostMachineScheduler`（新版 MachineScheduler） | 二选一 |

`SchedMachineModel.PostRAScheduler`（TableGen 里的
`bit PostRAScheduler = false;`）的语义是：

> **寄存器分配之后再排一次**，具体用哪个实现由 pass 配置决定。

链路（均可核对源码）：

```text
TargetSubtargetInfo::enablePostRAScheduler()
    return getSchedModel().PostRAScheduler;          // TargetSubtargetInfo.cpp:64
TargetSubtargetInfo::enablePostRAMachineScheduler()
    return enableMachineScheduler() && enablePostRAScheduler();

TargetPassConfig::addMachinePasses():
    if (Opts.misched_postra) addPass(&PostMachineSchedulerID);   // 新版
    else                     addPass(&PostRASchedulerID);        // 旧版
                                                   // TargetPassConfig.cpp:1062-1070
```

`PostRASchedulerID` 对应 `PostRASchedulerList.cpp` 的
`PostRASchedulerLegacy`（pass 名 `post-RA-sched`），即通常说的“旧调度器”：

- 它是**自顶向下的列表调度器**：可用节点进优先队列，逐条检查结构冒险/资源约束/
  输入是否就绪后再发射（`PostRASchedulerList.cpp:8-17`）；
- 寄存器已经分配完，所以它**不做寄存器压力**，重点是：
  - `ScheduleHazardRecognizer`：流水线冒险与 NOP 插入；
  - `AntiDepBreaker`：打断反依赖（`-break-anti-dependencies`）；
  - `LatencyPriorityQueue`：按关键路径优先级选节点。
- 启用条件除模型位外还有优化级别门槛，并可用隐藏选项
  `-post-RA-scheduler` 覆盖（`PostRASchedulerList.cpp:270-277`）；
- 有些 target 自己安排后置调度（`TargetMachine::targetSchedulesPostRAScheduling()`）。

`PostMachineScheduler`（`-misched-postra`）是同一阶段的新版实现：复用
MachineScheduler 的 DAG 变异/资源模型；旧版仍在，是因为部分 target 依赖
hazard recognizer 与旧的反依赖打断策略。

**读模型时怎么记**：`PostRAScheduler` 不是“资源字段”，而是**流水线开关**；
它不改变任何指令的 latency/资源，只决定 CodeGen 是否在 RA 之后多跑一个 pass。
llvm-mca 完全不看这个字段。

`EnableIntervals` 则是 MachineScheduler 内部的资源表示：
为 true 时用 `ResourceSegments` 跟踪占用区间，而不是把资源占用当成点事件
（`MachineScheduler.cpp:2907/3325/3505`；RISC-V SiFive7 打开它，
`RISCVSchedSiFive7.td:1614`）。它只影响 CodeGen 的调度决策，不影响 mca。

`LoopMicroOpBufferSize` 也不是调度器字段：它由 `BasicTTIImpl` 读取，作为循环
展开时“循环体微操作数”的目标（`BasicTTIImpl.h:755-756`），属于 TTI/展开启发式。

### 6.2 默认管线里到底谁在调度

“SelectionDAG 内调度器只做线性化，真正的调度在 pre-RA / post-RA”这个说法
方向正确，但要加三个限定：

1. **默认路径确实近似线性化**。`createDefaultScheduler` 的选择顺序是：
   target 自定义 `getDAGScheduler` → O0 / `enableMachineScheduler() &&
   enableMachineSchedDefaultSched()` / `Sched::Source` →
   `createSourceListDAGScheduler`（`SelectionDAGISel.cpp:299-311`）。
   source 调度器的描述就是 “similar to list-burr but schedules in source
   order when possible”（`ScheduleDAGRRList.cpp:73-76`）：它仍是带延迟依赖边的
   拓扑排序，只是尽量保持源序，把 ILP/压力权衡留给后面的 MachineScheduler。
2. **不是所有 target 都这样**。`getDAGScheduler` 可以整条覆盖；
   `enableMachineScheduler()` 在基类返回 false（`TargetSubtargetInfo.cpp:51`），
   没启用的 target 仍由 DAG 阶段的 BURR/ILP/hybrid 调度器做真正的 pre-RA 调度；
   `-pre-RA-sched=list-burr|list-ilp|...` 也可强制。
3. **post-RA 是可选的，而且分工不同**。它由 `PostRAScheduler` / `-misched-postra`
   开关；主要面向 in-order/hazard 机器（避免 stall、插 NOP、打断反依赖），
   乱序核通常不开。pre-RA MachineScheduler 也不是“只管寄存器压力”：
   它在压力约束下用 sched model 的资源、latency、issue width 最大化 ILP。



4. **pre-RA 也是 CPU 感知的，只是感知粒度是“模型”，不是“模拟器”**。
   `TargetSchedModel` 直接向 `MCSchedModel` 要 `NumMicroOps`、`IssueWidth`、
   `InstrLatency`、`OperandLatency`（含 `ReadAdvance` 折扣）和每条指令的
   `WriteProcRes` 区间（`TargetSchedule.cpp:91/169/237`）。
   `SchedBoundary::checkHazard` 每拍检查：uop 数是否超过发射宽度、
   `mustBeginGroup`/`mustEndGroup`、以及资源的 `acquire/release` 区间是否冲突
   （`MachineScheduler.cpp:2995-3055`）。如果 target 提供 itineraries，
   `CreateTargetMIHazardRecognizer` 还会启用 `ScoreboardHazardRecognizer`，
   用 `InstrItineraryData` 的 stage 做流水线阶段级冒险检查；没有 itineraries
   时这个识别器自动禁用（`TargetInstrInfo.cpp:1728`、
   `ScoreboardHazardRecognizer.cpp:30-70`）。
   但它**不是逐拍流水线模拟**：没有寄存器重命名/ROB 建模，`MicroOpBufferSize`
   只是启发式阈值；完整的逐拍模拟是 llvm-mca 的职责。

一句话：**默认现代 target 上，DAG 阶段选一个合法且尽量保序的线性序，真正的
指令级重排发生在 pre-RA MachineScheduler；post-RA 只在需要的 target 上再补一次
面向冒险与 ILP 的局部调度。**

---

## 7. 与 SSDL 的对应

| SSDL 构造 | 调度模型元素 |
|---|---|
| `machine model X { issue_width = ...; }` | `SchedMachineModel` |
| `machine cpu X { features = [...]; }` | `ProcessorModel` |
| `resource` / `res_group` / `super` | `ProcResource` / `ProcResGroup` |
| `write` / `read` | `SchedWrite` / `SchedRead` |
| `bind { resources; latency; release; acquire; }` | `WriteRes` |
| `advance { cycles; writes; }` | `ReadAdvance` |
| `map [...] { ... }` | `InstRW` |
| `sched_alias A -> B;` | `SchedAlias` |
| `variant W { case P: [...]; }` | `SchedWriteVariant` |
| `itineraries { ... }` | `ProcessorItineraries` |
| 维度/族展开 | 无直接对应（SSDL 的压缩手段，展开后消失） |

**一句话总结**：调度模型 = 一棵资源树 + 一张“写/读 → 资源与时间”的绑定表 +
一张“指令 → 写/读列表”的索引表；调度器只做查表和区间相加，不理解指令在算什么。
