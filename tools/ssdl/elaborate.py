#!/usr/bin/env python3
"""Elaborate a family source into the SSDL semantic IR and render v1.0 text.

Pipeline:

    family source --split_blocks--> machine/axis structure
                  --_expand_body--> per-instance statements (axes substituted)
                  --semir---------> SemProgram (identity + phase)
                  --render--------> flat v1.0 text

Phase 1b keeps the v1.2 renderer (``family_expand._expand_body``) as the single
implementation of norm rules, so the IR path is byte-identical by construction;
``--check-golden`` proves it against ``data/ssdl-golden.sha256``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import family_expand as fe  # noqa: E402
from ssdl import golden, semir  # noqa: E402


def _head(text: str) -> tuple[str, str]:
    """Kind and declared name of a rendered top-level statement."""
    parts = text.split(None, 2)
    if not parts:
        return "", ""
    kind = parts[0]
    name = ""
    if len(parts) >= 2:
        token = parts[1]
        if token not in ("=", "{", "->", ":") and "=" not in token:
            if not token.startswith(("(", "[", "{")):
                name = token.rstrip(";,:")
    if kind == "predicate_prolog":
        name = ""
    return kind, name


def _decls(
    lines: list[str],
    line_of: dict[tuple[str, str], int],
    scope: str,
) -> list[semir.Decl]:
    """Turn rendered statements into declarations, grouping brace blocks."""
    out: list[semir.Decl] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kind, name = _head(line)
        if line.endswith("{"):
            depth = fe._brace_delta(line)
            block = [line]
            index += 1
            while index < len(lines) and depth > 0:
                block.append(lines[index])
                depth += fe._brace_delta(lines[index])
                index += 1
            if depth != 0:
                raise fe.ExpandError(f"unterminated block in expanded machine {scope}")
            out.append(
                semir.Decl(
                    semir.Symbol(scope, kind, name),
                    "\n".join(block),
                    line_of.get((kind, name), 0),
                )
            )
            continue
        out.append(
            semir.Decl(
                semir.Symbol(scope, kind, name),
                line,
                line_of.get((kind, name), 0),
            )
        )
        index += 1
    return out


def elaborate(
    text: str,
    *,
    add_axis: tuple[str, str] | None = None,
    origins: list[tuple[str, str, int]] | None = None,
) -> semir.SemProgram:
    """Build the semantic program; all values here are ELAB."""
    dimensions, machines, globals_ = fe.split_blocks(text)
    if add_axis:
        axis, value = add_axis
        if axis not in dimensions:
            raise fe.ExpandError(
                f"cannot add {value!r}: dimension {axis!r} is not declared"
            )
        if value not in dimensions[axis].values:
            dimensions[axis].values.append(value)

    program = semir.SemProgram(
        dimensions={
            name: semir.Dimension(
                name=name,
                values=tuple(axis.values),
                name_format=axis.name_format,
                units=tuple(axis.units) if axis.units is not None else None,
                projections=tuple(
                    (key, tuple(values)) for key, values in axis.extra.items()
                ),
            )
            for name, axis in dimensions.items()
        }
    )
    program.globals = _decls(list(globals_), {}, "")

    families = {
        machine.name: machine
        for machine in machines
        if machine.family is None and machine.params
    }
    for machine in machines:
        if machine.family is not None:
            base = families.get(machine.family.name)
            if base is None:
                raise fe.ExpandError(
                    f"machine {machine.name}: unknown family {machine.family.name!r}"
                )
            if len(base.params) != len(machine.args):
                raise fe.ExpandError(
                    f"machine {machine.name}: family {base.name} expects "
                    f"{len(base.params)} args, got {len(machine.args)}"
                )
            env = dict(zip(base.params, machine.args))
            body = list(machine.body) + list(base.body)
            params = tuple(base.params)
            family: str | None = base.name
            args = tuple(machine.args)
        else:
            if machine.params:
                continue  # a family declaration is not emitted by itself
            env = {}
            body = list(machine.body)
            params = ()
            family = None
            args = ()

        local: list[tuple[str, str, int]] = []
        lines = fe._expand_body(body, env, dimensions, local)
        if origins is not None:
            origins.extend(local)
        line_of = {(kind, name): line for kind, name, line in local}
        program.machines.append(
            semir.Machine(
                name=machine.name,
                params=params,
                family=family,
                args=args,
                decls=_decls(lines, line_of, machine.name),
            )
        )
    return program


def render(program: semir.SemProgram) -> str:
    """The flat v1.0 text, byte-identical to the v1.2 expander."""
    lines = [decl.text for decl in program.globals]
    if program.globals:
        lines.append("")
    for machine in program.machines:
        lines.append(f"machine {machine.name} {{")
        for decl in machine.decls:
            lines.extend(f"    {line}" for line in decl.text.split("\n"))
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def expand_text(
    text: str,
    *,
    add_axis: tuple[str, str] | None = None,
    origins: list[tuple[str, str, int]] | None = None,
) -> str:
    return render(elaborate(text, add_axis=add_axis, origins=origins))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("-o", "--out")
    ap.add_argument("--symbols", action="store_true", help="print declaration identities")
    ap.add_argument(
        "--check-golden",
        action="store_true",
        help="compare this fixture's expansion with data/ssdl-golden.sha256",
    )
    args = ap.parse_args(argv)

    path = Path(args.input)
    text = path.read_text(encoding="utf-8")
    try:
        program = elaborate(text)
    except fe.ExpandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.symbols:
        for symbol in program.symbols():
            print(symbol.render())
        return 0

    if args.check_golden:
        try:
            name = f"fixture:{path.resolve().relative_to(ROOT).as_posix()}"
        except ValueError:
            print(f"error: {path} is outside {ROOT}", file=sys.stderr)
            return 2
        pinned = golden.read_manifest()
        if pinned is None or name not in pinned:
            print(f"error: {name} is not pinned; run tools/ssdl/golden.py --write", file=sys.stderr)
            return 2
        actual = golden.digest(render(program))
        if actual != pinned[name].sha:
            print(f"error: {name}: {pinned[name].sha[:12]} != {actual[:12]}", file=sys.stderr)
            return 1
        print(f"ok: {name} matches golden")
        return 0

    flat = render(program)
    if args.out:
        Path(args.out).write_text(flat)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(flat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
