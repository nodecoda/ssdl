#!/usr/bin/env python3
"""Typed field shapes for SSDL v2 record bodies.

The v2 grammar already says what each field *is*; this module makes that
statement executable.  A record body is parsed as text, so the front end can
only see the shape of a value (string / bool / list / expression).  That is
enough to catch the errors that matter: a string where a number belongs, a
scalar where a list belongs, an identifier where a bool belongs.

The table is deliberately small and explicit.  Adding a field to the parser
without adding it here makes the field unknown, which is the desired failure
mode: one table, one spelling.
"""
from __future__ import annotations

import re

INT_EXPR = "int_expr"
INT_LIST = "int_list"
BOOL = "bool"
BOOL_LIST = "bool_list"
IDENT = "ident"
IDENT_LIST = "ident_list"
SCHED_RW_LIST = "sched_rw_list"
STRING = "string"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SCHED_RW_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\[[^\]]*\])?$")

#: entity kind -> field -> expected shape
ENTITY_FIELDS: dict[str, dict[str, str]] = {
    "machine": {
        "features": IDENT_LIST,
        "tune_features": IDENT_LIST,
        "unsupported_features": IDENT_LIST,
        "default_march": STRING,
        "issue_width": INT_EXPR,
        "micro_op_buffer_size": INT_EXPR,
        "loop_micro_op_buffer": INT_EXPR,
        "load_latency": INT_EXPR,
        "high_latency": INT_EXPR,
        "mispredict_penalty": INT_EXPR,
        "post_ra_scheduler": BOOL,
        "complete_model": BOOL,
        "full_inst_rw_overlap_check": BOOL,
        "enable_intervals": BOOL,
        "model": IDENT,
    },
    "resource": {
        "super": IDENT,
        "units": INT_EXPR,
        "buffer": INT_EXPR,
        "name": STRING,
    },
    "res_group": {"buffer": INT_EXPR},
    "write_seq": {"repeat": INT_EXPR},
    "load_queue": {"resource": IDENT},
    "store_queue": {"resource": IDENT},
    "reg_file": {
        "num_phys": INT_EXPR,
        "classes": IDENT_LIST,
        "costs": INT_LIST,
        "allow_move_elim": BOOL_LIST,
        "max_move_elim_per_cy": INT_EXPR,
        "allow_zero_elim_only": BOOL,
    },
    "retire_unit": {"buffer": INT_EXPR, "max_per_cycle": INT_EXPR},
    "bind": {
        "resources": IDENT_LIST,
        "latency": INT_EXPR,
        "uops": INT_EXPR,
        "release": INT_LIST,
        "acquire": INT_LIST,
        "begin_grp": BOOL,
        "end_grp": BOOL,
        "single": BOOL,
        "retire_ooo": BOOL,
        "unsupported": BOOL,
    },
    "advance": {
        "cycles": INT_EXPR,
        "writes": SCHED_RW_LIST,
        "tunables": INT_LIST,
        "unsupported": BOOL,
    },
    "variant": {"variadic": BOOL},
    "map": {"unsupported": BOOL},
    "stage": {
        "cycles": INT_EXPR,
        "units": IDENT_LIST,
        "timeinc": INT_EXPR,
        "kind": "enum:required|reserved",
    },
    "itin": {"cycles": INT_LIST, "bypasses": IDENT_LIST, "uops": INT_EXPR},
    "itineraries": {"func_units": IDENT_LIST, "bypasses": IDENT_LIST},
}


def split_list(text: str) -> list[str]:
    """Split ``[a, b, ...]`` at top-level commas, respecting strings."""
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        return []
    body = body[1:-1]
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quoted = False
    escaped = False
    for ch in body:
        if quoted:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return [item for item in items if item]


def classify(text: str) -> str:
    """Classify a rendered value as string / bool / list / expr / empty."""
    body = text.strip()
    if not body:
        return "empty"
    if body.startswith('"') and body.endswith('"') and len(body) >= 2:
        return "string"
    if body in ("true", "false"):
        return "bool"
    if body.startswith("["):
        return "list"
    return "expr"


def _enum_values(expected: str) -> tuple[str, ...] | None:
    if expected.startswith("enum:"):
        return tuple(expected[len("enum:") :].split("|"))
    return None


def value_error(expected: str, text: str) -> str | None:
    """Return a human-readable mismatch, or ``None`` when the shape fits."""
    actual = classify(text)
    body = text.strip()
    enum = _enum_values(expected)
    if enum is not None:
        if actual != "expr" or body not in enum:
            return f"expects one of {', '.join(enum)}, got {body or 'nothing'}"
        return None
    if expected == STRING:
        if actual != "string":
            return f"expects a string, got {actual}"
        return None
    if expected == BOOL:
        if actual != "bool":
            return f"expects true or false, got {body or 'nothing'}"
        return None
    if expected == INT_EXPR:
        if actual != "expr":
            return f"expects an integer expression, got {actual}"
        return None
    if expected == IDENT:
        if actual != "expr" or not _IDENT_RE.match(body):
            return f"expects an identifier, got {body or 'nothing'}"
        return None
    if expected == INT_LIST:
        if actual != "list":
            return f"expects an integer list, got {actual}"
        for item in split_list(body):
            if classify(item) != "expr":
                return f"expects an integer list, got item {item!r}"
        return None
    if expected == BOOL_LIST:
        if actual != "list":
            return f"expects a true/false list, got {actual}"
        for item in split_list(body):
            if classify(item) != "bool":
                return f"expects a true/false list, got item {item!r}"
        return None
    if expected == IDENT_LIST:
        if actual != "list":
            return f"expects an identifier list, got {actual}"
        for item in split_list(body):
            if not _IDENT_RE.match(item):
                return f"expects an identifier list, got item {item!r}"
        return None
    if expected == SCHED_RW_LIST:
        if actual != "list":
            return f"expects a write/read list, got {actual}"
        for item in split_list(body):
            if not _SCHED_RW_RE.match(item):
                return f"expects a write/read list, got item {item!r}"
        return None
    return None
