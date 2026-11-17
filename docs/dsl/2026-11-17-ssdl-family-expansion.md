# 族/维度展开器与无损验证（Step 3）

- date: 2026-11-17
- status: **Implemented + 14/14 RISC-V models verified**
- 工具：`tools/ssdl/family_expand.py`
- 测试：`check.py` 66/66
- 源：LLVM `700e551714cc`

## 1. 实现了什么

`family_expand.py` 是压缩 SSDL → v1.0 扁平 SSDL 的前端。扁平输出仍走 `emit_td.py`，后端一行未改。

新增两个领域构造（不新增顶层关键字）：

```text
set LMUL = [MF8, MF4, MF2, M1, M2, M4, M8] :
    kind = dimension, name_format = "_{value}", units = [1, 2, 4, 8, 16, 32, 64];
set NF = 2..8 : kind = dimension, name_format = "{value}";

machine siFive7(vlen: INT) {
    write  WriteVLDE[LMUL];
    bind   WriteVLDE[LMUL] -> [VCQ, VL], latency = 3 + 2 * LMUL;   # 5,7,11,…,131
}

machine sifive-7-series : siFive7(512) {
    resource VCQ : units = 1;
    resource VL  : units = 1;
}
```

规则：

| 构造 | 语义 |
|---|---|
| `set D = [a, b] : kind = dimension` | 声明有限轴；只有已声明的轴可被索引 |
| `set D = a..b : kind = dimension` | 整数范围（`NF = 2..8`） |
| `name_format = "_{value}"` | 成员名格式；只允许一个 `{value}` |
| `W[D]` | 展开为 `W_a`、`W_b` …，一个成员一条语句 |
| `W[D in [a]]` | **稀疏成员**：只展开列出的成员 |
| `machine F(p: INT)` | 族声明；参数可用于数值表达式 |
| `machine X : F(512)` | 实例化；族体 + 实例体合并展开 |
| `units = [1, 2, …]` | 成员的整数投影；LMUL 以八分之一为单位，避免浮点 |
| `worst(D)` | 伪成员：名字为 `_WorstCase`，数值取最后一个（最宽）成员 |
| `latency = 3 + 2 * LMUL` | 轴名在数值位即「当前成员的 units」 |
| `release = [1, 1 + 2 * LMUL]` | 列表逐项求值，允许 `cdiv/max/min` |
| `latency = vlen / 128` | 有界整数表达式（`+ - * // %`、括号、参数、`cdiv/max/min`） |

`/` 只在整除时报成功（非整除是错误，不会静默取整）；`//` 是 TableGen `!div` 的语义。`cdiv(a, b)` 是 `ceil(a/b)`。表达式节点上限 64。

**不支持**：字符串插值（`"W_" # mx`）、`**`、递归、自定义函数、关键字参数。维度必须保持可枚举，这是与 TableGen `defm` 的分界线。

## 2. 覆盖性诊断（AC4）

`--check-coverage` 在展开后检查两件事：每条维度化 `write` 必须有 `bind`，每条 `bind` 必须对应已声明的 `write`。诊断带 SSDL 源行号。

实测：

```text
$ family_expand.py neg1.ssdl --check-coverage
machine m: bind W_C has no write (line 4)

$ family_expand.py neg2.ssdl --check-coverage
machine m: write W_C has no bind (line 4)

$ family_expand.py add.ssdl --add-axis-value D=C --check-coverage
machine m: write W_B has no bind (line 3)
machine m: write W_C has no bind (line 3)
```

第三条就是「加 LMUL=M16」的可执行形式：新增成员后，所有未覆盖该成员的站点一次报全，而不是静默落默认值。

## 3. 无损验证

### 3.1 合成 mini 族（完整端到端）

`tests/ssdl/family-mini.ssdl`：1 条维度化 `bind` + 1 个族参数，展开为 3 条记录。

- 展开：`bind W_A/B/C -> [P], latency = 4`（`512 / 128`）；
- `emit_td.py` 生成 TD；
- `llvm-tblgen -print-records` 与 `tests/ssdl/family-mini.expected.td` 逐记录比较，`check.py` 断言通过。

这一步验证了「压缩 → 展开 → 发射 → 记录」的整条链路。

### 3.2 真实 SiFive7 子族（对上游逐字段）

`tests/ssdl/sifive7-vlde.ssdl` 压缩了 SiFive7 的 8 条 `WriteVLDE` 记录（`RISCVSchedSiFive7.td:329`、`:836` 的真实产物）：

```text
set LMUL = [MF8, MF4, MF2, M1, M2, M4, M8] :
    kind = dimension, name_format = "_{value}";
machine vlde {
    resource VLEN512SiFive7VCQ : units = 1;
    resource VLEN512SiFive7VL  : units = 1;
    write WriteVLDE[LMUL];
    bind WriteVLDE[LMUL in [MF8, MF4, MF2]] -> [...], release = [1, 2], ...;
    bind WriteVLDE[LMUL in [M1]]              -> [...], release = [1, 3], ...;
    ...
    bind WriteVLDE[worst(LMUL)]               -> [...], release = [1, 17], ...;
}
```

测试 `sifive7_vlde_family_matches_upstream_values` 展开、发射、跑 `-print-records`，再与 `$SSDL_CACHE/riscv.rec` 中 `SiFive7VLEN512Model` 的 8 条 `WriteRes` 逐字段比较：

| 字段 | 结果 |
|---|---|
| `Latency` / `NumMicroOps` | 8/8 相同 |
| `ReleaseAtCycles` | 8/8 相同（`[1,2]`…`[1,17]`） |
| `AcquireAtCycles` | 8/8 相同（`[0,1]`） |
| `ProcResources` | 8/8 相同 |

这不是「生成器自己对自己」：期望值来自上游 `-print-records`，压缩源是独立手写的。

## 4. 压缩比的实际边界

对 `SiFive7VLEN512Model` 的 **3772 条显式** `WriteRes`（`Unsupported = 0`）做分组实测：

| 分组 | 数量 |
|---|---:|
| 含 LMUL 成员 token（任意位置） | 3646 |
| LMUL 在第 1 / 2 / 3 个 `_` 段 | 3167 / 303 / 176 |
| 无任何轴 token（标量/原子，必须扁平） | 126 |
| 组内载荷恒定 | 40 组 / 54 条 |
| 组内载荷随成员变化 | 488 组 / 3592 条 |
| 不同 `Latency` 向量 | 30 |
| 不同 `ReleaseAtCycles` 向量 | 26 |
| 折叠全部轴后的 canonical base | 355 |

早期按「后缀」统计会漏掉 `WriteVFMinMaxV_MF2_E32` 这类 LMUL 在中间的 479 条，因此旧表的 463/394 是低估。

结论：**压缩的收益来自「公式一次、记录多次」，不是文本折叠**。载荷只在 40 个组里恒定，其余 3592 条必须写成表达式；但载荷只有 30 种形状，说明公式是少数（§1.4 的两条即覆盖 117 + 53 组）。仅靠字符串压缩无法达到计划里的 20×。

## 4.1 矿工：把 3772 条记录折叠成 355 个 base

`tools/ssdl/family_mine.py` 不做生成，只回答 AC8 的问题：折叠所有轴之后，还剩几个 base 没有可命名的公式。

| 项 | 值 |
|---|---:|
| 显式 `WriteRes` | 3772 |
| canonical base | 340 |
| 分类：扁平 / 载荷恒定 / 公式 | 126 / 40 / 174 |
| 公式目录条目 | 22（其中 1 条是显式表） |
| 未解释 base | **0** |

公式目录 `data/riscv-sched-formulas.json` 每条都引用 TableGen class 行号；矿工只检查目录，不从记录反推公式。`family_catalogue_leaves_no_record_unexplained` 断言 3772 条全部归入三类之一，`family_catalogue_explains_every_segmented_family` 断言 SEG 九族 2016 条由 6 条公式逐成员复现。

两条语义是这一步逼出来的，已写进 `family_expand.py`：

1. `worst(AXIS)` 的数值 = **该 base 实际覆盖的最宽成员**，不是全局 M8。上游 `SiFive7IsWorstCaseMX<mx, SchedMxList>` 就是这样：覆盖 MF8..M4 的族，`_WorstCase` 复制 M4。表达方式是给窄族声明自己的轴视图（成员表不同，`name_format` 相同）。
2. 轴可以有第二个数值投影：`set SEW = [...] : units = [...], div_factor = [15, 28, 57];`，表达式写 `SEW.div_factor`。上游 `SiFive7GetDivOrSqrtFactor` 就是 `!cond(sew)` 表，这是逐行转写而不是拟合。

## 4.2 从 base 还原压缩源：发射器与第一条等价性诊断

`family_mine.py --emit` 把 340 个 base 还原成 `docs/examples/sifive7-family.ssdl`（历史版本 1143 行）：

| 指标 | 值 |
|---|---:|
| 压缩源行数 | 1143 |
| 展开后 bind 数 | 3772（等于显式 `WriteRes`） |
| 相对展开源 | 12259 → 1143（10.7×，单模型） |
| 轴视图 | 8 个 LMUL 视图 + SEW/EEW/NF（窄族需要自己的最宽成员） |
| 非笛卡尔成员集 | 按轴分解成矩形块（`[LMUL in [...], SEW in [...]]`） |

`family_expand --check-coverage` 通过；`emit_td` 通过；`-print-records` 后 `equiv.py` 给出第一条真实差异：

```text
1638 read      ReadAdvance 未生成
 474 write     SchedWrite 只在 intree（如 WriteVLDS*）
  92 variant   SchedWriteVariant 未生成
   1 InstRW    调度类映射未生成
```

结论：压缩前端的形状是对的（3772 条 bind 全部还原），但「等价」还差三类记录。这不是公式问题，是 Step 3.4 的范围问题：ReadAdvance 的维度化（`advance R[LMUL] : cycles = ...`）、`variant`、`map` 尚未进入压缩源。

## 4.3 全模型压缩源与四组无损对拍（Step A–E）

`family_mine.py --emit --profile sifive7-512` 现在发射完整模型：write/read 两半、variant、InstRW、资源、机器属性、processor features/tunes。压缩源 `docs/examples/sifive7-family.ssdl` 为 **3746 行**，展开为 **12375 行**。

| 压缩站点 | 条数 | 展开后记录 |
|---|---:|---:|
| `write` | 1287 | 4338 `SchedWrite` |
| `bind` | 1243 | 4246 `WriteRes` |
| `read` | 466 | 1755 `SchedRead` |
| `advance` | 538 | 1638 `ReadAdvance` |
| `variant` | 26 | 92 `SchedWriteVariant` |
| `map` | 1 | 1 `InstRW` |
| `set`（轴视图） | 67 | 0（编译期） |

四组 `equiv.py` 全部 `records match`：

```text
SiFive7VLEN512Model    4246 writes, 1638 reads, 92 variants, 1 InstRW, 8 resources
SiFive7VLEN128X100Model 4246 writes, 1638 reads, 92 variants, 1 InstRW, 8 resources
SiFive7VLEN1024X300Model 4246 writes, 1638 reads, 92 variants, 1 InstRW, 9 resources
Andes45Model            4200 writes, 1638 reads,  0 variants, 0 InstRW, 16 resources
```

machine props、resource buffer、processor name/features/tunes/default_march 同在这四组比较范围内。Andes45 没有 `variant`/`InstRW`，但有 641 条上游 `Unsupported = 1` 的 WriteRes，由 `emit_unsupported` 逐条表达。

Andes45 的公式覆盖与 SiFive7 不同：共享目录（每条都引用 SiFive7 TD 行号）解释了 9 个 SEG 族（2016 条）等，但 **3360 条**记录的延迟/载荷不服从这些公式，矿工报告为 `unexplained`。发射器对它们按成员或载荷组写显式 `bind`，没有引入任何新内核——因此 Step E 的停止条件（“需要新内核而内核无上游行号”）没有触发。这也解释了 Andes45 的压缩比只有 7432 → 11834（1.6×，3166 `bind` → 4200 `WriteRes`）：机制可移植，公式目录仍带 SiFive7 的行号依据。

**压缩比的口径**：整模型 3746 → 12375 是 **3.3×**，低于 §1.4 用「只发射 WriteRes 半边」估出的 6.4×。原因不是回退，而是当时的 1925 行并不是完整模型：它没有 variant 子写（474）、分支写（474）、unsupported 写（300）、variant 谓词，也没有把 read 的真实成员展开。完整模型里 1 条 `bind` 平均表达 3.4 条 `WriteRes`；真正的收益仍是「加一个维度值只改一处」，行数比只作为参考。

注意：processor 名 `sifive-7-series` 含 `-`，不是 SSDL 标识符；生成侧写作 `sifive_7_series`，`equiv.py` 在比较时统一折叠 `-` → `_`（记录本身仍由 `--in-processor/--gen-processor` 指定）。

## 4.4 全 RISC-V 模型扫掠

`tools/ssdl/family_sweep.py` 对 `$SSDL_CACHE/riscv.rec` 中每个有 processor 指向的 `SchedMachineModel` 自动生成 profile 并跑完整链路。`NoSchedModel` 被排除（它没有调度模型）。实测 **14/14 `records match`**：

| model | writes | reads | variants | InstRW | resources |
|---|---:|---:|---:|---:|---:|
| `Andes45Model` | 4200 | 1638 | 0 | 0 | 16 |
| `GenericOOOModel` | 4200 | 1639 | 0 | 0 | 6 |
| `MIPSP8700Model` | 4200 | 1639 | 0 | 0 | 12 |
| `RocketModel` | 4200 | 1639 | 0 | 1 | 7 |
| `SiFiveP400Model` | 4188 | 1638 | 0 | 2 | 13 |
| `SiFiveP500Model` | 4201 | 1639 | 0 | 2 | 9 |
| `SiFiveP600Model` | 4210 | 1638 | 0 | 94 | 15 |
| `SiFiveP800Model` | 4188 | 1638 | 0 | 2 | 18 |
| `SpacemitX100Model` | 4184 | 1638 | 0 | 0 | 10 |
| `SpacemitX60Model` | 4207 | 1638 | 1 | 1 | 7 |
| `SyntacoreSCR1Model` | 4200 | 1639 | 0 | 1 | 5 |
| `SyntacoreSCR7Model` | 4200 | 1639 | 0 | 1 | 9 |
| `XiangShanKunMingHuModel` | 4200 | 1639 | 0 | 1 | 16 |
| `XiangShanNanHuModel` | 4200 | 1639 | 0 | 1 | 7 |

```bash
python3 tools/ssdl/family_sweep.py --out $SSDL_CACHE/sweep-final --jobs 4
# 单模型调试：
python3 tools/ssdl/family_sweep.py --only RocketModel --jobs 1
```

扫掠暴露并修掉了四个只在其他模型出现的表示问题：

1. **WriteRes 子类**：`XSWriteStore` 等类不是记录名里的 `WriteRes`，矿工改按字段（`WriteType` + `Latency`）识别绑定；分支写按“有 `Latency`、无 `WriteType`”识别。
2. **目标级 ProcResource**：Rocket 等模型的资源 `SchedModel = ?`，由写绑定引用；`resource_table` 与 `equiv` 都按“本模型写到的资源”做闭包。
3. **`instregex`**：P600/P800 的 92 条 InstRW 用 `(instregex "...")`，必须保持正则而不是拆成标识符；生成的 `map [regex("...")]` 由 `emit_td` 还原成 `instregex`。
4. **独立 variant**：SpacemitX60 的 `SMX60CopySchedWriteVariant` 没有 `SchedAlias` 主体，InstRW 直接引用它；矿工把它发射成无轴 `variant`，选择项按成员具体名展开；`equiv` 对独立 variant 与 alias variant 做对称比较。

processor 名仍按 SSDL 标识符规则折叠 `-` → `_`；扫掠每个模型选一个 processor（按 def 名排序的第一个），模型语义与 processor 选择无关。

## 5. 剩余工作（Step 3 未完成项）

1. ~~全模型压缩源~~ → 已完成：3746 行 → 12375 行，四类记录齐全（§4.3）。
2. ~~公式库~~ → 已收敛为 `units` + `cdiv/max/min`，不再是自由投影（上游 `!cond(mx)` 就是同一组整数运算）。
3. ~~四组 `equiv.py`~~ → 已完成：SiFive7 ×3 + Andes45 全部 `records match`（§4.3）。
4. ~~`WorstCase` 语义~~ → 已实现 `worst(D)`：伪成员、不参与计数、数值取最宽成员。

这四项是 Step 4 定语法前必须回答的问题；第 3 项是 AC3 的唯一判据。

## 6. 复现

```bash
cd /path/to/ssdl
python3 tools/ssdl/family_expand.py tests/ssdl/family-mini.ssdl --check-coverage
python3 tools/ssdl/family_expand.py tests/ssdl/sifive7-vlde.ssdl --check-coverage
python3 tools/ssdl/check.py   # 66/66

# 四个已对拍实例（profile 名见 data/riscv-sched-profiles.json）
python3 tools/ssdl/family_mine.py --emit /tmp/sf.ssdl --profile sifive7-512
python3 tools/ssdl/family_expand.py /tmp/sf.ssdl --check-coverage
python3 tools/ssdl/family_expand.py /tmp/sf.ssdl -o /tmp/sf-flat.ssdl
python3 tools/ssdl/emit_td.py /tmp/sf-flat.ssdl --target-include SSDLStub.td \
    --records-json /tmp/sf-flat.json -o /tmp/sf-flat.td
$SSDL_LLVM_BUILD/bin/llvm-tblgen -print-records \
    -I $SSDL_LLVM_SRC/include -I tests/ssdl/stub-target \
    -o /tmp/sf-gen.rec /tmp/sf-flat.td
python3 tools/ssdl/equiv.py $SSDL_CACHE/riscv.rec /tmp/sf-gen.rec \
    --in-model SiFive7VLEN512Model --gen-model sifive_7_seriesModel \
    --in-processor SIFIVE_7 --gen-processor sifive_7_series \
    --strip-prefix VLEN512SiFive7

# 另三组只需换 profile 与名字：
#   sifive7-128x100  -> SiFive7VLEN128X100Model / sifive_x160Model / SIFIVE_X160 / VLEN128X100SiFive7
#   sifive7-1024x300 -> SiFive7VLEN1024X300Model / sifive_x390Model / SIFIVE_X390 / VLEN1024X300SiFive7
#   andes45          -> Andes45Model / andes_a45Model / ANDES_A45 / andes_a45（无 strip-prefix）
```

## 现在的流水线

族源不再需要手工先展开：`parser.load_source` 以 `kind = dimension` 为标记自动调用
`family_expand.expand`。`family_sweep.py` 因此直接对 `family.ssdl` 跑
`validate.py`（展开 + 覆盖性 + 诊断）和 `emit_td.py`，只在需要调试时才保留
`flat.ssdl`。展开仍是同一份实现，`check.py` 断言展开结果不含 v1.2 构造且可重新解析。
