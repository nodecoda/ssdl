# SSDL 直出 records：不经 TableGen 的等价性

- date: 2026-11-17
- 基线：`2026-11-16-ssdl-record-layer.md`、`2026-11-17-ssdl-family-expansion.md`
- 工具：`tools/ssdl/expand_records.py`、`equiv.py`

## 问题

`emit_td.py --records-json` 写出的是**定义视图**：一个 `def`、它的基类实例化、以及
源码显式绑定的字段。`llvm-tblgen -print-records` 写出的是**展开视图**：继承来的每个
字段、参数代入后的值、默认值，都在里面。

过去的等价性验证是：

```text
family.ssdl → emit_td → .td → llvm-tblgen → .rec → equiv 对比上游 .rec
```

这证明了“SSDL 能生成合法 TD”，但没有证明“SSDL 能直接生成正确的 records”——
中间那次 tblgen 展开仍是黑盒。

## 做法

`expand_records.py` 用同一份 `td_schema` 完成第二步展开，只实现调度 schema 依赖的
TableGen 语义：

| 语义 | 例子 |
|---|---|
| 类继承 | `WriteRes` → `ProcWriteResources` → 默认 `Latency = 1` |
| 模板参数（含默认值） | `ProcResource<2>` → `NumUnits = 2` |
| 类体内 `let` | `RISCVTuneFeature` → `let Implies = implied_features` |
| `NAME` | `ProcResourceUnits<!cast<ProcResourceKind>(NAME), num>` |
| `!cast<T>(x)` | 去掉 cast，保留 `x` |
| 匿名实例化 | 列表里的 `SchedVar<...>` 提成 `anonymous_N`，与 print-records 一致 |
| bit 打印 | `true/false` → `1/0`，与 print-records 一致 |

`equiv.py` 按后缀识别输入：`.json` 走 `expand_records`，其余走 `-print-records` 解析。
于是同一套比较逻辑可以同时吃两种产物：

```bash
equiv.py /tmp/riscv.rec flat.json --in-model SiFiveP800Model \
    --gen-model sifive_p870_dModel \
    --in-processor SIFIVE_P870_D --gen-processor sifive_p870_d
```

## 证据

14 个 RISC-V 调度模型全部 `records match`，且 `family_sweep.py` 现在对每个模型跑两次
比较：一次经 tblgen，一次直读 `flat.json`。两条路径都通过，才记为 `match`。

```bash
python3 tools/ssdl/family_sweep.py --records /tmp/riscv.rec \
    --out /tmp/ssdl-sweep-final --diagnose-only   # 诊断基线
python3 tools/ssdl/family_sweep.py --records /tmp/riscv.rec \
    --out /tmp/ssdl-sweep-final                  # 重跑完整等价性（含直读）
```

回归测试：`riscv_records_json_matches_without_tblgen`（14/14，缺 `/tmp` 产物时跳过）、
`records_json_expands_class_defaults`（类默认值/参数/bit 打印）。

## 边界

- 只展开调度 schema 用到的语义；`!if`、`!foreach`、`#` 字符串拼接等不在其中。
  未实现的构造会原样保留，并在 `equiv` 里表现为 diff，而不是被静默丢弃。
- 匿名记录的编号与上游不同；`equiv` 本来就不把 `anonymous_N` 当作契约的一部分。
- 这并不是要替换 tblgen。TD 文本仍是 `llvm-tblgen` 的输入；直读路径证明的是
  **records 是 SSDL 的一等产物**，TD 只是它的一种打印形式。
