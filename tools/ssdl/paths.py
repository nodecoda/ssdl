#!/usr/bin/env python3
"""External paths, in one place.

Every tool in this tree reads LLVM locations and cache locations from here;
no module hard-codes an absolute path.  Three environment variables exist:

    SSDL_LLVM_SRC    LLVM checkout to read TD declarations and RISC-V sources
    SSDL_LLVM_BUILD  LLVM build directory holding ``bin/llvm-tblgen``
    SSDL_CACHE       non-source artifacts (default: ``~/.cache/ssdl``)

Derived cache entries:

    $SSDL_CACHE/<target>.rec   ``llvm-tblgen -print-records`` dump per target
    $SSDL_CACHE/sweep-final/   ``family_sweep.py`` output, compared by check.py

Neither cache entry is checked in: both are reproducible with
``scripts/fetch-records.sh <commit>`` and ``family_sweep.py`` respectively.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LLVM_SRC_ENV = "SSDL_LLVM_SRC"
LLVM_BUILD_ENV = "SSDL_LLVM_BUILD"
CACHE_ENV = "SSDL_CACHE"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def llvm_src() -> Path | None:
    """The LLVM checkout, or None when ``SSDL_LLVM_SRC`` is unset."""
    return _env_path(LLVM_SRC_ENV)


def llvm_build() -> Path | None:
    """The LLVM build directory, or None when ``SSDL_LLVM_BUILD`` is unset."""
    return _env_path(LLVM_BUILD_ENV)


def cache_dir() -> Path:
    value = os.environ.get(CACHE_ENV)
    return Path(value).expanduser() if value else Path.home() / ".cache" / "ssdl"


CACHE = cache_dir()
#: RISC-V stays the default so every existing gate keeps its meaning; other
#: targets go through ``records_path`` instead of a second global.
RECORDS = CACHE / "riscv.rec"
SWEEP = CACHE / "sweep-final"


def records_path(target: str) -> Path:
    """The record dump for ``target`` (``riscv`` -> ``$SSDL_CACHE/riscv.rec``)."""
    return CACHE / f"{target}.rec"


def tblgen() -> str | None:
    """``llvm-tblgen`` from the build tree, else one from ``PATH``."""
    build = llvm_build()
    if build is not None:
        candidate = build / "bin" / "llvm-tblgen"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("llvm-tblgen")


def require_llvm_src() -> Path:
    src = llvm_src()
    if src is None:
        raise SystemExit(
            f"{LLVM_SRC_ENV} is not set; point it at an LLVM checkout, e.g.\n"
            f"  export {LLVM_SRC_ENV}=/path/to/llvm-project"
        )
    return src
