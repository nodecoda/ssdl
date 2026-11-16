# 族/维度展开器与无损验证（Step 3）

- date: 2026-11-17
- status: **Implemented + pilot verified（全模型重生成待完成）**
- 工具：`tools/ssdl/family_expand.py`
- 测试：`check.py` 59/59
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

测试 `sifive7_vlde_family_matches_upstream_values` 展开、发射、跑 `-print-records`，再与 `/tmp/riscv.rec` 中 `SiFive7VLEN512Model` 的 8 条 `WriteRes` 逐字段比较：

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
| canonical base | 355 |
| 轴集合为 `{LMUL}` / `{}` / `{LMUL,SEW}` / `{NF,EEW,LMUL}` | 182 / 126 / 38 / 9 |
| SEG 九族（每族 224 = NF7×EEW4×LMUL8） | 2016 条 / 6 条公式 |

公式目录 `data/riscv-sched-formulas.json` 每条都引用 TableGen class 行号；矿工只检查目录，不从记录反推公式。SEG 的 6 条公式已 100% 覆盖 2016 条（`family_catalogue_explains_every_segmented_family`）。

剩余 346 个 base 尚未入目录，`/tmp/mine/unexplained.json` 就是清单。

## 5. 剩余工作（Step 3 未完成项）

1. **全模型压缩源**：把 SiFive7VLEN512 的 3772 条显式 WR 全部写成 ~355 条语句（含 126 条扁平）。当前只完成了 `WriteVLDE` 子族。
2. ~~公式库~~ → 已收敛为 `units` + `cdiv/max/min`，不再是自由投影（上游 `!cond(mx)` 就是同一组整数运算）。
3. **四组 `equiv.py`**：`SiFive7VLEN512/128X100/1024X300` + `Andes45Model` 全部 `records match`。当前只做了子族字段级比较。
4. ~~`WorstCase` 语义~~ → 已实现 `worst(D)`：伪成员、不参与计数、数值取最宽成员。

这四项是 Step 4 定语法前必须回答的问题；第 3 项是 AC3 的唯一判据。

## 6. 复现

```bash
cd /home/ubuntu/llvm/llvm-dev
python3 tools/ssdl/family_expand.py tests/ssdl/family-mini.ssdl --check-coverage
python3 tools/ssdl/family_expand.py tests/ssdl/sifive7-vlde.ssdl --check-coverage
python3 tools/ssdl/check.py   # 59/59
```
