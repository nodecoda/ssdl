# SSDL — 调度与子目标描述语言

**Scheduling and Subtarget Description Language** · 表面 v2（语义 IR 已验证）

SSDL 是 LLVM 后端调度模型的领域专用语言：它不描述指令本身，只描述
**指令与微架构之间的运行时契约**——资源、延迟、读写推进、变体、特性、行程。

这是一个**学习项目**，不是上游提案。目标是把 TableGen 里被 `foreach` /
`defm` / `multiclass` 掩盖的领域模型抽出来，用一层没有语法噪音的语法重写，
再以 `llvm-tblgen -print-records` 为唯一 oracle，逐字段证明等价。
当前范围聚焦 RISC-V：**14 个调度模型全部记录级等价**。

---

## 0. 先跑起来

```bash
export SSDL_LLVM_SRC=/path/to/llvm-project          # LLVM 源码树
export SSDL_LLVM_BUILD=/path/to/llvm-build          # 含 bin/llvm-tblgen 的构建目录

python3 tools/ssdl/check.py                         # 160/160 checks passed
```

`check.py` 有两条硬性前置：`SSDL_LLVM_SRC` 指向一个 LLVM checkout，
且能找到 `llvm-tblgen`（`$SSDL_LLVM_BUILD/bin/llvm-tblgen`，或 PATH 上）。
缺任何一条都会立刻给出提示并退出 2，而不是静默跳过大半测试。

---

## 1. 为什么会有 SSDL

读 LLVM 的调度模型时，真正难的不是调度概念，而是 TableGen 的三层噪音：

1. **语法噪音**：一个 `SchedMachineModel` 被 `foreach`/`defm`/`multiclass`
   展开成几千条记录，源码里看不到最终的运行时契约。
2. **关键字无公开规范**：emitter 通过字符串绑定字段
   （`getValueAsInt("Latency")`），哪些字符串是关键字、哪些是普通字段，
   只存在于后端 C++ 的 hardcode 里。`tools/ssdl/td_schema.py` 把这份
   hardcode 还原成可检查的 schema。
3. **展开后不可读**：SiFive7VLEN512 有 3772 条显式 `WriteRes`，
   人无法逐条 review。

SSDL 的取舍是：**语义完整的替代品，不是更可读的替代品**。
6 个平级顶层关键字（IR 文本 9 个）、无 base/扩展概念、无元编程；压缩靠维度与参数，
展开后的显式声明仍然是可打印、可对拍的产物。

关键结论（`docs/dsl/2026-11-17-ssdl-direct-records.md`）：
**records 是 SSDL 的一等产物，TD 只是 records 的一种打印机。**

---

## 2. 一个表面：v2

**v2 是唯一表面语法**（6 个顶层关键字）。v1 已撤出表面，只保留为 **IR 文本**：
它是 `elaborate.render` 的输出、golden 的字节基线、`--syntax ir` 读回的格式，
和 `records.json` 同级，不是可以并行选用的第二套写法。决定记录见
`docs/dsl/2026-11-18-ssdl-one-surface.md`。

语义模型仍是同一份 —— 表面到 IR 只是一次文本转写，`SemProgram`、records
契约完全未动：

| v2 顶层关键字 | 覆盖的 IR 关键字 |
|---|---|
| `set instructions\|dimension\|relation` | `set`（三种 kind 显式化） |
| `predicate`（表达式 / `: select` / `: sti` / `: tii` / `prolog`） | `predicate`、`predicate_prolog`、`sti_decl`、`sti_function`、`tii_predicate` |
| `feature`（`kind = plain\|extension\|tune`） | `feature` |
| `machine both\|model\|cpu\|family\|instance\|alias` | `machine` |
| `itineraries` | `itineraries` |
| `extern fn` | 族展开的编译期查表 |

完整 EBNF 见 `docs/dsl/2026-11-17-ssdl-v2-grammar.md`；IR → v2 的逐条对照、
迁移工具和诚实边界见 `docs/dsl/2026-11-18-ssdl-ir-to-v2-migration.md`。
AArch64（31 模型）的可支持性普查、4 个缺口的关闭记录与 31/31 sweep
见 `docs/dsl/2026-11-18-ssdl-aarch64-coverage.md`。

工具链的 `--syntax` 只有两个取值：

| 取值 | 含义 | 用途 |
|---|---|---|
| `v2`（默认） | 表面语法 | 人写的模型 |
| `ir` | 内部 IR 文本 | 生成器输出、golden、对拍、调试 |

`--syntax v1` 会直接报错并提示改用 `ir`。夹具按层分目录：
`tests/ssdl/surface/` 是表面源，`tests/ssdl/*.ssdl` 是 IR 对拍孪生，
`tests/ssdl/ir-only/` 是没有表面写法的 IR 构造。

IR 文本的 9 个关键字（内部表示，仍然有效）：

| 关键字 | 作用 | 对应 TD |
|---|---|---|
| `set` | 具名集合 / 维度 / 机器参数 | `list<Instruction>`、`defvar` |
| `predicate` | 调度谓词（表达式）或选择/汇编谓词（块） | `MCInstPredicate`、`Predicate`、`AssemblerPredicate` |
| `predicate_prolog` | 谓词共享的 C++ 片段 | `PredicateProlog` |
| `feature` | 普通特性 / RISC-V 扩展 / 调优特性（`kind` 区分） | `SubtargetFeature`、`RISCVExtension`、`RISCVTuneFeature` |
| `sti_decl` | STI 谓词声明 | `STIPredicateDecl` |
| `sti_function` | STI 谓词实现（含等价类） | `STIPredicate` |
| `tii_predicate` | 指令选择谓词 | `TIIPredicate` |
| `machine` | 调度模型 / 处理器 / 别名 / 行程 | `SchedMachineModel`、`ProcessorModel`、`ProcessorAlias` |
| `itineraries` | 旧式行程表 | `ProcessorItineraries` |

IR 文本的 EBNF 见 `docs/dsl/history/2026-11-16-ssdl-v1.0-final.md`
（历史文档，已撤销表面地位）；维度的 IR 拼写见
`docs/dsl/history/2026-11-17-ssdl-v1.2-draft.md`；
身份/阶段/lowering 见 `docs/dsl/2026-11-18-ssdl-semantic-ir.md`。

一个最小模型（表面 v2）：

```text
machine both m {
    resource P { units = 2; }
    write W;
    bind W { resources = [P]; latency = 4; }
    map [ADD] { 0 = W; 1 = read_default; }
}
```

这四个要素的静态绑定、时间轴与吞吐公式见
`docs/dsl/2026-11-17-scheduling-model-elements.md`；
消费这些字段的调度算法见 `docs/dsl/2026-11-17-llvm-scheduling-algorithms.md`；
逐步调试方法见 `docs/dsl/2026-11-17-debugging-machine-scheduler.md`，
配套示例与一键脚本见 `docs/examples/sched-debug/`、`scripts/debug-sched-demo.sh`。

同一个 `machine` 也可以直接写成“族 + 实例”，把稀疏维度只写一次：

```text
set dimension LMUL = [MF8, M1, M8] { name_format = "_{value}"; }

machine family f(vlen: INT) {
    write W[LMUL];
    bind  W[LMUL] { resources = [P]; latency = vlen / 128; }
}

machine instance m : f(512) { }
```

读语法先看 `docs/examples/sifive7-family-tour-v2.ssdl`（约 150 行，手写走查，
每种构造一处）。整颗 SiFive7 的生成物是 `sifive7-family.ssdl`（IR）与
`sifive7-family-v2.ssdl`（表面，约 3700 行），只作 golden 基线，不逐行读；
文件角色见 `docs/examples/README.md`。

---

## 3. 一条链路：从 SSDL 到可对拍的 records

```text
model.ssdl ──parser──► IR text ──validate──► Program
   (v2 表面，先 to_ir 转写)                │
                                            ▼
                                             record_ir.RecordSet ◄── 一等产物
                                                       │
                              ┌────────────────────────┴───────────────────────┐
                              ▼                                                ▼
                    emit_td.py --records-json                        emit_td.py → .td
                              │                                                │
                              │                                   llvm-tblgen -print-records
                              ▼                                                ▼
                    expand_records.py（schema 展开）                       generated.rec
                              └────────────────── equiv.py ───────────────────┘
                                          upstream: $SSDL_CACHE/riscv.rec
```

每一环证明的东西不同：

- `family_expand`：压缩写法与展开写法是同一份语义（展开结果可重新解析）。
- `validate` / `diagnose`：覆盖性是错的就报错；效果诊断标出
  “仅 MCA 可见 / 仅 CodeGen 可见 / 死字段”。
- `emit_td`：records 表就是语义，TD 文本只是另一种打印。
- `equiv.py`（tblgen 路径）：SSDL 生成的 TD 能被 TableGen 接受，
  且展开后的调度语义与上游一致。
- `equiv.py`（直读路径）：上面的一致**不是** tblgen 帮忙凑出来的。

手工看一条链：

```bash
python3 tools/ssdl/emit_td.py tests/ssdl/sifive7-mini.ssdl \
    --attrs tests/ssdl/attrs.json --target-include SSDLStub.td \
    --records-json /tmp/mini.json
python3 tools/ssdl/expand_records.py /tmp/mini.json | head -40
```

---

## 4. 目录地图

```text
tools/ssdl/         解析器、records、TD 打印机、对拍与普查工具（纯标准库）
  parser.py         SSDL 文本 → Program
  semir.py          语义 IR：身份 / 阶段 / 声明
  elaborate.py      输入（v2 或 IR）→ SemProgram → IR 文本（拼写在 lowering 定落）
  family_expand.py  展开核：语句扫描、表达式、runtime 降级
  record_ir.py      RecordSet：SSDL 的一等产物
  emit_td.py        RecordSet → TD 文本 / records.json
  records.py        llvm-tblgen -print-records 的读取器
  td_schema.py      从 TD 声明抽取 schema（把后端字符串绑定变成可检查的契约）
  expand_records.py 用 schema 展开 records.json，无需 tblgen
  equiv.py          两套 records 的语义对拍
  census_sched.py    单个 target 的 SchedMachineModel 普查
  family_tables.py  从源码抽取 RISC-V 族与词表
  family_mine.py    记录 → 规范基 + 公式（只报告，不发明）
  family_sweep.py   14 个模型的全链路 sweep（--syntax v2|ir）
  to_v2.py          IR → v2 表面迁移（to_ir 的逆）
  check.py          160 项自检
tests/ssdl/         IR 对拍孪生；surface/ 是表面源；ir-only/ 是无表面写法的构造
data/               小基线：普查表、诊断表、profiles、族表、公式表
docs/dsl/           规范、领域模型、验证报告
docs/examples/      真实族源（sifive7-family.ssdl 是 IR，-v2.ssdl 是表面）
scripts/            复现缓存产物的脚本
```

`data/` 只存小文件；150MB 的 records dump 与 sweep 产物属于缓存，不入仓。

---

## 5. 环境变量与缓存

只认三个环境变量：

| 变量 | 含义 | 默认 |
|---|---|---|
| `SSDL_LLVM_SRC` | LLVM 源码树 | 无（必填） |
| `SSDL_LLVM_BUILD` | LLVM 构建目录（`bin/llvm-tblgen`） | 回退到 PATH 上的 `llvm-tblgen` |
| `SSDL_CACHE` | 非源码产物 | `~/.cache/ssdl` |

派生的缓存路径：

```text
$SSDL_CACHE/riscv.rec       上游 -print-records dump
$SSDL_CACHE/sweep-final/    14 模型 sweep 输出（check.py 会读取）
$SSDL_CACHE/mine/           family_mine.py 的报告
```

代码里没有任何绝对路径；全部经由 `tools/ssdl/paths.py`。

---

## 6. 复现

### 6.1 重新生成 records dump

```bash
scripts/fetch-records.sh 700e551714cc                        # riscv.rec
scripts/fetch-records.sh --target aarch64 700e551714cc       # aarch64.rec
```

脚本会在 `$SSDL_LLVM_SRC` 上开一个临时 worktree，检出指定 commit，
用 `$SSDL_LLVM_BUILD/bin/llvm-tblgen` 生成 `$SSDL_CACHE/<target>.rec`，
结束后清理 worktree。`data/*-sched-census.csv` 的表头记录了基线 commit。

### 6.2 重跑 14 模型 sweep（可选，耗时较长）

```bash
python3 tools/ssdl/family_sweep.py                          # 默认：mining → to_v2 → 表面路径
python3 tools/ssdl/family_sweep.py --syntax ir --out /tmp/ssdl-sweep-ir   # 直接走 IR
```

默认 `--syntax v2`：mining 产出 IR 文本后，用 `to_v2.py` 迁移成表面源，
再以 v2 前端走 validate / emit_td / 两条 equiv 对拍。**门禁结果：14/14
records match**，即“从表面源出发也能逐字段复现上游模型”。`--syntax ir`
是同一批模型的 IR 直达路径，用来把表面层的问题和语义层的问题分开。

AArch64 走直读路径（records → SSDL → records），同一套对拍：

```bash
python3 tools/ssdl/sweep_aarch64.py                     # 31 个模型，写 $SSDL_CACHE/aarch64-sweep
python3 tools/ssdl/sweep_aarch64.py --check             # 与 data/aarch64-sched-sweep.json 对比
```

**门禁结果：31/31 models records match**，`--records-json` 与
`llvm-tblgen` 两条路径都对拍；TD 路径用一份按模型引用合成的 stub
（指令/寄存器名是目标词表，不属于 SSDL），只为让 TableGen 做类型检查。

### 6.3 回归

```bash
python3 tools/ssdl/check.py                   # 160/160
python3 tools/ssdl/golden.py --check          # 24 expansions
```

`check.py` 的 160 项覆盖：语法/规范化、错误诊断、records 层、TD 打印、
schema 类型检查、关键字引用同步、14 模型对拍、31 模型 AArch64 sweep 表、
族展开、效果诊断。

---

## 7. 已知边界（语义层）

v1.2 的五条边界已逐条消除或契约化，完整说明见
`docs/dsl/2026-11-18-ssdl-semantic-ir.md` §4。仍然存在的限制：

1. **非 resource 的 model-scoped def 不自动加前缀**：`write_seq` 等仍共享
   TableGen def；两个 machine 用同一 local 名、不同成员时是硬错误。
2. **接口名默认显式**：被指令 `SchedRW` 引用的 write/read 名不做隐式改写。
3. **runtime 条件只覆盖标量字段**：`latency` / `uops` / `cycles`；
   列表字段需要逐元素变体，直接报错。
4. **没有语句级 `if`**：只有字段级 `?:`；`if` 是未实现的决策记录，不在 AC 内。
5. **relation 是有限表**：适合小表，没有一般公式；profile 的 `cycles` 行是
   手工钉住的样本，不是从上游重新推导。
6. **无元编程**：无 `foreach`、无递归、无用户函数。
7. **迁移器只搬语法**：`to_v2` 不保留注释，`tii_predicate X = return P;`
   没有 v2 写法（保留在 `tests/ssdl/ir-only/`）；裸 `machine X { ... }`
   在往返时会显式化成 `role = both;`（默认值，records 不变）。
8. **目标词表不在语言内**：`ADD`、`WZR`、`HasSVE` 属于目标 `.td`；AArch64
   sweep 的 `llvm-tblgen` 路径用合成 stub 提供这些名字，只验证类型与结构。
   `unsupported_features` 默认不转写（需要 host predicate defs，`--unsupported-features`）。

---

## 8. 文档索引

完整索引、状态（规范 / 实现记录 / 证据 / 历史）与维护约定见
`docs/dsl/README.md`；下表只列常用入口。

| 文档 | 内容 |
|---|---|
| `docs/dsl/README.md` | **文档索引与生命周期**：规范 / 实现记录 / 证据 / 历史 |
| `docs/dsl/2026-11-17-ssdl-v2-grammar.md` | **唯一表面规范**：EBNF、6 个顶层关键字、errata 处置、决策记录 |
| `docs/dsl/2026-11-18-ssdl-one-surface.md` | **v1 撤出表面的决定**：分层、CLI、夹具布局、保留项与代价 |
| `docs/dsl/2026-11-18-ssdl-ir-to-v2-migration.md` | IR → v2 迁移表、`to_v2`、14/14 门禁与工具边界 |
| `docs/dsl/2026-11-18-ssdl-semantic-ir.md` | **语义层**：身份/拼写、阶段、lowering、五条边界处置 |
| `docs/dsl/history/2026-11-17-ssdl-v1.2-verification.md` | 历史基线证据：14/14、直读路径、诊断基线 |
| `docs/dsl/history/2026-11-16-ssdl-v1.0-final.md` | 历史：IR 文本的 9 关键字 EBNF（已撤销表面地位） |
| `docs/dsl/history/2026-11-17-ssdl-v1.2-draft.md` | 历史：维度、机器参数、族与实例的 IR 拼写 |
| `docs/dsl/2026-11-16-ssdl-domain-model.md` | 语义层 / 策略层 / ABI 层 |
| `docs/dsl/2026-11-17-ssdl-direct-records.md` | 为什么 records 是一等产物 |
| `docs/dsl/2026-11-17-ssdl-effect-diagnostics.md` | 字段效果诊断与证据 |
| `docs/dsl/2026-11-17-ssdl-family-expansion.md` | 族展开规则 |
| `docs/dsl/2026-11-17-riscv-sched-census.md` | RISC-V 调度结构普查 |
| `docs/dsl/2026-11-17-riscv-sched-families.md` | 族与词表的抽取方法 |
| `docs/dsl/2026-11-18-ssdl-aarch64-coverage.md` | **AArch64 已支持（Verified）**：31 模型普查、4 个缺口关闭、31/31 sweep、诚实边界 |
| `docs/dsl/2026-11-16-ssdl-keyword-reference.md` | 自动生成的关键字参考（勿手改） |

---

## 9. 状态

学习项目，无上游兼容承诺，也不打算拆分为多包或发布到 PyPI。
唯一的验收标准是：**所声明的语义，能在 records 层被逐字段证明**。
