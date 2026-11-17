#!/usr/bin/env python3
"""Expand an ``ssdl-records`` table against the TD schema, without TableGen.

``emit_td.py --records-json`` writes the *definition* view: a def, its base
instantiations, and the fields the source bound.  ``llvm-tblgen
-print-records`` writes the *expanded* view: every inherited field, with class
parameters substituted and defaults filled in.  This module performs that
second step from the same ``td_schema`` the type checker uses, so the record
table can be compared with the in-tree dump directly -- no ``.td`` round-trip.

Only the TableGen semantics the scheduling schema relies on are implemented:
class inheritance, parameter binding (with defaults), ``let`` overrides,
``NAME``, and ``!cast<T>(x)``.  Anything else is kept verbatim and shows up as
a comparison diff instead of being silently dropped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssdl import paths, records, td_schema  # noqa: E402
from ssdl.td_value import parse_value, render  # noqa: E402

# Target classes the emitted records instantiate.  The schema sources are
# target-agnostic; RISCV's processor/feature classes live in the target tree.
RISCV_SOURCES = (
    "lib/Target/RISCV/RISCVProcessors.td",
    "lib/Target/RISCV/RISCVFeatures.td",
)


def schema_for(llvm_src: str | Path, extra: tuple[str, ...] = RISCV_SOURCES) -> td_schema.Schema:
    root = Path(llvm_src)
    paths = list(td_schema.default_sources(str(root)))
    paths += [root / rel for rel in extra if (root / rel).is_file()]
    return td_schema.parse_files(paths)


def split_args(text: str) -> list[str]:
    """Split a base-class argument string on top-level commas."""
    out: list[str] = []
    depth = 0
    quote = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                quote = False
        elif ch == '"':
            quote = True
        elif ch in "<[(":
            depth += 1
        elif ch in ">])":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


CAST_RE = re.compile(r"!cast\s*<[^>]*>\s*\(\s*([^()]*?)\s*\)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SIGN_RE = re.compile(r"(?<![\w.\"])(-|\+)\s+(\d)")


def _substitute_unquoted(text: str, env: dict[str, str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            j = i + 1
            while j < len(text):
                if text[j] == '"' and text[j - 1] != "\\":
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < len(text) and text[j] != '"':
            j += 1
        out.append(IDENT_RE.sub(lambda m: env.get(m.group(0), m.group(0)), text[i:j]))
        i = j
    return "".join(out)


def substitute(text: str, env: dict[str, str]) -> str:
    """Bind parameters, ``NAME``, and ``!cast`` in a field initialiser."""
    text = CAST_RE.sub(lambda m: m.group(1), text)
    text = _substitute_unquoted(text, env)
    return render(parse_value(SIGN_RE.sub(r"\1\2", text)))


def _bit_text(value: str) -> str:
    """``-print-records`` prints bit fields as 1/0, not true/false."""
    return {"true": "1", "false": "0"}.get(value, value)


def _apply_class(
    name: str,
    args: list[str],
    env: dict[str, str],
    put,
    schema: td_schema.Schema,
    seen: frozenset[str] = frozenset(),
) -> None:
    cls = schema.classes.get(name)
    if cls is None or name in seen:
        return
    local = dict(env)
    for index, param in enumerate(cls.params):
        if param.name is None:
            continue
        if index < len(args) and args[index]:
            local[param.name] = substitute(args[index], env)
        elif param.default:
            local[param.name] = substitute(param.default, env)
        else:
            local[param.name] = "?"
    for base in cls.bases:
        _apply_class(
            base.name,
            [substitute(arg, local) for arg in split_args(base.args)],
            local,
            put,
            schema,
            seen | {name},
        )
    for field, spec in cls.fields.items():
        value = substitute(spec.get("init", ""), local)
        if spec.get("type") == "bit":
            value = _bit_text(value)
        put(field, value)
    for field, value in cls.lets.items():
        put(field, substitute(value, local))


def expand_record(entry: dict, schema: td_schema.Schema) -> records.JsonRecordFile:
    """Fields of one def, expanded in place into a one-record table."""
    out = records.JsonRecordFile()
    name = entry["name"]
    env = {"NAME": name}
    lines: list[str] = []

    def put(field: str, value: str) -> None:
        needle = field + " = "
        for index, line in enumerate(lines):
            if line.startswith(needle):
                lines[index] = f"{field} = {value}"
                return
        lines.append(f"{field} = {value}")

    for base in entry.get("bases", ()):
        if isinstance(base, str):
            class_name, arg_text = base, ""
        else:
            class_name = base.get("class", "")
            arg_text = ", ".join(
                arg if isinstance(arg, str) else render(_from_node(arg))
                for arg in base.get("args", ())
            )
        _apply_class(class_name, split_args(arg_text), env, put, schema)
    for field, value in entry.get("fields", ()):
        if isinstance(value, dict) and value.get("kind") == "bit":
            put(field, "1" if value.get("value") else "0")
        else:
            put(field, value if isinstance(value, str) else render(_from_node(value)))
    if entry.get("scope"):
        put("SchedModel", entry["scope"])
    out.cls[name] = (
        entry["bases"][-1].get("class", "") if entry.get("bases") else ""
    )
    out.body[name] = lines
    return out


def _from_node(node):
    from ssdl.td_value import from_json_node

    return from_json_node(node)


def _anonymous_base(obj: dict) -> int:
    def number(name: str) -> int:
        match = re.fullmatch(r"anonymous_(\d+)", name)
        return int(match.group(1)) + 1 if match else 0

    best = 0
    for entry in obj.get("records", ()):
        best = max(best, number(entry["name"]))
        for base in entry.get("bases", ()):
            if isinstance(base, dict):
                for arg in base.get("args", ()):
                    best = max(best, _walk_anonymous(arg))
    return best


def _walk_anonymous(node) -> int:
    best = 0
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str) and name.startswith("anonymous_"):
            try:
                best = max(best, int(name.split("_", 1)[1]) + 1)
            except ValueError:
                pass
        for value in node.values():
            best = max(best, _walk_anonymous(value))
    elif isinstance(node, list):
        for value in node:
            best = max(best, _walk_anonymous(value))
    return best


def _hoist(node, sink: list[dict], counter: list[int]):
    """Lift inline class applications into anonymous defs, as TableGen does.

    ``print-records`` materialises ``SchedVar<...>`` inside a list as
    ``anonymous_N`` defs.  Reproducing that keeps the table comparable; the
    names only have to be unique, because the comparison strips them.
    """
    if not isinstance(node, dict):
        return node
    kind = node.get("kind")
    if kind == "apply":
        args = [_hoist(arg, sink, counter) for arg in node.get("args", ())]
        name = f"anonymous_{counter[0]}"
        counter[0] += 1
        sink.append(
            {
                "name": name,
                "bases": [{"class": node["class"], "args": args}],
                "fields": [],
            }
        )
        return {"kind": "ident", "name": name}
    if kind == "list":
        return {**node, "items": [_hoist(item, sink, counter) for item in node.get("items", ())]}
    if kind == "dag":
        return {**node, "args": [_hoist(arg, sink, counter) for arg in node.get("args", ())]}
    return node


def expand_obj(obj: dict, schema: td_schema.Schema) -> records.JsonRecordFile:
    """Expand a decoded ``ssdl-records-1`` table."""
    sink: list[dict] = []
    counter = [_anonymous_base(obj)]
    entries = []
    for entry in obj.get("records", ()):
        bases = []
        for base in entry.get("bases", ()):
            if isinstance(base, str):
                bases.append(base)
            else:
                bases.append(
                    {
                        **base,
                        "args": [_hoist(arg, sink, counter) for arg in base.get("args", ())],
                    }
                )
        fields = [
            (field, _hoist(value, sink, counter))
            for field, value in entry.get("fields", ())
        ]
        entries.append({**entry, "bases": bases, "fields": fields})
    out = records.JsonRecordFile()
    for entry in entries + sink:
        one = expand_record(entry, schema)
        for name, lines in one.body.items():
            out.cls[name] = one.cls[name]
            out.body[name] = lines
    return out


def expand_table(path: str, schema: td_schema.Schema) -> records.JsonRecordFile:
    """Expand every record of an ``ssdl-records-1`` table on disk."""
    import json

    with open(path, encoding="utf-8") as f:
        return expand_obj(json.load(f), schema)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="records JSON from emit_td.py --records-json")
    ap.add_argument(
        "--llvm-src",
        default=None,
        help="LLVM checkout holding the TD schema (default: $SSDL_LLVM_SRC)",
    )
    args = ap.parse_args(argv)
    table = expand_table(
        args.input, schema_for(args.llvm_src or paths.require_llvm_src())
    )
    for name, lines in table.body.items():
        print(f"def {name} {{ // {table.cls[name]}")
        for line in lines:
            print(f"  {line};")
        print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
