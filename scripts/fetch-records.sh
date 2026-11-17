#!/usr/bin/env bash
# Regenerate the RISC-V -print-records dump that check.py compares against.
#
#   scripts/fetch-records.sh <llvm-commit> [output]
#
# The dump goes to $SSDL_CACHE/riscv.rec by default.  Only the two source
# directories TableGen needs are exported from <llvm-commit>, so the working
# tree is untouched and a full LLVM checkout is not duplicated.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 <llvm-commit> [output]" >&2
    exit 2
fi

commit=$1
src=${SSDL_LLVM_SRC:?set SSDL_LLVM_SRC to an LLVM checkout}
build=${SSDL_LLVM_BUILD:?set SSDL_LLVM_BUILD to an LLVM build directory}
cache=${SSDL_CACHE:-$HOME/.cache/ssdl}
out=${2:-$cache/riscv.rec}
tblgen=$build/bin/llvm-tblgen

if [[ ! -x $tblgen ]]; then
    echo "error: $tblgen not found or not executable" >&2
    exit 2
fi
git -C "$src" rev-parse --verify "$commit^{commit}" >/dev/null

# $SSDL_LLVM_SRC may be the repository root or a subdirectory of it (the
# common "task tree" layout puts llvm/ under a wrapper repository).
top=$(git -C "$src" rev-parse --show-toplevel)
prefix=$(git -C "$src" rev-parse --show-prefix)
mkdir -p "$(dirname "$out")"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/ssdl-records.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# Pathspecs are relative to the repository top level, not to $src.
git -C "$top" archive "$commit" \
    "${prefix}include" "${prefix}lib/Target/RISCV" | tar -x -C "$tmp"

llvm=$tmp/${prefix%/}
"$tblgen" -print-records \
    -I "$llvm/include" \
    -I "$llvm/lib/Target/RISCV" \
    -o "$out" \
    "$llvm/lib/Target/RISCV/RISCV.td"

printf 'wrote %s from %s\n' "$out" "$(git -C "$src" rev-parse --short "$commit")"
