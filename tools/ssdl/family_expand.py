#!/usr/bin/env python3
"""Expand a compressed SSDL family into v1.0-compatible flat SSDL.

The compressed front end adds exactly two domain constructs; everything else
is the v1.0 language, so the flat output still goes through ``emit_td.py`` and
the record table unchanged.

    set LMUL = [MF8, MF4, MF2, M1, M2, M4, M8] :
        kind = dimension, name_format = "_{value}";

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
* ``set NAME = [v, ...] : kind = dimension`` declares a finite axis;
  ``a..b`` is accepted inside the list.  Only declared axes may be indexed.
  ``name_format`` is the one place a member name is composed (``"_{value}"``).
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
SET_DIM_RE = re.compile(
    rf"^set\s+({IDENT})\s*=\s*(.+?)\s*:\s*kind\s*=\s*dimension"
    rf'(?:\s*,\s*name_format\s*=\s*"([^"]*)")?\s*;\s*$'
)
RANGE_RE = re.compile(r"^(-?\d+)\s*\.\.\s*(-?\d+)$")
ITEM_HEAD_RE = re.compile(rf"^(write|bind|advance)\s+({IDENT})\s*\[")
DIM_USE_WORST_RE = re.compile(rf"^worst\(\s*({IDENT})\s*\)$")
DIM_USE_SUBSET_RE = re.compile(rf"^({IDENT})\s+in\s*\[([^\]]*)\]$")
DIM_USE_RE = re.compile(rf"^({IDENT})$")
FAMILY_RE = re.compile(rf"^machine\s+({IDENT})\s*\((.*?)\)\s*\{{")
INSTANCE_RE = re.compile(rf"^machine\s+({IDENT})\s*:\s*({IDENT})\s*\((.*?)\)\s*\{{")
PLAIN_RE = re.compile(rf"^machine\s+({IDENT})\s*\{{")
PARAM_RE = re.compile(rf"^\s*({IDENT})\s*:\s*(INT|BOOL)\s*$")


class ExpandError(Exception):
    pass


@dataclass
class Axis:
    values: list[str]
    name_format: str = "_{value}"

    def name(self, member: str) -> str:
        if "{value}" not in self.name_format:
            raise ExpandError(f"name_format {self.name_format!r} has no {{value}} placeholder")
        return self.name_format.replace("{value}", member)


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


def _parse_item(text: str):
    """Split ``write W[A, B in [x, y]] rest`` with balanced brackets."""
    head = ITEM_HEAD_RE.match(text)
    if not head:
        return None
    depth = 1
    index = head.end()
    while index < len(text) and depth:
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
        index += 1
    if depth:
        return None
    return head.group(1), head.group(2), text[head.end(): index - 1], text[index:].strip()


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


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


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join a ``set`` declaration that spans lines; everything else is one line."""
    raw_lines = text.splitlines()
    out: list[tuple[int, str]] = []
    index = 0
    while index < len(raw_lines):
        line = _strip_comment(raw_lines[index]).strip()
        if line.startswith("set ") and not line.endswith(";"):
            start = index + 1
            parts = [line]
            while index + 1 < len(raw_lines) and not parts[-1].endswith(";"):
                index += 1
                parts.append(_strip_comment(raw_lines[index]).strip())
            out.append((start, " ".join(parts)))
        else:
            out.append((index + 1, line))
        index += 1
    return out


def split_blocks(text: str) -> tuple[dict[str, Axis], list[Machine]]:
    """Return (dimensions, machines) from the compressed source."""
    dimensions: dict[str, Axis] = {}
    machines: list[Machine] = []
    current: Machine | None = None
    depth = 0
    for line_no, line in _logical_lines(text):
        if not line:
            continue
        if current is None:
            match = SET_DIM_RE.match(line)
            if match:
                name, rhs, name_format = match.group(1), match.group(2), match.group(3)
                range_match = RANGE_RE.match(rhs.strip())
                if range_match:
                    lo, hi = int(range_match.group(1)), int(range_match.group(2))
                    values = [str(v) for v in range(lo, hi + 1)]
                else:
                    if not rhs.strip().startswith("["):
                        raise ExpandError(f"line {line_no}: axis {name} expects a list or range")
                    values = _parse_items(rhs)
                if not values:
                    raise ExpandError(f"line {line_no}: axis {name} is empty")
                dimensions[name] = Axis(values=values, name_format=name_format or "_{value}")
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


def _parse_dim_uses(
    index: str, dimensions: dict[str, Axis]
) -> list[tuple[str, list[str], bool]]:
    """Return (axis, members, is_worst) for ``A, B in [..], worst(C)``."""
    uses = []
    for item in _split_top_level(index):
        match = DIM_USE_WORST_RE.match(item)
        if match:
            axis = match.group(1)
            if axis not in dimensions:
                raise ExpandError(f"undeclared dimension {axis!r}")
            uses.append((axis, [], True))
            continue
        match = DIM_USE_SUBSET_RE.match(item)
        if match:
            axis, subset = match.group(1), match.group(2)
            if axis not in dimensions:
                raise ExpandError(f"undeclared dimension {axis!r}")
            chosen = _parse_items(subset)
            unknown = [value for value in chosen if value not in dimensions[axis].values]
            if unknown:
                raise ExpandError(f"dimension {axis} has no member(s) {unknown}")
            uses.append((axis, chosen, False))
            continue
        match = DIM_USE_RE.match(item)
        if match:
            axis = match.group(1)
            if axis not in dimensions:
                raise ExpandError(f"undeclared dimension {axis!r}")
            uses.append((axis, list(dimensions[axis].values), False))
            continue
        raise ExpandError(f"bad dimension index {item!r}")
    return uses


def _expand_body(
    body: list[Stmt],
    env: dict[str, object],
    dimensions: dict[str, Axis],
    origins: list[tuple[str, str, int]],
) -> list[str]:
    out: list[str] = []
    for stmt in body:
        parsed = _parse_item(stmt.text)
        if parsed is None:
            out.append(_subst_fields(stmt.text, env))
            continue
        keyword, base, index, rest = parsed
        try:
            uses = _parse_dim_uses(index, dimensions)
        except ExpandError as exc:
            raise ExpandError(f"line {stmt.line}: {exc}") from exc
        for use_index in _product_index(uses):
            suffix = "".join(
                "_WorstCase" if is_worst else dimensions[axis].name(member)
                for (axis, _members, is_worst), member in zip(uses, use_index)
            )
            name = f"{base}{suffix}"
            origins.append((keyword, name, stmt.line))
            tail = rest if rest.startswith(";") else " " + rest
            text = f"{keyword} {name}{tail}".rstrip()
            out.append(_subst_fields(text, env))
    return out


def _product_index(uses: list[tuple[str, list[str], bool]]):
    """Yield one member per axis; ``worst`` contributes the pseudo-member."""
    if not uses:
        yield []
        return
    def walk(position: int, chosen: list[str]):
        if position == len(uses):
            yield list(chosen)
            return
        axis, members, is_worst = uses[position]
        if is_worst:
            chosen.append("WorstCase")
            yield from walk(position + 1, chosen)
            chosen.pop()
        else:
            for member in members:
                chosen.append(member)
                yield from walk(position + 1, chosen)
                chosen.pop()
    yield from walk(0, [])


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
        if value not in dimensions[axis].values:
            dimensions[axis].values.append(value)
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
