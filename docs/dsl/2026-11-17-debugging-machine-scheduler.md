# 调试 MachineScheduler：从小白到能单步

> 目标：让你能用一条命令看见调度器在想什么，并知道去代码的哪一行看。
> 所有日志都来自 `LLVM 24.0.0git @ 700e551`（assert 构建，RISC-V 后端），可复现。
>
> 示例与一键脚本：`docs/examples/sched-debug/`、`scripts/debug-sched-demo.sh`。

## 0. 先记住三句话

1. 调度器不执行指令，它只给每条指令挑一个"第几拍"。
2. 每一步只做三件事：**从 ready 集合挑一个 → 记账（发射槽/资源/延迟）→ 解锁后继**。
3. 模型里的所有字段，最终只影响两处：**谁能进 ready**、**先挑谁**。

---

## 1. 准备：一个能看见内部的 llc

### 1.1 构建要求

```
$ llc --version
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.      ← 必须有 "with assertions"
```

`-debug` / `-debug-only` 只在非 `NDEBUG` 构建里存在。Release 构建会直接报
"unknown argument"。

### 1.2 CPU 与 ISA 的坑（真实踩过）

```
$ llc -mtriple=riscv64-unknown-elf -mcpu=sifive-7-series demo.ll
LLVM ERROR: RV64 target requires an RV64 CPU
```

原因：`sifive-7-series` 在 `RISCVProcessors.td:162` 是
`RISCVTuneProcessorModel`——**只带调优信息，不带基础 ISA**。要显式给：

```
-mattr=+64bit,+m,+a,+f,+d,+c
```

另外，用宿主 clang 生成的 `.ll` 会带 `"target-cpu"="x86-64"` 属性，
llc 会刷一堆 "not a recognized processor" 警告；先把这三个属性删掉：

```
sed -i 's/ "target-cpu"="[^"]*"//; s/ "target-features"="[^"]*"//; s/ "tune-cpu"="[^"]*"//' demo.ll
```

### 1.3 得到可重复的 MIR

仓库里已经有现成输入：`docs/examples/sched-debug/demo.ll`。
不要每次都从 IR 跑整条流水线；先停在 pass 之前：

```
llc -mtriple=riscv64-unknown-elf -mcpu=sifive-7-series \
    -mattr=+64bit,+m,+a,+f,+d,+c -O2 \
    -stop-before=machine-scheduler -simplify-mir \
    docs/examples/sched-debug/demo.ll -o demo-before.mir
```

或者直接跑一键脚本（输出到 `$SSDL_CACHE/sched-debug/`）：

```
scripts/debug-sched-demo.sh
```

### 1.4 只跑这一个 pass

```
llc -run-pass=machine-scheduler demo-before.mir -o demo-after.mir \
    -debug-only=machine-scheduler 2> sched.log
```

`-debug-only=machine-scheduler` 同时覆盖 `MachineScheduler.cpp` 与
`ScheduleDAGInstrs.cpp`（两者 `DEBUG_TYPE` 相同）。legacy post-RA 调度器是
另一个 tag：`post-RA-sched`；但 RISC-V 在
`RISCVTargetMachine.cpp:325` 把 legacy 换成了 `PostMachineScheduler`，
所以 RISC-V 的 post-RA 日志仍然出现在 `machine-scheduler` 里。

---

## 2. 例子

```c
long f(long a, long b, long c, long d) {
  long x = a * b;
  long y = c + d;
  long z = x - y;
  long w = z << 3;
  return w + (a ^ b);
}
```

`Before MISched` 的 MIR（11 条可调度指令）：

```
%3:gpr = COPY $x13
%2:gpr = COPY $x12
%1:gpr = COPY $x11
%0:gpr = COPY $x10
%4:gpr = nsw MUL %1:gpr, %0:gpr
%5:gpr = ADD %2:gpr, %3:gpr
%6:gpr = SUB %4:gpr, %5:gpr
%7:gpr = SLLI %6:gpr, 3
%8:gpr = XOR %1:gpr, %0:gpr
%9:gpr = nsw ADD %7:gpr, %8:gpr
$x10 = COPY %9:gpr
```

---

## 3. 逐段读日志

### 3.1 区域：调度器一次只看一块

```
Current Schedule Region
f:%bb.0 
  From: %3:gpr = COPY $x13
    To: PseudoRET implicit $x10
 RegionInstrs: 11
```

调用、PHI、目标自定义边界会把基本块切成多个区域；区域之间不重排。
如果你发现某条指令"没动"，先确认它和你在看的指令在同一个区域里。

### 3.2 DAG：每条指令变成一张带数字的卡片

```
SU(0):   %3:gpr = COPY $x13
  # preds left       : 0
  # succs left       : 1
  # rdefs left       : 0
  Latency            : 3
  Depth              : 0
  Height             : 10
  Successors:
    SU(5): Data Latency=1 Reg=%3
  Pressure Diff      : GPRF16C 1    GPRF16C_with_GPRS07 1    GPRTC 1
```

| 字段 | 人话 |
|---|---|
| `# preds left` | 还有几个前驱没调度；到 0 才能进 ready |
| `Latency` | 结果几拍后可用（这里是 COPY 的 sched class 延迟） |
| `Depth` | 从入口到它的最长路径（DP） |
| `Height` | 它到出口的最长路径（DP） |
| `Successors` | 依赖边：`Data` 真依赖、`Anti` WAR、`Out` WAW、`Ord` 内存序 |
| `Pressure Diff` | 调度它会让寄存器压力变化多少 |

`ExitSU` 是一条虚拟出口，`SU(10)` 到它有 `Ord Latency=3 Artificial` 的边——
这条边是后面"为什么第一拍是 3"的关键。

### 3.3 ready 队列与 pick

```
Move SU(0) into Available Q
Move SU(1) into Available Q
Move SU(2) into Available Q
Move SU(3) into Available Q
hazard: SU(10) ReadyCycle = 3 is later than CurrCycle = 0 on an unbuffered resource
Critical Path(GS-RR ): 12
Cycle: 3 BotQ.A
```

- `TopQ` / `BotQ`：从上往下 / 从下往上两个方向的队列。
- `.A` = Available（可发射），`.P` = Pending（还没到 ready cycle）。
- `hazard: ... ReadyCycle = 3 ... CurrCycle = 0`：模型是
  `MicroOpBufferSize == 0`（顺序核），没准备好的节点不能出现在 ready 队列里，
  只能等。于是调度器直接把周期从 0 推到 3。
- 注意：**bottom-up 的 cycle 是从区域末尾往回数的**。`Cycle: 3` 不是"第 3 拍"，
  而是"距末尾 3 拍"。

### 3.4 记账：一条指令到底占用了什么

```
Pick Bot Cand SU(10) ONLY1      [pre-RA]
Scheduling SU(10) $x10 = COPY %9:gpr
  Ready @3c
  VLEN512SiFive7PipeAB +1x1u
BotQ.A @3c
  Retired: 1
  Executed: 3c
  Critical: 0c, 1 MOps
  ExpectedLatency: 3c
  - Latency limited.
  hazard: SU(9) ReadyCycle = 6 is later than CurrCycle = 3 on an unbuffered resource
```

| 行 | 含义 |
|---|---|
| `ONLY1` | 队列里只有它，没有比较 |
| `Ready @3c` | 它的 ready cycle |
| `PipeAB +1x1u` | 占用 `PipeAB` 1 个 uop、1 拍 |
| `Retired: 1` | 已退休 1 个 uop |
| `Max MOps 2`（后面出现） | `IssueWidth=2` 用满，必须换拍 |
| `hazard: SU(9) ReadyCycle = 6` | 后继 SU(9) 要到 6 拍才就绪，于是周期 3 → 6 |

这就是"记账"的全部：**发射槽、资源实例、ready cycle**。模型字段在这里变成拍数。

### 3.5 双向调度：Top 和 Bot 会互相抢

```
Queue BotQ.A: SU(7) SU(8) 
Queue TopQ.A: SU(0) SU(1) SU(2) SU(3) 
  TopQ.A + Remain MOps: 9
BotQ.A RemLatency SU(7) 5c
TopQ.A RemLatency SU(2) 12c
Picking from Bot:
  Cand SU(7) FIRST                              
  Cand SU(8) ORDER                              
Picking from Top:
  Cand SU(0) FIRST                              
  Cand SU(0) PHYS-REG                           
Pick Top Cand SU(0) PHYS-REG   [pre-RA]
```

- `RemLatency` / `Remain MOps`：这个方向还剩多少延迟和发射量，用来判断
  现在该抢延迟还是省资源。
- `Cand ... FIRST`：第一个候选（还没有比较）。
- `Cand ... ORDER`：用"原始顺序"规则胜出的候选。
- `Cand ... PHYS-REG`：物理寄存器 def/use 就近规则胜出。
- 最后一行：Top 候选打败了 Bot 候选。

RISC-V 默认是双向的（`RISCVSubtarget.cpp:257` 把 `OnlyBottomUp/OnlyTopDown`
都设为 false），因为上游认为双向更均衡。

### 3.6 最终顺序

```
*** Final schedule for %bb.0 ***
SU(0):  %3:gpr = COPY $x13
SU(1):  %2:gpr = COPY $x12
SU(2):  %1:gpr = COPY $x11
SU(3):  %0:gpr = COPY $x10
SU(5):  %5:gpr = ADD %2:gpr, %3:gpr     ← ADD 被提前到 MUL 前面
SU(4):  %4:gpr = nsw MUL %1:gpr, %0:gpr
SU(6):  %6:gpr = SUB %4:gpr, %5:gpr
SU(7):  %7:gpr = SLLI %6:gpr, 3
SU(8):  %8:gpr = XOR %1:gpr, %0:gpr
SU(9):  %9:gpr = nsw ADD %7:gpr, %8:gpr
SU(10): $x10 = COPY %9:gpr
```

post-RA 再排一次，最终汇编是：

```
add  a2, a2, a3
mul  a3, a1, a0
sub  a3, a3, a2
xor  a0, a0, a1
slli a3, a3, 3
add  a0, a0, a3
```

`XOR` 被插到 `SLLI` 前面，填的是 `ADD` 之后的延迟空档——这就是调度的收益。

---

## 4. 代码地图：按调用链看

| 你想知道 | 函数 | 位置 |
|---|---|---|
| 区域怎么切 | `isSchedBoundary` / `getSchedRegions` | `MachineScheduler.cpp:964-1013` |
| 主驱动 | `MachineSchedulerBase::scheduleRegions` | `:1016` |
| DAG 构建 | `ScheduleDAGInstrs::buildSchedGraph` | `ScheduleDAGInstrs.cpp:1182` |
| 每条指令的初始状态 | `initSUnits` | 同上 `:577` |
| 策略初始化 | `GenericScheduler::initialize` | `MachineScheduler.cpp:3950` |
| 选谁 | `GenericScheduler::pickNode` | `:4473` |
| 双向比较 | `pickNodeBidirectional` | `:4395` |
| 候选字典序 | `tryCandidate` | `:4258` |
| 压力比较 | `tryPressure` | `:4098` |
| 进队/挂起 | `SchedBoundary::releaseNode` | `:3113` |
| 记账 | `SchedBoundary::bumpNode` | `:3251` |
| 资源冲突 | `checkHazard` | `:3011` |
| 剩余量 | `SchedRemainder::init` | `:2835` |
| 把 MI 挪到位置 | `ScheduleDAGMILive::scheduleMI` | `:2187` |
| 解锁后继 | `ScheduleDAGMI::updateQueues` | `:1489` |

读代码的顺序建议：`schedule()` → `pickNode` → `bumpNode` → `scheduleMI`，
其它都可以按需展开。

---

## 5. 用 lldb/gdb 单步

```
lldb -- /home/ubuntu/llvm/builds/explore/bin/llc \
  -run-pass=machine-scheduler /tmp/demo-before.mir -o /tmp/out.mir

(lldb) break set -n llvm::GenericScheduler::pickNode
(lldb) break set -n llvm::SchedBoundary::bumpNode
(lldb) run
```

停下来后最常看：

```
p Cand.SU->NodeNum
p Cand.Reason
p Zone->CurrCycle
p Zone->CurrMOps
p SU->BotReadyCycle
p SU->hasReservedResource
```

条件断点：

```
break set -n llvm::SchedBoundary::bumpNode --condition 'SU->NodeNum == 9'
```

注意：assert 构建通常是优化过的，局部变量可能显示为 `<optimized out>`。
真要单步，配一个 `-DCMAKE_BUILD_TYPE=Debug` 的构建；日常定位优先用
`-debug-only`，只有"这个值为什么是 6"这种问题才上调试器。

---

## 6. 受控实验：改模型，看调度怎么变

这是 SSDL 最有用的调试方式——把"硬件假设"变成可改的一行：

```
machine sifive7 {
    resource PipeAB : units = 2;
    write W_ALU;
    bind W_ALU -> [PipeAB], latency = 3;   ← 改成 1
}
```

重新生成 TD → 重新跑同一个 `demo-before.mir` → diff `Final schedule`。
如果顺序没变，说明这条依赖本来就不是瓶颈；如果变了，你就拿到了
"latency 如何影响选择"的直接证据。

对比工具：

```
llc ... -debug-only=machine-scheduler 2> a.log   # latency=3
llc ... -debug-only=machine-scheduler 2> b.log   # latency=1
diff <(grep "Pick " a.log) <(grep "Pick " b.log)
```

吞吐侧再用 `llvm-mca -mcpu=sifive-7-series` 看每拍资源压力。

---

## 7. 常见坑清单

| 现象 | 原因 |
|---|---|
| `unknown argument '-debug-only'` | Release 构建，没有断言 |
| `RV64 target requires an RV64 CPU` | `sifive-7-series` 是 tune-only，缺 `-mattr=+64bit,...` |
| 一堆 "not a recognized processor" | 宿主 clang 的 IR 带了 x86 `target-cpu` 属性 |
| 指令没被重排 | 它在另一个 scheduling region（call/PHI 边界） |
| 没有压力相关的日志 | 区域太小，`ShouldTrackPressure=0`（小于半个整数寄存器堆） |
| 只有 bottom-up | 目标没有 override policy；RISC-V 是双向 |
| 看不到 post-RA 日志 | post-RA 由 tune 特性/`-misched-postra` 控制；RISC-V 用 `PostMachineScheduler` |
| `Cycle: 3` 看不懂 | bottom-up 的 cycle 从区域末尾往回数 |

---

## 8. 一页命令清单

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc

$LLC --version | grep assertions

# 仓库已提供 demo.ll；若从源重新生成：
clang -O1 -S -emit-llvm docs/examples/sched-debug/demo.c -o demo.ll
sed -i 's/ "target-cpu"="[^"]*"//; s/ "target-features"="[^"]*"//; s/ "tune-cpu"="[^"]*"//' demo.ll

$LLC -mtriple=riscv64-unknown-elf -mcpu=sifive-7-series \
     -mattr=+64bit,+m,+a,+f,+d,+c -O2 \
     -stop-before=machine-scheduler -simplify-mir \
     docs/examples/sched-debug/demo.ll -o demo-before.mir

$LLC -run-pass=machine-scheduler demo-before.mir -o demo-after.mir \
     -debug-only=machine-scheduler 2> sched.log

grep -n "Pick \|Ready @\|hazard\|Max MOps\|Final schedule" sched.log

$LLC -mtriple=riscv64-unknown-elf -mcpu=sifive-7-series \
     -mattr=+64bit,+m,+a,+f,+d,+c -O2 demo.ll -o demo.s
```
