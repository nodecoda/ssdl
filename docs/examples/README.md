# 例子目录：先读哪个

| 文件 | 角色 | 规模 |
|---|---|---|
| `sifive7-family-tour-v2.ssdl` | **阅读入口**：一个 SiFive7 风格模型的语法走查，手写、每种构造一处 | ~150 行 |
| `sifive7-family-v2.ssdl` | 整颗 SiFive7 的**表面**生成物，`to_v2(IR)` 的字节等价产物 | ~3700 行 |
| `sifive7-family.ssdl` | 同一模型的 **IR** 文本，golden 展开基线 | ~3700 行 |
| `sched-debug/` | MachineScheduler 单步调试示例（`.ll` / `.mir` / 日志） | 小 |

两份整模型文件**不是给人逐行读的**：它们是“生成器 + 等价性”的证据，
由 `family_mine.py` 产出 IR、`to_v2.py` 产出表面，`check.py` 断言两者
展开一致。要理解语法，读 tour；要验证全模型，跑 sweep。

```bash
# 只校验 tour 的语法与降级
python3 tools/ssdl/validate.py docs/examples/sifive7-family-tour-v2.ssdl --syntax v2
python3 tools/ssdl/emit_td.py docs/examples/sifive7-family-tour-v2.ssdl \
    --syntax v2 --target-include SSDLStub.td -o /tmp/tour.td
```

tour 只保留语义层已实现的构造（`set instructions`、`predicate prolog`、
`machine alias` 目前只在 flat 前端可用，未写入走查）。
