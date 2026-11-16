#!/usr/bin/env python3
"""Expand a compressed SSDL family into v1.0-compatible flat SSDL.

The compressed front end adds exactly two domain constructs; everything else
is the v1.0 language, so the flat output still goes through ``emit_td.py`` and
the record table unchanged.

    dimension LMUL = [MF8, MF4, MF2, M1, M2, M4, M8];

    machine siFive7(vlen: INT) {
        write  WriteVLDE[LMUL];
        bind   WriteVLDE[LMUL] -> [VCQ, VA1], latency = vlen / 128;
    }

    machine sifive-7-series : siFive7(512) {
        resource VCQ : units = 1;
        resource VA1 : units = 1;
    }

Rules
-----
* ``dimension NAME = [v, ...]`` or ``dimension NAME = a..b`` declares a finite
  axis.  Only declared axes may be indexed.
* ``NAME[AXIS]`` expands to one identifier per member (``WriteVLDE_MF8`` ...).
  A sparse site writes ``NAME[AXIS in [MF8, MF4, MF2]]``.
* ``machine F(p: INT, b: BOOL)`` declares a family; ``machine X : F(args)``
  instantiates it.  Parameters may appear in integer expressions
  (``latency = vlen / 128``) only; identifiers are not string-interpolated.
  That is deliberate: the axes have to stay enumerable.
* No recursion, no user functions, no arbitrary expressions outside the
  numeric fields (``latency``/``uops``/``cycles``).

``--check-coverage`` expands and then requires every declared dimensioned
write to be bound and every bound name to be declared.  ``--add-axis-value``
prepends a member to an axis before checking, which is how "add M16" is asked
today: the diagnostic names every site that does not cover the new member.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
DIM_RE = re.compile(rf"^\s*dimension\s+({IDENT})\s*=\s*(.+?)\s*;\s*$")
RANGE_RE = re.compile(r"^(-?\d+)\s*\.\.\s*(-?\d+)$")
ITEM_RE = re.compile(
    rf"^(\w+)\s+({IDENT})\s*\[\s*({IDENT})\s*(?:in\s*\[([^\]]*)\])?\s*\]\s*(.*)$"
)
FAMILY_RE = re.compile(rf"^machine\s+({IDENT})\s*\((.*?)\)\s*\{{")
INSTANCE_RE = re.compile(rf"^machine\s+({IDENT})\s*:\s*({IDENT})\s*\((.*?)\)\s*\{{")
PLAIN_RE = re.compile(rf"^machine\s+({IDENT})\s*\{{")
PARAM_RE = re.compile(rf"^\s*({IDENT})\s*:\s*(INT|BOOL)\s*$")


class ExpandError(Exception):
    pass


@dataclass
class Stmt:
    line: int
    text: str


@dataclass
class Machine:
    name: str
    params: list[str] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    family: "Machine | None" = None
    args: list[object] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    out = []
    in_string = False
    for index, char in enumerate(line):
        if char == '"':
            in_string = not in_string
        if char == "#" and not in_string:
            return line[:index]
        out.append(char)
    return line


def _parse_items(text: str) -> list[str]:
    items = [item.strip() for item in text.strip().strip("[]").split(",")]
    return [item for item in items if item]


def _eval_expr(expr: str, env: dict[str, object]) -> int:
    """Evaluate a bounded integer expression; no calls, no attributes."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpandError(f"bad expression {expr!r}: {exc}") from exc
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Name,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.Load,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ExpandError(f"unsupported syntax in {expr!r}: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in env:
            raise ExpandError(f"unknown parameter {node.id!r} in {expr!r}")
    value = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, env)  # noqa: S307
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ExpandError(f"non-integer result {value!r} in {expr!r}")
        return int(value)
    return int(value)


def _subst_fields(text: str, env: dict[str, object]) -> str:
    def repl(match: re.Match[str]) -> str:
        value = _eval_expr(match.group(2), env)
        return f"{match.group(1)} = {value}"

    return re.sub(r"\b(latency|uops|cycles)\s*=\s*([^,;]+)", repl, text)


def split_blocks(text: str) -> tuple[dict[str, list[str]], list[Machine]]:
    """Return (dimensions, machines) from the compressed source."""
    dimensions: dict[str, list[str]] = {}
    machines: list[Machine] = []
    current: Machine | None = None
    depth = 0
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if current is None:
            match = DIM_RE.match(line)
            if match:
                name, rhs = match.group(1), match.group(2)
                range_match = RANGE_RE.match(rhs)
                if range_match:
                    lo, hi = int(range_match.group(1)), int(range_match.group(2))
                    values = [str(v) for v in range(lo, hi + 1)]
                else:
                    if not rhs.startswith("["):
                        raise ExpandError(f"line {line_no}: dimension {name} expects a list or range")
                    values = _parse_items(rhs)
                if not values:
                    raise ExpandError(f"line {line_no}: dimension {name} is empty")
                dimensions[name] = values
                continue
            match = FAMILY_RE.match(line)
            if match:
                params = []
                for param in match.group(2).split(","):
                    if not param.strip():
                        continue
                    pm = PARAM_RE.match(param)
                    if not pm:
                        raise ExpandError(f"line {line_no}: bad parameter {param!r}")
                    params.append(pm.group(1))
                current = Machine(name=match.group(1), params=params)
                machines.append(current)
                depth = 1
                continue
            match = INSTANCE_RE.match(line)
            if match:
                args = []
                for arg in match.group(3).split(","):
                    if arg.strip():
                        args.append(_eval_expr(arg.strip(), {}))
                current = Machine(name=match.group(1), args=args)
                current.family = Machine(name=match.group(2))
                machines.append(current)
                depth = 1
                continue
            match = PLAIN_RE.match(line)
            if match:
                current = Machine(name=match.group(1))
                machines.append(current)
                depth = 1
                continue
            if line.strip() == "}":
                continue
            raise ExpandError(f"line {line_no}: expected a declaration, got {line.strip()!r}")
        else:
            if line.strip() == "}":
                depth -= 1
                if depth == 0:
                    current = None
                continue
            current.body.append(Stmt(line_no, line.strip()))
    if current is not None:
        raise ExpandError(f"unterminated machine {current.name}")
    return dimensions, machines


def _members(axis: str, subset: str | None, dimensions: dict[str, list[str]]) -> list[str]:
    if axis not in dimensions:
        raise ExpandError(f"undeclared dimension {axis!r}")
    declared = dimensions[axis]
    if subset is None:
        return list(declared)
    chosen = _parse_items(subset)
    unknown = [value for value in chosen if value not in declared]
    if unknown:
        raise ExpandError(f"dimension {axis} has no member(s) {unknown}")
    return chosen


def _expand_body(
    body: list[Stmt],
    env: dict[str, object],
    dimensions: dict[str, list[str]],
    origins: list[tuple[str, str, int]],
) -> list[str]:
    out: list[str] = []
    for stmt in body:
        match = ITEM_RE.match(stmt.text)
        if not match:
            out.append(_subst_fields(stmt.text, env))
            continue
        keyword, base, axis, subset, rest = match.groups()
        try:
            members = _members(axis, subset, dimensions)
        except ExpandError as exc:
            raise ExpandError(f"line {stmt.line}: {exc}") from exc
        for member in members:
            name = f"{base}_{member}"
            origins.append((keyword, name, stmt.line))
            tail = rest if rest.startswith(";") else " " + rest
            text = f"{keyword} {name}{tail}".rstrip()
            out.append(_subst_fields(text, env))
    return out


def expand(
    text: str,
    *,
    add_axis: tuple[str, str] | None = None,
    origins: list[tuple[str, str, int]] | None = None,
) -> str:
    dimensions, machines = split_blocks(text)
    if add_axis:
        axis, value = add_axis
        if axis not in dimensions:
            raise ExpandError(f"cannot add {value!r}: dimension {axis!r} is not declared")
        if value not in dimensions[axis]:
            dimensions[axis].append(value)
    families = {machine.name: machine for machine in machines if machine.family is None and machine.params}
    if origins is None:
        origins = []
    lines: list[str] = []
    for machine in machines:
        if machine.family is not None:
            base = families.get(machine.family.name)
            if base is None:
                raise ExpandError(f"machine {machine.name}: unknown family {machine.family.name!r}")
            if len(base.params) != len(machine.args):
                raise ExpandError(
                    f"machine {machine.name}: family {base.name} expects {len(base.params)} args, "
                    f"got {len(machine.args)}"
                )
            env = dict(zip(base.params, machine.args))
            body = list(machine.body) + list(base.body)
        else:
            env = {}
            body = list(machine.body)
        if machine.family is None and machine.params:
            continue  # a family declaration is not emitted by itself
        lines.append(f"machine {machine.name} {{")
        lines.extend(
            f"    {line}" for line in _expand_body(body, env, dimensions, origins)
        )
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check_coverage(
    flat: str,
    origins: list[tuple[str, str, int]] | None = None,
) -> list[str]:
    """Every dimensioned write must be bound exactly once; no orphan binds."""
    site = {(kind, name): line for kind, name, line in (origins or [])}
    errors: list[str] = []
    for block in re.finditer(r"machine\s+(\w+)\s*\{(.*?)\n\}", flat, re.DOTALL):
        name, body = block.group(1), block.group(2)
        writes = set(re.findall(r"^\s*write\s+(\w+)\s*;", body, re.MULTILINE))
        for base in writes:
            if not re.search(rf"^\s*bind\s+{re.escape(base)}\s*->", body, re.MULTILINE):
                where = site.get(("write", base))
                suffix = f" (line {where})" if where else ""
                errors.append(f"machine {name}: write {base} has no bind{suffix}")
        for bound in re.findall(r"^\s*bind\s+(\w+)\s*->", body, re.MULTILINE):
            if bound not in writes:
                where = site.get(("bind", bound))
                suffix = f" (line {where})" if where else ""
                errors.append(f"machine {name}: bind {bound} has no write{suffix}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("-o", "--out")
    parser.add_argument("--check-coverage", action="store_true")
    parser.add_argument(
        "--add-axis-value",
        metavar="AXIS=VALUE",
        help="append a member before checking coverage",
    )
    args = parser.parse_args(argv)

    add_axis = None
    if args.add_axis_value:
        axis, sep, value = args.add_axis_value.partition("=")
        if not sep or not axis or not value:
            print("error: --add-axis-value expects AXIS=VALUE", file=sys.stderr)
            return 2
        add_axis = (axis, value)

    origins: list[tuple[str, str, int]] = []
    try:
        flat = expand(Path(args.input).read_text(), add_axis=add_axis, origins=origins)
    except ExpandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check_coverage:
        errors = check_coverage(flat, origins)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("ok: coverage complete")
        return 0

    if args.out:
        Path(args.out).write_text(flat)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(flat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
