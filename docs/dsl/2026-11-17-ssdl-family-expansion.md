# 族/维度展开器与无损验证（Step 3）

- date: 2026-11-17
- status: **Implemented + pilot verified（全模型重生成待完成）**
- 工具：`tools/ssdl/family_expand.py`
- 测试：`check.py` 49/49
- 源：LLVM `700e551714cc`

## 1. 实现了什么

`family_expand.py` 是压缩 SSDL → v1.0 扁平 SSDL 的前端。扁平输出仍走 `emit_td.py`，后端一行未改。

新增两个领域构造（不新增顶层关键字）：

```text
dimension LMUL = [MF8, MF4, MF2, M1, M2, M4, M8];
dimension NF   = 2..8;

machine siFive7(vlen: INT) {
    write  WriteVLDE[LMUL];
    bind   WriteVLDE[LMUL] -> [VCQ, VL], latency = vlen / 128;
}

machine sifive-7-series : siFive7(512) {
    resource VCQ : units = 1;
    resource VL  : units = 1;
}
```

规则：

| 构造 | 语义 |
|---|---|
| `dimension D = [a, b]` | 声明有限轴；只有已声明的轴可被索引 |
| `dimension D = a..b` | 整数范围（`NF = 2..8`） |
| `W[D]` | 展开为 `W_a`、`W_b` …，一个成员一条语句 |
| `W[D in [a]]` | **稀疏成员**：只展开列出的成员 |
| `machine F(p: INT)` | 族声明；参数可用于数值表达式 |
| `machine X : F(512)` | 实例化；族体 + 实例体合并展开 |
| `latency = vlen / 128` | 有界整数表达式（`+ - * / // %`、括号、参数） |

**不支持**：字符串插值（`"W_" # mx`）、递归、自定义函数。维度必须保持可枚举，这是与 TableGen `defm` 的分界线。

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
dimension LMUL = [MF8, MF4, MF2, M1, M2, M4, M8, WorstCase];
machine vlde {
    resource VLEN512SiFive7VCQ : units = 1;
    resource VLEN512SiFive7VL  : units = 1;
    write WriteVLDE[LMUL];
    bind WriteVLDE[LMUL in [MF8, MF4, MF2]] -> [...], release = [1, 2], ...;
    bind WriteVLDE[LMUL in [M1]]              -> [...], release = [1, 3], ...;
    ...
    bind WriteVLDE[LMUL in [M8, WorstCase]]   -> [...], release = [1, 17], ...;
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

对 SiFive7VLEN512 的 4246 条 `bind` 做分组实测：

| 分组 | 数量 |
|---|---:|
| 维度后缀 base（去掉 `_<LMUL>` / `_WorstCase`） | 463 |
| 其中载荷在成员间**完全相同** | 69 个 base / 265 条记录 |
| 载荷随成员变化（latency/release 是维度的函数） | 394 个 base / 3096 条记录 |
| 无维度后缀（标量/原子，扁平绑定） | 885 条 |

结论：**压缩的收益来自「公式一次、记录多次」，不是文本折叠**。265 条常量族可以一行压多行；3096 条变载荷族必须写成 `cycles(...)` 或成员表。仅靠字符串压缩无法达到计划里的 20×。

## 5. 剩余工作（Step 3 未完成项）

1. **全模型压缩源**：把 SiFive7VLEN512 的 3772 条显式 WR 全部写成 ~128 条族语句（对应上游 128 个 `WriteRes<` 位置）。当前只完成了 `WriteVLDE` 子族。
2. **公式库**：`cycles(VLEN, LMUL, SEW, NF)`、`worst(LMUL)`、`legal_sew(LMUL)` 需要成为可复用投影，而不是每个族手写表。
3. **四组 `equiv.py`**：`SiFive7VLEN512/128X100/1024X300` + `Andes45Model` 全部 `records match`。当前只做了子族字段级比较。
4. **`WorstCase` 语义**：当前把它塞进 `LMUL` 成员列表；v1.2 应写成 `worst(LMUL)` 投影，且不参与成员计数。

这四项是 Step 4 定语法前必须回答的问题；第 3 项是 AC3 的唯一判据。

## 6. 复现

```bash
cd /home/ubuntu/llvm/llvm-dev
python3 tools/ssdl/family_expand.py tests/ssdl/family-mini.ssdl --check-coverage
python3 tools/ssdl/family_expand.py tests/ssdl/sifive7-vlde.ssdl --check-coverage
python3 tools/ssdl/check.py   # 49/49
```
