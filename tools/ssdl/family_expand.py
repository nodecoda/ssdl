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
* ``units = [1, 2, 4, 8, ...]`` gives each member its integer projection; the
  axis name then denotes *this member's* number inside a ``bind``
  (``latency = 3 + 2 * LMUL``).  ``worst(AXIS)`` reuses the widest member's
  number.  A naming-only axis stays out of numeric fields, and using it there
  reports ``unknown parameter`` rather than inventing a value.
* Numeric fields are ``latency``/``uops``/``cycles`` and the items of
  ``release``/``acquire``.  The only kernels are ``cdiv`` (ceil), ``max`` and
  ``min``; ``//`` is ``!div`` and ``/`` must divide exactly.  No recursion, no
  user functions, no ``**``, no keyword arguments.

``--check-coverage`` expands and then requires every declared dimensioned
write to be bound and every bound name to be declared.  ``--add-axis-value``
prepends a member to an axis before checking, which is how "add M16" is asked
today: the diagnostic names every site that does not cover the new member.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
SET_DECL_RE = re.compile(rf"^set\s+({IDENT})\s*=\s*(.+?)\s*:\s*(.+?)\s*;?\s*$")
RELATION_RE = re.compile(
    rf"^set\s+({IDENT})\s*:\s*kind\s*=\s*relation\s*=\s*(\[.*\])\s*;$"
)
RANGE_RE = re.compile(r"^(-?\d+)\s*\.\.\s*(-?\d+)$")
ITEM_HEAD_RE = re.compile(rf"^(write|read|bind|advance)\s+({IDENT})\s*\[")
DIM_USE_WORST_RE = re.compile(rf"^worst\(\s*({IDENT})\s*\)$")
DIM_USE_SUBSET_RE = re.compile(rf"^({IDENT})\s+in\s*\[([^\]]*)\]$")
DIM_USE_RE = re.compile(rf"^({IDENT})$")
VARIANT_HEAD_RE = re.compile(rf"^variant\s+({IDENT})\s*(?:\[(.*)\])?\s*\{{$")
VARIANT_CASE_RE = re.compile(r"^(case\s+.+?|default)\s*:\s*\[(.*)\]\s*;$")
SELECTED_RE = re.compile(rf"^({IDENT})\s*(?:\[(.*)\])?$")
MACHINE_IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
FAMILY_RE = re.compile(rf"^machine\s+({MACHINE_IDENT})\s*\((.*?)\)\s*\{{")
INSTANCE_RE = re.compile(
    rf"^machine\s+({MACHINE_IDENT})\s*:\s*({MACHINE_IDENT})\s*\((.*?)\)\s*\{{"
)
PLAIN_RE = re.compile(rf"^machine\s+({MACHINE_IDENT})\s*\{{")
PARAM_RE = re.compile(rf"^\s*({IDENT})\s*:\s*(INT|BOOL)\s*$")
EXTERN_RE = re.compile(
    rf"^extern\s+fn\s+({IDENT})\s*\(([^)]*)\)\s*->\s*(INT|BOOL)\s*:\s*({IDENT})\s*;$"
)


class ExpandError(Exception):
    pass


@dataclass
class Axis:
    values: list[str]
    name_format: str = "_{value}"
    units: list[int] | None = None
    extra: dict[str, list[int]] = field(default_factory=dict)

    def name(self, member: str) -> str:
        """Compose a member name from ``name_format``.

        ``name_format`` has exactly one placeholder vocabulary: ``{value}``
        (the dimension member).  Family parameters use ``{param}`` in a
        ``name`` template on an entity; mixing the two silently produced
        literal braces before this check.
        """
        if "{value}" not in self.name_format:
            raise ExpandError(
                f"name_format {self.name_format!r} has no {{value}} placeholder"
            )
        if self.name_format.count("{value}") > 1:
            raise ExpandError(
                f"name_format {self.name_format!r} repeats {{value}}; "
                "declare the suffix once"
            )
        leftover = self.name_format.replace("{value}", "")
        if "{" in leftover or "}" in leftover:
            raise ExpandError(
                f"name_format {self.name_format!r} has a placeholder other than "
                "{{value}}; family parameters belong to an entity `name` template"
            )
        return self.name_format.replace("{value}", member)

    def unit_of(self, member: str | None) -> int:
        """Integer projection of a member; ``None`` is the ``worst`` pseudo-member."""
        if self.units is None:
            raise ExpandError("axis is used in a numeric field but declares no `units`")
        return self.units[-1] if member is None else self.units[self.values.index(member)]

    def projection(self, key: str, member: str | None) -> int:
        """A named secondary projection, e.g. ``SEW.div_factor``."""
        if key not in self.extra:
            raise ExpandError(f"axis has no projection {key!r}")
        values = self.extra[key]
        return values[-1] if member is None else values[self.values.index(member)]


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


def _log2(value: int) -> int:
    """TableGen ``!logtwo``: only defined for positive powers of two."""
    if value <= 0 or value & (value - 1):
        raise ExpandError(f"log2({value}) is not a power of two")
    return value.bit_length() - 1


CALLS = {
    "cdiv": lambda a, b: (a + b - 1) // b,
    "max": max,
    "min": min,
    "log2": _log2,
}


class _FlattenAttributes(ast.NodeTransformer):
    """``SEW.div_factor`` becomes one env key, ``SEW.div_factor``."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name):
            return ast.copy_location(ast.Name(id=f"{node.value.id}.{node.attr}", ctx=node.ctx), node)
        return node
NODE_BUDGET = 64


def _split_ternary(expr: str) -> tuple[str, str, str] | None:
    """Split a top-level ``cond ? then : else``; strings are opaque."""
    depth = 0
    question = -1
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == '"':
            index += 1
            while index < len(expr) and expr[index] != '"':
                index += 2 if expr[index] == "\\" else 1
            index += 1
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and char == "?":
            question = index
            break
        index += 1
    if question < 0:
        return None

    depth = 0
    nested = 0
    colon = -1
    index = question + 1
    while index < len(expr):
        char = expr[index]
        if char == '"':
            index += 1
            while index < len(expr) and expr[index] != '"':
                index += 2 if expr[index] == "\\" else 1
            index += 1
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0:
            if char == "?":
                nested += 1
            elif char == ":":
                if nested == 0:
                    colon = index
                    break
                nested -= 1
        index += 1
    if colon < 0:
        raise ExpandError(f"conditional expression is missing ':': {expr!r}")
    return expr[:question].strip(), expr[question + 1 : colon].strip(), expr[colon + 1 :].strip()


def _translate_ternary(expr: str) -> str:
    """Rewrite ``cond ? then : else`` as ``(then if cond else else)``.

    The family dialect keeps C's spelling but no other construct uses ``?`` or
    a bare ``:`` inside an expression, so a depth-aware scan is enough.  The
    transform is recursive: branches may contain further conditionals.
    """
    parts = _split_ternary(expr)
    if parts is None:
        return expr
    condition, then, other = parts
    return (
        f"({_translate_ternary(then)} if {_translate_ternary(condition)} "
        f"else {_translate_ternary(other)})"
    )


def _eval_expr(
    expr: str, env: dict[str, object], lookups: dict[str, object] | None = None
) -> int:
    """Evaluate a bounded integer expression.

    Only ``+ - * // %``, the three kernels ``cdiv/max/min`` and names bound in
    ``env`` are allowed.  ``/`` is accepted only when it divides exactly: an
    accidental fraction is an error, never a silent floor.  ``//`` is the
    floor division that TableGen spells ``!div``.
    """
    original = expr
    expr = _translate_ternary(expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpandError(f"bad expression {original!r}: {exc}") from exc
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Name,
        ast.Call,
        ast.Attribute,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.IfExp,
        ast.Compare,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Not,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Load,
    )
    tree = _FlattenAttributes().visit(tree)
    if sum(1 for _ in ast.walk(tree)) > NODE_BUDGET:
        raise ExpandError(f"expression too large in {expr!r}")
    functions = {**CALLS, **(lookups or {})}
    call_functions = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    scope = {"true": True, "false": False, **env}
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ExpandError(f"unsupported syntax in {expr!r}: {type(node).__name__}")
        if id(node) in call_functions:
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in functions:
                raise ExpandError(f"unknown function in {expr!r}")
            if node.keywords:
                raise ExpandError(f"keyword arguments are not allowed in {expr!r}")
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in scope
        ):
            raise ExpandError(
                f"unknown parameter {node.id!r} in {original!r} "
                f"(axis names need `units` to appear in numeric fields)"
            )
    try:
        value = eval(  # noqa: S307
            compile(tree, "<expr>", "eval"),
            {"__builtins__": {}, **functions},
            scope,
        )
    except ExpandError:
        raise
    except Exception as exc:  # noqa: BLE001 - kernel misuse is a source error
        raise ExpandError(f"bad expression {expr!r}: {exc}") from exc
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ExpandError(f"non-integer result {value!r} in {expr!r}")
        return int(value)
    return int(value)


SCALAR_FIELD_RE = re.compile(r"^(latency|uops|cycles)\s*=\s*(.+)$")
LIST_FIELD_RE = re.compile(r"^(release|acquire)\s*=\s*\[(.*)\]$")


def _split_fields(text: str) -> list[str]:
    """Split on commas that are neither inside ``[]`` nor inside ``()``."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _subst_fields(
    text: str, env: dict[str, object], lookups: dict[str, object] | None = None
) -> str:
    """Evaluate the numeric fields of one statement.

    Fields are split before evaluation so a kernel argument may contain a
    comma (``cdiv(a, b)``); a regex would cut the call in half.
    """
    parts = _split_fields(text)
    tail = ""
    if parts and parts[-1].rstrip().endswith(";"):
        parts[-1] = parts[-1].rstrip()[:-1]
        tail = ";"
    rendered: list[str] = []
    for part in parts:
        field = part.strip()
        match = LIST_FIELD_RE.match(field)
        if match:
            items = [item.strip() for item in _split_fields(match.group(2)) if item.strip()]
            values = ", ".join(
                str(_eval_expr(item, env, lookups)) for item in items
            )
            rendered.append(f"{match.group(1)} = [{values}]")
            continue
        match = SCALAR_FIELD_RE.match(field)
        if match:
            rendered.append(
                f"{match.group(1)} = {_eval_expr(match.group(2), env, lookups)}"
            )
            continue
        rendered.append(field)
    return ", ".join(rendered) + tail


def _scan_logical_lines(text: str) -> list[tuple[int, str]]:
    """Split a family source into statements; layout is irrelevant.

    A statement ends at ``;`` or at a block brace, both only at bracket depth
    zero.  Physical newlines are whitespace, so a ``bind``/``map``/``machine``
    head may be broken anywhere.  Strings, ``[{ ... }]`` code blocks and
    comments are opaque: a ``;`` or ``{`` inside them never splits a statement.

    ``#`` starts a comment anywhere outside a string or code block.  ``//`` is
    floor division in this dialect (``LMUL // 4``), so it only starts a comment
    at the start of a statement, after ``;``/``{``/``}`` or after ``:``.
    ``/* ... */`` may span lines.  Intra-line spacing is preserved so the flat
    output stays byte-identical to the line-oriented v1.2 text.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    line_no = 1
    stmt_line = 1
    bracket_depth = 0
    inline_braces = 0
    prev = ""
    i = 0
    n = len(text)

    def space() -> None:
        if buf and buf[-1] != " ":
            buf.append(" ")

    def flush() -> None:
        nonlocal buf
        statement = "".join(buf).strip()
        if statement:
            out.append((stmt_line, statement))
        buf = []

    while i < n:
        char = text[i]
        if char in " \t\r\n":
            start = i
            while i < n and text[i] in " \t\r\n":
                if text[i] == "\n":
                    line_no += 1
                i += 1
            run = text[start:i]
            if buf:
                if "\n" in run:
                    # A statement broken across lines joins with one space;
                    # spacing inside one physical line is kept verbatim.
                    space()
                else:
                    buf.append(run)
            continue

        if char == "#":
            space()
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close < 0:
                raise ExpandError(f"line {line_no}: unterminated /* comment")
            line_no += text.count("\n", i, close + 2)
            space()
            i = close + 2
            continue
        if char == "/" and text.startswith("//", i) and prev in ("", ";", "{", "}", ":"):
            while i < n and text[i] != "\n":
                i += 1
            continue

        if char == '"':
            if not buf:
                stmt_line = line_no
            buf.append('"')
            i += 1
            while i < n:
                inner = text[i]
                if inner == "\\" and i + 1 < n:
                    buf.append(inner)
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if inner == "\n":
                    raise ExpandError(f"line {line_no}: unterminated string")
                buf.append(inner)
                i += 1
                if inner == '"':
                    break
            else:
                raise ExpandError(f"line {line_no}: unterminated string")
            prev = '"'
            continue

        if char == "[" and text.startswith("[{", i):
            close = text.find("}]", i + 2)
            if close < 0:
                raise ExpandError(f"line {line_no}: unterminated [{{ code block")
            if not buf:
                stmt_line = line_no
            buf.append("[{")
            buf.append(text[i + 2 : close])
            buf.append("}]")
            line_no += text.count("\n", i, close + 2)
            i = close + 2
            prev = "]"
            continue

        if not buf:
            stmt_line = line_no

        if char in "([":
            bracket_depth += 1
            buf.append(char)
            i += 1
            prev = char
            continue
        if char in ")]":
            bracket_depth = max(0, bracket_depth - 1)
            buf.append(char)
            i += 1
            prev = char
            continue

        if char == ";" and bracket_depth == 0 and inline_braces == 0:
            if not buf:
                # ``};`` closes a block declaration; a bare ``;`` is noise.
                if out and out[-1][1] == "}":
                    out[-1] = (out[-1][0], "};")
                i += 1
                prev = ";"
                continue
            buf.append(";")
            flush()
            i += 1
            prev = ";"
            continue
        if char == "{" and bracket_depth == 0:
            if prev == "=":
                # An inline table (``bitmask = { group = 0, bit = 8 }``) is
                # part of the statement, not a block.
                inline_braces += 1
                buf.append("{")
            else:
                buf.append("{")
                flush()
            i += 1
            prev = "{"
            continue
        if char == "}" and bracket_depth == 0:
            if inline_braces:
                inline_braces -= 1
                buf.append("}")
            else:
                flush()
                out.append((line_no, "}"))
            i += 1
            prev = "}"
            continue

        buf.append(char)
        i += 1
        prev = char

    if bracket_depth:
        raise ExpandError(f"line {line_no}: unbalanced () or [] at end of file")
    if inline_braces:
        raise ExpandError(f"line {line_no}: unterminated {{ }} table at end of file")
    flush()
    return out


def _parse_props(text: str) -> dict[str, str]:
    """Split ``kind = dimension, name_format = "...", units = [..]``."""
    props: dict[str, str] = {}
    for part in _split_top_level(text):
        key, sep, value = part.partition("=")
        if not sep:
            raise ExpandError(f"bad property {part!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        props[key.strip()] = value
    return props


def _axis_units(
    name: str, values: list[str], raw: str | None, line_no: int
) -> list[int] | None:
    """Numeric projection for an axis: explicit `units`, else numeric members."""
    if raw is None:
        if all(re.fullmatch(r"-?\d+", value) for value in values):
            return [int(value) for value in values]
        return None
    units = []
    for item in _parse_items(raw):
        try:
            units.append(int(item))
        except ValueError as exc:
            raise ExpandError(f"line {line_no}: axis {name} has non-integer unit {item!r}") from exc
    if len(units) != len(values):
        raise ExpandError(
            f"line {line_no}: axis {name} has {len(values)} members but {len(units)} units"
        )
    return units


PROFILE_PATH = Path(__file__).resolve().parents[2] / "data/riscv-sched-profiles.json"
_PROJECTIONS: dict[str, dict] | None = None


def _profile_projections() -> dict[str, dict]:
    """Host-pinned projection rows; the language declares the signature."""
    global _PROJECTIONS
    if _PROJECTIONS is None:
        if not PROFILE_PATH.is_file():
            raise ExpandError(f"missing projection profile {PROFILE_PATH}")
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        _PROJECTIONS = data.get("projections", {})
    return _PROJECTIONS


def _parse_relation(name: str, rhs: str, line_no: int) -> dict[tuple[int, ...], int]:
    """``[(key, ..., value), ...]``; keys and values must be integers."""
    rhs = rhs.strip()
    if not (rhs.startswith("[") and rhs.endswith("]")):
        raise ExpandError(f"line {line_no}: relation {name} expects [(key, value), ...]")
    table: dict[tuple[int, ...], int] = {}
    for item in _split_fields(rhs[1:-1]):
        item = item.strip()
        if not item:
            continue
        if not (item.startswith("(") and item.endswith(")")):
            raise ExpandError(f"line {line_no}: relation {name} row {item!r} is not a tuple")
        parts = [part.strip() for part in _split_fields(item[1:-1]) if part.strip()]
        if len(parts) < 2:
            raise ExpandError(
                f"line {line_no}: relation {name} row {item!r} needs a key and a value"
            )
        key = tuple(_eval_expr(part, {}) for part in parts[:-1])
        value = _eval_expr(parts[-1], {})
        if key in table:
            raise ExpandError(f"line {line_no}: relation {name} repeats key {key}")
        table[key] = value
    if not table:
        raise ExpandError(f"line {line_no}: relation {name} is empty")
    return table


def _build_lookups(
    relations: dict[str, dict[tuple[int, ...], int]],
    externs: dict[str, tuple[tuple[str, ...], str, str, int]],
) -> dict[str, object]:
    """Turn declared relations and profiles into n-ary elab-time functions."""
    lookups: dict[str, object] = {}
    for name, table in relations.items():
        def lookup(*args: object, _table=table, _name=name) -> int:
            key = tuple(int(arg) for arg in args)
            if key not in _table:
                raise ExpandError(
                    f"relation {_name} has no row for {key}; domain: {sorted(_table)}"
                )
            return _table[key]

        lookups[name] = lookup

    for name, (params, _ret, profile, line_no) in externs.items():
        entries = _profile_projections()
        if profile not in entries:
            raise ExpandError(
                f"line {line_no}: unknown projection {profile!r}; "
                f"add projections.{profile} to data/riscv-sched-profiles.json"
            )
        entry = entries[profile]
        rows = entry.get("rows", [])
        table: dict[tuple[int, ...], int] = {}
        for row in rows:
            if len(row) != len(params) + 1:
                raise ExpandError(
                    f"projections.{profile}: row {row} has {len(row)} columns, "
                    f"expected {len(params) + 1}"
                )
            table[tuple(int(value) for value in row[:-1])] = int(row[-1])
        if not table:
            raise ExpandError(f"projections.{profile} has no pinned rows")

        def lookup(*args: object, _table=table, _name=profile, _arity=len(params)) -> int:
            if len(args) != _arity:
                raise ExpandError(
                    f"projection {_name} expects {_arity} argument(s), got {len(args)}"
                )
            key = tuple(int(arg) for arg in args)
            if key not in _table:
                raise ExpandError(
                    f"no pinned value for {_name}{key}; add it to projections.{_name}.rows"
                )
            return _table[key]

        lookups[name] = lookup
    return lookups


def _brace_delta(text: str) -> int:
    """Brace balance of one statement, ignoring strings and ``[{ }]`` blocks."""
    delta = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if char == "[" and text.startswith("[{", i):
            close = text.find("}]", i + 2)
            i = len(text) if close < 0 else close + 2
            continue
        if char == "{":
            delta += 1
        elif char == "}":
            delta -= 1
        i += 1
    return delta


GLOBAL_KEYWORDS = (
    "feature",
    "predicate",
    "predicate_prolog",
    "sti_decl",
    "sti_function",
    "tii_predicate",
    "itineraries",
)


# Public name used by tools/ssdl/to_v2.py: the same statement scanner, exposed
# so the migrator and the expander agree on what a statement is.
scan_statements = _scan_logical_lines


def split_blocks(
    text: str,
) -> tuple[
    dict[str, Axis],
    dict[str, dict[tuple[int, ...], int]],
    dict[str, tuple[tuple[str, ...], str, str, int]],
    list[Machine],
    list[str],
]:
    """Return (dimensions, relations, externs, machines, globals).

    ``globals`` are target-level declarations (features, predicates, ...) that
    the family layer copies through untouched.  Relations and externs are
    compile-time lookups and are consumed here.
    """
    dimensions: dict[str, Axis] = {}
    relations: dict[str, dict[tuple[int, ...], int]] = {}
    externs: dict[str, tuple[tuple[str, ...], str, str, int]] = {}
    machines: list[Machine] = []
    globals_: list[str] = []
    current: Machine | None = None
    depth = 0
    global_depth = 0
    for line_no, line in _scan_logical_lines(text):
        if not line:
            continue
        if current is None:
            if global_depth:
                # A top-level block declaration (predicate/itineraries/...) is
                # copied as one unit, braces included.
                globals_.append(line)
                global_depth += _brace_delta(line)
                continue
            extern = EXTERN_RE.match(line)
            if extern:
                name = extern.group(1)
                params = []
                for param in extern.group(2).split(","):
                    if not param.strip():
                        continue
                    pm = PARAM_RE.match(param)
                    if not pm:
                        raise ExpandError(
                            f"line {line_no}: bad extern parameter {param!r}"
                        )
                    params.append(pm.group(1))
                if name in externs:
                    raise ExpandError(f"line {line_no}: extern {name} is declared twice")
                externs[name] = (
                    tuple(params),
                    extern.group(3),
                    extern.group(4),
                    line_no,
                )
                continue
            relation = RELATION_RE.match(line)
            if relation:
                name = relation.group(1)
                if name in relations:
                    raise ExpandError(
                        f"line {line_no}: relation {name} is declared twice"
                    )
                relations[name] = _parse_relation(name, relation.group(2), line_no)
                continue
            match = SET_DECL_RE.match(line)
            if match:
                name, rhs = match.group(1), match.group(2).strip()
                props = _parse_props(match.group(3))
                if props.get("kind") != "dimension":
                    raise ExpandError(
                        f"line {line_no}: set {name} is not a dimension "
                        f"(kind = {props.get('kind')!r})"
                    )
                range_match = RANGE_RE.match(rhs)
                if range_match:
                    lo, hi = int(range_match.group(1)), int(range_match.group(2))
                    values = [str(v) for v in range(lo, hi + 1)]
                else:
                    if not rhs.startswith("["):
                        raise ExpandError(f"line {line_no}: axis {name} expects a list or range")
                    values = _parse_items(rhs)
                if not values:
                    raise ExpandError(f"line {line_no}: axis {name} is empty")
                units = _axis_units(name, values, props.get("units"), line_no)
                extra = {
                    key: _axis_units(name, values, value, line_no)
                    for key, value in props.items()
                    if key not in ("kind", "name_format", "units")
                }
                dimensions[name] = Axis(
                    values=values,
                    name_format=props.get("name_format") or "_{value}",
                    units=units,
                    extra={key: value for key, value in extra.items() if value is not None},
                )
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
            if line.split(" ", 1)[0].rstrip(":") in GLOBAL_KEYWORDS:
                globals_.append(line)
                global_depth = _brace_delta(line)
                continue
            raise ExpandError(f"line {line_no}: expected a declaration, got {line.strip()!r}")
        else:
            text = line.strip()
            if text == "}":
                depth -= 1
                if depth == 0:
                    current = None
                    continue
                current.body.append(Stmt(line_no, "}"))
                continue
            if text.endswith("{"):
                depth += 1
            current.body.append(Stmt(line_no, text))
    if current is not None:
        raise ExpandError(f"unterminated machine {current.name}")
    return dimensions, relations, externs, machines, globals_


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


OVERRIDE_RE = re.compile(
    rf"^override\s+({IDENT})\s+({IDENT})\b"
)
ITEM_NAME_RE = re.compile(rf"^({IDENT})\s+({IDENT})\b")
OVERRIDE_KINDS = (
    "bind",
    "resource",
    "res_group",
    "advance",
    "variant",
    "read_variant",
    "reg_file",
    "write",
    "read",
)


def _split_overrides(
    instance_body: list[Stmt], family_body: list[Stmt], machine: str
) -> tuple[list[Stmt], list[Stmt]]:
    """Apply ``override <kind> <name>`` to the inherited family statements.

    An instance may replace a whole inherited statement.  Field-level merging
    was rejected deliberately: a *partial* bind is how a model silently keeps a
    stale latency.  The replacement must repeat every field it needs.
    """
    overrides: dict[tuple[str, str], int] = {}
    stripped: list[Stmt] = []
    for stmt in instance_body:
        match = OVERRIDE_RE.match(stmt.text)
        if match is None:
            stripped.append(stmt)
            continue
        kind, name = match.group(1), match.group(2)
        if kind not in OVERRIDE_KINDS:
            raise ExpandError(
                f"line {stmt.line}: override {kind} {name}: "
                f"expected one of {', '.join(OVERRIDE_KINDS)}"
            )
        overrides[(kind, name)] = stmt.line
        stripped.append(Stmt(stmt.line, stmt.text[match.start(1) :]))
    if not overrides:
        return instance_body, family_body

    inherited: set[tuple[str, str]] = set()
    kept: list[Stmt] = []
    skip_depth = 0
    for stmt in family_body:
        if skip_depth:
            skip_depth += _brace_delta(stmt.text)
            continue
        match = ITEM_NAME_RE.match(stmt.text)
        if (
            match is not None
            and match.group(1) in OVERRIDE_KINDS
            and (match.group(1), match.group(2)) in overrides
        ):
            key = (match.group(1), match.group(2))
            inherited.add(key)
            delta = _brace_delta(stmt.text)
            if delta > 0:
                skip_depth = delta
            continue
        kept.append(stmt)

    missing = sorted(set(overrides) - inherited)
    if missing:
        kind, name = missing[0]
        raise ExpandError(
            f"line {overrides[(kind, name)]}: override {kind} {name}: "
            f"family {machine!r} does not declare it"
        )
    return stripped, kept


def _expand_variant(
    line: int,
    base: str,
    index_text: str,
    cases: list[Stmt],
    env: dict[str, object],
    dimensions: dict[str, Axis],
) -> list[str]:
    """Expand ``variant Base[A] { case P: [Sel[A]]; default: [Sel2[A]]; }``.

    Selections must use axes of the subject; each member of the subject is
    substituted into the selection before the case is emitted.
    """
    uses = _parse_dim_uses(index_text, dimensions) if index_text.strip() else []
    axes = [axis for axis, _members, _worst in uses]
    if len(set(axes)) != len(axes):
        raise ExpandError(f"line {line}: axis repeated in variant index {index_text!r}")

    parsed_cases = []
    for case in cases:
        match = VARIANT_CASE_RE.match(case.text)
        if not match:
            raise ExpandError(f"line {case.line}: bad variant case {case.text!r}")
        head = "default" if match.group(1) == "default" else "case " + match.group(1)[5:].strip()
        selected = [item.strip() for item in _split_top_level(match.group(2)) if item.strip()]
        parsed_cases.append((case.line, head, selected))

    out: list[str] = []
    for use_index in _product_index(uses):
        mapping = {
            axis: member
            for (axis, _members, _worst), member in zip(uses, use_index)
        }
        suffix = "".join(
            "_WorstCase" if is_worst else dimensions[axis].name(member)
            for (axis, _members, is_worst), member in zip(uses, use_index)
        )
        out.append(f"variant {base}{suffix} {{")
        for case_line, head, selected in parsed_cases:
            names = []
            for entry in selected:
                names.append(_expand_selected(case_line, entry, mapping, dimensions))
            out.append(f"    {head}: [{', '.join(names)}];")
        out.append("}")
    return out


def _expand_selected(
    line: int,
    text: str,
    mapping: dict[str, str],
    dimensions: dict[str, Axis],
) -> str:
    match = SELECTED_RE.match(text)
    if not match:
        raise ExpandError(f"line {line}: bad variant selection {text!r}")
    base, index_text = match.group(1), match.group(2)
    if index_text is None:
        return base
    suffix = ""
    for item in _split_top_level(index_text):
        item = item.strip()
        worst = DIM_USE_WORST_RE.match(item)
        if worst:
            subject_axis = _subject_axis(line, base, worst.group(1), mapping, dimensions)
            suffix += "_WorstCase"
            continue
        subset = DIM_USE_SUBSET_RE.match(item)
        if subset:
            raise ExpandError(f"line {line}: variant selection cannot narrow an axis")
        plain = DIM_USE_RE.match(item)
        if not plain:
            raise ExpandError(f"line {line}: bad variant selection index {item!r}")
        axis = plain.group(1)
        subject_axis = _subject_axis(line, base, axis, mapping, dimensions)
        # The selection's own set carries the suffix format, e.g. "_{value}_Pred".
        suffix += dimensions[axis].name(mapping[subject_axis])
    return base + suffix


def _subject_axis(
    line: int,
    base: str,
    axis: str,
    mapping: dict[str, str],
    dimensions: dict[str, Axis],
) -> str:
    """Map a selection's set onto the subject axis it must follow.

    A selection may use a different view of the same axis (``LMUL_7`` with
    ``name_format = "_{value}_Pred"``) because the suffix differs while the
    members do not.  Two views with identical members are the same axis; if
    that is ambiguous the source has to name the subject's view directly.
    """
    if axis in mapping:
        return axis
    if axis not in dimensions:
        raise ExpandError(f"line {line}: undeclared dimension {axis!r}")
    candidates = [
        subject
        for subject in mapping
        if subject in dimensions and dimensions[subject].values == dimensions[axis].values
    ]
    if len(candidates) != 1:
        raise ExpandError(
            f"line {line}: selection {base} uses {axis!r}, which does not name an axis "
            f"of the subject (candidates: {candidates or 'none'})"
        )
    return candidates[0]



RUNTIME_HEADS = frozenset(
    {
        "opcode",
        "pseudo",
        "is_reg",
        "is_vreg",
        "is_not_vreg",
        "is_imm",
        "is_valid_reg",
        "is_invalid_reg",
        "is_zero",
        "reg_is",
        "imm_is",
        "imm_is_enum",
        "imm_lt",
        "imm_gt",
        "imm_le",
        "imm_ge",
        "imm_range",
        "reg_simple",
        "imm_simple",
        "num_ops",
        "same_reg",
        "func",
        "func_tii",
        "non_portable",
        "feature",
        "all_of",
        "any_of",
        "not",
    }
)


def _runtime_head(condition: str) -> str | None:
    """The predicate head if this condition depends on the instruction."""
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", condition)
    if match is not None and match.group(1) in RUNTIME_HEADS:
        return match.group(1)
    return None


def _runtime_anywhere(text: str) -> bool:
    """True if any field or list item carries an instruction-dependent ``?:``."""
    for part in _split_fields(text):
        field = part.strip().rstrip(";").strip()
        if "?" not in field:
            continue
        candidates = [field]
        open_at = field.find("[")
        close_at = field.rfind("]")
        if 0 <= open_at < close_at:
            candidates.extend(_split_top_level(field[open_at + 1 : close_at]))
        for candidate in candidates:
            rhs = candidate.split("=", 1)[1] if "=" in candidate else candidate
            parts = _split_ternary(rhs)
            if parts is not None and _runtime_head(parts[0]) is not None:
                return True
    return False


def _runtime_scalar(text: str) -> tuple[int, str, str, str, str] | None:
    """(field index, field name, condition, then, else) for one bind."""
    parts = _split_fields(text)
    found = []
    for index, part in enumerate(parts):
        field = part.strip()
        if field.endswith(";"):
            field = field[:-1].strip()
        match = re.match(r"^(latency|uops|cycles)\s*=\s*(.+)$", field)
        if match is None or "?" not in match.group(2):
            continue
        split = _split_ternary(match.group(2))
        if split is None:
            continue
        condition, then, other = split
        if _runtime_head(condition) is None:
            continue
        found.append((index, match.group(1), condition, then, other))
    if len(found) > 1:
        raise ExpandError(
            "a bind may carry one runtime conditional field; split the statement"
        )
    return found[0] if found else None


def _lower_runtime_bind(
    line: int,
    text: str,
    env: dict[str, object],
    origins: list[tuple[str, str, int]],
    lookups: dict[str, object] | None = None,
    emitted: dict[tuple[str, str], int] | None = None,
) -> list[str] | None:
    """Lower ``bind W ..., latency = pred ? a : b`` to aux writes + a variant.

    The subject ``W`` becomes a ``SchedWriteVariant``; each branch is a concrete
    aux write with its own ``WriteRes``.  Only scalar fields are lowered: a
    conditional inside a list would need per-element variants, which the
    record model cannot express.
    """
    found = _runtime_scalar(text)
    if found is None:
        if _runtime_anywhere(text):
            raise ExpandError(
                f"line {line}: a runtime condition is only supported on "
                "latency/uops/cycles; bind the whole field instead"
            )
        return None

    index, field_name, condition, then, other = found
    parts = _split_fields(text)
    base = text.split()[1]

    def rebuild(value: str) -> str:
        pieces = []
        for position, part in enumerate(parts):
            piece = part.strip()
            if position == index:
                semi = ";" if piece.endswith(";") else ""
                piece = f"{field_name} = {value}{semi}"
            pieces.append(piece)
        return ", ".join(pieces)

    # Aux names are a pure function of the subject and the branch index, so
    # two expansions of one source spell them identically.  They are shared
    # target vocabulary like any other SchedWrite; only WriteRes and the
    # variant are model-scoped.  Reject a source name that would take the same
    # spelling inside one machine rather than letting the backend see two
    # declarations for one write.
    aux = [f"{base}__case0", f"{base}__case1"]
    for aux_name in aux:
        for keyword in ("write", "bind"):
            key = (keyword, aux_name)
            if emitted is not None and key in emitted:
                raise ExpandError(
                    f"{aux_name!r} collides with a declaration expanded from "
                    f"line {emitted[key]}"
                )
            if emitted is not None:
                emitted[key] = line
    out = [f"write {aux[0]};", f"write {aux[1]};"]
    for name, value in zip(aux, (then, other)):
        branch = re.sub(
            rf"^(bind\s+){re.escape(base)}\b", rf"\g<1>{name}", rebuild(value), count=1
        )
        out.append(_subst_fields(branch, env, lookups))
        origins.append(("write", name, line))
        origins.append(("bind", name, line))
    out.extend(
        [
            f"variant {base} {{",
            f"    case {condition}: [{aux[0]}];",
            f"    default: [{aux[1]}];",
            "}",
        ]
    )
    return out


def _expand_body(
    body: list[Stmt],
    env: dict[str, object],
    dimensions: dict[str, Axis],
    origins: list[tuple[str, str, int]],
    lookups: dict[str, object] | None = None,
) -> list[str]:
    out: list[str] = []
    emitted: dict[tuple[str, str], int] = {}
    pos = 0
    while pos < len(body):
        stmt = body[pos]
        variant = VARIANT_HEAD_RE.match(stmt.text)
        if variant:
            cases = []
            cursor = pos + 1
            while cursor < len(body) and body[cursor].text != "}":
                cases.append(body[cursor])
                cursor += 1
            if cursor == len(body):
                raise ExpandError(f"line {stmt.line}: unterminated variant {variant.group(1)}")
            out.extend(
                _expand_variant(
                    stmt.line, variant.group(1), variant.group(2) or "", cases,
                    env, dimensions,
                )
            )
            pos = cursor + 1
            continue
        pos += 1
        parsed = _parse_item(stmt.text)
        if parsed is None:
            if _runtime_anywhere(stmt.text):
                raise ExpandError(
                    f"line {stmt.line}: a runtime condition cannot appear in a "
                    "structural position; use a family parameter (elaborated per "
                    "instance) or a profile projection"
                )
            # A bare declaration (no axis index) still owns its spelling; the
            # aux names generated below must not shadow it.
            bare = re.match(rf"^(write|read)\s+({IDENT})\s*;", stmt.text)
            if bare:
                key = (bare.group(1), bare.group(2))
                if key in emitted:
                    raise ExpandError(
                        f"{key[0]} {key[1]} expanded twice "
                        f"(lines {emitted[key]} and {stmt.line})"
                    )
                emitted[key] = stmt.line
            out.append(_subst_fields(stmt.text, env, lookups))
            continue
        keyword, base, index, rest = parsed
        try:
            uses = _parse_dim_uses(index, dimensions)
            axes = [axis for axis, _members, _is_worst in uses]
            if len(set(axes)) != len(axes):
                raise ExpandError(f"axis repeated in index {index!r}")
            for use_index in _product_index(uses):
                suffix = "".join(
                    "_WorstCase" if is_worst else dimensions[axis].name(member)
                    for (axis, _members, is_worst), member in zip(uses, use_index)
                )
                name = f"{base}{suffix}"
                key = (keyword, name)
                if key in emitted:
                    raise ExpandError(
                        f"{keyword} {name} expanded twice "
                        f"(lines {emitted[key]} and {stmt.line})"
                    )
                emitted[key] = stmt.line
                origins.append((keyword, name, stmt.line))
                tail = rest if rest.startswith(";") else " " + rest
                text = f"{keyword} {name}{tail}".rstrip()
                subst_env = _axis_env(uses, use_index, dimensions, env)
                lowered = (
                    _lower_runtime_bind(
                        stmt.line, text, subst_env, origins, lookups, emitted
                    )
                    if keyword == "bind"
                    else None
                )
                if lowered is not None:
                    out.extend(lowered)
                else:
                    out.append(_subst_fields(text, subst_env, lookups))
        except ExpandError as exc:
            raise ExpandError(f"line {stmt.line}: {exc}") from exc
    return out


def _axis_env(
    uses: list[tuple[str, list[str], bool]],
    use_index: list[str],
    dimensions: dict[str, Axis],
    env: dict[str, object],
) -> dict[str, object]:
    """Bind each indexed axis to its member's integer units.

    An axis without `units` stays unbound; using it in a numeric field then
    reports ``unknown parameter`` instead of inventing a value.
    """
    merged = dict(env)
    for (axis, _members, is_worst), member in zip(uses, use_index):
        spec = dimensions[axis]
        if spec.units is None:
            continue
        if axis in merged:
            raise ExpandError(f"axis {axis} shadows a family parameter of the same name")
        chosen = None if is_worst else member
        merged[axis] = spec.unit_of(chosen)
        for key in spec.extra:
            merged[f"{axis}.{key}"] = spec.projection(key, chosen)
    return merged


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
    """Render flat v1.0 text through the semantic IR (``elaborate.py``).

    ``expand_legacy`` keeps the v1.2 text path for debugging and parity checks.
    """
    tools = str(Path(__file__).resolve().parents[1])
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from ssdl.elaborate import expand_text

    return expand_text(text, add_axis=add_axis, origins=origins)


def expand_legacy(
    text: str,
    *,
    add_axis: tuple[str, str] | None = None,
    origins: list[tuple[str, str, int]] | None = None,
) -> str:
    dimensions, relations, externs, machines, globals_ = split_blocks(text)
    lookups = _build_lookups(relations, externs)
    if add_axis:
        axis, value = add_axis
        if axis not in dimensions:
            raise ExpandError(f"cannot add {value!r}: dimension {axis!r} is not declared")
        if value not in dimensions[axis].values:
            dimensions[axis].values.append(value)
    families = {machine.name: machine for machine in machines if machine.family is None and machine.params}
    if origins is None:
        origins = []
    lines: list[str] = list(globals_)
    if globals_:
        lines.append("")
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
            instance_body, inherited = _split_overrides(
                list(machine.body), list(base.body), base.name
            )
            body = instance_body + inherited
        else:
            for stmt in machine.body:
                if OVERRIDE_RE.match(stmt.text) is not None:
                    raise ExpandError(
                        f"line {stmt.line}: override is only valid in a machine "
                        "instance; a family or plain machine owns its statements"
                    )
            env = {}
            body = list(machine.body)
        if machine.family is None and machine.params:
            continue  # a family declaration is not emitted by itself
        lines.append(f"machine {machine.name} {{")
        lines.extend(
            f"    {line}"
            for line in _expand_body(body, env, dimensions, origins, lookups)
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
        # A variant subject is aliased to its variant, so it has no bind.
        subjects = set(re.findall(r"^\s*variant\s+(\w+)", body, re.MULTILINE))
        for base in writes - subjects:
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
        "--legacy",
        action="store_true",
        help="use the v1.2 text expander instead of the semantic IR",
    )
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
        expander = expand_legacy if args.legacy else expand
        flat = expander(Path(args.input).read_text(), add_axis=add_axis, origins=origins)
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
