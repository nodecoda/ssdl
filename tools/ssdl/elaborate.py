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
import re
import string
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import family_expand as fe  # noqa: E402
from ssdl import golden, semir  # noqa: E402



RESOURCE_HEAD_RE = re.compile(r"^(resource|res_group)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")
NAME_PROP_RE = re.compile(r'(?:^|[,:]\s*)name\s*=\s*"([^"]*)"')
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _template(template: str, env: dict[str, object], where: str) -> str:
    """Expand ``{param}`` placeholders; only known ELAB names are allowed."""
    out: list[str] = []
    for literal, field, spec, conversion in string.Formatter().parse(template):
        out.append(literal)
        if field is None:
            continue
        if spec or conversion:
            raise fe.ExpandError(f"{where}: name template {template!r} may not use format specs")
        if field not in env:
            raise fe.ExpandError(
                f"{where}: name template {template!r} uses unknown parameter {field!r}"
            )
        out.append(str(env[field]))
    return "".join(out)


def _strip_name(line: str) -> tuple[str, str | None]:
    """Remove an inline ``name = "..."`` property from a resource declaration."""
    match = RESOURCE_HEAD_RE.match(line)
    if match is None:
        return line, None
    tail = match.group(3)
    prop = NAME_PROP_RE.search(tail)
    if prop is None:
        return line, None
    template = prop.group(1)
    rest = (tail[: prop.start()] + tail[prop.end() :]).strip()
    if rest.startswith(","):
        rest = ":" + rest[1:]
    if not rest or rest.startswith(";"):
        return f"{match.group(1)} {match.group(2)};", template
    return f"{match.group(1)} {match.group(2)} {rest}", template


def _rename_idents(segment: str, renames: dict[str, str]) -> str:
    if not renames:
        return segment
    return IDENT_RE.sub(lambda m: renames.get(m.group(0), m.group(0)), segment)


def _matching_bracket(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index
    raise fe.ExpandError(f"unbalanced [ in {text!r}")


def _rename_line(line: str, renames: dict[str, str]) -> str:
    """Rewrite resource references in one rendered statement."""
    if not renames:
        return line
    match = RESOURCE_HEAD_RE.match(line)
    if match is not None:
        kind, local = match.group(1), match.group(2)
        head = f"{kind} {renames.get(local, local)}"
        return head + _rename_idents(line[match.end(2) :], renames)
    kind, _name = _head(line)
    if kind == "bind":
        arrow = line.find("->")
        if arrow < 0:
            return line
        open_at = line.find("[", arrow)
        if open_at < 0:
            return line
        close_at = _matching_bracket(line, open_at)
        inner = _rename_idents(line[open_at + 1 : close_at], renames)
        return line[: open_at + 1] + inner + line[close_at:]
    if kind in ("load_queue", "store_queue"):
        eq = line.rfind("=")
        if eq < 0:
            return line
        return line[: eq + 1] + _rename_idents(line[eq + 1 :], renames)
    return line


def _collect_resources(
    lines: list[str], env: dict[str, object], where: str
) -> list[tuple[str, str, str | None]]:
    """(kind, local name, explicit name template) for one machine."""
    specs: list[tuple[str, str, str | None]] = []
    for line in lines:
        cleaned, template = _strip_name(line)
        match = RESOURCE_HEAD_RE.match(cleaned)
        if match is None:
            continue
        if template is not None:
            template = _template(template, env, where)
        specs.append((match.group(1), match.group(2), template))
    return specs


def _spellings(
    per_machine: list[tuple[str, list[tuple[str, str, str | None]]]],
) -> dict[str, dict[str, str]]:
    """Assign one spelling per model-scoped resource identity.

    ``SchedWrite``/``SchedRead`` are target vocabulary: the emitter hoists them
    once and only the model-scoped payload (``WriteRes``/``ReadAdvance``) is
    per machine, so two instances may share the spelling ``W_A``.  Resources
    carry their payload in the def itself, so a local name used by more than
    one machine defaults to ``<machine>_<local>`` to keep the defs distinct.
    """
    counts: Counter[str] = Counter()
    for _name, specs in per_machine:
        counts.update(local for _kind, local, _template in specs)

    result: dict[str, dict[str, str]] = {}
    seen: dict[str, list[str]] = {}
    for name, specs in per_machine:
        renames: dict[str, str] = {}
        for kind, local, explicit in specs:
            if explicit is not None:
                spelling = explicit
            elif counts[local] > 1:
                spelling = f"{name}_{local}"
            else:
                spelling = local
            identity = f"machine {name}/{kind} {local}"
            previous = seen.setdefault(spelling, [])
            if previous:
                raise fe.ExpandError(
                    f"spelling {spelling!r} is shared by {previous[0]} and {identity}"
                )
            previous.append(identity)
            if spelling != local:
                renames[local] = spelling
        result[name] = renames
    return result


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
    renames: dict[str, str] | None = None,
) -> list[semir.Decl]:
    """Turn rendered statements into declarations, grouping brace blocks."""
    renames = renames or {}
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
            spelled = "\n".join(_rename_line(item, renames) for item in block)
            out.append(
                semir.Decl(
                    semir.Symbol(scope, kind, name),
                    spelled,
                    line_of.get((kind, name), 0),
                    spelling=renames.get(name, name)
                    if kind in ("resource", "res_group")
                    else name,
                )
            )
            continue
        out.append(
            semir.Decl(
                semir.Symbol(scope, kind, name),
                _rename_line(line, renames),
                line_of.get((kind, name), 0),
                spelling=renames.get(name, name)
                if kind in ("resource", "res_group")
                else name,
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
    dimensions, relations, externs, machines, globals_ = fe.split_blocks(text)
    lookups = fe._build_lookups(relations, externs) if (relations or externs) else {}
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
    pending: list[
        tuple[
            semir.Machine,
            list[str],
            list[tuple[str, str, str | None]],
            dict[tuple[str, str], int],
        ]
    ] = []
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
        lines = fe._expand_body(body, env, dimensions, local, lookups)
        if origins is not None:
            origins.extend(local)
        specs = _collect_resources(lines, env, f"machine {machine.name}")
        cleaned = [_strip_name(line)[0] for line in lines]
        line_of = {(kind, name): line for kind, name, line in local}
        pending.append(
            (
                semir.Machine(
                    name=machine.name,
                    params=params,
                    family=family,
                    args=args,
                ),
                cleaned,
                specs,
                line_of,
            )
        )

    spellings = _spellings([(machine.name, specs) for machine, _l, specs, _lo in pending])
    for machine, lines, _specs, line_of in pending:
        machine.decls = _decls(lines, line_of, machine.name, spellings[machine.name])
        program.machines.append(machine)
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
