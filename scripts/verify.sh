#!/usr/bin/env bash
# Run the full SSDL regression suite against the configured LLVM checkout.
set -euo pipefail
: "${SSDL_LLVM_SRC:?set SSDL_LLVM_SRC to an LLVM checkout}"
exec python3 "$(cd "$(dirname "$0")/.." && pwd)/tools/ssdl/check.py" "$@"
