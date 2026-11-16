# 让 llvm-tblgen 直接读 record 表：`-load-records` 设计

> 2026-11-16。承接 `2026-11-16-ssdl-record-layer.md`：记录层已经能产出带类型的
> record 表，并且证明"只用表就能重放出逐字节相同的 `-print-records` dump"。
> 剩下的一步是让宿主从正的门口把它吃进去。

## 1. 目标与非目标

**目标**：`llvm-tblgen` 增加 `-load-records=<json>`，在正常解析完输入 `.td` 之后，
把表里的 def 追加进同一个 `RecordKeeper`，后续 emitter（`-gen-subtarget`、
`-gen-instr-info`、`-print-records`）看到的就是这些记录。

```bash
# 现在（绕道）：SSDL -> model.td -> tblgen
# 之后（直连）：SSDL -> model.json -> tblgen
llvm-tblgen -load-records=model.json lib/Target/RISCV/RISCV.td -gen-subtarget
```

**非目标**：

- 不替换 `TGParser`，不解析 TD 文本（表里没有语法歧义要处理）。
- 不支持在 JSON 里定义 `class` / `multiclass` / `foreach`——记录层的前提就是"已经展开"。
- 不改 emitter：它们继续按字符串绑定 record。

## 2. 为什么仍然要传一个 `.td`

record 的字段值引用**宿主环境**：`InstRW` 的 `(instrs ADD, SUB)` 指向指令，
`ProcessorModel.Features` 指向 feature，`SchedModel` 指向模型记录。
这些类与指令来自目标 TD，不可能出现在表里。所以接口是"解析 TD 得到环境，
再把表合并进去"，而不是"只给 JSON 就出码"。

这也解释了 SSDL 的定位：它替换的是**模型的描述**，不是目标架构的指令定义。

## 3. JSON 形状（`ssdl-records-1`）

`record_ir.RecordSet.dumps()` 的格式，值已经是带类型的节点：

```json
{
 "format": "ssdl-records-1",
 "records": [
  {"name": "sifive_7_series_WR_W_ALU",
   "bases": [{"class": "WriteRes",
              "args": [{"kind": "ident", "name": "W_ALU"},
                       {"kind": "list", "items": [{"kind": "ident", "name": "SiFive7PipeA"}]}]}],
   "fields": [["Latency", {"kind": "int", "value": 1}]],
   "scope": "sifive_7_seriesModel",
   "origin": "machine sifive7.bind W_ALU"}
 ]
}
```

- `bases`：类名 + 位置参数；类必须已经在 `RecordKeeper` 里（否则是错误，不是猜测）。
- `fields`：字段名 + 值节点。
- `scope`：`SchedModel` 作用域的等价物；loader 直接写字段 `SchedModel = <scope>`。
- `style` / `suffix`：打印提示，loader 忽略。
- `origin`：SSDL 的构造位置，只进诊断。

节点类型：`int` / `bit` / `string` / `code` / `ident` / `list` / `dag` / `apply` / `unset`。
`apply`（`CheckSameRegOperand<1, 2>`）按"类 + 位置参数"实例化成匿名记录，和 TD 表达式
里的 `ClassName<...>` 是同一条路径（`VarDefInit`）。

## 4. C++ 落点

```
include/llvm/TableGen/RecordJSONLoader.h   # loadRecordsJSON(RecordKeeper&, MemoryBufferRef)
lib/TableGen/RecordJSONLoader.cpp          # 与 RecordKeeper 同库，Main.cpp 直接调用
lib/TableGen/Main.cpp                      # -load-records=<json>，ParseFile 之后
llvm/test/TableGen/records-json-load.td    # 最小 lit 测试（含 Inputs/*.json）
```

放进 `lib/TableGen` 而不是 `utils/TableGen`：这是 `RecordKeeper` 的通用入口，
不依赖任何 emitter；`llvm-min-tblgen` 不链接它，也不受影响。

实现要点（按代码顺序）：

1. **三遍构造**。这是本轮唯一一个非显然的约束：

   ```text
   1. 建 Record（只有名字）——表内引用可以前向，名字映射先建好；
   2. 给每个 Record 登记 direct superclass——必须在创建任何 DefInit 之前；
   3. 实例化类、写字段、resolve、addDef。
   ```

   第 2 步不能省：`DefInit` 在构造时缓存 `Record::getType()`（`Record.cpp:2821`），
   而 `getType()` 由 direct superclass 列表算出。先建 `DefInit` 再补 superclass，
   类型就永久错了；debug 下还会撞 `addDirectSuperClass` 的
   `assert(!CorrespondingDefInit)`。文本路径没有这个顺序问题，因为解析期只产生
   `VarInit`，类型在最终 resolve 时才定型。

2. **类实例化 = `AddParentClass` 的复用**。逐个 base：拷贝 `Class->getValues()`，
   用 `MapResolver` 绑定默认值 + 位置参数，`resolveReferences(R)`，再
   `appendAssertions` / `appendDumps`。`NAME` 用
   `"<ClassName>:NAME"` → `Rec->getNameInit()` 绑定，与
   `TGParser::QualifiedNameOfImplicitName` 同形；`ProcResource` 的
   `!cast<ProcResourceKind>(NAME)` 就靠它。

3. **`apply` 值**。`CheckSameRegOperand<1, 2>` 是匿名类实例化：
   `VarDefInit::get(...)->Fold()`。`VarDefInit` 按 (class, args) uniquing，
   所以表里重复的 apply 值只产生一个匿名 def，和文本路径一致。

4. **值构造按目标类型**。`RecordVal::getType()`（字段）或模板参数的声明类型
   决定 list 的元素类型；int/bit/string/code/ident/dag 先建节点，再
   `Init::getCastTo(Ty)` 做一次转换与校验。`code` 是
   `StringInit::get(RK, text, SF_Code)`——这个版本的 TableGen 已经没有
   `CodeInit`。

5. **`scope`**。写 `SchedModel` 字段；记录没有该字段就报错。`scope` 指向的
   记录可以出现在表的后半部分（映射里已有骨架）。

6. **诊断**。所有错误带 `record '<name>' (<origin>)`，其中 `origin` 是 SSDL
   的构造位置（`machine sifive7.bind W_ALU`）。这是表相对 TD 的实质优势：
   TD 只有行列号，表知道自己在源语言里是哪一行写出来的。

7. **冲突**。同名 def 已存在默认报错。设计里预留的
   `-load-records-allow-replace` 没有实现——迁移期直接把宿主 TD 里的模型删掉
   更干净；等真需要覆盖语义时再加，避免现在就定义"部分覆盖"的边界。

## 5. 验证结果

构建：`builds/record-loader`（`LLVM_TARGETS_TO_BUILD=RISCV`，
`LLVM_ENABLE_PROJECTS=`）。下表全部是本机实测。

| 级别 | 结果 |
|---|---|
| M1 `-print-records` | stub 25 条记录：文本路径与 `-load-records` 路径 **0 行差异**（399745 行，匿名 def 编号也相同） |
| M2 emitters | `-gen-subtarget` 475 行、`-gen-instr-info` 1727 行，**逐字节相同** |
| M3 真实模型 | 三个 SiFive7 模型 12063 / 12118 / 12130 条记录：文本 vs loader **逐字节相同**（各 486014 行）；`equiv.py` 对 `/tmp/riscv.rec` 三个都是 `records match`（writes / reads / variants / InstRW / resources / machine props / processor features / tunes / march / itinerary） |
| M4 负控制 | 6 类错误全部按预期拒绝，见下 |

M4 的实际输出（节选）：

```text
record 'stub_cpu_WR_W_ALU': base class 'NoSuchClass' is not declared by the
    host TableGen input
record 'stub_cpu_WR_W_ALU' (machine stub-cpu): value '"1"' has type 'string',
    expected 'int'
record 'W_ALU' appears twice in the table
record 'W_ALU' (machine stub-cpu): scope set to 'stub_cpuModel', but the record
    has no SchedModel field
record 'stub_cpu_WR_W_ALU' (machine stub-cpu): unknown identifier 'Nope'
record 'stub_cpu_WR_W_ALU' (machine stub-cpu): no field or template argument
    named 'NoSuchField'
```

回归：`llvm-lit test/TableGen` 488/488（其中新增
`records-json-load.td` 1 条）。emitter 一行未改。

## 6. 风险与边界

- **内部 API 稳定性**：`Record` 构造、`addDirectSuperClass`、`MapResolver`、
  `VarDefInit::Fold` 都是 TableGen 内部接口，随版本变化。补丁集中在两个新文件，
  便于 rebase；`Main.cpp` 只多了一个 option 和一次调用。
- **匿名记录编号**：`VarDefInit` 的匿名 def 名来自全局计数器。M1/M3 里两条路径
  的编号恰好一致（宿主解析相同、实例化顺序相同），但这不是契约；跨版本比较
  应当把 `anonymous_[0-9]+` 规范化。
- **`dag` 的形参名**：表里的 dag 节点只存 operator + 参数值，不存 `arg: name`。
  当前模型没有用到命名 dag 参数；需要时在 `td_value.py` 的 dag 节点上加 `names`
  并在这里还原。
- **`bits<N>` 值**：`td_value.py` 还没有 bits 节点。现有模型没有这种字段；
  加的时候在 loader 里构造 `BitsInit`。
- **顺序**：`-print-records` 按名字排序，所以插入顺序不影响 dump；emitter 侧
  （`getAllDerivedDefinitions`）按 `DefsByDirectSuperClass` 取，`addDef` 里会
  登记并清缓存，M3 已覆盖。

## 7. 这一步证明了什么

三件事同时成立，才算"record 表是 contract"：

1. 表能独立重建 tblgen 看到的记录（上轮的 `record_ir.py` 文本重放）；
2. 表能跳过文本被宿主直接吃进去（本轮的 `-load-records`）；
3. 吃进去之后，调度身份与上游一致（`equiv.py`，三个模型 `records match`）。

于是 SSDL 的产物链条可以收成一条直线：

```text
SSDL --parse--> 语义模型 --lower--> RecordSet --+--> .td 文本（给人看/给旧流程）
                                                +--> JSON 表（给 -load-records）
```

第 3 条也解释了为什么这件事值得放进 tblgen：如果只有自己的宿主能读这张表，
"record 是契约"就只是自家后端的实现细节；能让未修改的 emitter 在未修改的
TableGen 语义下产出逐字节相同的 `.inc`，才说明表描述的是 TableGen 的记录，
而不是我们发明的一种中间格式。
