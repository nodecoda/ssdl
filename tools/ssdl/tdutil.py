"""Small TableGen text helpers shared by the emitter and predicate lowerer."""
from __future__ import annotations

import re


def td_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def td_code(code: str) -> str:
    """TableGen code block.  The parser already preserves nested [{}]."""
    return "[{" + code + "}]"


def td_def_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not out or out[0].isdigit():
        out = "_" + out
    return out


def join_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"
