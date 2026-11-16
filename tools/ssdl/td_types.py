#!/usr/bin/env python3
"""Type a record table against the TableGen declarations.

The record layer (``record_ir.py``) models *which* field a backend binds to.
This module models *what kind of value* the declaration allows, which is the
other half of the contract: ``int Latency`` rejects ``"1"``, ``list<int>
ReleaseAtCycles`` rejects a bare string, and ``RISCVExtension<int, int, string,
list<SubtargetFeature>, ...>`` rejects a list pushed into the wrong slot.

Values are the TableGen expressions the emitter writes; they are parsed into a
small ``Init`` tree (int, bit, string, code, list, dag, def reference, class
application, ``?``) and checked against the declared type of the field or the
positional parameter of the class.  Identifiers that name host records (an
instruction, a predicate defined by the target) cannot be resolved here; they
are counted as *external* instead of guessed at.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Sequence

if __package__ in (None, ""):
    from record_ir import Record, RecordSet  # type: ignore
    from td_schema import DEFAULT_SOURCES, ClassDef, Schema, parse_files  # type: ignore
    from td_value import (  # type: ignore
        ApplyValue,
        BitValue,
        CodeValue,
        DagValue,
        IdentValue,
        Init,
        IntValue,
        ListValue,
        StringValue,
        UnsetValue,
        UnknownValue,
        parse_value,
        render,
    )
else:
    from .record_ir import Record, RecordSet
    from .td_schema import DEFAULT_SOURCES, ClassDef, Schema, parse_files
    from .td_value import (
        ApplyValue,
        BitValue,
        CodeValue,
        DagValue,
        IdentValue,
        Init,
        IntValue,
        ListValue,
        StringValue,
        UnsetValue,
        UnknownValue,
        parse_value,
        render,
    )


# ---------------------------------------------------------------- types
@dataclass(frozen=True)
class Type:
    kind: str  # "int" | "bit" | "string" | "code" | "dag" | "list" | "bits" | "class" | "unknown"
    name: str = ""
    element: "Type | None" = None
    size: int = 0

    def render(self) -> str:
        if self.kind == "list":
            return f"list<{self.element.render() if self.element else '?'}>"
        if self.kind == "bits":
            return f"bits<{self.size}>"
        return self.name or self.kind


def parse_type(text: str) -> Type:
    compact = "".join(text.split())
    if compact.startswith("list<") and compact.endswith(">"):
        return Type("list", element=parse_type(compact[5:-1]))
    if compact.startswith("bits<") and compact.endswith(">"):
        try:
            return Type("bits", size=int(compact[5:-1]))
        except ValueError:
            return Type("unknown", name=compact)
    if compact in ("int", "bit", "string", "code", "dag", "list", "bits"):
        return Type(compact, name=compact)
    if not compact:
        return Type("unknown", name="")
    return Type("class", name=compact)


@dataclass
class Diagnosis:
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass
class TypeChecker:
    schema: Schema
    records: RecordSet
    errors: list[Diagnosis] = field(default_factory=list)
    external: int = 0
    checked: int = 0

    # -- schema lookups --------------------------------------------------
    def class_of(self, name: str) -> str | None:
        record = self.records.by_name.get(name)
        if record is not None and record.bases:
            return record.bases[0].name
        record = self.schema.defs.get(name)
        if record is not None and record.bases:
            return record.bases[0].name
        if name in self.schema.classes:
            return name
        return None

    def is_subclass(
        self, cls: str, target: str, _seen: set[str] | None = None
    ) -> bool | None:
        """True/False, or None when the class is not known to the schema."""
        if cls == target:
            return True
        if cls not in self.schema.classes:
            return None  # declared outside the parsed headers: host side
        seen = _seen or set()
        if cls in seen:
            return False
        seen.add(cls)
        for base in self.schema.classes[cls].bases:
            result = self.is_subclass(base.name, target, seen)
            if result is not False:
                return result
        return False

    # -- checking --------------------------------------------------------
    def error(self, where: str, message: str) -> None:
        self.errors.append(Diagnosis(where, message))

    def check_record(self, record: Record) -> None:
        for base in record.bases:
            cls = self.schema.classes.get(base.name)
            if cls is None:
                continue
            self.check_args(f"{record.name}:{base.name}", base.args, cls)
        for name, text in record.fields:
            declared = self.declared_field(record, name)
            if declared is None:
                self.error(f"{record.name}.{name}", "field is not declared by any base")
                continue
            self.check_value(f"{record.name}.{name}", parse_value(text), declared)

    def declared_field(self, record: Record, name: str) -> Type | None:
        for base in record.bases:
            info = self.schema.fields_of(base.name).get(name)
            if info is not None:
                return parse_type(info["type"])
        return None

    def check_args(self, where: str, args: Sequence[str], cls: ClassDef) -> None:
        params = cls.params
        if len(args) > len(params):
            self.error(
                where,
                f"{len(args)} arguments for {len(params)} parameters",
            )
        for index, text in enumerate(args):
            if index >= len(params):
                break
            self.check_value(
                f"{where}[{index}] ({params[index].name})",
                parse_value(text),
                parse_type(params[index].type),
            )

    def check_value(self, where: str, value: Init, declared: Type) -> None:
        self.checked += 1
        if isinstance(value, UnknownValue):
            self.error(where, f"cannot parse value {value.text!r}")
            return
        if isinstance(value, UnsetValue):
            return  # `?` is the declared "no value" marker
        kind = declared.kind
        if kind == "int":
            if not isinstance(value, IntValue):
                self.error(where, f"expected int, got {render(value)}")
        elif kind == "bit":
            if not isinstance(value, (BitValue, IntValue)):
                self.error(where, f"expected bit, got {render(value)}")
            elif isinstance(value, IntValue) and value.value not in (0, 1):
                self.error(where, f"expected bit, got {value.value}")
        elif kind == "string":
            if not isinstance(value, StringValue):
                self.error(where, f"expected string, got {render(value)}")
        elif kind == "code":
            if not isinstance(value, CodeValue):
                self.error(where, f"expected code, got {render(value)}")
        elif kind == "dag":
            if not isinstance(value, DagValue):
                self.error(where, f"expected dag, got {render(value)}")
        elif kind == "list":
            if not isinstance(value, ListValue):
                self.error(where, f"expected list, got {render(value)}")
                return
            element = declared.element or Type("unknown")
            for index, item in enumerate(value.items):
                self.check_value(f"{where}[{index}]", item, element)
        elif kind == "bits":
            if not isinstance(value, IntValue):
                self.error(where, f"expected bits<{declared.size}>, got {render(value)}")
            elif not 0 <= value.value < (1 << declared.size):
                self.error(
                    where,
                    f"{value.value} does not fit bits<{declared.size}>",
                )
        elif kind == "class":
            self.check_instance(where, value, declared.name)
        else:
            self.error(where, f"unknown declared type {declared.render()}")

    def check_instance(self, where: str, value: Init, target: str) -> None:
        if isinstance(value, IdentValue):
            cls = self.class_of(value.name)
            if cls is None:
                self.external += 1  # a host record (instruction, predicate def)
                return
            result = self.is_subclass(cls, target)
            if result is None:
                self.external += 1
            elif not result:
                self.error(where, f"{value.name} is a {cls}, not a {target}")
            return
        if isinstance(value, ApplyValue):
            cls = self.class_of(value.name)
            if cls is None:
                self.external += 1
                return
            result = self.is_subclass(cls, target)
            if result is False:
                self.error(where, f"{value.name} is a {cls}, not a {target}")
            params = self.schema.classes.get(value.name)
            if params is not None:
                self.check_args(where, [render(a) for a in value.args], params)
            return
        self.error(where, f"expected a {target}, got {render(value)}")


def check(records: RecordSet, schema: Schema) -> TypeChecker:
    checker = TypeChecker(schema, records)
    for record in records:
        checker.check_record(record)
    return checker


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("records", help="records JSON produced by emit_td.py --records-json")
    ap.add_argument("--llvm-src", default="/home/ubuntu/llvm/trees/explore/llvm")
    ap.add_argument(
        "--source",
        action="append",
        default=[],
        help="extra TD file to read declarations from (repeatable)",
    )
    ap.add_argument("--limit", type=int, default=20, help="errors to print (default 20)")
    args = ap.parse_args(argv)

    sources = [pathlib.Path(args.llvm_src) / rel for rel in DEFAULT_SOURCES]
    sources.extend(pathlib.Path(path) for path in args.source)
    schema = parse_files(sources)
    with open(args.records, encoding="utf-8") as f:
        records = RecordSet.from_json(json.load(f))
    checker = check(records, schema)
    for diagnosis in checker.errors[: args.limit]:
        print(diagnosis.render(), file=sys.stderr)
    if len(checker.errors) > args.limit:
        print(f"... {len(checker.errors) - args.limit} more", file=sys.stderr)
    print(
        f"{len(records)} records, {checker.checked} values checked, "
        f"{checker.external} external, {len(checker.errors)} errors"
    )
    return 1 if checker.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
