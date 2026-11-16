# RISC-V 调度族与维度表（Step 2）

- date: 2026-11-17
- status: **Evidence（源码抽取，可复算）**
- 机读表：`data/riscv-sched-families.json`
- 抽取器：`tools/ssdl/family_tables.py`（`--check` 与源码逐字节对拍）
- 源：LLVM `700e551714cc`，`lib/Target/RISCV/`

## 1. 两层参数化，不能混

普查（`2026-11-17-riscv-sched-census.md`）把放大来源分成两类，Step 2 也必须分开建模：

| 层 | 载体 | 例子 | 作用域 |
|---|---|---|---|
| **机器族** | `SchedMachineModel` 子类 + 模板参数 | `SiFive7SchedMachineModel<int vlen>`（`RISCVSchedSiFive7.td:1608`） | `machine` 级：选资源、选记录名 |
| **词表维度** | 目标级 `defvar` 列表 + `foreach` | `SchedMxList`（`RISCVScheduleV.td:12`）、`SchedSEWSet`（`:31`） | 目标级：铸 `SchedWrite`/`SchedRead` 名字 |

v1.0 只有 `machine X = Y;` 的改名别名（`2026-11-16-ssdl-v1.0-final.md:347`），两层都表达不了。

## 2. SiFive7 族

`SiFive7SchedMachineModel` 的 4 个轴（模板参数 + 类体字段，排除 `let` 与派生字段 `VLEN = vlen`）：

| 成员 | `vlen` | `has_dual_valu` | `fp_latencies` | `has_fast_gather` | 资源数（普查） |
|---|---:|---|---|---|---:|
| `SiFive7VLEN128X100Model` | 128 | false | Low | true | 8 |
| `SiFive7VLEN512Model` | 512 | false | Default | false | 8 |
| `SiFive7VLEN1024X300Model` | 1024 | true | Low | true | 9 |

- 成员定义：`RISCVSchedSiFive7.td:1642` / `:1647` / `:1651`；
- 资源绑定循环：`:1658-1665`，参数为 `VLEN, HasDualVALU, FPLatencies, HasFastGather`；
- 观察域乘积 = 3（VLEN）× 2 × 2 × 2 = **24**，实际成员 **3**。这是**稀疏族**：`has_dual_valu=true` 只在 VLEN=1024 出现，`has_fast_gather=false` 只在 512 出现。
- 结论：族表必须显式列成员，不能展开 24 个再删 21 个；成员之间的差异是参数，不是记录数（三者显式 WR 都是 3772）。

## 3. 向量词表轴

### 3.1 LMUL（`RISCVScheduleV.td:12`）

`SchedMxList = [MF8, MF4, MF2, M1, M2, M4, M8]`（7 值，升序）。

子集（`:14-29`，实现为 `!listremove` / 别名）：

| 名称 | 值 | 用途 |
|---|---|---|
| `SchedMxListW` | 去掉 `M8` | widening / narrowing |
| `SchedMxListWRed` | 全表 | widening reduction |
| `SchedMxListFW` | 去掉 `M8`、`MF8` | FP widening |
| `SchedMxListF` | 去掉 `MF8` | 浮点 |
| `SchedMxListEEW8` | 全表 | 8-bit 索引/跨步 |
| `SchedMxListEEW16` | = `SchedMxListF` | 16-bit |
| `SchedMxListEEW32` | 再去 `MF4` | 32-bit |
| `SchedMxListEEW64` | 再去 `MF2` | 64-bit |

子集不是独立维度，是同一 LMUL 轴上的**投影**。语言不能要求用户为每个子集再声明一遍。

### 3.2 SEW（`RISCVScheduleV.td:31-44`）

基础映射：

| LMUL | 合法 SEW |
|---|---|
| `M1` / `M2` / `M4` / `M8` | `[8, 16, 32, 64]` |
| `MF2` | `[8, 16, 32]` |
| `MF4` | `[8, 16]` |
| `MF8` | `[8]` |

两条规则：浮点去掉 8；widening 去掉 64。浮点 + `MF8` 被源码 `assert` 拒绝，不是合法组合。

### 3.3 EEW 与 NF

| 轴 | 值 | 调度侧出现点 |
|---|---|---|
| `EEW` | `[8, 16, 32, 64]` | `RISCVSchedSiFive7.td:680`、Andes45、P600/P800、Spacemit、TTAscalon 等 12 处 |
| `NF` | `2..8`（`nf=3-8` 与 `nf=2-8` 的并集） | `RISCVSchedSiFive7.td:690`、`:704` 等 12 处 |

### 3.4 名字组合：270 个 base 不是字面量

对 SiFive7VLEN512Model 的显式 `WriteRes` 按 `WriteType` 分类（本轮实测）：

| 类别 | 记录数 | 说明 |
|---|---:|---|
| LMUL 后缀（`_MF8` … `_M8`） | 2716 | `LMULSchedWrites` / `LMULWriteRes` 生成 |
| `_WorstCase` | 434 | `LMULSchedWritesImpl` 的显式 worst-case 记录（`RISCVScheduleV.td:159-163`） |
| 无维度后缀 | 622 | 标量/原子等扁平绑定，本来就不需要压缩 |
| 合计 | 3772 | 与普查一致 |

在 3150 条维度记录中，有 **270 个不同的 base 名**无法用「一个引号字面量」匹配到 `LMUL*<"...">` 调用。原因是它们由多轴拼接而成：

```tablegen
defm : LMULWriteResMX<"WriteVLSEG" # nf # "e" # eew, [VCQ, VL], mx, IsWorstCase>;
```

（`RISCVSchedSiFive7.td:691`；`nf` 来自 `foreach nf=3-8`，`eew` 来自 `[8,16,32,64]`。）

这正是「字符串拼接不可分析」的硬证据：名字里承载了 NF 和 EEW 两个维度，但语法上只是一个 `string`。v1.2 的 `W[LMUL, NF, EEW]` 必须让这三个轴在语法里可枚举，而不是藏在名字里。

## 4. 派生量（必须是一等投影，不是手写常量）

| 投影 | 值 | 源 |
|---|---|---|
| `worst(LMUL)` | `M8`（`SchedMxList` 末元素） | `RISCVScheduleV.td:49-52` |
| `legal_sew(LMUL)` | §3.2 的表 | `RISCVScheduleV.td:31-44` |
| `cycles(LMUL, SEW, NF, VLEN)` | `vl * ceil((SEW * NF) / DLEN)`，`DLEN = VLEN / 2` | `RISCVSchedSiFive7.td:91-110` |

`cycles` 说明 latency 不是常量表，而是维度的有界整数函数。SSDL v1.2 若只允许常量 `latency`，就只能把公式算完再展开——那正是当前 4246 条 `bind` 的来源。

## 5. 对 v1.2 的硬约束

1. **族参数在 `machine`**：`machine SiFive7(vlen: INT, has_dual_valu: BOOL, ...) { ... }` + `machine X : SiFive7(512, false, ...)`；保持 9 个顶层关键字。
2. **维度在目标级**：`set LMUL = [...] : kind = dimension;`，与 v1.1 §9.1 的词表所有权一致（`2026-11-16-ssdl-v1.1-draft.md:409`）。
3. **结构化索引**：`W[LMUL]`，禁止 `"W_" # mx` 式字符串拼接（`RISCVScheduleV.td:159-163` 的上游做法）。
4. **稀疏成员显式列出**：SiFive7 是 3/24，不是满笛卡尔积。
5. **投影是一等构造**：`worst(LMUL)`、`legal_sew(LMUL)`、`cycles(...)` 可被 `bind` 引用，但不能递归或自定义。
6. **成员覆盖度可不同**：SCR345 的 SCR3（31 条显式 WR）与 SCR4/5（67 条）共用一份源，族表必须允许成员级覆盖差异，否则会把 SCR3 误判为漏绑。

## 6. 复现

```bash
cd /home/ubuntu/llvm/llvm-dev
python3 tools/ssdl/family_tables.py \
    --llvm /home/ubuntu/llvm/trees/explore/llvm \
    -o data/riscv-sched-families.json
python3 tools/ssdl/family_tables.py \
    --llvm /home/ubuntu/llvm/trees/explore/llvm --check
```

`check.py` 的 `sifive7_family_table_records_the_sparse_shape` 固定上表的精确元组；`riscv_family_table_matches_the_sources` 重新抽取并与提交的 JSON 逐字节比较。
