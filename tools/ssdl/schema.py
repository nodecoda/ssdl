#!/usr/bin/env python3
"""Build an SSDL attribute schema from ``llvm-tblgen -print-records``.

SSDL 1.1 deliberately does not define a universal attribute universe: the host
supplies one.  This tool exports the part the target description already knows
(instruction bits such as ``isPseudo``/``mayLoad``, and register bits such as
``isConstant``) and lets the caller merge semantic attributes that only the
host can provide (for example "this register is the architectural zero").

Output format (same as ``tests/ssdl/attrs.json``)::

    {"order": {"inst": [...], "reg": [...]},
     "inst": {"ADD": {"is_pseudo": false, ...}},
     "reg":  {"X0": {"is_constant": true, ...}}}

Only scalar fields are exported.  Lists, dags and code blocks are skipped
because the set evaluator's comparisons are typed over bool/int/string.

Fields whose value is still unset in the record dump (``mayLoad = ?``) are
exported as JSON ``null``.  They are *not* false: TableGen infers most of them
later (``CodeGenDAGPatterns::InferInstructionFlags``), so a comparison against
``null`` must be an error rather than a silent ``false``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

RECORD_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(?://\s*(.*))?$")
FIELD_RE = re.compile(r"^\s+(?:bit|int|string)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?);\s*$")

INST_FIELDS = {
    "isPseudo": "is_pseudo",
    "mayLoad": "may_load",
    "mayStore": "may_store",
    "isBranch": "is_branch",
    "isTerminator": "is_terminator",
    "isCodeGenOnly": "is_code_gen_only",
    "isAsmParserOnly": "is_asm_parser_only",
    "isBarrier": "is_barrier",
    "isCall": "is_call",
    "isReturn": "is_return",
    "hasSideEffects": "has_side_effects",
    "isConvergent": "is_convergent",
}

REG_FIELDS = {
    "isConstant": "is_constant",
    "isArtificial": "is_artificial",
    "CoveredBySubRegs": "covered_by_subregs",
}


_UNKNOWN = object()


def _value(text: str) -> Any:
    text = text.strip()
    if text == "?":
        return _UNKNOWN
    if text in ("0", "false"):
        return False
    if text in ("1", "true"):
        return True
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return None


def records(text: str):
    """Yield ``(name, classes, fields)`` for each top-level record."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = RECORD_RE.match(lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1)
        classes = set((match.group(2) or "").split())
        start = i
        depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while depth > 0 and i < len(lines):
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        fields: dict[str, Any] = {}
        for line in lines[start + 1 : i - 1]:
            field = FIELD_RE.match(line)
            if not field:
                continue
            value = _value(field.group(2))
            if value is None:
                continue
            fields[field.group(1)] = None if value is _UNKNOWN else value
        yield name, classes, fields


def build_schema(
    records_text: str,
    merge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order = {"inst": [], "reg": []}
    inst: dict[str, dict[str, Any]] = {}
    reg: dict[str, dict[str, Any]] = {}
    for name, classes, fields in records(records_text):
        if name.startswith("anonymous_"):
            continue
        if "Instruction" in classes:
            order["inst"].append(name)
            inst[name] = {
                out: fields[src] for src, out in INST_FIELDS.items() if src in fields
            }
        elif "Register" in classes:
            order["reg"].append(name)
            reg[name] = {
                out: fields[src] for src, out in REG_FIELDS.items() if src in fields
            }

    if merge:
        for kind, table in (("inst", inst), ("reg", reg)):
            for name, attrs in merge.get(kind, {}).items():
                if name not in table:
                    table[name] = {}
                    order[kind].append(name)
                table[name].update(attrs)

    return {"order": order, "inst": inst, "reg": reg}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("records", help="llvm-tblgen -print-records output")
    ap.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    ap.add_argument(
        "--merge",
        default=None,
        help="JSON schema whose semantic attributes are merged on top",
    )
    args = ap.parse_args(argv)

    try:
        with open(args.records, encoding="utf-8") as f:
            text = f.read()
        merge = None
        if args.merge:
            with open(args.merge, encoding="utf-8") as f:
                merge = json.load(f)
    except OSError as exc:
        print(f"schema: {exc}", file=sys.stderr)
        return 2

    schema = build_schema(text, merge)
    output = json.dumps(schema, indent=2, sort_keys=False) + "\n"
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
        except OSError as exc:
            print(f"schema: {exc}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
