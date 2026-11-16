#!/usr/bin/env python3
"""The record table that sits between SSDL and any TableGen surface syntax.

TableGen's pipeline is ``schema -> records -> emitter``.  The emitters bind to
records **by string**: ``getValueAsInt("Latency")`` has no type relationship to
``int Latency`` declared in ``TargetSchedule.td``.  SSDL replaces the first
step (the schema is the semantic model), so what a host actually consumes is
the second step: a table of definitions, each with an identity, a class
instantiation, and a set of field bindings.

``.td`` text is one printer for that table -- the one ``llvm-tblgen`` happens
to accept.  ``emit_td.py`` and ``predicates.py`` therefore build this table
while they lower and render each record into the text they emit, so the text
cannot drift from the table.  ``--records-json`` serialises the table, and
``record_ir.py records.json`` prints it back as TableGen; both paths reach the
same ``RecordKeeper``.

A field value stays a TableGen *expression* (``1``, ``[PipeA]``, ``true``,
``SiFive7Model``): the record layer models the definition structure the
backend binds to, not the expression grammar.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Base:
    """One class instantiation in a def's base list."""

    name: str
    args: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.args:
            return self.name
        return f"{self.name}<{', '.join(self.args)}>"

    def to_json(self) -> dict:
        return {"class": self.name, "args": list(self.args)}

    @classmethod
    def from_json(cls, obj: dict) -> "Base":
        return cls(obj["class"], tuple(obj.get("args", ())))

    @classmethod
    def parse(cls, text: str) -> "Base":
        """Split ``Name<arg, arg>`` into a class and its top-level arguments.

        The lowerers compute a few bases as text (``Predicate<"...">``); this
        keeps those sites readable without giving up the structure.
        """
        text = text.strip()
        if "<" not in text:
            return cls(text)
        name, rest = text.split("<", 1)
        assert rest.endswith(">"), text
        return cls(name.strip(), tuple(split_args(rest[:-1])))


def split_args(text: str) -> list[str]:
    """Split a TableGen argument list on top-level commas."""
    args: list[str] = []
    depth = 0
    current: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            current.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current.append(ch)
        elif ch in "<[({":
            depth += 1
            current.append(ch)
        elif ch in ">])}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        args.append(tail)
    return args


@dataclass(frozen=True)
class Record:
    """A definition: ``def NAME : Base, Base { let Field = Value; }``."""

    name: str
    bases: tuple[Base, ...]
    fields: tuple[tuple[str, str], ...] = ()
    # `SchedModel = <scope>` is inherited from a surrounding `let ... in {}` in
    # the printed text; keeping it on the record lets a reader group records by
    # model without re-parsing scopes.
    scope: str | None = None
    style: str = "block"  # "block": `{\n  let ...;\n}`  "inline": `{ let ...; }`
    # Some emitters write `};` after a block body; TableGen accepts both.
    suffix: str = ""
    origin: str = ""

    def render(self) -> str:
        head = f"def {self.name} : " + ", ".join(base.render() for base in self.bases)
        if not self.fields:
            return head + ";"
        body = [f"let {name} = {value};" for name, value in self.fields]
        if self.style == "inline":
            # `def X { ... };` is rejected by llvm-tblgen ("a class or def body
            # should not end with a semicolon"), so the inline form carries no
            # terminator unless a caller asks for one.
            return head + " { " + " ".join(body) + " }" + self.suffix
        return head + " {\n" + "\n".join("  " + line for line in body) + "\n}" + self.suffix

    def to_json(self) -> dict:
        obj: dict = {
            "name": self.name,
            "bases": [base.to_json() for base in self.bases],
            "fields": [[name, value] for name, value in self.fields],
        }
        if self.scope is not None:
            obj["scope"] = self.scope
        if self.style != "block":
            obj["style"] = self.style
        if self.suffix:
            obj["suffix"] = self.suffix
        if self.origin:
            obj["origin"] = self.origin
        return obj

    @classmethod
    def from_json(cls, obj: dict) -> "Record":
        return cls(
            name=obj["name"],
            bases=tuple(Base.from_json(base) for base in obj["bases"]),
            fields=tuple((name, value) for name, value in obj.get("fields", ())),
            scope=obj.get("scope"),
            style=obj.get("style", "block"),
            suffix=obj.get("suffix", ""),
            origin=obj.get("origin", ""),
        )


class DuplicateRecord(Exception):
    def __init__(self, name: str, previous: Record, record: Record):
        self.name = name
        self.previous = previous
        self.record = record
        super().__init__(
            f"def {name!r} emitted twice: {previous.origin or '?'} and "
            f"{record.origin or '?'}"
        )


@dataclass
class RecordSet:
    """Definitions in emission order, with duplicate-name detection."""

    records: list[Record] = field(default_factory=list)
    by_name: dict[str, Record] = field(default_factory=dict)

    def add(
        self,
        name: str,
        bases: Sequence[Base | str],
        fields: Iterable[tuple[str, str]] = (),
        *,
        scope: str | None = None,
        style: str = "block",
        suffix: str = "",
        origin: str = "",
        where: str = "",
    ) -> Record:
        resolved: list[Base] = []
        for base in bases:
            resolved.append(base if isinstance(base, Base) else Base(base))
        record = Record(
            name=name,
            bases=tuple(resolved),
            fields=tuple(fields),
            scope=scope,
            style=style,
            suffix=suffix,
            origin=origin or where,
        )
        if name in self.by_name:
            raise DuplicateRecord(name, self.by_name[name], record)
        self.by_name[name] = record
        self.records.append(record)
        return record

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def __contains__(self, name: str) -> bool:
        return name in self.by_name

    def __getitem__(self, name: str) -> Record:
        return self.by_name[name]

    def of_class(self, cls: str) -> list[Record]:
        return [r for r in self.records if any(b.name == cls for b in r.bases)]

    # -- serialisation ---------------------------------------------------
    def to_json(self) -> dict:
        return {
            "format": "ssdl-records-1",
            "records": [record.to_json() for record in self.records],
        }

    @classmethod
    def from_json(cls, obj: dict) -> "RecordSet":
        out = cls()
        for entry in obj["records"]:
            record = Record.from_json(entry)
            if record.name in out.by_name:  # pragma: no cover - invalid table
                raise DuplicateRecord(record.name, out.by_name[record.name], record)
            out.by_name[record.name] = record
            out.records.append(record)
        return out

    def dumps(self) -> str:
        """One record per line: greppable, diffable, still valid JSON."""
        lines = [json.dumps(record.to_json()) for record in self.records]
        body = ",\n".join("  " + line for line in lines)
        return (
            '{\n "format": "ssdl-records-1",\n "records": [\n'
            + body
            + "\n ]\n}\n"
        )

    # -- printing --------------------------------------------------------
    def render(self) -> str:
        """Print every record as TableGen, grouping scopes into ``let`` blocks.

        Unscoped records come first: a scoped block names its
        ``SchedMachineModel``, which must already be defined, and the global
        scheduling vocabulary it references (``SchedWrite``, ``FuncUnit``) is
        unscoped too.
        """
        chunks: list[str] = [record.render() for record in self.records if record.scope is None]
        runs: list[tuple[str, list[Record]]] = []
        for record in self.records:
            if record.scope is None:
                continue
            if runs and runs[-1][0] == record.scope:
                runs[-1][1].append(record)
            else:
                runs.append((record.scope, [record]))
        for scope, run in runs:
            body = "\n\n".join(record.render() for record in run)
            body = "\n".join("  " + line for line in body.splitlines())
            chunks.append(f"let SchedModel = {scope} in {{\n{body}\n}}")
        return "\n\n".join(chunks) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="records JSON produced by emit_td.py --records-json")
    ap.add_argument("-o", "--output", help="write TableGen to this file (default: stdout)")
    ap.add_argument("--count", action="store_true", help="print the record count only")
    ap.add_argument(
        "--include",
        dest="includes",
        action="append",
        default=[],
        help="TableGen file to include before the records (repeatable)",
    )
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        records = RecordSet.from_json(json.load(f))
    if args.count:
        print(len(records))
        return 0
    text = "".join(f'include "{name}"\n' for name in args.includes) + records.render()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
