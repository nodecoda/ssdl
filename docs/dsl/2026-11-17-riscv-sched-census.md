# RISC-V 调度模型普查（Step 1）

- date: 2026-11-17
- status: **Evidence（可复算）**
- 数据源：`llvm-tblgen -print-records` → `/tmp/riscv.rec`（151 MB）
- LLVM：`700e551714cc`（`/home/ubuntu/llvm/trees/explore/llvm`）
- 机读表：`data/riscv-sched-census.csv`（由 `tools/ssdl/census_riscv.py` 生成）

## 1. 口径

1. **模型枚举**：记录表中末类名以 `SchedModel` / `SchedMachineModel` 结尾的记录，排除 `NoSchedModel`。共 **25 条模型记录**，来自 **17 个模型文件**；其余 6 个 `*Sched*.td` 是共享词表（含 `RISCVSchedSiFive8.td`——名字带 `Sched`，但只有 `SchedWrite` 定义、没有模型）。
2. **显式 vs 默认**：`-print-records` 给每个未绑定的 `SchedWrite` / `SchedRead` 生成 `Unsupported = 1` 的记录。表中 `WR explicit` / `RA explicit` 只统计 `Unsupported != 1`；`WR stmts` 是源文件的 `*WriteRes<` 语句数（含 `LMULWriteRes<` 包装）。
3. **维度轴**：源文件中 `foreach <var> = <expr> in` 的 `(var, expr)` 去重集合，见 CSV 的 `dimension_axes` 列。

## 2. 普查表

| file | model | lines | foreach | defm | WR stmts | WR explicit | RA explicit | resources | InstRW | variants | family |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| RISCVSchedAndes45.td | Andes45Model | 1373 | 62 | 256 | 120 | 3559 | 1330 | 16 | 0 | 0 | SchedMachineModel |
| RISCVSchedGenericOOO.td | GenericOOOModel | 501 | 0 | 6 | 121 | 121 | 111 | 6 | 0 | 0 | SchedMachineModel |
| RISCVSchedMIPSP8700.td | MIPSP8700Model | 278 | 0 | 12 | 82 | 82 | 76 | 12 | 0 | 0 | SchedMachineModel |
| RISCVSchedRocket.td | RocketModel | 267 | 0 | 14 | 67 | 67 | 61 | 0 | 1 | 0 | SchedMachineModel |
| RISCVSchedSiFive7.td | SiFive7VLEN1024X300Model | 1669 | 61 | 327 | 128 | 3772 | 1331 | 9 | 1 | 91 | SiFive7SchedMachineModel |
| RISCVSchedSiFive7.td | SiFive7VLEN128X100Model | 1669 | 61 | 327 | 128 | 3772 | 1331 | 8 | 1 | 91 | SiFive7SchedMachineModel |
| RISCVSchedSiFive7.td | SiFive7VLEN512Model | 1669 | 61 | 327 | 128 | 3772 | 1331 | 8 | 1 | 91 | SiFive7SchedMachineModel |
| RISCVSchedSiFiveP400.td | SiFiveP400Model | 547 | 30 | 129 | 15 | 3781 | 1545 | 13 | 2 | 0 | SchedMachineModel |
| RISCVSchedSiFiveP500.td | SiFiveP500Model | 99 | 0 | 13 | 10 | 113 | 101 | 9 | 2 | 0 | SchedMachineModel |
| RISCVSchedSiFiveP600.td | SiFiveP600Model | 1502 | 50 | 315 | 122 | 3775 | 1542 | 15 | 94 | 0 | SchedMachineModel |
| RISCVSchedSiFiveP800.td | SiFiveP800Model | 1190 | 50 | 333 | 125 | 3781 | 1545 | 18 | 2 | 0 | SchedMachineModel |
| RISCVSchedSpacemitX100.td | SpacemitX100Model | 1441 | 47 | 308 | 126 | 3779 | 1546 | 10 | 0 | 0 | SchedMachineModel |
| RISCVSchedSpacemitX60.td | SpacemitX60Model | 1352 | 43 | 257 | 120 | 3558 | 1328 | 7 | 1 | 1 | SchedMachineModel |
| RISCVSchedSyntacoreSCR1.td | SyntacoreSCR1Model | 121 | 0 | 14 | 25 | 25 | 17 | 5 | 1 | 0 | SchedMachineModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR3RV32Model | 451 | 0 | 66 | 67 | 31 | 25 | 5 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR3RV64Model | 451 | 0 | 66 | 67 | 31 | 25 | 5 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR4RV32Model | 451 | 0 | 66 | 67 | 67 | 61 | 7 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR4RV64Model | 451 | 0 | 66 | 67 | 67 | 61 | 7 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR5RV32Model | 451 | 0 | 66 | 67 | 67 | 61 | 7 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR345.td | SyntacoreSCR5RV64Model | 451 | 0 | 66 | 67 | 67 | 61 | 7 | 1 | 0 | SyntacoreSchedModel |
| RISCVSchedSyntacoreSCR7.td | SyntacoreSCR7Model | 380 | 0 | 24 | 90 | 92 | 84 | 9 | 1 | 0 | SchedMachineModel |
| RISCVSchedTTAscalonX.td | TTAscalonXGModel | 1111 | 49 | 277 | 127 | 3521 | 1327 | 10 | 1 | 0 | TTAscalonSchedMachineModel |
| RISCVSchedTTAscalonX.td | TTAscalonXModel | 1111 | 49 | 277 | 127 | 3521 | 1327 | 9 | 1 | 0 | TTAscalonSchedMachineModel |
| RISCVSchedXiangShanKunMingHu.td | XiangShanKunMingHuModel | 433 | 0 | 7 | 105 | 104 | 92 | 16 | 1 | 0 | SchedMachineModel |
| RISCVSchedXiangShanNanHu.td | XiangShanNanHuModel | 317 | 0 | 8 | 92 | 92 | 63 | 7 | 1 | 0 | SchedMachineModel |

## 3. 结论

### 3.1 作者侧规模不是线性增长，而是按维度放大

| 模型 | 源语句 | 显式记录 | 放大倍数 |
|---|---:|---:|---:|
| Rocket | 67 | 67 | 1.0× |
| GenericOOO | 121 | 121 | 1.0× |
| SiFive7（单成员） | 128 | 3772 | 29.5× |
| Andes45 | 120 | 3559 | 29.7× |
| SpacemitX100 | 125 | 3779 | 30.2× |
| SiFiveP400 | 15 | 3781 | **252×** |

P400 最极端：15 条 `LMULWriteRes` 式语句展开成 3781 条记录。这不是「写得啰嗦」，是**维度没有一等公民**——作者只能写模板，读者只能看名字猜维度。

### 3.2 25 条模型里只有 11 条（8 个文件）使用 `foreach`

其余 14 条（Rocket、XiangShan×2、GenericOOO、MIPSP8700、SCR1/SCR7、SCR345 的 6 条等）是逐条手写。两类模型的维护成本不同：

- 手写模型：改一条记录 = 改一行，但**新增一种指令要改 N 个地方**；
- 展开模型：改一处模板 = 改所有成员，但**漏一个维度成员没有任何检查**（TableGen 会填 `Unsupported=1`）。

SSDL v1.2 要同时解决这两侧：既有族/维度压缩，又有覆盖性诊断。

### 3.3 SiFive7 一族是稀疏族，不是满笛卡尔积

`SiFive7SchedMachineModel` 有 4 个参数（`RISCVSchedSiFive7.td:1608-1631`），但只有 3 个成员（`:1642/:1647/:1651`），而参数组合空间是 16。族表必须显式列出成员，不能「展开 16 个再删 13 个」——后者无法解释成员之间的差异。

三个成员的记录数相同（各 3772 显式 WR），但资源数不同（8 / 8 / 9）且参数不同（`HasDualVALU` 仅 1024X300，`HasFastGather` 仅 128X100 与 1024X300）。这说明记录数不是区分成员的指标，**参数才是**。

### 3.4 SCR345 暴露「一个文件多条模型」的第二种稀疏性

`RISCVSchedSyntacoreSCR345.td` 451 行、0 个 `foreach`、66 个 `defm`，产出 6 条模型：SCR3 的 RV32/RV64 各 31 条显式 WR，SCR4/SCR5 的各 67 条。同一份源，成员间记录数不同。Step 2 的族表必须允许「成员覆盖度不同」，否则会把 SCR3 的 31 条误判为漏绑。

### 3.5 维度轴是词表级，不是机器级

CSV 的 `dimension_axes` 列显示：`mx=SchedMxList`、`eew=[8,16,32,64]`、`nf=2-8`、`sew=SchedSEWSet<mx,...>.val`。这些轴绑定的是 `WriteVLDE_M1` 这类**词表名字**，不是 `machine` 属性。v1.2 的 `set ... : kind = dimension` 必须提升到目标级，与 v1.1 §9.1 的词表所有权一致（`docs/dsl/2026-11-16-ssdl-v1.1-draft.md:409`）。

## 4. 复现

```bash
cd /home/ubuntu/llvm/llvm-dev

# 生成 records（一次性，约 151 MB）
llvm-tblgen -print-records \
    -I /home/ubuntu/llvm/trees/explore/llvm/include \
    -I /home/ubuntu/llvm/trees/explore/llvm/lib/Target/RISCV \
    -o /tmp/riscv.rec \
    /home/ubuntu/llvm/trees/explore/llvm/lib/Target/RISCV/RISCV.td

# 生成 / 校验普查表
python3 tools/ssdl/census_riscv.py --records /tmp/riscv.rec \
    --llvm /home/ubuntu/llvm/trees/explore/llvm \
    -o data/riscv-sched-census.csv
python3 tools/ssdl/census_riscv.py --records /tmp/riscv.rec \
    --llvm /home/ubuntu/llvm/trees/explore/llvm --check
```

`check.py` 中的 `riscv_census_matches_the_committed_table` 在有 `/tmp/riscv.rec` 与 LLVM 源时执行同一校验；缺失时跳过（与既有 `backend_bindings_are_declared_in_the_schema` 的策略一致）。

## 5. 对 Step 2 的输入

- 族表只需覆盖 8 个带 `foreach` 的文件（11 条模型），手写模型作为「无维度」控制组；
- 维度轴清单：`mx`（LMUL 7 值）、`sew`（`SchedSEWSet` 投影）、`eew`（4 值）、`nf`（2–8）、以及 `Lmul=[1,2,4,8]` 这类第二套 LMUL 表示（Andes45）；
- 成员覆盖度可能不同（SiFive7 3/16、SCR3 vs SCR4/5），族表必须能表达稀疏成员与成员级覆盖差异。
