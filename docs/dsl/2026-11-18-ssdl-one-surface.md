# SSDL 只有一个表面：撤销 v1 文法

- date: 2026-11-18
- status: **已执行**（records 契约未动，四套门禁全绿）
- 决定：**v2 是唯一表面语法；v1 文本降级为 IR，不再是语言层。**

---

## 0. 一句话

以前是「两套语法、一份语义」，现在把第一层砍成一条线：

```text
表面   v2  ──to_ir──►  IR 文本  ──expand/elaborate──►  SemProgram  ──►  records
        └── 唯一的文法 ──┘   └── 内部表示，没有文法地位 ──┘
```

IR 文本仍然有 EBNF（它要能被读回来），但它的身份是**序列化格式**，
和 `records.json` 同级，不是"另一种可以拿来写模型的语法"。

---

## 1. 为什么这不是"删代码"

语义内核本来就只有一份（`family_expand` + `elaborate` + `emit_td`），
v2 一直只是 `syntax_v2.to_ir` 的纯文本前端。所谓"两套语法"是**定位问题**，
不是实现问题：

| 维度 | 之前 | 现在 |
|---|---|---|
| 文法文档 | v1.0/v1.1/v1.2 + v2，并列 | 只有 `2026-11-17-ssdl-v2-grammar.md` |
| 旧文法文档 | 与 v2 同级 | `docs/dsl/history/`，标题带"已撤销" |
| CLI | `--syntax v1` 是默认 | 默认 `v2`；`v1` 直接报错并提示用 `ir` |
| 测试夹具 | `tests/ssdl/*.ssdl` 被当作示例 | `surface/` 是示例；根目录是 IR 对拍孪生 |
| 旧构造 | `v1-compat/` | `ir-only/`（没有表面写法的 IR 构造） |
| 生成器输出 | "v1 源" | "IR 文本"（`from_records.py` 的产物） |

**没有删掉一行语义代码，也没有改 records 契约。** 改的是默认值、命名和文档层级。

---

## 2. 代码落点

### 2.1 两个层，两个取值

`tools/ssdl/parser.py`：

```python
SURFACE = "v2"
IR = "ir"
LAYERS = (SURFACE, IR)

def coerce_layer(value: str) -> str:
    if value == "v1":
        raise argparse.ArgumentTypeError(
            "v1 is no longer a surface syntax; pass 'ir' for the internal IR text"
        )
```

所有工具的 `--syntax` 都改用它，默认 `v2`：

```text
elaborate.py  emit_td.py  validate.py  family_sweep.py
```

`--syntax v1` 的报错是**有意的教学错误**，而不是静默兼容：

```console
$ python3 tools/ssdl/validate.py model.ssdl --syntax v1
validate.py: error: argument --syntax: v1 is no longer a surface syntax;
  pass 'ir' for the internal IR text
```

### 2.2 库层默认值是 IR，不是表面

`parser.parse` / `parse_file` / `expand_source*` 的默认仍是 IR：

```python
def parse(text: str, syntax: str = IR, source_map=None) -> Program:
```

理由：这些是内部 API，上游调用者（golden、family_mine、from_records、
154 项回归）处理的本来就是 IR。**表面默认只出现在用户入口（CLI）**。
库层传错层会在解析阶段报错，不会静默改写语义。

### 2.3 打印器就是 IR 定义

```python
# tools/ssdl/elaborate.py
def render(program) -> str:
    """Print the program as flat IR text."""
```

`render` 不再是"v1.0 的打印器"，而是 IR 文本的**定义**：golden 钉的是它的
字节，`--syntax ir` 读回的是它的输出，diff 看的是它。

---

## 3. 文件布局

```text
tests/ssdl/
  *.ssdl               IR 对拍孪生（--syntax ir）
  surface/*.ssdl       v2 表面源（人写）
  ir-only/*.ssdl       没有表面写法的 IR 构造（如 tii_predicate 的裸 return）
  README.md            三个桶的说明

docs/dsl/
  2026-11-17-ssdl-v2-grammar.md     唯一文法
  2026-11-18-ssdl-semantic-ir.md      语义层（不带版本号）
  2026-11-18-ssdl-one-surface.md    本次决定（本文）
  history/                          已撤销的表面文法
```

---

## 4. 保留的东西（以及为什么）

| 保留 | 理由 |
|---|---|
| `syntax_v2.to_ir` | 表面到 IR 的唯一转写（名字已与语义一致） |
| `to_v2.py` | IR 生成器（`family_mine` / `from_records`）产出的仍是 IR，需要一条到表面的路 |
| golden 字节哈希 | 钉住 IR 投影的稳定性；这是 IR 作为序列化格式的核心契约 |
| `family_expand` 的族语法 | 生成器的目标格式；人不再手写，但它必须可读回 |
| `tests/ssdl/ir-only/` | 没有表面写法的构造，防止实现静默腐烂 |

## 5. 判据

按本项目的三条判据（表达力、心智负担、领域命名）：

- **表达力**：不变。表面上能写的东西一个没少，因为表面还是 v2。
- **心智负担**：减少一层。新人不需要先学 v1 才能读测试或读生成器输出，
  只需要知道"根目录那些是 IR"。
- **领域命名**：更准。`feature kind = extension`、`machine family/instance`
  这些才是领域概念；`bind W -> [P], latency = 1` 是它的序列化。

## 6. 已知代价

1. **默认值切换是一次不兼容变更**：所有旧命令（`emit_td.py x.ssdl`）现在
   需要 `--syntax ir`。仓内调用点已全部更新；仓外没有调用者。
2. **命名迁移已完成**：`to_v1` → `to_ir`，`v1_to_v2` → `ir_to_v2`，
   语义层文档不再带版本号。历史文档（`docs/dsl/history/`）保留当时的
   字样，不是现行用法。
3. **历史文档不重写**：`docs/dsl/2026-11-16-ssdl-*` 是对当时实现的记录，
   保留原文，只在标题处标注状态。
