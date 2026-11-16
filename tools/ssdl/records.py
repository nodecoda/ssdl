"""Reader for ``llvm-tblgen -print-records`` output.

``-print-records`` is the only stable, textual view of TableGen's fully
expanded records: it prints every field after class inheritance, ``let``
overrides, and ``foreach`` expansion.  Both the equivalence checker and the
records-to-SSDL generator are written against this format so that they compare
what the compiler actually sees, not what the source text appears to say.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RECORD_RE = re.compile(r"def (\S+) \{\t// (.*)$")


@dataclass
class RecordFile:
    """Parsed records: ``cls[def]`` is the last class on the comment line."""

    cls: dict[str, str] = field(default_factory=dict)
    body: dict[str, list[str]] = field(default_factory=dict)

    def value(self, name: str, prefix: str) -> str | None:
        for line in self.body.get(name, ()):
            text = line.strip()
            if text.startswith(prefix + " "):
                return text.split("=", 1)[1].strip().rstrip(";")
        return None

    def has(self, name: str, prefix: str) -> bool:
        return self.value(name, prefix) is not None

    def scoped_to(self, model: str) -> list[str]:
        """Defs that carry ``SchedModel = <model>``, in dump order."""
        needle = f"SchedModel = {model}"
        return [
            name
            for name, body in self.body.items()
            if any(needle in line for line in body)
        ]

    def defs_of(self, class_suffix: str) -> list[str]:
        return [n for n, c in self.cls.items() if c.endswith(class_suffix)]


def load(path: str) -> RecordFile:
    recs = RecordFile()
    cur: str | None = None
    buf: list[str] = []
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("def "):
                m = RECORD_RE.match(line.rstrip("\n"))
                if m:
                    cur = m.group(1)
                    recs.cls[cur] = m.group(2).split()[-1] if m.group(2) else ""
                    buf = []
                else:
                    cur = None
            elif cur is not None:
                if line.startswith("}"):
                    recs.body[cur] = buf
                    cur = None
                else:
                    buf.append(line.rstrip())
    return recs


def as_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return (value,)
    inner = value[1:-1].strip()
    return tuple(x.strip() for x in inner.split(",") if x.strip())


def as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def as_bit(value: str | None) -> bool:
    return value == "1"


def unquote(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value
