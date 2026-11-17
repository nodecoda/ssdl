#!/usr/bin/env python3
"""Extract the RISC-V model families and vocabulary dimensions from source.

Step 2 of the v1.2 plan: the census says *how much* is expanded; this tool
says *what the expansion is over*.  It reads the pinned upstream sources and
emits a machine-checkable table, so the language design is driven by the
actual axes rather than by the SiFive7 example alone.

Two different kinds of parameterisation are kept apart:

* ``families``  -- a ``SchedMachineModel`` subclass with template arguments and
  a handful of members (``SiFive7SchedMachineModel<int vlen>``).  These are
  machine-level: they select resources and record names.
* ``vocabulary`` -- target-level lists iterated by ``foreach`` to mint
  ``SchedWrite``/``SchedRead`` names (``SchedMxList``, SEW, EEW, NF).  These
  are not machine properties; v1.1 §9.1 already makes the vocabulary
  target-scoped.

Usage::

    python3 tools/ssdl/family_tables.py --llvm <llvm> -o data/riscv-sched-families.json
    python3 tools/ssdl/family_tables.py --llvm <llvm> --check
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import paths  # noqa: E402

SIFIVE7 = "RISCVSchedSiFive7.td"
SCHEDULE_V = "RISCVScheduleV.td"


class ExtractionError(Exception):
    """The source no longer has the shape this extractor understands."""


def _snake(name: str) -> str:
    """HasDualVALU -> has_dual_valu; FPLatencies -> fp_latencies."""
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


def _domain_product(param_names: list[str], members: list[dict]) -> int:
    """Product of the value counts actually present among the members."""
    product = 1
    for param in param_names:
        product *= len({member[_snake(param)] for member in members})
    return product


def _fplatencies(value: str) -> str:
    match = re.fullmatch(r"SiFive7(\w+?)FPLatencies", value)
    if not match:
        raise ExtractionError(f"unexpected FPLatencies value: {value}")
    return match.group(1)


def _generic_class_body(text: str, header: str) -> tuple[str, str]:
    pattern = re.compile(
        r"class\s+" + header + r"\s*\{(.*?)\n\}",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ExtractionError(f"class {header} not found")
    return match.group(1), match.group(0)


def extract_sifive7(text: str, filename: str = SIFIVE7) -> dict:
    header = r"SiFive7SchedMachineModel<\s*int\s+(\w+)\s*>"
    pattern = re.compile(
        r"class\s+SiFive7SchedMachineModel<\s*int\s+(\w+)\s*>\s*:\s*SchedMachineModel\s*\{(.*?)\n\}",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ExtractionError("SiFive7SchedMachineModel not found")
    template_arg, body = match.group(1), match.group(2)

    defaults: dict[str, object] = {}
    param_names = [template_arg]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("let "):
            continue
        m = re.match(r"int\s+(\w+)\s*=\s*(\w+);", stripped)
        if m:
            # ``int VLEN = vlen`` is the template argument, not a new axis.
            if m.group(2) != template_arg:
                defaults[m.group(1)] = int(m.group(2))
                param_names.append(m.group(1))
            continue
        m = re.match(r"bit\s+(\w+)\s*=\s*(true|false);", stripped)
        if m:
            defaults[m.group(1)] = m.group(2) == "true"
            param_names.append(m.group(1))
            continue
        m = re.match(r"(\w+)\s+(\w+);", stripped)
        if m and not stripped.startswith(("string", "list")):
            defaults[m.group(2)] = None  # no default; every member must set it
            param_names.append(m.group(2))

    member_pattern = re.compile(
        r"def\s+(SiFive7\w+Model)\s*:\s*SiFive7SchedMachineModel<\s*(\d+)\s*>\s*\{(.*?)\n\}",
        re.DOTALL,
    )
    members = []
    for member_match in member_pattern.finditer(text):
        name, vlen, member_body = member_match.groups()
        values = {template_arg: int(vlen)}
        values.update(defaults)
        for let in re.finditer(r"let\s+(\w+)\s*=\s*([^;]+);", member_body):
            field, value = let.group(1), let.group(2).strip()
            if field == "FPLatencies":
                values[field] = _fplatencies(value)
            elif value in ("true", "false"):
                values[field] = value == "true"
            else:
                values[field] = value
        missing = [p for p in param_names if values.get(p) is None]
        if missing:
            raise ExtractionError(f"{name} leaves {missing} unset")
        member = {"name": name}
        member.update({_snake(p): values[p] for p in param_names})
        members.append(member)

    if not members:
        raise ExtractionError("no SiFive7 members found")

    binding = re.search(r"foreach\s+model\s*=\s*\[", text)
    binding_site = f"{filename}:{text[: binding.start()].count(chr(10)) + 1}" if binding else None
    return {
        "class": "SiFive7SchedMachineModel",
        "template_args": [{"name": template_arg, "type": "int"}],
        "params": [_snake(p) for p in param_names],
        "defaults": {_snake(k): v for k, v in defaults.items()},
        "members": members,
        "member_count": len(members),
        "observed_domain_product": _domain_product(param_names, members),
        "binding_site": binding_site,
    }


def _parse_list(text: str) -> list[str]:
    items = [item.strip().strip('"') for item in text.strip().strip("[]").split(",")]
    return [item for item in items if item]


def extract_mx_lists(text: str, filename: str = SCHEDULE_V) -> dict:
    lists: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    removals: dict[str, tuple[str, list[str]]] = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*defvar\s+(\w+)\s*=\s*(\[[^\]]*\])\s*;", line)
        if m:
            lists[m.group(1)] = _parse_list(m.group(2))
            continue
        m = re.match(r"\s*defvar\s+(\w+)\s*=\s*!listremove\(\s*(\w+)\s*,\s*(\[[^\]]*\])\s*\)\s*;", line)
        if m:
            removals[m.group(1)] = (m.group(2), _parse_list(m.group(3)))
            continue
        m = re.match(r"\s*defvar\s+(\w+)\s*=\s*(\w+)\s*;", line)
        if m:
            aliases[m.group(1)] = m.group(2)

    if not lists:
        raise ExtractionError("no defvar lists found")

    def resolve(name: str) -> list[str]:
        if name in lists:
            return lists[name]
        if name in aliases:
            return resolve(aliases[name])
        if name in removals:
            base, drop = removals[name]
            return [value for value in resolve(base) if value not in drop]
        raise ExtractionError(f"unresolved list {name}")

    resolved = {name: resolve(name) for name in {*lists, *aliases, *removals}}
    mx_line = next(
        (no for no, line in enumerate(text.splitlines(), 1) if re.search(r"defvar\s+SchedMxList\s*=", line)),
        None,
    )
    return {
        "base": "SchedMxList",
        "values": resolved["SchedMxList"],
        "subsets": {name: values for name, values in sorted(resolved.items()) if name != "SchedMxList"},
        "source": f"{filename}:{mx_line}" if mx_line else filename,
    }


def extract_sew_sets(text: str, filename: str = SCHEDULE_V) -> dict:
    body, block = _generic_class_body(text, r"SchedSEWSet<\s*string\s+mx,\s*bit\s+isF\s*=\s*0,\s*bit\s+isWidening\s*=\s*0>")
    base = {
        m.group(1): [int(v) for v in m.group(2).replace(" ", "").split(",") if v]
        for m in re.finditer(r'!eq\(mx,\s*"(\w+)"\)\s*:\s*\[([^\]]*)\]', body)
    }
    if not base:
        raise ExtractionError("SchedSEWSet has no !cond mapping")
    if not re.search(r"!listremove\(\s*t\s*,\s*\[8\]\s*\)", body):
        raise ExtractionError("SchedSEWSet no longer removes SEW 8 for floating point")
    if not re.search(r"!listremove\(\s*remove8\s*,\s*\[64\]\s*\)", body):
        raise ExtractionError("SchedSEWSet no longer removes SEW 64 for widening")

    def apply(lmul: str, is_f: bool, is_widening: bool) -> list[int]:
        values = list(base[lmul])
        if is_f:
            values = [v for v in values if v != 8]
        if is_widening:
            values = [v for v in values if v != 64]
        return values

    derived = {}
    for lmul in base:
        # Floating point forbids MF8 (asserted in the source); skip combinations
        # the upstream class itself rejects.
        variants = {"default": (False, False), "widening": (False, True)}
        if lmul != "MF8":
            variants["fp"] = (True, False)
            variants["fp_widening"] = (True, True)
        derived[lmul] = {name: apply(lmul, *flags) for name, flags in variants.items()}

    line = text[: text.find(block)].count("\n") + 1
    return {
        "base": base,
        "derived": derived,
        "rules": {"fp_removes": 8, "widening_removes": 64},
        "source": f"{filename}:{line}" if line else filename,
    }


def extract_axis_ranges(riscv_dir: Path) -> dict:
    eew: set[int] = set()
    nf: set[int] = set()
    eew_sites: list[str] = []
    nf_sites: list[str] = []
    for path in sorted({*riscv_dir.glob("RISCVSched*.td"), *riscv_dir.glob("RISCVSchedule*.td")}):
        text = path.read_text(errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            site = f"{path.name}:{line_no}"
            m = re.search(r"foreach\s+eew\s*=\s*\[([^\]]*)\]", line)
            if m:
                values = {int(v) for v in m.group(1).replace(" ", "").split(",") if v}
                eew |= values
                eew_sites.append(site)
            m = re.search(r"foreach\s+nf\s*=\s*(\d+)-(\d+)", line)
            if m:
                nf |= set(range(int(m.group(1)), int(m.group(2)) + 1))
                nf_sites.append(site)
    return {
        "EEW": {"values": sorted(eew), "sites": eew_sites},
        "NF": {
            "min": min(nf) if nf else None,
            "max": max(nf) if nf else None,
            "values": sorted(nf),
            "sites": nf_sites,
        },
    }


def extract_projections(schedule_v: str, sifive7: str, worst_value: str) -> list[dict]:
    projections = []
    worst_body, worst_block = _generic_class_body(schedule_v, r"LargestLMUL<\s*list<string>\s+MxList>")
    if "!foldl" not in worst_body:
        raise ExtractionError("LargestLMUL no longer folds the list")
    line = schedule_v[: schedule_v.find(worst_block)].count("\n") + 1
    projections.append(
        {
            "name": "worst(LMUL)",
            "axis": "LMUL",
            "value": worst_value,
            "rule": "last element of SchedMxList (list is sorted ascending)",
            "source": f"{SCHEDULE_V}:{line}",
        }
    )

    match = re.search(
        r"class\s+SiFive7GetCyclesSegmented<\s*string\s+mx,\s*int\s+(\w+),\s*int\s+(\w+),\s*int\s+(\w+)>",
        sifive7,
    )
    if not match:
        raise ExtractionError("SiFive7GetCyclesSegmented signature changed")
    line = sifive7[: match.start()].count("\n") + 1
    projections.append(
        {
            "name": "cycles",
            "axis": ["LMUL", match.group(1), match.group(2), match.group(3)],
            "value": "vl * ceil((SEW * NF) / DLEN), DLEN = VLEN / 2",
            "source": f"{SIFIVE7}:{line}",
        }
    )
    return projections


def build(riscv_dir: Path, commit: str) -> dict:
    sifive7_text = (riscv_dir / SIFIVE7).read_text(errors="replace")
    schedule_v_text = (riscv_dir / SCHEDULE_V).read_text(errors="replace")
    ranges = extract_axis_ranges(riscv_dir)
    mx = extract_mx_lists(schedule_v_text)
    return {
        "llvm": commit,
        "source": "llvm/lib/Target/RISCV",
        "families": {"siFive7": extract_sifive7(sifive7_text)},
        "vocabulary": {
            "LMUL": mx,
            "SEW": extract_sew_sets(schedule_v_text),
            "EEW": ranges["EEW"],
            "NF": ranges["NF"],
        },
        "projections": extract_projections(schedule_v_text, sifive7_text, mx["values"][-1]),
    }


def llvm_commit(llvm_dir: Path) -> str:
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


def render(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm", default=None,
                        help="LLVM checkout (default: $SSDL_LLVM_SRC)")
    parser.add_argument("-o", "--out", default=str(ROOT / "data/riscv-sched-families.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    llvm_dir = Path(args.llvm) if args.llvm else paths.require_llvm_src()
    riscv_dir = llvm_dir / "lib/Target/RISCV"
    if not riscv_dir.is_dir():
        print(f"error: {riscv_dir} is not a directory", file=sys.stderr)
        return 2

    text = render(build(riscv_dir, llvm_commit(llvm_dir)))
    out = Path(args.out)
    if args.check:
        if not out.is_file():
            print(f"error: {out} does not exist", file=sys.stderr)
            return 2
        if out.read_text() != text:
            print(f"error: {out} is stale; regenerate it", file=sys.stderr)
            return 1
        print(f"ok: {out} matches the RISC-V sources")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
