#!/usr/bin/env python3
"""Run the SSDL family pipeline over every RISC-V scheduling model.

For each ``SchedMachineModel`` that a processor points at, this script

  1. mines/emits a compressed SSDL source,
  2. validates it (the parser expands, ``check_coverage`` runs, diagnostics fire),
  3. lowers it to TableGen and dumps records,
  4. compares those records with the in-tree model (``equiv.py``), both through
     ``llvm-tblgen`` and straight from the record table.

It is the sweep version of the four hand-verified instances in
``data/riscv-sched-profiles.json``: every model gets a synthetic profile
(processor name -> instance, empty resource prefix, family = instance_family).
Results are written as ``results.json`` and ``summary.md`` under ``--out``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import paths  # noqa: E402
from ssdl.census_riscv import llvm_commit  # noqa: E402
from ssdl.diagnose import diagnose  # noqa: E402
from ssdl.parser import parse_file  # noqa: E402
from ssdl.records import load, unquote  # noqa: E402


def sanitise(name: str) -> str:
    out = re.sub(r"[^0-9A-Za-z_]", "_", name)
    if not out or out[0].isdigit():
        out = "m_" + out
    return out


def discover(records: Path, only: list[str] | None) -> list[dict]:
    recs = load(str(records))
    by_model: dict[str, list[str]] = {}
    for name in recs.body:
        if recs.cls.get(name) not in (
            "RISCVProcessorModel",
            "RISCVTuneProcessorModel",
            "ProcessorModel",
        ):
            continue
        model = recs.value(name, "SchedMachineModel SchedModel")
        if model:
            by_model.setdefault(model, []).append(name)
    models = []
    for name in recs.body:
        if recs.cls.get(name) != "SchedMachineModel" or name == "NoSchedModel":
            continue
        if only and name not in only:
            continue
        processors = sorted(by_model.get(name, []))
        if not processors:
            continue
        processor = processors[0]
        display = unquote(recs.value(processor, "string Name")) or processor
        instance = sanitise(display.replace("-", "_")).lower()
        family = instance + "_family"
        models.append(
            {
                "model": name,
                "processor": processor,
                "instance": instance,
                "family": family,
                "processor_name": display,
                "processors": processors,
            }
        )
    return models


@dataclass
class Result:
    model: str
    status: str
    detail: str
    summary: list[str]
    diagnostics: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.diagnostics is None:
            self.diagnostics = []


LEVEL_ORDER = {"error": 0, "warning": 1, "note": 2}


def diagnose_source(source: Path) -> list[dict]:
    """Count effect findings in one mined source, keyed by level and effect.

    A family source is expanded by ``parse_file``, so the counts are the same
    whether the caller keeps the authored form or the flat form.
    """
    counts: dict[tuple[str, str], int] = {}
    for finding in diagnose(parse_file(str(source))):
        key = (finding.level, finding.effect or "")
        counts[key] = counts.get(key, 0) + 1
    return [
        {"level": level, "effect": effect, "count": count}
        for (level, effect), count in sorted(
            counts.items(), key=lambda item: (LEVEL_ORDER[item[0][0]], item[0][1])
        )
    ]


def diagnostics_table(results: list[Result], llvm_commit_id: str = "unknown") -> str:
    """Deterministic CSV: one row per model, level, and effect."""
    lines = [
        "# RISC-V scheduling effect diagnostics; regenerate with "
        "tools/ssdl/family_sweep.py --diagnose-only",
        f"# llvm={llvm_commit_id}",
        "model,level,effect,count",
    ]
    for result in results:
        for item in result.diagnostics:
            lines.append(
                f"{result.model},{item['level']},{item['effect']},{item['count']}"
            )
    return "\n".join(lines) + "\n"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def run_model(item: dict, args: argparse.Namespace) -> Result:
    out = args.out / sanitise(item["model"])
    out.mkdir(parents=True, exist_ok=True)
    ssdl = out / "family.ssdl"
    flat = out / "flat.ssdl"
    if args.diagnose_only:
        # Prefer the authored family source; older sweep directories only have
        # the flat one, and both expand to the same text.
        source = ssdl if ssdl.is_file() else flat
        if not source.is_file():
            return Result(item["model"], "FAIL", f"missing {ssdl}", [])
        return Result(item["model"], "match", "", [], diagnose_source(source))
    profile = {
        "profiles": {
            "sweep": {
                "model": item["model"],
                "vlen": args.vlen,
                "prefix": "",
                "family": item["family"],
                "instance": item["instance"],
                "processor": item["processor"],
                "resource_pattern": "^$",
                "lmul": ["MF8", "MF4", "MF2", "M1", "M2", "M4", "M8"],
                "lmul_units": [1, 2, 4, 8, 16, 32, 64],
            }
        }
    }
    profile_path = out / "profile.json"
    profile_path.write_text(json.dumps(profile))

    td = out / "flat.td"
    records_json = out / "flat.json"
    rec = out / "generated.rec"
    eq = out / "equiv.txt"
    py = sys.executable
    steps = [
        [py, str(ROOT / "tools/ssdl/family_mine.py"), "--records", str(args.records),
         "--profiles", str(profile_path), "--profile", "sweep", "--emit", str(ssdl)],
        [py, str(ROOT / "tools/ssdl/validate.py"), str(ssdl)],
        [py, str(ROOT / "tools/ssdl/emit_td.py"), str(ssdl),
         "--target-include", "SSDLStub.td",
         "--records-json", str(records_json), "-o", str(td)],
        [args.tblgen, "-print-records",
         "-I", str(args.llvm_src / "include"),
         "-I", str(ROOT / "tests/ssdl/stub-target"),
         "-o", str(rec), str(td)],
        [py, str(ROOT / "tools/ssdl/equiv.py"), str(args.records), str(rec),
         "--in-model", item["model"],
         "--gen-model", item["instance"] + "Model",
         "--in-processor", item["processor"],
         "--gen-processor", item["instance"]],
        # The same comparison straight from the record table, so the claim does
        # not depend on llvm-tblgen reproducing the table.
        [py, str(ROOT / "tools/ssdl/equiv.py"), str(args.records), str(records_json),
         "--in-model", item["model"],
         "--gen-model", item["instance"] + "Model",
         "--in-processor", item["processor"],
         "--gen-processor", item["instance"],
         "--llvm-src", str(args.llvm_src)],
    ]
    last_stdout = ""
    for cmd in steps:
        proc = run(cmd, ROOT)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return Result(
                item["model"], "FAIL", detail[0] if detail else "command failed", []
            )
        last_stdout = proc.stdout
    diagnostics = diagnose_source(ssdl)
    eq.write_text(last_stdout)
    text = last_stdout
    summary = [line for line in text.splitlines() if line.startswith(("intree:", "generated:"))]
    if "records match" in text:
        return Result(item["model"], "match", "", summary, diagnostics)
    detail = next((line for line in text.splitlines() if line.startswith("MISMATCH")), "no match")
    first = next(
        (line.strip() for line in text.splitlines() if line.strip().endswith("only in intree")
         or line.strip().endswith("only in generated") or line.endswith("differ")),
        "",
    )
    return Result(
        item["model"], "MISMATCH", f"{detail}; {first}".strip("; "), summary, diagnostics
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--records", type=Path, default=paths.RECORDS)
    ap.add_argument("--llvm-src", type=Path, default=paths.llvm_src(),
                    help="LLVM checkout (default: $SSDL_LLVM_SRC)")
    ap.add_argument("--tblgen", default=paths.tblgen(),
                    help="llvm-tblgen (default: $SSDL_LLVM_BUILD/bin/llvm-tblgen)")
    ap.add_argument("--out", type=Path, default=paths.SWEEP)
    ap.add_argument("--vlen", type=int, default=128)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument(
        "--diagnose-only",
        action="store_true",
        help="skip mine/expand/lower and diagnose the flat sources already in --out",
    )
    ap.add_argument(
        "--diagnostics-table",
        type=Path,
        help="write the per-model effect-finding counts as CSV",
    )
    args = ap.parse_args(argv)
    if args.llvm_src is None:
        ap.error("--llvm-src is required when SSDL_LLVM_SRC is unset")
    if not args.tblgen:
        ap.error(
            "--tblgen is required when SSDL_LLVM_BUILD is unset and "
            "llvm-tblgen is not on PATH"
        )
    args.out.mkdir(parents=True, exist_ok=True)

    models = discover(args.records, args.only)
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_model, item, args) for item in models]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result.status:8s} {result.model} {result.detail}".rstrip())
    results.sort(key=lambda r: r.model)

    (args.out / "results.json").write_text(
        json.dumps([result.__dict__ for result in results], indent=2) + "\n"
    )
    lines = ["# RISC-V SSDL sweep", ""]
    matched = sum(1 for result in results if result.status == "match")
    lines.append(f"{matched}/{len(results)} models records match")
    lines.append("")
    lines.append("| model | status | evidence |")
    lines.append("|---|---|---|")
    for result in results:
        evidence = result.summary[0].split("intree:")[-1].strip() if result.summary else result.detail
        lines.append(f"| `{result.model}` | {result.status} | {evidence} |")
    lines.append("")
    lines.append("| model | errors | warnings | notes |")
    lines.append("|---|---|---|---|")
    for result in results:
        counts = {
            level: sum(item["count"] for item in result.diagnostics if item["level"] == level)
            for level in ("error", "warning", "note")
        }
        lines.append(
            f"| `{result.model}` | {counts['error']} | {counts['warning']} | {counts['note']} |"
        )
    warnings = [
        (result.model, item)
        for result in results
        for item in result.diagnostics
        if item["level"] in ("error", "warning")
    ]
    if warnings:
        lines.append("")
        lines.append("## Loud findings")
        lines.append("")
        for model, item in warnings:
            lines.append(f"- `{model}`: `{item['effect']}` ×{item['count']} ({item['level']})")
    (args.out / "summary.md").write_text("\n".join(lines) + "\n")
    if args.diagnostics_table:
        args.diagnostics_table.write_text(
            diagnostics_table(results, llvm_commit(args.llvm_src))
        )
    return 0 if matched == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
