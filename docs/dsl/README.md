# docs/dsl 索引与文档生命周期

这个目录同时存**规范**、**实现记录**和**历史分析**。读之前先看状态，
不要把历史文档里的语法当成现在可以写的东西。

状态口径：

| 状态 | 含义 | 维护规则 |
|---|---|---|
| **规范** | 当前唯一可写语法 / 语义契约 | 改语法必须先改 `tools/ssdl` 与 `check.py`，再改这里 |
| **实现记录** | 代码为什么这样写、边界在哪 | 行为变化时同步更新 |
| **证据** | 可复算的普查/对拍结果 | 换基线 commit 时重跑并改数字 |
| **历史** | 已撤销或仅存档 | 只读，不再更新；语法以规范为准 |
| **自动生成** | 由工具产出 | 不手改，按文件头命令重生成 |

## 当前规范（先读这两份）

| 文档 | 状态 | 回答什么 |
|---|---|---|
| `2026-11-17-ssdl-v2-grammar.md` | **规范** | v2 表面语法：6 个顶层关键字、EBNF、维度与族、errata |
| `2026-11-18-ssdl-semantic-ir.md` | **规范** | 语义 IR：身份/拼写分离、阶段、lowering、records 契约 |

## 实现记录

| 文档 | 状态 | 回答什么 |
|---|---|---|
| `2026-11-18-ssdl-one-surface.md` | 实现记录 | 为什么 v1 撤出表面、IR 与表面的分层、夹具布局 |
| `2026-11-18-ssdl-ir-to-v2-migration.md` | 实现记录 | 每个 IR 构造在 v2 里怎么写，`to_v2` 的边界 |
| `2026-11-16-ssdl-domain-model.md` | 实现记录 | 每个语法元素在上游的消费点（语义/策略/ABI 层） |
| `2026-11-16-ssdl-record-layer.md` | 实现记录 | 为什么 records 是一等产物，TD 只是打印器 |
| `2026-11-16-ssdl-record-loader-design.md` | 实现记录 | 直读 record 表的设计与边界 |
| `2026-11-17-ssdl-direct-records.md` | 实现记录 | 不经 TableGen 的等价性怎么证明 |
| `2026-11-17-ssdl-family-expansion.md` | 实现记录 | 族/维度展开规则、发射器、14 模型对拍 |
| `2026-11-17-ssdl-effect-diagnostics.md` | 实现记录 | 字段效果诊断：仅 MCA / 仅 CodeGen / 死字段 |
| `2026-11-17-ssdl-v2-grammar-review.md` | 实现记录 | v2 文法的编译器视角评审（`to_v1` 已改名 `to_ir`） |

## 证据（可复算）

| 文档 | 状态 | 回答什么 |
|---|---|---|
| `2026-11-17-riscv-sched-census.md` | 证据 | RISC-V 调度模型的结构普查 |
| `2026-11-17-riscv-sched-families.md` | 证据 | 族与词表如何从源码抽取 |
| `2026-11-18-ssdl-aarch64-coverage.md` | 证据 | AArch64 31 模型可支持性与 31/31 sweep |

## 领域与算法（读模型时配套）

| 文档 | 状态 | 回答什么 |
|---|---|---|
| `2026-11-17-scheduling-model-elements.md` | 说明 | 调度模型四要素：资源、延迟、释放、读推进 |
| `2026-11-17-llvm-scheduling-algorithms.md` | 说明 | MachineScheduler / llvm-mca 如何消费这些字段 |
| `2026-11-17-debugging-machine-scheduler.md` | 说明 | 一步一步调试调度器 |

## 历史（只读）

| 文档 | 状态 | 说明 |
|---|---|---|
| `2026-11-16-ssdl-review.md` | 历史 | 对 v1.0 Final 的评审；其中的 `set dimension`/9 关键字不是现行表面 |
| `2026-11-16-ssdl-parser-notes.md` | 历史 | v1.1 解析器与最小展开器实现记录 |
| `2026-11-17-ssdl-syntax-inventory.md` | 历史 | v1→v2 迁移前的语法盘点（基线 102/102） |
| `history/2026-11-16-ssdl-v1.0-final.md` | 历史 | IR 文本的 9 关键字 EBNF 来源 |
| `history/2026-11-17-ssdl-v1.1-draft.md` | 历史 | v1.1 草案 |
| `history/2026-11-17-ssdl-v1.2-draft.md` | 历史 | 维度、机器参数、族与实例的 IR 拼写来源 |
| `history/2026-11-17-ssdl-v1.2-verification.md` | 历史 | v1.2 基线证据 |

## 例子

| 文件 | 角色 |
|---|---|
| `../examples/sifive7-family-tour-v2.ssdl` | **阅读入口**：~150 行手写语法走查 |
| `../examples/sifive7-family.ssdl` | 整颗 SiFive7 的 IR 生成物（golden 基线） |
| `../examples/sifive7-family-v2.ssdl` | 同一模型的表面生成物（字节等价） |
| `../examples/README.md` | 三个文件的分工与校验命令 |

## 自动生成

| 文档 | 状态 | 说明 |
|---|---|---|
| `2026-11-16-ssdl-keyword-reference.md` | 自动生成 | 后端 TD 关键字参考；文件头有重生成命令 |

## 维护约定

1. 语法只有一个规范来源：`2026-11-17-ssdl-v2-grammar.md`。历史文档里的
   EBNF 只解释来源，不作为写法依据。
2. 改语法或语义时，顺序是：`tools/ssdl` + `tests/ssdl` + `check.py` →
   规范文档 → 证据/实现记录里的数字。四套门禁全绿才算完成。
3. 示例只有两份且必须同步：`docs/examples/sifive7-family.ssdl`（IR）
   与 `-v2.ssdl`（表面）。`check.py` 断言后者等于 `to_v2(前者)`。
4. 新文档带 `status:` 头，并登记到本索引；撤销的文档移到 `history/` 或
   标为历史，不留在“当前”区。
