# SSDL 语义 IR：身份/拼写分离与完整表达式

- date: 2026-11-17（原 1.3 里程碑）
- 状态：完成（AC1–AC11）
- 命名：语义层不带版本号。表面只有一个（v2），IR 只有一份；"1.3" 是里程碑
  编号，不是可以与表面并列的版本。
- 基线：`docs/dsl/history/2026-11-17-ssdl-v1.2-verification.md`
- 证据（里程碑当时的基线）：`check.py` **102/102**、`golden.py --check` **22/22**；
  当前门禁数字见 `README.md` 与 `docs/dsl/README.md`

## 0. 一句话

这一版把实现从“文本行展开 + records 即真值”改为：

```text
surface syntax → AST → elaborator（身份/阶段/拼写）→ semantic IR → TD records
```

records 仍是唯一的外部 oracle，但不再是语言的定义域。它是语义 IR 的一次
**可验证投影**：同一个 IR 可以打印成 IR 文本、records 表或 TD 文本。

## 1. 三个概念

### 1.1 身份与拼写分离

`Symbol(scope, kind, name, axes)` 是声明身份（`tools/ssdl/semir.py:47`）；
`Decl.spelling` 是 lowering 拼写（`semir.py:78`）。拼写不是身份的一部分。

```text
resource P : units = 1;                    # identity: machine small / resource P
                                           # spelling: small_P（仅当多个 machine 声明同名 P）
resource P : units = 1, name = "PipeA_{vlen}";
                                           # 显式模板；指令在渲染后剥除
```

规则由 `elaborate._spellings` 执行（`tools/ssdl/elaborate.py:134`）：

- 同一 local 名只被一个 machine 使用时，拼写保持源名（单实例输出不变）；
- 被多个 machine 使用时默认 `<machine>_<local>`；
- `name` 模板可覆盖，但必须在有限域上单射，冲突同时打印两个身份；
- `bind` 目标、`load_queue`/`store_queue` 的 `resource =` 引用随拼写重写。

**例外不是漏网**：`SchedWrite`/`SchedRead` 是 target-level 词表。
它们写在 machine 块里只是为了让源文件局部，TableGen 只发射一次
（`tools/ssdl/emit_td.py:713`），所以两个实例共享 `W_A` 的拼写是
**一个词表条目**，不是两个身份。model-scoped 的部分（`WriteRes`、
`SchedWriteVariant`）仍按实例命名。

### 1.2 阶段：ELAB 与 RUNTIME

`Phase` 定义在 `semir.py:23`：

| 阶段 | 来源 | 允许出现的位置 |
|---|---|---|
| `ELAB` | 族参数、轴成员、`kind = relation` 表、`extern fn` 投影 | 任何位置；展开期完全折叠 |
| `RUNTIME` | 指令谓词、subtarget 谓词 | 仅 `bind` 的 `latency` / `uops` / `cycles` 标量字段 |

`?:` 的条件是 ELAB 时在展开期折叠（`family_expand._eval_expr`，
`family_expand.py:260`、`_split_ternary` `:189`）。条件是 RUNTIME 时走定向
lowering（`_lower_runtime_bind`，`family_expand.py:1128`）：

```text
bind W -> [P], latency = opcode([ADD]) ? 4 : 7;
```

展开为：

```text
write W__case0; write W__case1;
bind W__case0 -> [P], latency = 4;
bind W__case1 -> [P], latency = 7;
variant W { case opcode([ADD]): [W__case0]; default: [W__case1]; }
```

结构性位置（machine 属性、资源字段、名字）遇到 RUNTIME 条件直接报错，
错误文本给出两条出路：改成族参数，或绑定 host profile（`family_expand.py:1237`）。
列表字段上的 RUNTIME 条件也明确拒绝：记录模型没有“逐元素变体”。

### 1.3 降级透明

每个糖只有一条 desugaring，并且可以用 `elaborate.py` 打印核心形式：

```bash
python3 tools/ssdl/elaborate.py tests/ssdl/two-instances-runtime.ssdl
python3 tools/ssdl/elaborate.py tests/ssdl/two-instances-runtime.ssdl --symbols
```

aux 拼写是 subject 与分支序号的纯函数（`<subject>__case0/1`）。源文件若
占用了同一拼写，展开期硬错误并指出两行（`generated_aux_name_cannot_shadow_a_source_declaration`，
`tools/ssdl/check.py:2399`）。`variant` 与 `WriteRes` 由 emitter 加实例前缀
（`def shallow_W_AVariant` / `def shallow_WR_W_A__case0`）。

## 2. v1.2 五条边界的处置

| v1.2 边界 | 1.3 状态 | 机制与证据 |
|---|---|---|
| 多实例族资源命名 | **消除** | 身份/拼写分离；`tests/ssdl/two-instances.ssdl`；`check.py:2457` |
| 没有条件表达式 | **elab 消除，runtime 定向降级** | `?:` 折叠 + aux/variant lowering；`check.py:2348` |
| 多行语句 | **消除** | 语句边界只由深度 0 的 `;`/`{}` 决定（`_scan_logical_lines`，`family_expand.py:407`）；`tests/ssdl/multiline.ssdl` |
| 投影属于 host | **契约化** | `kind = relation`（`RELATION_RE`，`family_expand.py:59`）+ `extern fn`（`:74`）+ `data/riscv-sched-profiles.json` |
| 匿名记录编号 | **消除（发射侧）** | 辅助记录不再用 `anonymous_N`；`-print-records` 仍会为 TableGen 内联类编号，`equiv` 归一 |

`projection-contract.ssdl` 钉住两条查表路径：`DivFactor(8)=1`、
`DivFactor(16)=15`、`cycles(1,8)=64`、`cycles(1,16)=32`。relation 与 extern
都从 flat 输出消失（编译期契约，不进入 records）。

## 3. 确定性、归一与比较

- **确定性**：同一源连续两次展开，flat 文本与 `Decl.spelling` 完全一致
  （`check.py:2348`）。
- **重排不变**：声明顺序改变文本，但不改变 `equiv` 判定
  （`check.py:2419`）。
- **前缀归一**：`equiv.strip_prefix`（`tools/ssdl/equiv.py:180`）同时折叠
  `defm` 的无分隔拼接（`VLEN512SiFive7PipeA`）和 SSDL 的 `_` 拼接
  （`small_PipeA`）；CLI 是 `--strip-prefix`（in-tree）与
  `--gen-strip-prefix`（generated），均可重复。归一作用于 key、WriteRes 的
  `ProcResources` 列表和 ReadAdvance 的 `ValidWrites`。
- **身份优先**：write/read/variant 仍优先用 `WriteType`/`ReadType`/`MatchRW`
  匹配，record 名只是兜底；`anonymous_N` 仍由 `canon_name` 归一。
- 验证：`instance_prefixes_are_normalized`（`check.py:2457`）证明
  `small_P` 与 `big_P` 在前缀折叠后是同一个资源契约。

## 4. 仍然存在的限制（诚实边界）

1. **非 resource 的 model-scoped def 仍可能撞名**：`write_seq` 的定义体
   （成员列表）是全局的。两个 machine 用同一 local 名、不同成员时，emitter
   报“declares it differently”（`emit_td.py:771`），而不是自动加前缀。
   目前只有 resource/res_group 进入 `_spellings` 的自动前缀。
2. **接口名默认显式**：被指令 `SchedRW` 引用的 write/read 名不做隐式改写；
   `write` 上的 `name = "..."` 模板尚未实现。这是有意的 stringly-typed 防护。
3. **runtime 条件只覆盖三个标量字段**：`latency`/`uops`/`cycles`。
   `release`/`acquire` 等列表字段需要逐元素变体，记录模型无法表达，直接报错。
4. **没有语句级 `if`**：1.3 只有字段级 `?:`。决策记录允许以后加 elab-only
   的 `if`，但它不在本轮验收标准内，因此没有实现。
5. **relation 是有限表**：适合小表（如 SEW → div factor）；没有一般公式。
   profile 的 `cycles` 行是手工钉住的样本（VLEN512 / M1），不是从上游重新推导。
6. **无元编程**：仍无 `foreach`、无递归、无用户函数。展开必须全域且可判定。
7. **`validate.py` 不重复 relation/extern 检查**：展开期已经强制；静态检查
   器只做它自己的覆盖性/效果诊断。
8. **不是上游提案**：学习项目，public repo 只为复现与讨论。

## 5. 复现

```bash
export SSDL_LLVM_SRC=/path/to/llvm-project
export SSDL_LLVM_BUILD=/path/to/llvm-build

python3 tools/ssdl/golden.py --check            # 24/24（当前）
python3 tools/ssdl/check.py                     # 154/154（当前）
python3 tools/ssdl/elaborate.py tests/ssdl/two-instances-runtime.ssdl
python3 tools/ssdl/elaborate.py tests/ssdl/two-instances-runtime.ssdl --symbols
python3 tools/ssdl/elaborate.py tests/ssdl/projection-contract.ssdl
```

## 6. 文件索引

| 关注点 | 位置 |
|---|---|
| 语义 IR | `tools/ssdl/semir.py` |
| elaborator / 拼写 | `tools/ssdl/elaborate.py` |
| 语句扫描 | `tools/ssdl/family_expand.py:407`（`_scan_logical_lines`） |
| 表达式与阶段 | `family_expand.py:189`、`:260` |
| runtime 降级 | `family_expand.py:1128`（`_lower_runtime_bind`） |
| relation / extern | `family_expand.py:59`、`:74`、`:656` |
| 等价归一 | `tools/ssdl/equiv.py:180` |
| golden | `tools/ssdl/golden.py`、`data/ssdl-golden.sha256` |
