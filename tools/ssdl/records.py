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

    #: Last class on the comment line (the most derived one).
    cls: dict[str, str] = field(default_factory=dict)
    #: Full class chain as printed, base first.  Custom subclasses (a target's
    #: ``class Foo : SchedWriteRes<...>``) only reveal what they are here.
    chain: dict[str, tuple[str, ...]] = field(default_factory=dict)
    body: dict[str, list[str]] = field(default_factory=dict)

    def kind(self, name: str, known: "set[str] | tuple[str, ...]") -> str | None:
        """The most derived class of ``name`` from ``known``, if any."""
        for token in reversed(self.chain.get(name, ())):
            if token in known:
                return token
        return None

    def value(self, name: str, prefix: str) -> str | None:
        for line in self.body.get(name, ()):
            text = line.strip()
            if text.startswith(prefix + " "):
                return text.split("=", 1)[1].strip().rstrip(";")
        return None

    def has(self, name: str, prefix: str) -> bool:
        return self.value(name, prefix) is not None

    def block(self, name: str, prefix: str) -> str | None:
        """Like :meth:`value`, but join a ``[{ ... }]`` block across lines.

        ``-print-records`` wraps a ``code`` field when its text is long or
        contains a newline, so the value is not on one line.  The block ends
        when the delimiters balance.
        """
        body = self.body.get(name, ())
        field = prefix.split()[-1]
        for i, line in enumerate(body):
            text = line.strip()
            # ``-print-records`` writes ``code Predicate =``; the JSON table
            # (and its expansion) has no types, only ``Predicate =``.
            if not (text.startswith(prefix + " ") or text.startswith(field + " = ")):
                continue
            value = text.split("=", 1)[1].strip()
            while value.count("[{") > value.count("}]") and i + 1 < len(body):
                i += 1
                value += "\n" + body[i]
            return value.rstrip().rstrip(";")
        return None

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
                    tokens = tuple(m.group(2).split()) if m.group(2) else ()
                    recs.chain[cur] = tokens
                    recs.cls[cur] = tokens[-1] if tokens else ""
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


class JsonRecordFile(RecordFile):
    """``RecordFile`` view of an ``ssdl-records-1`` table.

    The JSON table stores field names and values but not their TableGen types,
    so lookups match on the field name alone (the last token of a
    ``-print-records`` prefix such as ``int Latency``).
    """

    def value(self, name: str, prefix: str) -> str | None:
        field = prefix.split()[-1]
        needle = field + " = "
        for line in self.body.get(name, ()):
            text = line.strip()
            if text.startswith(needle):
                return text[len(needle):]
        return None


def load_json(path: str) -> JsonRecordFile:
    """Read the records table written by ``emit_td.py --records-json``."""
    import json

    if __package__ in (None, ""):
        from td_value import from_json_node, render  # type: ignore
    else:
        from .td_value import from_json_node, render
    recs = JsonRecordFile()
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    for entry in obj.get("records", ()):
        name = entry["name"]
        bases = entry.get("bases", ())
        if bases:
            last = bases[-1]
            recs.cls[name] = last if isinstance(last, str) else last.get("class", "")
        else:
            recs.cls[name] = ""
        lines: list[str] = []
        if entry.get("scope"):
            lines.append(f"SchedModel = {entry['scope']}")
        for field_name, value in entry.get("fields", ()):
            text = value if isinstance(value, str) else render(from_json_node(value))
            lines.append(f"{field_name} = {text}")
        recs.body[name] = lines
    return recs
