; Generated from demo.c with:
;   clang -O1 -S -emit-llvm demo.c -o demo.ll
; and then retargeted to RISC-V (the host clang emits x86 attributes).
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
target triple = "riscv64-unknown-elf"

define dso_local i64 @f(i64 %0, i64 %1, i64 %2, i64 %3) {
  %5 = mul nsw i64 %1, %0
  %6 = add i64 %2, %3
  %7 = sub i64 %5, %6
  %8 = shl i64 %7, 3
  %9 = xor i64 %1, %0
  %10 = add nsw i64 %8, %9
  ret i64 %10
}
