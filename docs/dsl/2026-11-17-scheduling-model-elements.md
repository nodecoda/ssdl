# 调度模型的四个要素：图与约束

> 定位：这份文档解释 SSDL 到底在给 LLVM 描述什么。它不介绍 SSDL 语法，
> 只画清楚 `MCSchedModel` 里四类字段的关系——**opcode → 资源 + 时间**。
> 所有数字取自 `RISCVSchedSiFive7.td`，所有语义取自 LLVM 源码，见 §6 索引。

## 0. 一句话

调度模型不是指令语义，是一张 **opcode → (资源向量, 时间向量)** 的映射表：

| 它回答的问题 | 字段 |
|---|---|
| 一条指令占几个发射槽、占哪些执行单元？ | `IssueWidth`、`NumMicroOps`、`ProcResource<units>` |
| 结果多久可用？ | `WriteRes.Latency` |
| 执行单元什么时候被占用、什么时候释放？ | `AcquireAtCycles` / `ReleaseAtCycles` |
| 读操作数要等多久？能否旁路？ | `ReadAdvance<read, cycles, [writes]>` |

两个消费者各取一半：MachineScheduler 主要用**依赖边**（Latency、ReadAdvance）
和**发射预算**（IssueWidth、NumMicroOps）；llvm-mca 完整消费**资源轴**
（units、ReleaseAtCycles）并逐拍模拟。

---

## 1. 静态绑定：一个 opcode 被拆成「1 个写 + n 个读」

```
                     ┌──────────── 机器预算 (SchedMachineModel) ────────────┐
                     │ IssueWidth = 2        每拍最多 2 个 uop 进发射        │
                     │ MicroOpBufferSize = 0 in-order，不投机乱序           │
                     │ LoadLatency / MispredictPenalty ...                 │
                     └───────────────────────┬─────────────────────────────┘
                                             │ 给块内所有 sched class 当上限
   MCInst "ADD" ──InstRW / ItinRW──►  (SchedWrite W_ALU, [SchedRead R1, R2])
                                             │       指令不再是"一条"，而是"一写多读"
                    ┌────────────────────────┴─────────────────────────┐
                    ▼ 写侧                                            ▼ 读侧
            WriteRes<W_ALU,...>                            ReadAdvance<R1, 2, [W_ALU]>
            ├ ProcResources   = [PipeAB]  ← 占谁             └ 消费者可提前 2 拍读
            ├ Latency         = 3         ← 多久可用
            ├ NumMicroOps     = 1         ← 吃几个发射槽
            ├ ReleaseAtCycles = [..]      ← 何时还资源（缺省每资源 1 拍）
            └ AcquireAtCycles = [..]      ← 何时拿资源
                    │
                    ▼
            ProcResource<units>  ← 同名资源有几个实例 = 同拍能并行几路
```

要点：

- `ProcResources` 是**资源集合**，不是时长；时长由 `ReleaseAtCycles` 缺省值决定。
- `SchedWrite`/`SchedRead` 的分解是调度模型的地基：调度器从不知道 `ADD`
  是什么，它只知道“这条例程有一次 `W_ALU` 写、两次 `R1/R2` 读”。

---

## 2. 时间轴：四个要素在同一条指令上如何交汇

SiFive7：`IssueWidth=2`，`PipeAB` 是两个 ALU 的 `ProcResGroup`，
`WriteIALU` 延迟 3，ALU→GPR 旁路 `ReadAdvance cycles=2`。

```
cycle              0        1        2        3
                 ───┬────────┬────────┬────────┬───
dispatch      ADD1 │ ADD2   │        │        │      IssueWidth=2：同拍两个槽
PipeAB 占用        │ [A1]   │ [A2]   │        │      ReleaseAtCycles 缺省 = 1 拍
                   │        │        │        │      （与 Latency 无关！）
result r3 可用     │        │        │        ▲      t = issue + Latency(3)
consumer 可发射    │        ▲        │        │      t = issue + (3 − 2)
                   └ 依赖边 = Latency − ReadAdvance = 1 拍
```

两个推论：

1. `IssueWidth` 是**每拍发射预算**，`NumMicroOps` 是单条指令**从这个预算里
   吃掉几格**。`DispatchStage.cpp:80-88` 就是 `NumMicroOps` 对 `DispatchWidth`
   做减法、不够就 carry over 到下一拍。
2. 资源周期由 `ProcResources × ReleaseAtCycles` 决定；`NumMicroOps` 不乘进去。
   `WriteSFB` 的 `NumMicroOps=2` + `[PipeA, PipeB]` 建模的是“两个发射槽，
   两条管线各占一拍”，不是“两条管线各占两拍”。

---

## 3. Latency ≠ 占用：IDiv 是最好的反例

```
     WriteRes:  ProcResources = [PipeB, IDiv]
                Latency         = 66
                ReleaseAtCycles = [1,    65]
                                   │     │
                        位置一一配对（SubtargetEmitter.cpp:1228-1254）
                                   │     │
        PipeB 只占 1 拍 ────────────┘     └──── IDiv 占 65 拍
        => div 在跑时，普通 IALU 仍可从 PipeB 发射
        => 第二次 div 必须等到第 65 拍，吞吐 1/65
```

```
                 结果可用
   issue ────────────●──────────────► t=66   （依赖边，消费者等这个）
   资源空出 ──●        ●────────────► t=1,65 （吞吐边，下一条指令等这个）
```

这也是 `Latency=66` 与 `ReleaseAtCycles=[1,65]` 数值不同的原因：前者含写回级，
后者只描述资源被占用的区间。

---

## 4. 两条轴，两个消费者，一个数

```
        依赖轴（边权）                          资源轴（每拍压力）
  opcode ─► WriteRes.Latency                    IssueWidth ─────────┐
            ReadAdvance(减) ─► edge = L − RA                       │
                   │                          ProcResource<units> ─┤
                   ▼                          ReleaseAtCycles ─────┘
          MachineScheduler / DAG                          │
          （ScheduleDAGInstrs.cpp:294）                   ▼
                                                  llvm-mca 的每拍压力表
```

llvm-mca 把两条轴压成一个数（`lib/MCA/Support.cpp:85-116`）：

```
RThroughput = max( NumMicroOps / DispatchWidth,
                   Σ ReleaseAtCycles(resource) / NumResources )
```

所以读一张调度表，只要问两个问题：**依赖边多长？每拍资源够不够？**
前者决定关键路径，后者决定吞吐下界；两个都不是指令语义。

---

## 5. 为什么它看起来像动态规划

确实像，但只像一半。

**像的那一半**：在 DAG 上算“最早可用时间”就是一个拓扑序上的 DP：

```
ready(v) = max over (u → v) of  ready(u) + w(u, v)
```

这正是 `MachineTraceMetrics` 计算 depth/height 的做法：每个节点只依赖前驱，
拓扑序一遍扫完，`O(V+E)`。`ReadAdvance` 只是把边权 `w = Latency − RA`
改小；`ReleaseAtCycles` 只是在边上再加一项资源可用时间。max-plus 形式下，
“关键路径”和“吞吐下界”都是 max 而不是 sum。

**不像的那一半**：完整的调度问题没有可压缩的状态。

```
start(v) = max( max_u (start(u) + w(u,v)),  free_time(resource(v), dur(v)) )
```

一旦 `free_time` 是**全局资源日历**（哪条管线哪一拍空着），子问题之间就
通过日历耦合，最优子结构消失。精确解的状态是「已发射集合 × 资源日历」，
指数级；资源受限项目调度（RCPSP）与 job-shop 都是 NP-hard。
LLVM 的选择是：

- **MachineScheduler**：带优先级的贪心 list scheduling，优先级来自上面那个
  DP 算出的高度/深度；不搜索全局最优。
- **llvm-mca**：离散事件模拟，逐拍推进 `DispatchStage` / `ExecuteStage` /
  `ResourceManager`，是解释器，不是 DP。
- DP 只出现在**子问题**里：依赖链、高度/深度、每拍的资源压力前缀和。

一句话：**DP 算的是“如果顺序已定，最早什么时候”；调度器要决定的是“顺序本身”，
那一步是贪心 + 界，不是 DP。**

---

## 6. 证据索引

| 结论 | 位置 |
|---|---|
| `ReleaseAtCycles` 缺省每资源 1 拍 | `utils/TableGen/SubtargetEmitter.cpp:1228-1254` |
| `ReleaseAtCycles` 与 `ProcResources` 按位置配对 | 同上，`SubtargetEmitter.cpp:1228-1237` |
| 资源占用语义（含 super/group 叠加） | `lib/MCA/InstrBuilder.cpp:46-200` |
| 每拍资源压力、`RThroughput` 公式 | `lib/MCA/Support.cpp:85-116` |
| `NumMicroOps` 对 `DispatchWidth` 做减法 | `lib/MCA/Stages/DispatchStage.cpp:80-88` |
| 依赖边 `Latency − ReadAdvance` | `lib/CodeGen/TargetSchedule.cpp:169-230` |
| DAG 边设置 | `lib/CodeGen/ScheduleDAGInstrs.cpp:294` |
| SiFive7 数字 | `lib/Target/RISCV/RISCVSchedSiFive7.td:1605`（模型）、`:335`（IALU）、`:250`（旁路）、`:347-362`（div） |
