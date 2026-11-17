# MachineScheduler 调试示例

`demo.c` 是源，`demo.ll` 是已重定向到 RISC-V 的 IR（宿主 clang 生成的
`target-cpu`/`target-features` 属性已去掉）。

一键复现：

```bash
export SSDL_LLVM_BUILD=/path/to/llvm-build        # 含 bin/llc 与断言
scripts/debug-sched-demo.sh
```

脚本会做四件事，产物写到 `$SSDL_CACHE/sched-debug/`：

```text
demo-before.mir   -stop-before=machine-scheduler 的 MIR
sched.log         -run-pass=machine-scheduler 的 -debug-only 日志
demo-after.mir    调度后的 MIR
demo.s            最终汇编
```

对应讲解见 `docs/dsl/2026-11-17-debugging-machine-scheduler.md`。

注意：`sifive-7-series` 是 tune-only CPU，不带基础 ISA，所以脚本显式加
`-mattr=+64bit,+m,+a,+f,+d,+c`；缺了它会报
`RV64 target requires an RV64 CPU`。
