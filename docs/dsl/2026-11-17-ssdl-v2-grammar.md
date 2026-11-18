# SSDL 2.0 文法（规范性草案）

- date: 2026-11-17
- status: **Draft / normative target**
- 基线：`docs/dsl/2026-11-17-ssdl-syntax-inventory.md`
- 语义：不变。`SemProgram`、身份/阶段/拼写、records 契约见
  `2026-11-17-ssdl-v1.3-semantic-ir.md`。
- 原则：一个概念一个关键字；同一概念的多个角色用紧跟关键字的 kind 表达；
  一种字段体；位置敏感项显式；host 边界显式；规范 = 实现；errata 清零。

本文件是 v2 的唯一文法来源。v1.0 / v1.2 / v1.3 的 EBNF 在本文件生效后转为历史文档。

块声明（`machine` / `feature` / `predicate select|sti|tii` / `set ... { ... }`）
的 `}` 后允许一个可选的 `;`，前端忽略；推荐不写。块内成员不接受额外的 `;`。

---

## 1. 词法

```ebnf
IDENT       = ident_start { ident_char | "-" } ;
ident_start = letter | "_" ;
ident_char  = letter | digit | "_" ;

INT         = [ "-" ] digit { digit } ;
STRING      = '"' { string_char | escape } '"' ;
BOOL        = "true" | "false" ;

COMMENT     = "#"  { any_char } newline
            | "/*" { any_char } "*/" ;

HOST_BLOCK  = "[{" { any_char } "}]" ;

host_block  = "host" HOST_BLOCK ;
```

### 1.0 `//` 是运算符，不是注释

v1 把 `//` 当行注释，同时又在表达式里用 `//` 表示地板除；两者无法在词法层
同时成立。v2 只保留 `#` 与 `/* */` 两种注释，`//` 一律是 §7 的地板除运算符。
这是对 v1 注释语法的**不兼容收紧**，v1 文件进入 `v1-compat/` 后才能继续使用
`//` 注释。

### 1.1 `-` 的词法规则（吸收 S5）

`-` 是普通标识符字符：在标识符内**一律**并入 `IDENT`，唯一例外是 `->`
（它在词法阶段优先匹配）。这条规则必须是上下文无关的，否则
`ssdl-sifive-7` 这类真实 CPU 名无法原样书写：

```text
ssdl-sifive-7       # 一个 IDENT
sifive-7-series     # 一个 IDENT
rv64-2p1            # 一个 IDENT
a-b                 # 一个 IDENT
a - b               # 标识符 a、减号、标识符 b（减法按 §7 求值）
-1                  # 负整数字面量：token 起始位置的 `-` + 数字
```

名字位置与值位置的区别是**位置**，不是字符：

- 名字位置（`machine` 名、`write`/`read`、列表元素、`sched_model` 等）
  原样使用 `IDENT`，`-` 永远属于名字。
- 数值字段（`latency` / `uops` / `cycles` / 参数表达式等）的字段值按 §7
  重新解析；因此 `n-1`、`n - 1` 都是减法。需要把名字与减法写在一起时，
  唯一可靠的办法是留空格：`n - 1`。

v1 的 `-` 行为与本节一致，v2 不改变既有 CPU 名的拼写。

### 1.2 `host` 边界

所有 C++ 片段只在 `host [{ ... }]` 中出现。`[{ ... }]` 内部不做语法分析，
以 `}]` 结束；这是 v1.0 `CODE_BLOCK` 的同一词法，只是加了 `host` 标记。

---

## 2. 顶层

```ebnf
program
    = { top_decl }
    ;

top_decl
    = set_decl
    | predicate_decl
    | feature_decl
    | machine_decl
    | itineraries_decl
    | extern_decl
    ;
```

v2 的顶层关键字是 6 个：`set` / `predicate` / `feature` / `machine` /
`itineraries` / `extern`。`predicate_prolog`、`sti_decl`、`sti_function`、
`tii_predicate` 不再是顶层关键字，分别并入 `predicate` 的 `prolog` 形式与
`: sti` / `: tii` 形式（见 §4）。

---

## 3. `set`

```ebnf
set_decl
    = "set" set_kind IDENT "=" set_value ";" 
    | "set" set_kind IDENT "=" set_value set_body
    ;

set_kind
    = "instructions" | "dimension" | "relation"
    ;

set_value
    = "[" [ set_item { "," set_item } [ "," ] ] "]"
    | INT ".." INT
    | "inst" "(" attr_cond ")"
    | "reg"  "(" attr_cond ")"
    ;

set_item
    = "..." IDENT
    | "..." set_expr
    | IDENT
    | INT ".." INT
    | INT
    ;

set_body
    = "{" { set_field ";" } "}"
    ;

set_field
    = "name_format" "=" STRING
    | "units"       "=" int_list
    | "projections" "=" "{" { projection ";" } "}"
    ;

projection
    = IDENT "=" int_list
    ;
```

语义：

- `set instructions X = ...`：元素是指令/寄存器引用；`inst(cond)`/`reg(cond)`
  在展开期求值为显式列表。
- `set dimension X = [...] { name_format = "..."; units = [...]; }`：
  每个成员有一个显示后缀与可选整数投影；`projections` 增加 `X.key` 查表。
- `set relation X = [(k1, k2, ...), ...];`：有限 n 元关系，键必须唯一，空表报错。

`name_format` 只能包含一个 `{value}`；`units` 与成员一一对应；缺失 `units`
的维度不能用于数值表达式。

范围端点是有符号整数：`-2..2` 合法。负号只在允许负值的上下文有意义（集合项、
`int_list`）；操作数下标由谓词签名拒绝负数（`is_reg(-1)` 在 validate 阶段报错）。

```text
set dimension SEW = [8, 16, 32, 64] {
    name_format = "_E{value}";
    units       = [8, 16, 32, 64];
}

set relation DivFactor = [(8, 1), (16, 15), (32, 28), (64, 57)];
```

---

## 4. `predicate`

```ebnf
predicate_decl
    = "predicate" "sched"  IDENT [ "(" [ param { "," param } [ "," ] ] ")" ]
      "=" predicate_expr ";"
    | "predicate" "select" IDENT [ "(" [ param { "," param } [ "," ] ] ")" ]
      predicate_body
    | "predicate" "sti"    IDENT predicate_body
    | "predicate" "tii"    IDENT predicate_body
    | "predicate" "prolog" "=" host_block ";"
    ;

predicate_body
    = "{" { predicate_field } "}"
    ;

predicate_field
    (* select *)
    = "when"      "=" host_block ";"
    | "during"    "=" feature_expr ";"
    | "desc"      "=" STRING ";"
    | "recompute_per_function" "=" BOOL ";"
    (* sti *)
    | "default"     "=" BOOL ";"
    | "overrides"   "=" BOOL ";"
    | "expand_for_mc" "=" BOOL ";"
    | "opcode_mask" "=" BOOL ";"
    | "delegates"   "=" ident_list ";"
    | "impl" IDENT ":" IDENT "{" { sti_impl_item } "}"
    (* tii *)
    | "method" "=" STRING ";"
    | "switch" "opcode" "{" { switch_case } "default" ":" statement "}"
    ;

sti_impl_item
    = "sched_model" "=" IDENT ";"
    | ( "equiv_class" | "dep_breaking_class" ) IDENT
      "(" ( IDENT | set_expr ) "," predicate_expr [ "," int_set ] ")"
      ";"
    ;

switch_case
    = "case" inst_set ":" statement
    ;

statement
    = "return" predicate_expr ";"
    ;
```

语义：

- `kind` 一律写在 `predicate` 之后、名字之前。旧写法 `predicate P = ...`
  与 `predicate P : select` 会报错并提示新位置。
- `sched` 的 `when` 是表达式；`select` 的 `when` 是 host 代码，`during` 是
  feature 谓词；两者可只写其一。
- `sti` 的声明与实现合并：`impl <名字> : <machine> { ... }` 中，`<名字>`
  是 v1 `sti_function` 的名字，也是生成的 `STIPredicate` def 名；`<machine>`
  是 `SchedModel` 作用域。`sched_model` 字段可以省略，写了必须与 `<machine>`
  一致。lowering 到 v1 的 `STIPredicateDecl` + `STIPredicate`。
- `tii` 的 `switch opcode` lowering 到 `TIIPredicate`。`method` 是 TII 钩子名，
  缺省等于谓词名：`predicate tii RegShiftedFn { method = "hasShiftedReg"; ... }`
  发射 `def RegShiftedFn : TIIPredicate<"hasShiftedReg", ...>`。在调度谓词里用
  `mc(RegShiftedFn)` 引用它，得到规范的 `MCSchedPredicate<RegShiftedFn>`；TII def
  会自动排在引用它的记录之前（TableGen 的 class 实参不能前向引用）。
- `predicate prolog = host [...];` 是全局共享代码，等价 v1 的 `PredicateProlog`。

五个 kind 是同一个“谓词声明”概念的不同角色，不是五个独立概念：

| kind | 语义层 | 产物 | 用途 |
|---|---|---|---|
| `sched` | 调度谓词（MC 或 SCHED） | `Predicate<...>` | 变体条件、STI 默认值 |
| `select` | 指令选择 / 汇编匹配 | `Predicate<...>` + `AssemblerPredicate<...>` | ISel、汇编器 |
| `sti` | STI 声明与实现合并 | `STIPredicateDecl` + `STIPredicate` | 依赖打断、idiom 识别 |
| `tii` | 目标指令信息谓词 | `TIIPredicate` | MC 层 opcode 判定 |
| `prolog` | 全局共享 host 代码 | `PredicateProlog` | 谓词共享变量 |

保留单关键字的理由：拆成四个关键字会把 `prolog` 再次变成特例，且迁移成本
只有噪音；角色表让“predicate 还能定义 STI 实现”的意外感消失。

```text
predicate sched IsZeroReg(i: INT) = reg_is(i, ZeroRegs);

predicate select HasStdExtI {
    when   = host [{ Subtarget->hasStdExtI() }];
    during = all_of([StdExtI]);
    desc   = "'I' (Base Integer Instruction Set)";
}

predicate sti IsDependencyBreaking {
    opcode_mask = true;
    delegates   = [isZeroIdiom];
    impl OrrXzrXzr : siFive7 {
        dep_breaking_class OrrXzrXzr(
            [ORR], all_of(opcode([ORR]), IsZeroReg(1), IsZeroReg(2)), [1, 2]
        );
    }
}

predicate tii IsDependencyBreaking {
    switch opcode {
        case [ORR]: return all_of(opcode([ORR]), IsZeroReg(1));
        case [EOR]: return all_of(opcode([EOR]), same_reg(1, 2));
        default:    return false;
    }
}
```

> 注：同一名字不能同时声明为 `sti` 与 `tii`；上例只展示两种形态。
> Phase 5 决策门已通过：`select`/`sti`/`tii` 的 records 与 v1
> 逐字段一致（`v2/sifive7-mini-v2.ssdl` 对拍 `sifive7-mini.expected.td`），
> 因此 v2.0 删除 `predicate_prolog`/`sti_decl`/`sti_function`/`tii_predicate`
> 四个顶层关键字，§11 的降级预案不启用。

---

## 5. `feature`

```ebnf
feature_decl
    = "feature" feature_kind IDENT feature_body
    ;

feature_kind
    = "plain" | "extension" | "tune"
    ;

feature_body
    = "{" { feature_field ";" } "}"
    ;

feature_field
    = "desc"        "=" STRING
    | "implies"     "=" ident_list
    | "mattr"       "=" STRING
    | "inline_behavior" "=" ( "ignore" | "default" | "inverse" | "must_match" )
    | host_feature_field
    (* typed host field: bool IsRV64 = true; int Foo = 1; string Bar = "x"; *)
    | ( "bool" | "int" | "string" ) IDENT "=" literal
    (* extension *)
    | "version"      "=" STRING
    | "experimental" "=" BOOL
    | "bitmask" "=" bitmask_body
    (* tune *)
    | "positive_directive" "=" STRING
    | "negative_directive" "=" STRING
    ;

literal
    = BOOL | INT | STRING
    ;

host_feature_field
    = "host" "{" "field" "=" STRING ";" "value" "=" STRING ";" "}"
    ;

bitmask_body
    = "{" "group" "=" INT ";" "bit" "=" INT ";" "}"
    ;
```

语义：

- `kind` 必须显式写在 `feature` 之后；体内 `kind =` 与头部 `NAME : kind`
  两种旧写法都会报错并提示新位置。
- `plain` 必须写 `mattr`（命令行特性名）；缺省时报
  `kind = plain requires field 'mattr'`。`bool/int/string <Field> = <value>`
  直接 lowering 到 `SubtargetFeatureKV` 的 `Field/Value`。
- `extension` 的 `version`/`bitmask`/`experimental` 走 v1.3 的三层 emit。
- `tune` 的 `positive_directive`/`negative_directive` 进入命令行映射。
- `host [{ ... }]` 保留给无法类型化的 host-only 字段。

```text
feature plain Feature64Bit {
    bool IsRV64 = true;
    mattr = "64bit";
    desc  = "Implements RV64";
}

feature extension StdExtI {
    version = "2.1";
    bitmask = { group = 0; bit = 8; };
    desc = "Base Integer Instruction Set";
}

feature tune TunePostRA {
    bool UsePostRAScheduler = true;
    positive_directive = "use-postra-scheduler";
    negative_directive = "no-use-postra-scheduler";
}
```

---

## 6. `machine`

```ebnf
machine_decl
    = "machine" "alias" IDENT "=" IDENT ";"
    | "machine" machine_role IDENT
      [ "(" [ param { "," param } [ "," ] ] ")" ]
      [ ":" IDENT [ "(" [ arg { "," arg } [ "," ] ] ")" ] ]
      "{" { machine_item } "}"
    ;

machine_role
    = "both" | "model" | "cpu" | "family" | "instance"
    ;

param
    = IDENT ":" ( "INT" | "BOOL" )
    ;

arg
    = INT | BOOL
    ;
```

语义：

- `machine both X { ... }`：一个名字同时发射 `SchedMachineModel` 与
  `ProcessorModel`（v1 的默认行为）；迁移 v1 源时用它保持 records 不变。
- `machine model X { ... }`：只发射 `SchedMachineModel` 与 model-scoped 实体。
- `machine cpu X { model = M; features = [...]; tune_features = [...];
  default_march = "..."; }`：只发射 `ProcessorModel`，`SchedModel = M`。
- `machine family F(params) { ... }`：参数化的 model+cpu 模板，本身不发射。
- `machine instance X : F(args) { ... }`：实例，发射 model+cpu（沿用 v1.3 的
  实例前缀拼写规则）。
- `machine alias X = Y;`：`ProcessorAlias`，不复制记录。
- `family`/`instance` 是 `model`+`cpu` 的语法糖；需要显式拆分时用 `model`/`cpu`。
- 参数列表只允许出现在 `family`；`: F(args)` 只允许出现在 `instance`。
- `cpu` 不得声明实体/关系；`model` 不得写 `model =`；`alias` 不接受 body。

```text
machine model siFive7 {
    issue_width = 4;
    resource PipeA { units = 2; }
    write W_ALU;
    bind W_ALU { resources = [PipeA]; latency = 1; }
}

machine cpu sifive-7-series {
    model = siFive7;
    features = [Feature64Bit, StdExtI];
    tune_features = [TunePostRA];
    default_march = "rv64i2p1";
}

machine alias sifive7-alias = siFive7;
```

### 6.1 machine 项

```ebnf
machine_item
    = machine_prop
    | entity_decl
    | relation_decl
    | itineraries_decl
    ;

machine_prop
    = "features"                    "=" ident_list ";"
    | "tune_features"               "=" ident_list ";"
    | "unsupported_features"        "=" ident_list ";"
    | "default_march"               "=" STRING ";"
    | "model"                       "=" IDENT ";"
    | "issue_width"                 "=" int_expr ";"
    | "micro_op_buffer_size"        "=" int_expr ";"
    | "loop_micro_op_buffer"        "=" int_expr ";"
    | "load_latency"                "=" int_expr ";"
    | "high_latency"                "=" int_expr ";"
    | "mispredict_penalty"          "=" int_expr ";"
    | "post_ra_scheduler"           "=" BOOL ";"
    | "complete_model"              "=" BOOL ";"
    | "full_inst_rw_overlap_check"  "=" BOOL ";"
    | "enable_intervals"            "=" BOOL ";"
    ;
```

`model` 只允许在 `machine cpu` 中；其他角色写 `model =` 是错误。

`unsupported_features` 的元素是 `list<Predicate>` 的**宿主谓词 def 名**，按字面发射：
AArch64 写 `[HasSVE, HasMTE]`，RISC-V 写 `[HasStdExtZbkb]`。它们属于目标词表
（与 `opcode(ADD)` 同一约定），SSDL 只检查名字能作为 TableGen def，不检查存在性；
不存在时由 `llvm-tblgen` 报错。v1.1 的 `Has<Name>` 前缀约定已删除——它把
`HasSVE` 拼成了 `HasHasSVE`。

### 6.2 角色允许项

| 项 | `both` | `model` | `cpu` | `family` | `instance` | `alias` |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| machine 属性（`issue_width` 等） | ✓ | ✓ | — | ✓ | ✓ | — |
| `model =` | — | — | ✓ | — | — | — |
| `features` / `tune_features` / `default_march` | ✓ | — | ✓ | ✓ | ✓ | — |
| 实体（resource/write/...） | ✓ | ✓ | — | ✓ | ✓ | — |
| 关系（bind/map/variant/...） | ✓ | ✓ | — | ✓ | ✓ | — |
| `itineraries` | ✓ | ✓ | — | ✓ | ✓ | — |

`both`/`family`/`instance` 同时生成 model 与 cpu；`cpu` 不得声明实体或关系。
`alias` 只接受 `machine alias X = Y;`，不接受 body。

### 6.3 实体

```ebnf
entity_decl
    = resource_decl
    | res_group_decl
    | rw_decl
    | write_seq_decl
    | combo_func_decl
    | queue_decl
    | reg_file_decl
    | retire_unit_decl
    ;

resource_decl
    = "resource" IDENT [ dim_index ] [ record_body ]
    ;

res_group_decl
    = "res_group" IDENT [ dim_index ] "=" ident_list [ record_body ]
    ;

rw_decl
    = ( "write" | "read" ) IDENT [ dim_index ] ";"
    | ( "func_unit" | "bypass" | "itin_class" ) IDENT ";"
    ;

write_seq_decl
    = "write_seq" IDENT [ dim_index ] "=" ident_list [ record_body ]
    ;

combo_func_decl
    = "combo_func" IDENT "=" ident_list ";"
    ;

queue_decl
    = ( "load_queue" | "store_queue" ) record_body
    ;

reg_file_decl
    = "reg_file" IDENT record_body
    ;

retire_unit_decl
    = "retire_unit" record_body
    ;

record_body
    = "{" { field ";" } "}"
    ;

field
    = IDENT "=" value
    ;
```

`record_body` 是统一的字段体；每个实体只接受下表中的字段。

| 实体 | 字段 |
|---|---|
| `resource` | `super = IDENT`、`units = int_expr`、`buffer = int_expr`、`name = name_template` |
| `res_group` | `buffer = int_expr` |
| `write_seq` | `repeat = int_expr` |
| `load_queue`/`store_queue` | `resource = IDENT` |
| `reg_file` | `num_phys`、`classes`、`costs`、`allow_move_elim`、`max_move_elim_per_cy`、`allow_zero_elim_only` |
| `retire_unit` | `buffer`、`max_per_cycle` |

字段类型由 `tools/ssdl/field_types.py` 的 `ENTITY_FIELDS` 定义，v2 前端与
`validate` 共用同一张表。类型只做**形状**检查（`int_expr` / `int_list` /
`bool` / `bool_list` / `ident` / `ident_list` / `sched_rw_list` / `string` /
`enum`），不要求字面量可求值：family 参数与投影仍可出现在 `int_expr` 位置。

```text
resource LSU { units = 1; buffer = 4; }
res_group AGU = [AGU0, AGU1] { buffer = 2; }
write_seq WS = [W_ALU, W_LD] { repeat = 2; }
load_queue { resource = LSU; }
reg_file GPR {
    num_phys = 180;
    classes = [GPR64, GPR32];
    allow_move_elim = [true, true];
    max_move_elim_per_cy = 4;
}
```

### 6.4 关系

```ebnf
relation_decl
    = bind_decl
    | advance_decl
    | map_decl
    | map_itin_decl
    | alias_decl
    | variant_decl
    ;

bind_decl
    = "bind" IDENT [ dim_index ] bind_body
    ;

bind_body
    = "{" { bind_field ";" } "}"
    ;

bind_field
    = "resources" "=" ident_list
    | "latency"   "=" int_expr
    | "uops"      "=" int_expr
    | "release"   "=" int_list
    | "acquire"   "=" int_list
    | "begin_grp" "=" BOOL
    | "end_grp"   "=" BOOL
    | "single"    "=" BOOL
    | "retire_ooo" "=" BOOL
    | "unsupported" "=" BOOL
    ;

advance_decl
    = "advance" IDENT [ dim_index ] advance_body
    ;

advance_body
    = "{" { advance_field ";" } "}"
    ;

advance_field
    = "cycles"   "=" int_expr
    | "writes"   "=" sched_rw_list
    | "tunables" "=" int_list
    | "unsupported" "=" BOOL
    ;

map_decl
    = "map" inst_set map_body
    ;

map_body
    = "{" { map_field ";" } "}"
    ;

map_field
    = INT "=" sched_rw
    | "unsupported" "=" BOOL
    ;

sched_rw
    = IDENT [ dim_index ]
    ;

sched_rw_list
    = "[" [ sched_rw { "," sched_rw } [ "," ] ] "]"
    ;

map_itin_decl
    = "map_itin" ident_list "->" sched_rw_list ";"
    ;

alias_decl
    = "alias" IDENT "->" IDENT ";"
    ;

variant_decl
    = ( "variant" | "read_variant" ) IDENT [ dim_index ] variant_body
    ;

variant_body
    = "{" { variant_field ";" } "}"
    ;

variant_field
    = "variadic" "=" BOOL
    | "case" predicate_expr ":" sched_rw_list
    | "default" ":" sched_rw_list
    ;
```

`map` 的操作数位置是**显式整数下标**：

```text
map ADD {
    0 = W_ALU;
    1 = read_default;
    2 = read_default;
}
```

- 同一 `map` 内下标不得重复；下标从 0 开始且不得有空洞。
- `map [ADD, SUB] { ... }` 表示同一组读写映射到多条指令。
- `unsupported = true;` 与 v1 的 `: unsupported = true` 等价，可和下标字段混写。
- `regex("...")` 仍可出现在 `inst_set` 中；与显式指令名混写仍报错。

### 6.5 `itineraries`

```ebnf
itineraries_decl
    = "itineraries" [ ":" "packetizer_namespace" "=" STRING ] itineraries_body
    ;

itineraries_body
    = "{" { itineraries_field ";" } "}"
    ;

itineraries_field
    = "sched_model" "=" IDENT
    | "func_units"  "=" ident_list
    | "bypasses"    "=" ident_list
    | itin_decl
    ;

itin_decl
    = "itin" IDENT itin_body
    ;

itin_body
    = "{" { itin_field ";" } "}"
    ;

itin_field
    = "stages"   "=" "[" [ stage { "," stage } [ "," ] ] "]"
    | "cycles"   "=" int_list
    | "bypasses" "=" ident_list
    | "uops"     "=" int_expr
    ;

stage
    = "stage" stage_body
    ;

stage_body
    = "{" { stage_field ";" } "}"
    ;

stage_field
    = "cycles"  "=" INT
    | "units"   "=" ident_list
    | "timeinc" "=" INT
    | "kind"    "=" ( "required" | "reserved" )
    ;
```

`cycles` 是这一级行程占用的周期数（`InstrStage` 的第一个参数），不是
stage 在列表中的序号；`timeinc` 是进入下一级前的 time increment（第三个
参数）。这两个名字必须与语义一致，不要退回 TD 的位置化拼写。

`itin.cycles` 是 **operand cycles**（`InstrItinData` 的第三个参数），
不是每级 stage 的周期数：第 0 个元素是该指令结果的可用周期，其余元素是
各操作数的读取周期；它可以省略，长度与 `stages` 无关。`stage` 的 `units`
必须出现在 `itineraries.func_units` 中。

```text
itineraries : packetizer_namespace = "SiFive7" {
    func_units = [FU_ALU, FU_LSU];
    bypasses   = [no_bypass];
    itin IIC_iLoad {
        stages = [
            stage { cycles = 1; units = [FU_ALU]; },
            stage { cycles = 2; units = [FU_LSU]; timeinc = 1; },
        ];
        cycles = [3, 1];
    }
}
```

## 7. 表达式

优先级从低到高（C 语言同序）：

```ebnf
int_expr
    = ternary
    ;

ternary
    = or_expr [ "?" int_expr ":" int_expr ]
    ;

or_expr
    = and_expr { "||" and_expr }
    ;

and_expr
    = eq_expr { "&&" eq_expr }
    ;

eq_expr
    = rel_expr { ( "==" | "!=" ) rel_expr }
    ;

rel_expr
    = add_expr { ( "<" | ">" | "<=" | ">=" ) add_expr }
    ;

add_expr
    = mul_expr { ( "+" | "-" ) mul_expr }
    ;

mul_expr
    = unary { ( "*" | "/" | "//" | "%" ) unary }
    ;

unary
    = "!" unary
    | "-" unary
    | primary
    ;

primary
    = call "(" [ int_expr { "," int_expr } [ "," ] ] ")"
    | "(" int_expr ")"
    | INT
    | IDENT
    | IDENT "." IDENT
    ;

call
    = "cdiv" | "max" | "min" | "log2"
    ;
```

- `/` 必须整除；`//` 是地板除。
- 比较/布尔返回 0/1，只用于条件位置。
- 表达式节点数有上限（v1.3 的 `NODE_BUDGET = 64`）；超出报错。
- 阶段规则不变：ELAB 全折叠；RUNTIME 只允许进入 `bind` 的
  `latency`/`uops`/`cycles` 标量字段。

### 7.1 编译期属性条件

`inst(attr_cond)` / `reg(attr_cond)` 使用独立的属性表达式：

```ebnf
attr_cond
    = attr_or
    ;

attr_or
    = attr_and { "||" attr_and }
    ;

attr_and
    = attr_cmp { "&&" attr_cmp }
    ;

attr_cmp
    = attr_unary [ ( "==" | "!=" | "<" | ">" | "<=" | ">=" ) attr_atom ]
    ;

attr_unary
    = "!" attr_unary
    | "(" attr_cond ")"
    | attr_atom
    ;

attr_atom
    = IDENT "." IDENT
    | IDENT
    | INT
    | STRING
    | BOOL
    ;
```

### 7.2 列表与集合

```ebnf
ident_list
    = "[" [ IDENT { "," IDENT } [ "," ] ] "]"
    ;

int_list
    = "[" [ int_expr { "," int_expr } [ "," ] ] "]"
    ;

bit_list
    = "[" [ BOOL { "," BOOL } [ "," ] ] "]"
    ;

value
    = literal
    | ident_list
    | int_list
    | bit_list
    | int_expr
    ;

int_set
    = "[" [ int_item { "," int_item } [ "," ] ] "]"
    ;

int_item
    = "..." IDENT
    | INT ".." INT
    | INT
    ;

inst_set
    = "[" [ inst_item { "," inst_item } [ "," ] ] "]"
    ;

inst_item
    = IDENT
    | "regex" "(" STRING ")"
    ;

set_expr
    = IDENT
    | "inst" "(" attr_cond ")"
    | "reg" "(" attr_cond ")"
    | inst_set
    ;

inst_ref
    = IDENT
    ;

reg_ref
    = IDENT
    ;
```

### 7.3 谓词表达式

```ebnf
predicate_expr
    = "true" | "false"
    | "not" "(" predicate_expr ")"
    | "all_of" "(" [ predicate_expr { "," predicate_expr } [ "," ] ] ")"
    | "any_of" "(" [ predicate_expr { "," predicate_expr } [ "," ] ] ")"
    | "feature" "(" IDENT ")"
    | IDENT "(" [ predicate_expr { "," predicate_expr } [ "," ] ] ")"
    | atom_pred
    ;

atom_pred
    = "opcode" "(" ( IDENT | set_expr ) ")"
    | "pseudo" "(" ( IDENT | set_expr ) ")"
    | "is_reg" "(" INT ")"
    | "is_vreg" "(" INT ")"
    | "is_not_vreg" "(" INT ")"
    | "is_imm" "(" INT ")"
    | "is_valid_reg" "(" INT ")"
    | "is_invalid_reg" "(" INT ")"
    | "is_zero" "(" INT ")"
    | "reg_is" "(" INT "," ( IDENT | reg_ref | set_expr ) [ "," "map" "=" IDENT ] ")"
    | "imm_is" "(" INT "," INT [ "," "map" "=" IDENT ] ")"
    | "imm_is_enum" "(" INT "," STRING [ "," "map" "=" IDENT ] ")"
    | "imm_lt" "(" INT "," INT [ "," "map" "=" IDENT ] ")"
    | "imm_gt" "(" INT "," INT [ "," "map" "=" IDENT ] ")"
    | "imm_le" "(" INT "," INT [ "," "map" "=" IDENT ] ")"
    | "imm_ge" "(" INT "," INT [ "," "map" "=" IDENT ] ")"
    | "imm_range" "(" INT "," INT "," INT [ "," "map" "=" IDENT ] ")"
    | "reg_simple" "(" INT [ "," "map" "=" IDENT ] ")"
    | "imm_simple" "(" INT [ "," "map" "=" IDENT ] ")"
    | "num_ops" "(" INT ")"
    | "same_reg" "(" INT "," INT ")"
    | "func" "(" STRING "," STRING ")"
    | "func_tii" "(" STRING "," STRING [ "," "tii" "=" STRING ] ")"
    | "non_portable" "(" host_block ")"
    | "sched_code" "(" host_block ")"
    ;
```

命名谓词调用（吸收 S2）在 v2 中是正式产生式，并要求实参个数/类型在前端检查。

`opcode` / `pseudo` / `reg_is` 等原子里的 `IDENT` 是**目标词汇**：`ADD`、`X0`、
`GPR64` 由目标 `.td` 定义，SSDL 只检查语法与谓词签名，不检查这些名字是否存在。
唯一例外是已声明的 `set`：同名标识符按 `set` 处理，展开需要 `--attrs`。

```ebnf
feature_expr
    = "all_of"  "(" ident_list ")"
    | "any_of"  "(" ident_list ")"
    | "none_of" "(" ident_list ")"
    | "not"     "(" feature_expr ")"
    ;
```

---

## 8. 族、维度与名称

```ebnf
dim_index
    = "[" dim_use { "," dim_use } [ "," ] "]"
    ;

dim_use
    = IDENT
    | "worst" "(" IDENT ")"
    | IDENT "in" "[" [ IDENT { "," IDENT } [ "," ] ] "]"
    ;

name_template
    = STRING   (* 只允许 {param} 占位符 *)
    ;
```

- `dim_index` 出现在 `write`/`read` 的声明名、`bind`/`advance`/`variant`/
  `read_variant` 的 subject、`map` 的 `sched_rw`，以及 `resource`/`res_group`/
  `write_seq` 的实体名后。
- v1.2 的支持面不对称（`map`/`write_seq` 等不能索引）在 v2 中必须消除：
  **所有接受标识符的调度名字位置都接受 `dim_index`**，语义由 elaborator 统一处理。
- 拼写规则不变：`<machine>_<local>` 仅在同名被多个 machine 声明时使用；
  `name = "PipeA_{vlen}"` 必须在有限域上单射。
- v2.0 只有 `resource` 接受 `name` 模板；被 `SchedRW` 引用的 write/read 接口名
  仍默认显式（`name` 模板是 v2.1 候选）。

---

## 9. `extern`

```ebnf
extern_decl
    = "extern" "fn" IDENT "(" [ extern_param { "," extern_param } [ "," ] ] ")"
      "->" ( "INT" | "BOOL" ) ":" IDENT ";"
    ;

extern_param
    = IDENT ":" ( "INT" | "BOOL" )
    ;
```

签名进 `lookup`，值由 host profile 提供；未知投影/未钉住的键在展开期报错。

---

## 10. errata 处置表（v2 清零）

| ID | v1 旧写法 | v2 写法 | v2 拒绝/提示消息 |
|---|---|---|---|
| S1 | `predicate P = ...` | `predicate sched P = ...` | `predicate needs a kind right after the keyword` |
| S2 | `P(1, 2)` 参数个数不匹配 | 正式谓词调用；实参数检查 | `predicate P expects 1 argument(s), got 2` |
| S3 | `feature` 字段间 `,` | 字段体 `field;` | `expected ';'`（带行列） |
| S4 | `resource P : units = 2` | `resource P { units = 2; }` | `':' is a v1 field separator; v2 writes a field body` |
| S5 | `IDENT` 隐式含 `-` | §1.1 的显式词法规则 | 词法错误，带位置 |
| S6 | `feature F : extension` / `feature F { kind = ...; }` | `feature extension F { ... }` | `write the kind after the keyword, not 'NAME : kind'` / `kind moved to the header` |
| S7 | `predicate P : tii` | `predicate tii P { ... }` | `predicate needs a kind right after the keyword` |
| E1 | `feature` 隐式要求 `mattr` | `mattr = "..."` 正式字段 | `kind = plain requires field 'mattr'` |

块声明后的可选 `;` 不是 errata：`}` 后两种拼法都接受，推荐不写。

---

## 11. v2.0 的谓词降级预案（未启用）

Phase 5 决策门已通过，本节仅作为历史备份保留；v2.0 不保留 v1 的五关键字。

若 Phase 5 决策门判定“谓词统一”风险过高，v2.0 保留 v1 的三关键字，但只允许
**字段体**形式（不再有 `:` 属性）：

```ebnf
sti_decl     = "sti_decl" IDENT record_body ;
sti_function = "sti_function" IDENT ":" IDENT record_body ;
tii_predicate = "tii_predicate" IDENT "=" statement ;
```

这与 §4 的目标形态一一对应，后续 v2.1 可机械迁移，不需要再改其它语法。

---

## 12. 覆盖率表

每个产生式至少一个 fixture。`✓ P2` 表示 Phase 2 前端已实现且有 v1/v2 parity
测试；`P3`/`P4`/`P5` 是后续阶段。

| 产生式 | fixture | 状态 |
|---|---|---|
| `set instructions` | `v2/sets.ssdl` | P3 |
| `set dimension`（基本形式） | `v2/family-mini-v2.ssdl` | ✓ P2 |
| `set dimension` + units/projections | `v2/machine-roles-v2.ssdl` | ✓ P3 |
| `set relation` | `v2/projection-contract-v2.ssdl` | ✓ P2 |
| `predicate` 表达式 | `v2/predicates-v2.ssdl` | ✓ P3 |
| `predicate : select` | `v2/sifive7-mini-v2.ssdl` | ✓ P3 |
| `predicate prolog` | `v2/sifive7-mini-v2.ssdl` | ✓ P3 |
| `predicate : sti` / `: tii` | `v2/sifive7-mini-v2.ssdl` | ✓ P3 |
| `predicate tii` + `method =` + `mc(TII)` | `aarch64/tii.ssdl` | ✓ AArch64 P2 |
| `feature` plain/extension/tune | `v2/machine-roles-v2.ssdl` | ✓ P3 |
| `feature` typed field / host / bitmask | `v2/machine-roles-v2.ssdl` | ✓ P3 |
| `machine both` / `model` / `cpu` | `v2/machine-roles-v2.ssdl` | ✓ P3 |
| `machine family` / `instance` | `v2/family-mini-v2.ssdl` | ✓ P2 |
| `machine alias` | `v2/machine-roles.ssdl` | P3（parser 路径） |
| `resource` / `res_group` | `v2/entities-v2.ssdl` | ✓ P2 |
| `write` / `read` / `write_seq` | `v2/entities-v2.ssdl` | ✓ P2 |
| `reg_file` / `retire_unit` / queues | `v2/entities-v2.ssdl` | ✓ P2 |
| `bind` / `advance` | `v2/multiline-v2.ssdl` | ✓ P2 |
| `map` 显式下标 | `v2/map-v2.ssdl` | ✓ P2 |
| `variant` / `read_variant` | `v2/variants-v2.ssdl` | ✓ P2 |
| `itineraries` + 命名 stage | `v2/itineraries-v2.ssdl` | ✓ P2 |
| `extern` | `v2/projection-contract-v2.ssdl` | ✓ P2 |
| `host` block | `v2/sifive7-mini-v2.ssdl` | ✓ P3 |
| `dim_index` | `v2/multiline-v2.ssdl` | ✓ P2 |
| `worst` / `in []` 子集 | `v2/dim-uses.ssdl` | P3 |
| `name_template` | `v2/two-instances-v2.ssdl` | ✓ P2 |
| RUNTIME 降级 | `v2/two-instances-runtime-v2.ssdl` | ✓ P2 |

---

## 13. 迁移示例（同一模型，v1 → v2）

v1.3：

```text
set D = [A] : kind = dimension;
machine fam(vlen: INT) {
    resource PipeA : units = 1;
    write W[D];
    bind W[D] -> [PipeA], latency = vlen / 128;
}
machine m512 : fam(512) { }
```

v2：

```text
set dimension D = [A];

machine family fam(vlen: INT) {
    resource PipeA { units = 1; }
    write W[D];
    bind W[D] { resources = [PipeA]; latency = vlen / 128; }
}

machine instance m512 : fam(512) { }
```

语义完全相同：`SemProgram`、拼写、flat v1.0（由 v1 renderer 打印）、records 都不变。

---

## 14. 规范与实现的关系

1. v2 parser 只接受本文件 §1–§9 的写法；§10 的旧写法逐条给出迁移错误。
2. v1 parser 保留到 v1 兼容套件删除；两套前端共享 `SemProgram`。
3. `render(program, syntax="v2")` 必须能从同一 `SemProgram` 打印 v2；
   `render(..., syntax="v1")` 继续打印 v1.0，用于旧版对拍。
4. 本文件的任何产生式若没有 fixture，视为规范未完成。

---

## 15. 实现边界（诚实清单）

Phase 2 的 v2 前端是 `tools/ssdl/syntax_v2.py` 的 `to_v1`：先把 v2 表面转写成
v1 族源码，再走既有 elaborator。因此语义内核、拼写规则、runtime 降级和
records 契约完全未动；v2 能表达的构造在 v1 里必然已经存在。

已进入前端的构造：`set instructions|dimension|relation`（含
`projections`）、`extern fn`、`machine both|model|cpu|family|instance|alias`、
全部机器实体与关系、`itineraries`（含命名 stage 与 body 内的
`sched_model`）、`feature`（typed field / host / bitmask / extension /
tune）、`predicate` 的表达式 / `select` / `sti` / `tii` 四种形式与
`predicate prolog`（含 `sched_code(host [{...}])`）。

v2 表面已经能完整表达 §5 的示例模型：`v2/sifive7-mini-v2.ssdl` 经
v2 前端后，与手写 `sifive7-mini.expected.td` 的 records 逐字段一致
（llvm-tblgen 对拍）。

Phase 6 补齐了迁移管线：`tools/ssdl/to_v2.py` 把 v1 源转成 v2 表面，
`family_sweep.py --syntax v2` 让 14 个模型从 v2 源走完
validate → emit_td → 两条 equiv 对拍。结果是 **14/14 `records match`**；
`to_v2` → `to_v1` → 展开后的 family 文本与 v1 源逐字节相同。迁移表、
工具边界和复现命令见 `docs/dsl/2026-11-17-ssdl-v1-to-v2-migration.md`。

迁移器不是第二个语义实现：它只重排语法，遇到没有 v2 写法的构造
（例如 `tii_predicate X = return P;`）会报错而不是猜。注释不保留，
`machine both` 会在回到 v1 时显式写成 `role = both;`（默认值，records 不变）。

`machine both|model|cpu` 在 flat 层落成 `role = both|model|cpu;` 机器属性：
`both` 同时发射 model 与 processor（v1 默认），`model` 只发射
`SchedMachineModel` 与 model-scoped 记录，`cpu` 只发射 `ProcessorModel`
（`SchedModel` 指向 `model =` 命名的机器）。未写 `role` 的 v1 机器保持
“model + processor” 的旧行为。

已知透传限制：

- `machine family|instance` 的名字现在允许 `-`：族扫描器的机器名正则已与
  §1.1 对齐，拼写仍按原样进入 flat 文本，`emit_td` 负责把 `-` 转成 `_`。
- `machine alias X = Y;` 只对 parser 路径有意义；`SemProgram` 没有 alias 节点，
  所以 AC4 的 IR parity 不覆盖它。
- 全局 `itineraries` 在 IR 里是原样透传文本，源文件的对齐空格不属于契约；
  AC4 只比较 `Symbol`、`spelling`、`phase` 与机器内语句文本。
- §8 要求“所有调度名字位置都接受 `dim_index`”，但 `map_itin` 与 `map` 的
  `sched_rw` 带轴下标时，v1 族扫描器没有对应的展开路径；Phase 2 只保证
  不带下标的写法，带下标的支持属于 Phase 3 的 elaborator 工作。

