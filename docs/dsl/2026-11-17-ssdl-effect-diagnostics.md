# SSDL 效果诊断（1.0）

> 目的：把 `effects.py` 里的消费者语义变成源码级诊断，让作者不必读 emitter
> 才知道某个字段到底影响谁。诊断独立于 lowering：`validate.py` 和
> `emit_td.py` 共用同一套规则。

## 级别

| 级别 | 含义 | 默认输出 |
|---|---|---|
| `warning` | 有真实效果，但不在源码暗示的子系统里 | 是 |
| `note` | 有用的上下文，不是问题 | 否（`--verbose`） |
| `error` | 构造不可能产生它看起来要的效果 | 是，并使退出码非 0 |

所有诊断末尾带 `[Class.Field]`，可直接查：

```bash
python3 tools/ssdl/td_schema.py --field ProcWriteResources.RetireOOO
```

## 规则

### `machine`

| 构造 | 级别 | 说明 |
|---|---|---|
| `loop_micro_op_buffer` | warning | 只是 TTI unroll 阈值，MachineScheduler 和 llvm-mca 都不读 |
| `load_latency` / `high_latency` / `mispredict_penalty` | note | CodeGen 回退/启发式；llvm-mca 忽略 |
| `post_ra_scheduler` | note | 只控制 legacy post-RA 调度器 |
| `enable_intervals` | note | 只影响 CodeGen 的 chip-level intervals |
| `complete_model` | note | TableGen 完整性门；运行时断言仅 debug |
| `full_inst_rw_overlap_check` | note | 仅 TableGen 校验，从不发射 |
| `unsupported_features` | note | 只过滤完整性检查，不删除 sched class |
| 只有 `itineraries` | warning | llvm-mca 拒绝 itinerary-only CPU |
| `itineraries` + sched 表 | note | CodeGen 先查 itinerary；llvm-mca 忽略它、用 sched model |

### `bind`

| 构造 | 级别 | 说明 |
|---|---|---|
| `retire_ooo` | warning | 只有 llvm-mca 读；CodeGen 从不读 |
| `acquire` | note | 只有 CodeGen 读；llvm-mca 固定 acquire=0 |
| `single` | note | `begin_grp + end_grp` 的语法糖，无独立运行时字段 |
| `unsupported` | note | 标记 class 无效：CodeGen 回退，llvm-mca 硬报错 |
| `latency > 1` 且省略 `release` | note | release 默认每资源 1 cycle，不是 latency |

### `map` / `variant`

| 构造 | 级别 | 说明 |
|---|---|---|
| `map ... : unsupported = true` | warning | `InstRW.Unsupported` 是死字段，设置无效果；应标记 bound write |
| `variant`/`read_variant` case 使用 `pseudo`/`non_portable` | warning | llvm-mca 把这些原子展开为 false，静默走 default variant |

### 谓词与 STI

| 构造 | 级别 | 说明 |
|---|---|---|
| `sti_decl` 默认 `expand_for_mc = true` 且 predicate 使用 `pseudo`/`non_portable` | warning | MC 版本把它们展开为 false；设 `expand_for_mc = false` 或用可移植谓词 |
| `dep_breaking_class` 的 broken-deps 为空 | note | 空列表表示所有显式输入操作数都独立 |
| `opcode_mask = true` 但没有带显式 operand 的 `dep_breaking_class` | note | mask 只会被清空 |
| `predicate_prolog` | note | 只拼进 `resolveSchedClass`；MCA variant 解析看不到其符号 |
| `tii_predicate` 使用 `pseudo`/`non_portable` | warning | MC helper 展开为 false，只有 MachineInstr 调用者能看到 |

### `feature`

| 构造 | 级别 | 说明 |
|---|---|---|
| `kind = extension` 带 `version`/`bitmask` | note | 版本/bitmask 只在 RISCVISAInfo/TargetParser；MC 只看到 bool bit |
| `kind = tune` 带 directive | note | directive 由 clang driver/TargetParser 消费，不是 RISCVSubtarget |

## 用法

```bash
# 只报 warning/error
python3 tools/ssdl/emit_td.py model.ssdl -o model.td

# 连 note 一起看
python3 tools/ssdl/emit_td.py model.ssdl -o model.td --verbose
python3 tools/ssdl/validate.py model.ssdl --verbose
```

库调用：

```python
from ssdl import diagnose, parser
findings = diagnose.diagnose(parser.parse_file("model.ssdl"))
```

`emit_td.emit(..., findings=[])` 会把 warning/note 追加到调用方列表；error
findings 会进入 `SSDLEmitError`。

## 与 `effects.py` 的关系

诊断不复制语义：每条规则只引用一个 `Class.Field`，消息和证据都来自
`effects.py`。因此改审计结论只改一处；`check.py` 的 `effect_*` 检查保证
键、证据行和角色一致。

## 输出聚合

同一 `(level, message, effect)` 只打印一行，首次出现的位置保留，末尾附 `(xN)`。
真实模型会在几千个 write 上设置同一个字段（例如 `acquire`），逐站点打印没有信息量。

## 真实模型基线

`data/riscv-sched-diagnostics.csv` 固定了 14 个 RISC-V 调度模型的诊断计数。
`--diagnose-only` 直接读作者形态的 `family.ssdl`（入口自动展开）；旧目录只有
`flat.ssdl` 时回退，两者计数相同。

```bash
python3 tools/ssdl/family_sweep.py --records $SSDL_CACHE/riscv.rec \
    --out $SSDL_CACHE/sweep-final --diagnose-only \
    --diagnostics-table data/riscv-sched-diagnostics.csv
```

| 结论 | 证据 |
|---|---|
| 0 个 error | 没有模型写出“不可能生效”的构造 |
| 1 个 warning | `XiangShanNanHuModel` 的 `LoopMicroOpBufferSize = 48`：TD 注释写的是“Instruction queue size”，但唯一消费者是 TTI 的 unroll 阈值，MachineScheduler / llvm-mca 都不读 |
| note 主体是 `AcquireAtCycles` 和 `Unsupported` | `acquire` 只在 CodeGen 的 hazard/scheduler 里生效，MCA 直接建 `CycleSegment(0, release)`；`unsupported` 让 CodeGen 回退但让 MCA 硬报错 |

这些数字随上游模型变化；`riscv_model_diagnostics_match_the_committed_table`
会在 sweep 目录存在时重新生成并比对，表格过期即失败。

## 族源

入口按 `kind = dimension` 标记自动展开 v1.2 族源再诊断（`parser.load_source`），
所以 `family.ssdl` 与它的 `flat.ssdl` 产生完全相同的 finding。
