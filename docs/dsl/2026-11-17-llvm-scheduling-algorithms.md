# LLVM 指令调度算法

> 配套文档：`2026-11-17-scheduling-model-elements.md` 讲模型字段（问题是什么），
> 这篇讲消费这些字段的算法（谁在算、怎么算）。基线 `700e551714cc`。
> 文中 `path:line` 均可在该 checkout 上直接核对。

## 0. 谱系：五个调度器 + 一个模拟器

```
        SelectionDAG 阶段                    Machine IR 阶段
  ┌───────────────────────┐        ┌──────────────────────────────────┐
  │ ScheduleDAGRRList     │        │ MachineScheduler（pre-RA）        │
  │  - list-burr          │        │   ScheduleDAGMILive              │
  │  - list-hybrid        │        │   + GenericScheduler             │
  │  - list-ilp           │        │     （bottom-up / top-down /      │
  │  - source / fast      │        │       bidirectional）            │
  │  - vliw / linearize   │        │ MachinePipeliner（SMS，循环）     │
  └───────────┬───────────┘        │ WindowScheduler（折叠+列表）      │
              │                    └───────────────┬──────────────────┘
              ▼                                    ▼
        MachineInstr  ──────► 寄存器分配 ──►  PostMachineScheduler（新）
                                                  或 PostRAScheduler（legacy）
                                              （top-down + anti-dep breaker）

  llvm-mca：离线逐拍模拟，消费同一份模型，但不参与 codegen 的调度决策。
```

| pass | 位置 | 算法族 | 主要输入 |
|---|---|---|---|
| `ScheduleDAGRRList` | SelectionDAG → MI | 列表调度（含回溯） | SDNode、`TargetLowering` 延迟 |
| `SSAMachineScheduler` | PHI 消除前 | 同 GenericScheduler | SchedModel + 寄存器压力 |
| `MachineScheduler` | PHI 消除/合并后，RA 前 | 双向列表调度 | SchedModel + 压力 + 关键路径 |
| `MachinePipeliner` | RA 前，循环 | Swing Modulo Scheduling | SchedModel + DFA |
| `WindowScheduler` | RA 前，循环 | 折叠 + 列表调度 | SchedModel |
| `PostMachineScheduler` | RA 后 | GenericScheduler（post-RA 模式） | SchedModel |
| `PostRAScheduler` | RA 后（默认） | top-down 列表 + 反依赖改名 | Itinerary / SchedModel |
| `llvm-mca` | 离线 | 离散事件模拟 | SchedModel |

RISC-V 打开 pre-RA 调度（`RISCVSubtarget.h:168`），post-RA 由 tune 特性
`use-postra-scheduler` 控制（`RISCVFeatures.td:2079` → `enablePostRAScheduler`）。

---

## 1. 调度区域：先把基本块切段

`MachineSchedulerBase::scheduleRegions` 逐块处理，块内再切成
`[RegionBegin, RegionEnd)`（`MachineScheduler.cpp:1016`）。边界由
`isSchedBoundary` 决定（`:964-969`）：

```
call || TII->isSchedulingBoundary(MI) || MI->isFakeUse() || MI->isPHI()
```

区域从下往上切、也从下往上调度（DAG 构建同样自底向上）。空的或单指令
区域直接跳过。区域之间不重排，所以跨基本块的调度不在 MachineScheduler 里，
而在 MachinePipeliner / WindowScheduler / 块布局里。

---

## 2. DAG 构建：所有调度器的公共地基

### 2.1 SUnit

`initSUnits`（`ScheduleDAGInstrs.cpp:577-612`）为每条真实指令建一个 `SUnit`：

```
SU->Latency = SchedModel.computeInstrLatency(MI)
buffer=0 的资源 → SU->hasReservedResource = true   // 阻塞整条流水线
buffer=1 的资源 → SU->isUnbuffered = true           // 只阻塞同类资源
```

这两行就是 SSDL `resource ... : buffer = 0|1` 在算法里的落点。

### 2.2 边的种类

自底向上扫描区域，先处理 def、再处理 use（`:988-1040`），产生：

| 边 | 含义 | 延迟 |
|---|---|---|
| `Data` | vreg/physreg 真依赖 | `computeOperandLatency` |
| `Output` | WAW | `computeOutputLatency`（目标/模型决定） |
| `Anti` | WAR | 0（允许同拍发射） |
| `Order` | 内存序 | 1 或 0（按证据） |
| `Barrier` | 全局内存对象/不可分析访存 | 强制串行 |
| `Weak` | 聚类等软约束 | 不进关键路径 |
| `Artificial` | `EntrySU`/`ExitSU` 连接 | 0 |

延迟计算是唯一把模型接进 DAG 的地方（`TargetSchedule.cpp:169-230`）：

```
边权 = WriteRes.Latency − ReadAdvance(use, [producer write])
```

`adjustSchedDependency` 再给目标一次改写机会（`ScheduleDAGInstrs.cpp:284`）。

### 2.3 内存依赖

按 `getUnderlyingObjectsForInstr` 得到对象集合，用 `MemopFrontier` 记录
每个对象上"最近一次 store / 进行中的 loads"（`:1100-1180`）：

- 可分析访存：只和同一对象的前沿连边；
- 不可分析访存：和所有前沿连边；
- 超过 `HugeRegion` / `MaxMemopsInFlight`：提升为 barrier chain，退化为全序。

### 2.4 高度与深度

`SUnit::ComputeDepth/ComputeHeight`（`ScheduleDAG.cpp:275-320`）就是上一篇
说的 DP：

```
Depth(v)  = max over preds (Depth(u) + w(u,v))
Height(v) = max over succs (Height(u) + w(v,u))
```

惰性计算、带 dirty 标记；`biasCriticalPath` 把关键路径前驱排到前面，
让 DFS 聚类顺着关键路径走。

---

## 3. Pre-RA MachineScheduler 主循环

```
scheduleRegions
  └─ ScheduleDAGMILive::schedule                (MachineScheduler.cpp:1996)
       buildDAGWithRegPressure()                // DAG + RPTracker
       postProcessDAG()                         // 目标 mutation（宏融合等）
       findRootsAndBiasEdges()
       SchedImpl->initialize(this)              // 建 Rem / Top / Bot / HazardRec
       initQueues(TopRoots, BotRoots)
       while (SUnit *SU = SchedImpl->pickNode(IsTopNode)) {
         scheduleMI(SU, IsTopNode);
         SchedImpl->schedNode(SU, IsTopNode);
         updateQueues(SU, IsTopNode);
       }
```

两个方向各有一个 `SchedBoundary`（`Top` / `Bot`），各带：

```
Available  已就绪、可发射的优先队列
Pending    已释放但还没到 ReadyCycle 的节点（0/1 模型都会用到）
CurrCycle  当前边界推进到的周期
CurrMOps   本周期已用掉的发射槽
ReservedCycles[unit]        每个资源实例被占到的周期
ZoneCritResIdx              当前临界资源（0 = 发射口受限）
ExpectedLatency / DependentLatency  已调度部分的关键路径
```

`pickNode` 的三条路径（`:4473`）：

1. 只有一边有候选 → 直接取（`pickOnlyChoice`，`:3458`）；
2. `OnlyBottomUp` / `OnlyTopDown` → 单边界；
3. 否则 `pickNodeBidirectional`：两边各取最优，再用
   `tryCandidate(Cand, TopCand, nullptr)` 比较（跨边界时跳过纯 tie-breaker）。

### 3.1 边界推进 = 时间 + 资源

`SchedBoundary::bumpNode`（`:3251`）是"发射一条指令"的全部记账：

```
1. checkHazard(SU)                    // 发射宽度 / 组约束 / reserved 资源
2. 按 MicroOpBufferSize 决定是否等 ReadyCycle
     0   → 断言 ReadyCycle <= CurrCycle（就绪节点才可见）
     1   → 有序核：等到 ReadyCycle
     >1  → 只对 isUnbuffered 的资源等
3. countResource(...) 更新 ReservedCycles / ExecutedResCounts
4. 更新 ExpectedLatency / DependentLatency
5. NextCycle > CurrCycle → bumpCycle（清空 CurrMOps，推进周期）
6. BeginGroup/EndGroup → 强制换拍
7. CurrMOps >= IssueWidth → 换拍
```

`checkHazard`（`:3011`）检查四件事：目标 hazard recognizer、
`CurrMOps + uops > IssueWidth`、组约束、以及 `hasReservedResource` 的资源
下一次可用周期是否晚于当前周期。**模型里的 IssueWidth / NumMicroOps /
buffer / ReleaseAtCycles 就是在这里变成拍数的。**

### 3.2 候选比较 = 严格字典序

`GenericScheduler::tryCandidate`（`:4258`）按顺序比较两个候选，第一个
能分出胜负的规则决定结果：

| # | 规则 | 条件 |
|---|---|---|
| 1 | 物理寄存器 def/use 就近 | `tryBiasPhysRegs` |
| 2 | `RPDelta.Excess`（超出硬上限的压力） | 压力跟踪开启 |
| 3 | `RPDelta.CriticalMax`（超出临界上限） | 压力跟踪开启 |
| 4 | acyclic latency limited 且本拍尚未发射：latency | 同边界 |
| 5 | unbuffered 资源的 stall 拍数 | 同边界 |
| 6 | 同簇优先 | 始终 |
| 7 | weak 边少者优先 | 同边界 |
| 8 | `RPDelta.CurrentMax`（当前最大值） | 压力跟踪开启 |
| 9 | `CritResources` 少者优先 | 同边界 |
| 10 | `DemandedResources` 多者优先 | 同边界 |
| 11 | latency（非 acyclic-limited） | 同边界 + policy |
| 12 | 原始顺序（top 取小 NodeNum，bottom 取大） | 同边界 |

`tryLess`/`tryGreater`（`:3758`）的返回值语义是"是否已分出胜负"，
`TryCand.Reason != NoCand` 才表示新候选胜出——所以这是一次线性扫描的
字典序比较，不是打分求和。

Policy 不是固定的：`setPolicy`（`:3588`）根据当前 zone 是否资源受限、
对侧 zone 的剩余资源是否更紧、以及"当前周期 + 剩余延迟 > 临界路径"
（`shouldReduceLatency`）来设置 `ReduceLatency / ReduceResIdx /
DemandResIdx`，相当于在调度过程中切换目标函数。

### 3.3 剩余量：让贪心有全局感

`SchedRemainder::init`（`:2835`）在区域开始时一次算好：

```
RemIssueCount        整个区域还要发的 uop 数
RemainingCounts[P]   每个资源还要消耗的周期数
CriticalPath         区域的关键路径（高度）
CyclicCritPath       循环背边的关键路径
IsAcyclicLatencyLimited  非循环路径是否已经超过缓冲能吸收的量
```

`setPolicy` / `tryLess(CritResources, ...)` 用这些量判断"现在该抢延迟
还是该省资源"，这就是贪心算法里唯一的"全局信息"。

### 3.4 复杂度

每个 `pickNode` 扫描两个 ready 队列（`O(|Q|)`），候选的资源/压力增量
是增量维护的；一个区域最坏 `O(n²)`，但区域通常只有几十条指令，
且 `pickOnlyChoice` 在收敛时频繁命中。不是多项式最优解，也没有回溯。

---

## 4. 寄存器压力：调度器的第二目标

`RegPressureTracker` 维护活跃 vreg 的 pressure set 计数，
`RegPressureDelta`（`RegisterPressure.h:265-290`）给出三个量：

| 字段 | 含义 |
|---|---|
| `Excess` | 超出目标硬上限的最大差值（负数 = 在降压力） |
| `CriticalMax` | 超出"临界上限"的最大增量 |
| `CurrentMax` | 超出当前最大值的最大增量 |

`tryPressure`（`MachineScheduler.cpp:4098`）：

1. 一个降、一个升 → 降者胜；
2. 跨边界不比较幅度（不可比）；
3. 同一 pressure set → 比增量大小；
4. 不同 set → 比 `getRegPressureSetScore`，降压力时反过来排。

只有区域足够大才开压力跟踪（`initPolicy`：指令数 > 最大整型寄存器类
可分配数的一半），小区域直接省掉这笔开销。

---

## 5. Post-RA 调度

### 5.1 PostMachineScheduler（新）

复用同一套 `GenericScheduler`，但输入是 `ScheduleDAGMI`（没有 vreg
liveness / 压力跟踪），`scheduleRegions(..., FixKillFlags=true)`。
post-RA 的候选比较更短（`PostGenericScheduler::tryCandidate`，`:4737`）：

```
stall → cluster → CritResources → DemandedResources → latency → 原顺序
```

### 5.2 PostRAScheduler（legacy，默认）

`ListScheduleTopDown`（`PostRASchedulerList.cpp:544`）是教科书式 top-down：

```
Available = 所有无前驱节点
while (Available 或 Pending 非空):
  Pending 中 depth <= CurCycle 的节点移入 Available
  从 Available 取优先级最高、且 HazardRec 允许的节点
  必要时 emitNoop / 空转一拍
  发射后释放后继
```

它不认识 SchedModel 的 `IssueWidth`，发射宽度由目标的
`ScheduleHazardRecognizer`（`atIssueLimit()`）回答。

反依赖改名（`AntiDepBreaker`，`include/llvm/CodeGen/AntiDepBreaker.h`）：
RA 之后 WAR 变成真的寄存器约束，调度器通过把目标寄存器换成同类的
另一个物理寄存器来打断 WAR；`aggressive` 与 `critical` 两种策略由目标选。

---

## 6. 循环：软件流水

### 6.1 MachinePipeliner（SMS）

`SwingSchedulerDAG::schedule`（`MachinePipeliner.cpp:877`）：

```
buildSchedGraph + 循环携带依赖（PHI 的 order 边）
findCircuits(NodeSets)                  // 找环
ResMII = calculateResMII()              // 资源下界
RecMII = max over circuits ceil(delay/distance)   // 实现里 Distance 固定为 1
MII = max(ResMII, RecMII)
for (II = MII; II <= MAX_II; ++II):
   按 height 排序逐个插入，[EarlyStart, EarlyStart+II-1] 内找合法槽
   资源用 modulo reservation table / DFAPacketizer 检查
   失败就 II+1
finalizeSchedule → kernel + prologue + epilogue
```

`ResMII` 有两种算法（`ResourceManager::calculateResMII`，`:4213`）：
有 DFA 时用 `DFAPacketizer` 逐个打包，包数就是 ResMII；否则
`max(ceil(Σuops/IssueWidth), max_P ceil(ΣReleaseAtCycles(P)/units(P)))`。
后者正是上一篇的吞吐公式。

### 6.2 WindowScheduler

把基本块在某个位置"折叠"（复制三份成 `TripleMBB`），在折叠后的指令序列上
做列表调度，枚举折叠位置取最优（`WindowScheduler.cpp:1-35`）。是 SMS 的
轻量替代，不要求循环是规则的。

---

## 7. SelectionDAG 列表调度（更早的一层）

`ScheduleDAGRRList` 在 SelectionDAG 选择指令后、生成 MI 前调度，
支持 top-down 与 bottom-up。核心优先级是 **Sethi-Ullman 数**
（`getNodePriority`，`:1984`）：

```
TokenFactor / CopyToReg / EXTRACT_SUBREG ...  → 0（贴近使用者）
无后继有前驱（store 等）                      → 0xffff（贴近生产者）
无前驱有后继（常量等）                        → 0（贴近使用者）
其余                                          → Sethi-Ullman 数
```

`BURRSort`（`:2483`）在优先级相同时依次比较：physreg def 亲和、调用顺序、
`closestSucc`、最大 scratch 寄存器数。`hybrid_ls_rr_sort`（`:2628`）先看
是否会造成高压力：高压力走 BURRSort，否则先比延迟。

与 MachineScheduler 最大的不同：它**会回溯**（`PickNodeToScheduleBottomUp`，
`:1416`）。候选都因活跃物理寄存器冲突而不能发时，调度器会尝试
"反调度已发节点 / 复制节点 / 插入跨类 copy"来腾出寄存器
（`NumBacktracks`、`NumUnfolds`、`NumDups` 三个统计量）。

---

## 8. llvm-mca：同一模型的另一个消费者

llvm-mca 不重排指令，只按给定顺序逐拍模拟
（`DispatchStage` → `Scheduler` → `ExecuteStage` → `RetireControlUnit`）：

```
每拍：按 IssueWidth/NumMicroOps 派发
      资源按 ReleaseAtCycles 占用、到期释放
      依赖按 Latency/ReadAdvance 判定何时可读
      输出每拍压力与 RThroughput
```

它是"调度结果好不好"的裁判，不是调度器。

---

## 9. 模型字段 → 算法位置

| SSDL / TD 字段 | 算法里的落点 |
|---|---|
| `IssueWidth` | `SchedBoundary::checkHazard` 的发射宽度检查 |
| `NumMicroOps` | 同上；`DispatchStage` 的派发槽 |
| `resource : units` | `ReservedCycles` 的实例数、`countResource` |
| `resource : buffer = 0` | `hasReservedResource` → 阻塞整条流水线 |
| `resource : buffer = 1` | `isUnbuffered` → `getLatencyStallCycles` 启发式 |
| `bind ... latency` | DAG 边权、`ComputeHeight/Depth`、`tryLatency` |
| `bind ... release/acquire` | `countResource` 的资源日历（acquire 仅 CodeGen 读） |
| `advance ... cycles` | `computeOperandLatency` 的减法项 |
| `begin_grp/end_grp/single` | `bumpNode` 的强制换拍 |
| `micro_op_buffer_size` | `bumpNode` 的 stall 策略（0 / 1 / >1） |
| `post_ra_scheduler` | `SchedMachineModel.PostRAScheduler` → `enablePostRAScheduler()`（RISC-V 被 tune 特性覆盖） |
| `itineraries` | legacy `HazardRecognizer` / itinerary 路径 |
| `reg_file` / `retire_unit` / `load_queue` / `store_queue` | 只有 llvm-mca |

---

## 10. 为什么是贪心，不是全局 DP

- **MachineScheduler**：每步从 ready 队列取局部最优，不回溯；全局信息只有
  `SchedRemainder` 的关键路径与剩余资源量。被建模成"字典序 + 增量"。
- **ScheduleDAGRRList**：唯一有回溯的一层，但回溯范围是"为解寄存器冲突
  而反调度最近的节点"，不是对顺序做状态空间搜索。
- **SMS**：II 从小到大试，每个 II 内仍是贪心插入；失败就加 II，属于
  "枚举参数 + 贪心"。
- **MCA**：模拟，不是优化。

精确解需要同时决定顺序和资源日历，状态爆炸；这也解释了为什么所有
调度器的优先级最终都落在 depth/height、压力、资源三个可增量维护的量上。

---

## 11. 证据索引

| 结论 | 位置 |
|---|---|
| 区域边界 | `lib/CodeGen/MachineScheduler.cpp:964-1013` |
| 区域调度驱动 | 同上 `:1016-1110` |
| pre-RA 主循环 | 同上 `:1996-2050`、`:1462-1497` |
| 双向选取 | 同上 `:4395-4470` |
| 候选字典序 | 同上 `:4258-4370` |
| 压力比较 | 同上 `:4098-4134` |
| policy 切换 | 同上 `:3588-3650` |
| hazard / 资源记账 | 同上 `:3011-3110`、`:3251-3420` |
| 剩余量 | 同上 `:2835-2856` |
| SUnit 初始化（buffer 语义） | `lib/CodeGen/ScheduleDAGInstrs.cpp:577-612` |
| DAG 构建 / 内存前沿 | 同上 `:1182-1181`、`:880-1180` |
| 延迟计算 | `lib/CodeGen/TargetSchedule.cpp:169-230` |
| depth/height | `lib/CodeGen/ScheduleDAG.cpp:275-320` |
| post-RA top-down | `lib/CodeGen/PostRASchedulerList.cpp:544-705` |
| 反依赖改名 | `include/llvm/CodeGen/AntiDepBreaker.h:28-45` |
| SMS | `lib/CodeGen/MachinePipeliner.cpp:877-940`、`:2030-2059`、`:2884-3010` |
| ResMII | 同上 `:4150-4250` |
| window scheduling | `lib/CodeGen/WindowScheduler.cpp:1-35` |
| RR 列表调度优先级 | `lib/CodeGen/SelectionDAG/ScheduleDAGRRList.cpp:1984-2060` |
| Output/Anti 延迟 | `lib/CodeGen/ScheduleDAGInstrs.cpp:336-343`、`ScheduleDAG.h:109-121` |
| RR 回溯 | 同上 `:1406-1470` |
| 默认 selector | `lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:302-330` |
