#!/usr/bin/env bash
# Reproduce the MachineScheduler debugging walkthrough end to end.
#
#   scripts/debug-sched-demo.sh
#
# Reads docs/examples/sched-debug/demo.ll, stops before the pre-RA machine
# scheduler, runs that pass alone with -debug-only=machine-scheduler, and
# writes the artifacts under $SSDL_CACHE/sched-debug (default
# ~/.cache/ssdl/sched-debug).
#
# Requires an assertions-enabled llc: `llc --version` must say
# "Optimized build with assertions" (or be a plain Debug build).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
input=$root/docs/examples/sched-debug/demo.ll
build=${SSDL_LLVM_BUILD:?set SSDL_LLVM_BUILD to an LLVM build directory}
llc=${LLC:-$build/bin/llc}
out=${SSDL_CACHE:-$HOME/.cache/ssdl}/sched-debug

triple=riscv64-unknown-elf
# sifive-7-series is a tune-only processor model: it carries no base ISA, so
# the RISC-V 64-bit features have to be requested explicitly.
cpu=sifive-7-series
attrs=+64bit,+m,+a,+f,+d,+c

if [[ ! -x $llc ]]; then
    echo "error: $llc not found or not executable" >&2
    exit 2
fi
if ! "$llc" --version | grep -q assertions; then
    echo "error: $llc is not an assertions build; -debug-only is unavailable" >&2
    exit 2
fi

mkdir -p "$out"
echo "+ $llc --version"
"$llc" --version | sed -n '1,4p'

echo "+ stop before machine-scheduler -> $out/demo-before.mir"
"$llc" -mtriple=$triple -mcpu=$cpu -mattr=$attrs -O2 \
    -stop-before=machine-scheduler -simplify-mir \
    "$input" -o "$out/demo-before.mir"

echo "+ run only machine-scheduler   -> $out/demo-after.mir"
"$llc" -run-pass=machine-scheduler "$out/demo-before.mir" \
    -o "$out/demo-after.mir" -debug-only=machine-scheduler \
    2> "$out/sched.log"

echo "+ emit assembly                -> $out/demo.s"
"$llc" -mtriple=$triple -mcpu=$cpu -mattr=$attrs -O2 \
    "$input" -o "$out/demo.s"

echo
echo "== key lines in $out/sched.log =="
grep -nE "Current Schedule Region|RegionInstrs|Critical Path|Cycle: |Pick (Top|Bot)|Ready @|hazard:|Max MOps|Final schedule" \
    "$out/sched.log" | head -60

echo
echo "== final schedule (pre-RA) =="
sed -n '/\*\*\* Final schedule/,/^$/p' "$out/sched.log" | head -20

echo
echo "== assembly =="
sed -n '/^f:/,/^\.Lfunc_end/p' "$out/demo.s"
