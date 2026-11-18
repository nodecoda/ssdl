#!/usr/bin/env python3
"""Census of one target's in-tree scheduling models.

The census answers three questions that the SSDL v1.2 plan depends on:

1. How large is each model *as authored* (source lines, ``foreach``, ``defm``,
   ``WriteRes<`` statements)?
2. How large is it *after expansion* (explicit ``WriteRes`` / ``ReadAdvance``
   records, resources, ``InstRW``, variants)?
3. Which dimensions does the author spell out (``foreach x = ...`` in ...)?

Methodology note (the trap this tool exists to avoid): ``llvm-tblgen
-print-records`` emits a ``WriteRes`` for *every* ``SchedWrite`` and a
``ReadAdvance`` for *every* ``SchedRead``, even when the model never bound it.
Those fill-ins carry ``Unsupported = 1``.  Counting them as authored work makes
every model look like ~4200 records; Rocket actually has 67 explicit writes.
The ``*_explicit`` columns exclude ``Unsupported = 1``.

Models are enumerated from the record table, not from the source text: a model
is any record whose last class ends in ``SchedModel`` / ``SchedMachineModel``
(``SiFive7SchedMachineModel``, ``SyntacoreSchedModel``, ...), minus
``NoSchedModel``.  Enumerating with ``class == SchedMachineModel`` misses 11 of
the target's models.

Usage::

    python3 tools/ssdl/census_sched.py --records "$SSDL_CACHE/riscv.rec" \
        --llvm "$SSDL_LLVM_SRC" -o data/riscv-sched-census.csv
    python3 tools/ssdl/census_sched.py --target aarch64 \
        --records "$SSDL_CACHE/aarch64.rec" ... -o data/aarch64-sched-census.csv
    python3 tools/ssdl/census_sched.py ... --check   # fail if out of date
"""
from __future__ import annotations

import argparse
import csv
import difflib
import io
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import paths as paths_mod  # noqa: E402
from ssdl import records as records_mod  # noqa: E402
from ssdl import targets as targets_mod  # noqa: E402

COLUMNS = [
    "file",
    "model",
    "lines",
    "foreach",
    "defm",
    "write_res_stmts",
    "write_res_explicit",
    "read_advance_explicit",
    "proc_resources",
    "instr_rw",
    "variants",
    "family_class",
    "dimension_axes",
]

MODEL_CLASS_SUFFIXES = ("SchedModel", "SchedMachineModel")
SCOPED_FIELD_RE = re.compile(r"SchedMachineModel\s+SchedModel\s*=\s*(\w+)")
FOREACH_RE = re.compile(r"^\s*foreach\s+(\w+)\s*=\s*(.+?)\s+in\b")
DEF_RE_TEMPLATE = r"^\s*def\s+{name}\s*[:{{;]"


def is_model_class(cls: str | None) -> bool:
    return bool(cls) and any(cls.endswith(s) for s in MODEL_CLASS_SUFFIXES)


def model_names(recs: "records_mod.RecordFile") -> list[str]:
    """Every concrete model, sorted.  ``NoSchedModel`` is not a model."""
    return sorted(
        name
        for name, cls in recs.cls.items()
        if is_model_class(cls) and name != "NoSchedModel"
    )


def index_by_model(recs: "records_mod.RecordFile") -> dict[str, list[str]]:
    """One pass over the record table: ``SchedModel`` -> defs that scope to it.

    ``RecordFile.scoped_to`` rescans every body per model; with 25 models and a
    150 MB dump that is 25 full scans.  One pass keeps the census interactive.
    """
    index: dict[str, list[str]] = defaultdict(list)
    for name, body in recs.body.items():
        for line in body:
            match = SCOPED_FIELD_RE.search(line)
            if match:
                index[match.group(1)].append(name)
                break
    return index


def explicit(recs: "records_mod.RecordFile", name: str) -> bool:
    return recs.value(name, "bit Unsupported") != "1"


def count_class(
    recs: "records_mod.RecordFile",
    defs: list[str],
    cls: str,
    *,
    explicit_only: bool = False,
) -> int:
    return sum(
        1
        for name in defs
        if recs.cls.get(name) == cls and (not explicit_only or explicit(recs, name))
    )


def file_metrics(path: Path) -> dict[str, object]:
    text = path.read_text(errors="replace")
    axes = []
    for line in text.splitlines():
        match = FOREACH_RE.match(line)
        if match:
            axes.append(f"{match.group(1)}={match.group(2)}")
    return {
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        "foreach": len(re.findall(r"^\s*foreach\b", text, re.MULTILINE)),
        "defm": len(re.findall(r"\bdefm\b", text)),
        "write_res_stmts": len(re.findall(r"\b\w*WriteRes<", text)),
        "dimension_axes": ";".join(sorted(set(axes))),
    }


def model_source_file(target_dir: Path, model: str) -> str:
    """The ``*Sched*.td`` file that declares ``def <model>``.

    Every model in these targets is an explicit ``def`` today, so a word-boundary search
    is enough; a future ``defm``-generated model returns "-" rather than a
    guess, and Step 2 has to name its family explicitly.
    """
    pattern = re.compile(DEF_RE_TEMPLATE.format(name=re.escape(model)), re.MULTILINE)
    for path in sorted(target_dir.glob("*Sched*.td")):
        if pattern.search(path.read_text(errors="replace")):
            return path.name
    return "-"


def llvm_commit(llvm_dir: Path | None) -> str:
    if llvm_dir is None:
        return "unknown"
    try:
        proc = subprocess.run(
            ["git", "-C", str(llvm_dir), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return proc.stdout.strip()


def census(
    recs: "records_mod.RecordFile",
    *,
    target_dir: Path | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Return (rows, per-file metrics reused across rows)."""
    index = index_by_model(recs)
    rows: list[dict[str, object]] = []
    cache: dict[str, dict[str, object]] = {}
    for model in model_names(recs):
        defs = index.get(model, [])
        source = model_source_file(target_dir, model) if target_dir else "-"
        if source != "-" and source not in cache:
            cache[source] = file_metrics(target_dir / source)  # type: ignore[operator]
        metrics = cache.get(source, {
            "lines": 0,
            "foreach": 0,
            "defm": 0,
            "write_res_stmts": 0,
            "dimension_axes": "",
        })
        variants = count_class(recs, defs, "SchedWriteVariant") + count_class(
            recs, defs, "SchedReadVariant"
        )
        rows.append(
            {
                "file": source,
                "model": model,
                "lines": metrics["lines"],
                "foreach": metrics["foreach"],
                "defm": metrics["defm"],
                "write_res_stmts": metrics["write_res_stmts"],
                "write_res_explicit": count_class(
                    recs, defs, "WriteRes", explicit_only=True
                ),
                "read_advance_explicit": count_class(
                    recs, defs, "ReadAdvance", explicit_only=True
                ),
                "proc_resources": count_class(recs, defs, "ProcResource"),
                "instr_rw": count_class(recs, defs, "InstRW"),
                "variants": variants,
                "family_class": recs.cls.get(model, ""),
                "dimension_axes": metrics["dimension_axes"],
            }
        )
    rows.sort(key=lambda row: (str(row["file"]), str(row["model"])))
    return rows, sorted(cache)


def render(rows: list[dict[str, object]], commit: str, target: str = "riscv") -> str:
    out = io.StringIO()
    out.write(
        f"# {target} scheduling census; regenerate with tools/ssdl/census_sched.py\n"
    )
    out.write(f"# llvm={commit}\n")
    out.write("# explicit = record has Unsupported != 1 (TableGen fills the rest)\n")
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True, help="-print-records dump")
    parser.add_argument("--llvm", default=None, help="LLVM source root (for source metrics)")
    parser.add_argument(
        "--target", choices=("riscv", "aarch64"), default="riscv"
    )
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="CSV path (default: data/<target>-sched-census.csv)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against --out instead of writing it",
    )
    args = parser.parse_args(argv)

    llvm_dir = Path(args.llvm) if args.llvm else None
    target = targets_mod.profile(args.target)
    target_dir = llvm_dir / "lib/Target" / target.td_dir if llvm_dir else None
    if target_dir is not None and not target_dir.is_dir():
        print(f"error: {target_dir} is not a directory", file=sys.stderr)
        return 2

    recs = records_mod.load(args.records)
    rows, files = census(recs, target_dir=target_dir)
    text = render(rows, llvm_commit(llvm_dir), target.display)
    out = Path(args.out) if args.out else ROOT / f"data/{target.name}-sched-census.csv"

    if args.check:
        if not out.is_file():
            print(f"error: {out} does not exist; run without --check first", file=sys.stderr)
            return 2
        expected = out.read_text()
        if expected != text:
            diff = difflib.unified_diff(
                expected.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=str(out),
                tofile="recomputed",
                n=2,
            )
            sys.stderr.writelines(diff)
            return 1
        print(f"ok: {len(rows)} models across {len(files)} files match {out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}: {len(rows)} models across {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
