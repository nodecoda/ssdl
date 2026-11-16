# SSDL 记录层：record 是契约，`.td` 是打印器

> 2026-11-16。承接 `2026-11-16-ssdl-parser-notes.md` §17（全量 SiFive7 等价）与
> `2026-11-16-ssdl-v1.1-draft.md` §11（实现状态）。
> 这一轮回答一个架构问题：SSDL 的产物到底是 `.td` 文本，还是 TableGen 的 record 表？

## 0. 一句话

TableGen 的设计逻辑是 **schema → record → 后端按字符串绑定 record**；SSDL 替换掉第一层，
所以它的直接产物就是 **record 表**。我们之前"先生成 TD 再让 tblgen 读"只是走了一次
`RecordKeeper` 的正门，不是语义上的必经之路。

## 1. TableGen 的三层，以及 SSDL 站在哪一层

```text
        TableGen                          SSDL
  ┌──────────────────┐            ┌──────────────────────────┐
  │ 1. schema        │            │ 语义模型（9 个顶层关键字）│
  │   class / def    │  ← 替换 →  │ 机器、资源、绑定、谓词……  │
  │   （语法即声明）  │            │                          │
  ├──────────────────┤            ├──────────────────────────┤
  │ 2. record 表     │  ← 同一个 →│ RecordSet（本轮新增）     │
  │   name+class+值  │            │ name + bases + fields     │
  ├──────────────────┤            ├──────────────────────────┤
  │ 3. emitter       │  ← 不变 →  │ （宿主自己的后端）        │
  │  getValueAs*("…")│            │  按字符串绑定到字段        │
  └──────────────────┘            └──────────────────────────┘
```

关键点不是"又一种配置语法"，而是：TableGen 把**领域模型的语法实现本身**声明化了。
第 1 层（`class SchedWriteRes<...> { int Latency = 1; }`）既是语法定义，也是领域模型；
第 2 层是它的实例化；第 3 层只能通过字符串去摸第 2 层，**没有任何类型关系**——
`getValueAsInt("Latency")` 与 `int Latency` 之间对编译器来说是两个不相关的名字。

这正是"写 TD 只能复制粘贴"的根因：语义在 TD 里，绑定在 C++ 里，中间没有可检查的接口。
SSDL 把它拆成两件可检查的东西：**语义模型**（9 个关键字，`v1.0-final`）和
**record 表**（本轮的产物）。`.td` 文本只是这张表的一种打印格式，一种 tblgen 恰好接受的正门。

## 2. record 表的最小模型

`tools/ssdl/record_ir.py`：

```python
Record(
    name,                     # def 的身份，后端按它找记录
    bases=[Base(cls, args)],  # 类实例化；多基类（如 RISCVExtension+Bitmask）是一等公民
    fields=[(name, value)],   # 后端按字符串读的字段；值保留 TD 表达式文本
    scope=None,               # `let SchedModel = M` 围栏；见 §6
    style="block",            # `{ ... }` 还是 `{ ... }` 内联
    suffix="",                # 少数要写 `};` 的位置
    origin="machine siFive7", # 出自 SSDL 的哪个构造，用于诊断
)
```

**值也有类型（`td_types.py`）**。`record_ir.py` 只保证"后端绑定的是哪个字段"，
另一半契约是"这个字段要什么类型的值"：字段值与位置参数被解析成 `Init` 树
（int / bit / string / code / list / dag / def 引用 / 类应用 / `?`），再对着
`td_schema.py` 解析出的声明检查——`int Latency` 拒绝 `"1"`，
`list<ProcResourceKind> ProcResources` 拒绝裸字符串，
`RISCVExtension<int, int, string, list<SubtargetFeature>, ...>` 拒绝把字符串推进 implies 槽。
标识符指向宿主记录（指令、目标自有谓词）时无法在此解析，计为 *external* 而不是猜。

`--records-json` 里存的就是这棵树，不是字符串：`{"kind": "int", "value": 1}`、
`{"kind": "list", "items": [{"kind": "ident", "name": "PipeA"}]}`。
所以宿主（或未来的 tblgen 加载器）拿到表就能直接用，不需要再实现一遍 TD 表达式解析。

```text
$ python3 tools/ssdl/td_types.py /tmp/v512.json \
      --source llvm/lib/Target/RISCV/{RISCVFeatures,RISCVProcessors}.td
12063 records, 53943 values checked, 93 external, 0 errors
```

把这份表里任意一条 `Latency = 3` 改成 `"3"`，同一命令立刻报
`…WR_….Latency: expected int, got "3"`。三个模型的完整数字：

| 模型 | 记录 | 检查的值 | external | 错误 |
|---|---|---|---|---|
| `SiFive7VLEN512Model` | 12063 | 53943 | 93 | 0 |
| `SiFive7VLEN128X100Model` | 12118 | 54431 | 94 | 0 |
| `SiFive7VLEN1024X300Model` | 12130 | 54516 | 95 | 0 |

顺带暴露了 `td_schema.py` 只在 target-independent 头文件上跑过的两个盲点：
形参默认值里的 `!subst("Feature", "Has", NAME)` 逗号被当成参数分隔符；
`def FeatureStdExtZvl # I # b : ...` 这种**计算出来的 def 名**让解析器失去同步。
两者都只在读真实目标头文件时出现，现在都处理了。

两个刻意的取舍：

1. **字段值保留表达式文本**（`1`、`[PipeA]`、`true`、`SiFive7Model`）。记录层建模的是
   "后端按名字读哪个字段"，不是表达式文法。把值做成 `Init` 树是下一步（§7），
   它属于宿主侧，不属于 DSL。
2. **`bases` 带参数**。`WriteRes<W_ALU, [PipeA]>` 与 `InstRW<[...], (instrs ADD, SUB)>`
   的模板参数是记录身份的一部分：`equiv.py` 就是靠 `WriteType` / `ProcResources` /
   `MatchRW` 而不是记录名来对拍的。

### 2.1 文本不再是第二个源

`emit_td.py` / `predicates.py` 的每一处 `def` 都走同一个入口：

```python
out.append(self.define(where, name, "WriteRes", [write, resources_text], body))
```

`define()` 先写进 `RecordSet`，再把**同一条 record** 渲染成文本返回。文本由 record 生成，
不是与 record 平行维护，因此不会漂移。`check.py` 把这条不变量固定成测试：

```text
ok   every_def_in_the_output_belongs_to_the_record_table
     # 文本里出现的 def 名集合 == 表里的 def 名集合
ok   record_fields_survive_the_json_table_and_llvm_tblgen
     # 表里每个值都是带 kind 的节点；重放后仍是同一份 dump
ok   record_values_match_the_declared_types
     # 25 条 stub record、98 个值全部符合声明；两条负控制被拒绝
ok   keyword_reference_is_up_to_date
     # 生成的关键字参考与后端绑定一致
```

## 3. 证据：表能独立重建 llvm-tblgen 看到的记录

`emit_td.py --records-json model.json` 输出表；`record_ir.py model.json --include X.td`
只用表打印回 TableGen。两条路径都过 `llvm-tblgen -print-records`，然后逐字节比较 dump。

```bash
python3 tools/ssdl/from_records.py /tmp/riscv.rec \
    --model SiFive7VLEN512Model --processor SIFIVE_7 -o /tmp/v512.ssdl
python3 tools/ssdl/emit_td.py /tmp/v512.ssdl --target-include SSDLStub.td \
    -o /tmp/v512.td --records-json /tmp/v512.json
python3 tools/ssdl/record_ir.py /tmp/v512.json --include SSDLStub.td -o /tmp/v512.replay.td

llvm-tblgen -print-records -I llvm/include -I tests/ssdl/stub-target -o /tmp/a.rec /tmp/v512.td
llvm-tblgen -print-records -I llvm/include -I tests/ssdl/stub-target -o /tmp/b.rec /tmp/v512.replay.td
cmp /tmp/a.rec /tmp/b.rec        # 无输出
```

三个 SiFive7 模型的结果（`-print-records` 的完整 dump，含 stub 目标自身与
`ValueTypes.td` 的记录，共 45802 条 `def`）：

| 调度模型 | 处理器 | 表内记录数 | JSON 重放 dump |
|---|---|---|---|
| `SiFive7VLEN512Model` | `sifive-7-series` | 12063 | 与文本路径**逐字节相同** |
| `SiFive7VLEN128X100Model` | `sifive-x160` | 12118 | 与文本路径**逐字节相同** |
| `SiFive7VLEN1024X300Model` | `sifive-x390` | 12130 | 与文本路径**逐字节相同** |

重放时的记录顺序与文本不同（表按发射顺序，文本按"特性→全局词表→谓词→机器→后置谓词"
分组），但 `-print-records` 是按 def 名组织的，so dump 相同。同时 `equiv.py` 对
`/tmp/riscv.rec` 的调度身份对拍仍是三个模型全部 `records match`
（4246 writes / 1638 reads / 92 variants / 1 InstRW），所以"表 → 文本"这一路没有改变语义。

`check.py` 里对应两条：

```text
ok   record_fields_survive_the_json_table_and_llvm_tblgen
     # 1) 表里每个字段都在 tblgen 的 dump 里、值相等（true→1, false→0）
     # 2) 只用 JSON 重放后，每条记录的基类链与字段与原文相同
ok   record_table_fields_are_declared_by_their_classes
     # 表里出现的 (class, field) 必须由该类的 TD 声明提供
```

第二条直接回答"手写映射是 hardcode"：`emit_td.py` 里的
`("latency", "Latency")`、`("uops", "NumMicroOps")` 之类仍是 Python 常量，
但一旦字段名写错，record 表就会把一个该类没有声明的字段喂给 tblgen——测试在
生成阶段就红。三个模型的 12063/12118/12130 条记录全部通过该检查。

## 3.5 关键字参考表（生成的公开说明）

`td_schema.py --reference` 把普查结果渲染成 Markdown，提交在
`docs/dsl/2026-11-16-ssdl-keyword-reference.md`：245 行，每行一个关键字，
给出**形态**（`field` / `class` / `record` / `def`）、**声明**（`Class.Field: type = default`）
和 **读取点**（`文件:行`，最多 3 处）。例如：

```text
| `Latency`      | `ProcWriteResources`：`int` = `1` | SubtargetEmitter.cpp:1210 |
| `IssueWidth`   | `SchedMachineModel`：`int` = `-1` | SubtargetEmitter.cpp:1542 |
| `Unsupported`  | `InstRW`、`ProcReadAdvance`、`ProcWriteResources`：`bit` = `false` | SubtargetEmitter.cpp:1206, 1324 |
| `NoSchedModel` | `def NoSchedModel : SchedMachineModel` | CodeGenSchedule.cpp:526 |
```

`check.py` 的 `keyword_reference_is_up_to_date` 比对提交的文档与生成结果，
所以"后端加了绑定但文档没更新"会直接红。这就是最初那个问题的答案：
关键字不再是"gen 代码里的 hardcode"，而是一张可读、可 diff、可回归的表。

## 4. 后端绑定普查：关键字确实都在 schema 里

`td_schema.py` 现在是 schema 的可执行形式：

```bash
python3 tools/ssdl/td_schema.py --field WriteRes.Latency
# WriteRes.Latency: int = 1
python3 tools/ssdl/td_schema.py --check
# 493 backend bindings, all declared (class=107, def=3, field=347, record=36)
python3 tools/ssdl/td_schema.py --census   # 逐条列出 name -> 声明位置
```

扫描 8 个 emitter 源文件（`SubtargetEmitter.cpp`、`Common/CodeGenSchedule.cpp`、
`PredicateExpander.cpp`、`SubtargetFeatureInfo.cpp`、`DFAPacketizerEmitter.cpp`、
`AsmMatcherEmitter.cpp` 等），按 6 种形态抽取字符串绑定：

| 形态 | 例子 | 次数 | 去重后 |
|---|---|---|---|
| 字段 | `getValueAsInt("Latency")`、`emitProcessorProp(..., "IssueWidth")`、`addFieldExpander("InstRW", "Instrs")` | 347 | 152 |
| 类 | `isSubClassOf("SchedWriteRes")` | 107 | 62 |
| 记录 | `getAllDerivedDefinitions("InstRW")` | 36 | 28 |
| def | `getDef("...")` | 3 | 3 |
| **合计** | | **493** | **245** |

结论：**245 个 (kind, name) 绑定，0 个未在 TD 声明**。所以问题从来不是"关键字没定义"，
而是"定义在 TD、使用在 C++、两者之间没有类型关系"。SSDL 要做的是把这条关系
放进语义模型，而不是再发明一套关键字。

`check.py` 用 `backend_bindings_are_declared_in_the_schema` 固定这个普查，
并加了 240 的下界；正则失效会变成"普查为空"，不会伪装成通过。

## 5. 这一轮修掉的两个真问题

1. **内联 def 结尾的分号是非法的**。`llvm-tblgen` 对 `def X : C { ... };` 报
   `error: A class or def body should not end with a semicolon`。旧发射器给
   `ReadAdvance ... unsupported` 生成的是**不带**分号的形状，但给 `InstRW ... unsupported`
   生成的是**带**分号的形状——后者从未被任何测试覆盖，因为 stub 与 SiFive7 的 map
   都没有写 `unsupported`。现在内联体统一不带分号，并新增
   `inst_rw_unsupported_is_lowered`：既断言文本形状，也真的让 tblgen 解析并检查
   `Unsupported` 位是 `1`/`0`。
2. **`STIPredicate.SchedModel` 从 `let ... in` 前缀改成字段**。
   `TargetInstrPredicate.td:415-420` 里 `SchedModel` 就是 `STIPredicate` 的字段，
   旧发射器写成 `let SchedModel = M in\ndef F : STIPredicate<...>;`。现在写成
   `def F : STIPredicate<...> { let SchedModel = M; }`——record 表里它是
   `fields` 的一项，可以被 `record_fields_survive_the_json_table_and_llvm_tblgen`
   逐字段对拍，而不是藏在作用域里。

## 6. 作用域仍然存在：`SchedWrite` 没有 `SchedModel` 字段

`ProcResource`/`WriteRes`/`ReadAdvance`/`InstRW`/`SchedAlias`/`RegisterFile`/
`RetireControlUnit`/queue 这些记录都要带 `SchedModel`，而 `SchedWrite`/`SchedRead`
（`Sched<[...]>` 里的全局词表）**没有**这个字段。所以表里保留 `scope` 属性：
重放时先把无作用域记录打印出来，再把同一模型的记录包进一个
`let SchedModel = M in { ... }`。这不是排版偏好，是 schema 的事实——
`SchedWrite` 不能进那个块。

## 7. 边界（明确没做的）

- **llvm-tblgen 不读 JSON**。本轮的证明是"表能重建同样的 dump"，不是"宿主已直连"。
  真要直连，需要在 `llvm-tblgen` 加一个 record 加载入口（`RecordKeeper::addDef` +
  `Record::setValue`，约两三百行），那是唯一剩下的、属于宿主的改动。
- **类型检查只看声明**。`code`、`dag` 的内容不做语义检查（`[{...}]` 里的 C++
  与 `(instrs ...)` 的操作数列表只校验外壳）；它们属于宿主的知识。
- **关键字参考只覆盖这 8 个 emitter**。`GIsel`、`DAGISel`、`Intrinsic` 等其它
  TableGen 后端有各自的字符串绑定，不在本表里。
- **位置参数按槽位检查，不按名字**。`RegisterFile<numPhys, ...>`、
  `RISCVExtension<maj, min, ...>` 的槽位顺序由 class 的形参表决定；`td_types.py`
  按位置对拍形参的类型，所以"顶错槽"（`parser-notes` §17.3 的 `RISCVExtension` 那一类）已经是
  可判定错误，但报错只能给出 `[3] (implies)`，给不出调用点。
- **`UnsupportedFeatures`** 仍未比较：它的元素是宿主的 `Has<Feature>` 谓词。

## 8. 对最初问题的回答

> TableGen 的价值在于它把领域模型的语法实现给 TD 化了，统一用 TD 语法描述，
> 像用 schema 描述语法一样，这样很容易调整语法实现。

是的，而且这句话解释了为什么"写 TD 都是复制粘贴"：语法层（schema）是公开的，
绑定层（`getValueAs*("...")`）是隐式的。SSDL 的做法是把两层都显式化：

- 语义层：9 个平级关键字，无 base/扩展，无元编程（`v1.0-final`）；
- 记录层：`RecordSet`，名字/基类/字段/作用域都可检查；
- 绑定层：`td_schema.py --check` 把 C++ 里的 245 个字符串绑定对回 TD 声明，0 未声明。

我们绕道 `.td` 的原因只是 `llvm-tblgen` 的正门是 TD 文本；`record_ir.py` 证明了
这条绕道是可逆的——三个模型的 12k 条记录，只用 JSON 表就能重建出逐字节相同的
`-print-records` dump。剩下要做的不是语言，而是给宿主开一扇读 record 表的门。
