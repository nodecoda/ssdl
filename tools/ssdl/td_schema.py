#!/usr/bin/env python3
"""Extract the target-independent scheduling schema from TableGen sources.

TableGen is a schema language: a ``class`` declares a record shape, a ``def``
instantiates it, and the emitter reads the resulting records.  The emitter
binds to that schema **by string**: ``getValueAsInt("Latency")`` has no type
relationship to ``int Latency`` in ``TargetSchedule.td``.  This module recovers
the schema side of that relationship so the binding can be checked.

Only the declaration subset needed by the scheduling/subtarget backends is
parsed: classes, parameters, bases, fields, initialisers, ``let`` overrides,
defs, and DAG operators.  Expressions are kept as text; they are not evaluated.

Usage::

    td_schema.py --llvm-src /path/to/llvm --json
    td_schema.py --field SchedMachineModel.Latency
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

# The target-independent headers that define the scheduling/subtarget domain.
DEFAULT_SOURCES = (
    "include/llvm/Target/TargetSchedule.td",
    "include/llvm/Target/TargetSubtarget.td",
    "include/llvm/Target/TargetInstrPredicate.td",
    "include/llvm/Target/TargetItinerary.td",
    "include/llvm/Target/Target.td",
    "include/llvm/CodeGen/ValueTypes.td",
)

PUNCT = set("{}<>[]();,:=")


@dataclass
class Param:
    type: str
    name: str | None
    default: str = ""


@dataclass
class Base:
    name: str
    args: str = ""


@dataclass
class ClassDef:
    name: str
    params: list[Param] = field(default_factory=list)
    bases: list[Base] = field(default_factory=list)
    fields: dict[str, dict[str, str]] = field(default_factory=dict)
    lets: dict[str, str] = field(default_factory=dict)


@dataclass
class Def:
    name: str
    bases: list[Base] = field(default_factory=list)
    fields: dict[str, dict[str, str]] = field(default_factory=dict)
    lets: dict[str, str] = field(default_factory=dict)


@dataclass
class Schema:
    classes: dict[str, ClassDef] = field(default_factory=dict)
    defs: dict[str, Def] = field(default_factory=dict)
    dag_ops: list[str] = field(default_factory=list)

    def declaring_classes(self, field_name: str) -> list[str]:
        """Classes that declare ``field_name``, in sorted order."""
        return sorted(
            name for name, cls in self.classes.items() if field_name in cls.fields
        )

    def resolve(self, binding: "Binding") -> list[str]:
        """Where a backend binding lands in the schema ([] means undeclared)."""
        if binding.kind in ("record", "class"):
            # getAllDerivedDefinitions()/isSubClassOf() name a class.
            return [binding.name] if binding.name in self.classes else []
        if binding.kind == "def":
            return [binding.name] if binding.name in self.defs else []
        return self.declaring_classes(binding.name)

    def fields_of(self, name: str, _seen: set[str] | None = None) -> dict[str, dict[str, str]]:
        """Effective fields of a class, including inherited ones."""
        if name not in self.classes:
            return {}
        seen = set() if _seen is None else _seen
        if name in seen:
            return {}
        seen.add(name)
        out: dict[str, dict[str, str]] = {}
        for base in self.classes[name].bases:
            out.update(self.fields_of(base.name, seen))
        out.update(self.classes[name].fields)
        return out

    def has_field(self, class_name: str, field_name: str) -> bool:
        return field_name in self.fields_of(class_name)

    def class_names(self) -> set[str]:
        return set(self.classes)

    def def_names(self) -> set[str]:
        return set(self.defs)

    def to_json(self) -> dict:
        def one_class(c: ClassDef) -> dict:
            return {
                "params": [vars(p) for p in c.params],
                "bases": [vars(b) for b in c.bases],
                "fields": c.fields,
                "lets": c.lets,
            }

        return {
            "classes": {n: one_class(c) for n, c in sorted(self.classes.items())},
            "defs": {
                n: {"bases": [vars(b) for b in d.bases], "fields": d.fields, "lets": d.lets}
                for n, d in sorted(self.defs.items())
            },
            "dag_ops": self.dag_ops,
        }


# ---------------------------------------------------------------- tokenizer
def tokenize(text: str) -> list[tuple[str, str]]:
    i, n = 0, len(text)
    out: list[tuple[str, str]] = []
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "[" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}]", i + 2)
            if j < 0:
                j = n - 2
            out.append(("code", text[i : j + 2]))
            i = j + 2
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(("str", text[i : j + 1]))
            i = j + 1
            continue
        if c == "!":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("bang", text[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(("id", text[i:j]))
            i = j
            continue
        if c.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            out.append(("num", text[i:j]))
            i = j
            continue
        if text.startswith("...", i):
            out.append(("punct", "..."))
            i += 3
            continue
        out.append(("punct", c) if c in PUNCT else ("other", c))
        i += 1
    return out


# ---------------------------------------------------------------- parser
class Parser:
    def __init__(self, toks: list[tuple[str, str]]):
        self.toks = toks
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("eof", "")

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        self.i += 1
        return tok

    def at(self, value: str) -> bool:
        return self.peek()[1] == value

    def expect(self, value: str) -> tuple[str, str]:
        tok = self.next()
        if tok[1] != value:
            raise ValueError(f"expected {value!r}, got {tok!r} at token {self.i}")
        return tok

    def skip_balanced(self, open_: str, close: str) -> None:
        depth = 0
        while True:
            tok = self.next()
            if tok[0] == "eof":
                return
            if tok[1] == open_:
                depth += 1
            elif tok[1] == close:
                depth -= 1
                if depth == 0:
                    return

    def expr_until(self, stops: Iterable[str]) -> str:
        start = self.i
        depth = 0
        stops = set(stops)
        while True:
            tok = self.peek()
            if tok[0] == "eof":
                break
            if depth == 0 and tok[1] in stops:
                break
            if tok[1] in ("<", "[", "(", "{"):
                depth += 1
            elif tok[1] in (">", "]", ")", "}"):
                if depth == 0:
                    break
                depth -= 1
            self.next()
        return " ".join(t[1] for t in self.toks[start : self.i])


def _split_param(seg: list[tuple[str, str]]) -> Param:
    eq = None
    depth = 0
    for i, tok in enumerate(seg):
        if tok[1] in ("<", "[", "("):
            depth += 1
        elif tok[1] in (">", "]", ")"):
            depth -= 1
        elif tok[1] == "=" and depth == 0:
            eq = i
            break
    left = seg if eq is None else seg[:eq]
    right: list[tuple[str, str]] = [] if eq is None else seg[eq + 1 :]
    name = None
    for k in range(len(left) - 1, -1, -1):
        if left[k][0] == "id":
            name = left[k][1]
            left = left[:k]
            break
    return Param(
        type=" ".join(t[1] for t in left),
        name=name,
        default=" ".join(t[1] for t in right),
    )


def parse_params(p: Parser) -> list[Param]:
    out: list[Param] = []
    if not p.at("<"):
        return out
    p.next()
    seg: list[tuple[str, str]] = []
    depth = 0
    while True:
        tok = p.next()
        if tok[0] == "eof":
            break
        if tok[1] == "<":
            depth += 1
            seg.append(tok)
            continue
        if tok[1] == ">":
            if depth == 0:
                if seg:
                    out.append(_split_param(seg))
                break
            depth -= 1
            seg.append(tok)
            continue
        if tok[1] == "," and depth == 0:
            out.append(_split_param(seg))
            seg = []
            continue
        seg.append(tok)
    return out


def parse_bases(p: Parser) -> list[Base]:
    out: list[Base] = []
    if not p.at(":"):
        return out
    p.next()
    while p.peek()[0] == "id":
        name = p.next()[1]
        args = ""
        if p.at("<"):
            start = p.i
            p.skip_balanced("<", ">")
            args = " ".join(t[1] for t in p.toks[start + 1 : p.i - 1])
        out.append(Base(name, args))
        if p.at(","):
            p.next()
            continue
        break
    return out


def parse_body(p: Parser) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    fields: dict[str, dict[str, str]] = {}
    lets: dict[str, str] = {}
    p.expect("{")
    while not p.at("}"):
        tok = p.peek()
        if tok[0] == "eof":
            break
        if tok[1] == "let":
            p.next()
            name = p.next()[1]
            p.expect("=")
            value = p.expr_until({";", "in"})
            if p.at("in"):
                p.next()
                if p.at("{"):
                    p.next()
                    p.skip_balanced("{", "}")
                else:
                    while not p.at(";") and p.peek()[0] != "eof":
                        p.next()
            else:
                p.expect(";")
            lets[name] = value
            continue
        if tok[1] in ("def", "class", "defm", "multiclass", "foreach", "defvar", "defset"):
            p.next()
            while not p.at(";") and not p.at("{") and p.peek()[0] != "eof":
                p.next()
            if p.at("{"):
                p.skip_balanced("{", "}")
            if p.at(";"):
                p.next()
            continue
        start = p.i
        depth = 0
        while True:
            t = p.peek()
            if t[0] == "eof" or (depth == 0 and t[1] == "="):
                break
            if t[1] in ("<", "[", "(", "{"):
                depth += 1
            elif t[1] in (">", "]", ")", "}"):
                depth -= 1
            p.next()
        if not p.at("="):
            while not p.at(";") and not p.at("}") and p.peek()[0] != "eof":
                p.next()
            if p.at(";"):
                p.next()
            continue
        toks = p.toks[start : p.i]
        name = None
        for k in range(len(toks) - 1, -1, -1):
            if toks[k][0] == "id":
                name = toks[k][1]
                toks = toks[:k]
                break
        p.expect("=")
        value = p.expr_until({";"})
        p.expect(";")
        if name:
            fields[name] = {"type": " ".join(t[1] for t in toks), "init": value}
    p.expect("}")
    return fields, lets


def parse_text(text: str) -> Schema:
    schema = Schema()
    p = Parser(tokenize(text))

    def scope(single: bool = False) -> None:
        while not p.at("}") and p.peek()[0] != "eof":
            tok = p.next()
            if tok[1] == "include":
                if p.peek()[0] == "str":
                    p.next()
                if p.at(";"):
                    p.next()
            elif tok[1] == "class":
                name = p.next()[1]
                params = parse_params(p)
                bases = parse_bases(p)
                if p.at(";"):
                    p.next()
                    fields, lets = {}, {}
                else:
                    fields, lets = parse_body(p)
                    if p.at(";"):
                        p.next()
                new = ClassDef(name, params, bases, fields, lets)
                old = schema.classes.get(name)
                if old and (old.fields or old.bases) and not (fields or bases):
                    pass  # keep the full definition over a forward declaration
                else:
                    schema.classes[name] = new
            elif tok[1] == "def":
                name = p.next()[1]
                bases = parse_bases(p)
                if p.at(";"):
                    p.next()
                    fields, lets = {}, {}
                else:
                    fields, lets = parse_body(p)
                    if p.at(";"):
                        p.next()
                schema.defs[name] = Def(name, bases, fields, lets)
                if not bases and not fields:
                    schema.dag_ops.append(name)
            elif tok[1] == "let":
                name = p.next()[1]
                p.expect("=")
                value = p.expr_until({";", "in"})
                if p.at("in"):
                    p.next()
                    if p.at("{"):
                        p.next()
                        scope()
                        p.expect("}")
                    else:
                        scope(single=True)
                else:
                    p.expect(";")
            elif tok[1] == "foreach":
                while not p.at("{") and p.peek()[0] != "eof":
                    p.next()
                p.skip_balanced("{", "}")
            elif tok[1] in ("defm", "multiclass"):
                while not p.at("{") and not p.at(";") and p.peek()[0] != "eof":
                    p.next()
                if p.at("{"):
                    p.skip_balanced("{", "}")
                if p.at(";"):
                    p.next()
            elif tok[1] in ("defvar", "defset"):
                while not p.at(";") and not p.at("{") and p.peek()[0] != "eof":
                    p.next()
                if p.at("{"):
                    p.skip_balanced("{", "}")
                if p.at(";"):
                    p.next()
            if single:
                return

    scope()
    return schema


def parse_files(paths: Iterable[str | pathlib.Path]) -> Schema:
    merged = Schema()
    for path in paths:
        one = parse_text(pathlib.Path(path).read_text(encoding="utf-8"))
        for name, cls in one.classes.items():
            old = merged.classes.get(name)
            if old and (old.fields or old.bases) and not (cls.fields or cls.bases):
                continue
            merged.classes[name] = cls
        merged.defs.update(one.defs)
        merged.dag_ops.extend(op for op in one.dag_ops if op not in merged.dag_ops)
    return merged


def default_sources(llvm_src: str) -> list[pathlib.Path]:
    root = pathlib.Path(llvm_src)
    return [root / rel for rel in DEFAULT_SOURCES]



# ---------------------------------------------------------------- bindings
# The emitters reach into records by *string*.  These are the shapes that
# string appears in; every hit is a keyword the TD schema must declare, and
# nothing else in the C++ record API is a binding to the schema.
BACKEND_SOURCES = (
    "utils/TableGen/SubtargetEmitter.cpp",
    "utils/TableGen/Common/CodeGenSchedule.cpp",
    "utils/TableGen/Common/CodeGenSchedule.h",
    "utils/TableGen/Common/PredicateExpander.cpp",
    "utils/TableGen/Common/SubtargetFeatureInfo.cpp",
    "utils/TableGen/Common/SubtargetFeatureInfo.h",
    "utils/TableGen/DFAPacketizerEmitter.cpp",
    "utils/TableGen/AsmMatcherEmitter.cpp",
)

BINDING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("record", re.compile(r'getAllDerivedDefinitions(?:_IfDefined)?\(\s*"([A-Za-z0-9_]+)"')),
    ("class", re.compile(r'isSubClassOf\(\s*"([A-Za-z0-9_]+)"')),
    ("field", re.compile(r'getValueAs[A-Za-z]*\(\s*"([A-Za-z0-9_]+)"')),
    ("field", re.compile(r'emitProcessorProp\([^,]+,\s*[^,]+,\s*"([A-Za-z0-9_]+)"')),
    ("field", re.compile(r'addFieldExpander\(\s*"[A-Za-z0-9_]+",\s*"([A-Za-z0-9_]+)"')),
    ("field", re.compile(r'getValue\(\s*"([A-Za-z0-9_]+)"')),
    ("def", re.compile(r'getDef\(\s*"([A-Za-z0-9_]+)"')),
)


@dataclass(frozen=True)
class Binding:
    kind: str  # "record" | "class" | "field" | "def"
    name: str
    file: str
    line: int


def census(llvm_src: str | pathlib.Path) -> tuple[list[Binding], list[pathlib.Path]]:
    """Every backend binding, plus the files it was found in."""
    root = pathlib.Path(llvm_src)
    bindings: list[Binding] = []
    scanned: list[pathlib.Path] = []
    for rel in BACKEND_SOURCES:
        path = root / rel
        if not path.exists():
            continue
        scanned.append(path)
        for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for kind, pattern in BINDING_PATTERNS:
                for match in pattern.finditer(text):
                    bindings.append(Binding(kind, match.group(1), rel, lineno))
    return bindings, scanned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--llvm-src", default="/home/ubuntu/llvm/trees/explore/llvm")
    ap.add_argument("--json", action="store_true", help="dump the schema")
    ap.add_argument("--field", help="print FIELD of CLASS (CLASS.FIELD)")
    ap.add_argument("--list-classes", action="store_true")
    ap.add_argument(
        "--census",
        action="store_true",
        help="resolve every backend string binding against the schema",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="like --census, but exit non-zero when a binding is undeclared",
    )
    args = ap.parse_args(argv)

    schema = parse_files(default_sources(args.llvm_src))
    if args.json:
        json.dump(schema.to_json(), sys.stdout, indent=2, sort_keys=True)
        print()
    elif args.field:
        if "." not in args.field:
            ap.error("--field expects CLASS.FIELD")
        cls, field = args.field.split(".", 1)
        info = schema.fields_of(cls).get(field)
        if info is None:
            print(f"{args.field}: not declared", file=sys.stderr)
            return 1
        print(f"{args.field}: {info['type']} = {info['init']}")
    elif args.list_classes:
        for name in sorted(schema.classes):
            print(name)
    elif args.census or args.check:
        bindings, scanned = census(args.llvm_src)
        by_kind: dict[str, int] = {}
        undeclared: list[Binding] = []
        for binding in bindings:
            by_kind[binding.kind] = by_kind.get(binding.kind, 0) + 1
            if not schema.resolve(binding):
                undeclared.append(binding)
        if args.census:
            print(f"scanned {len(scanned)} emitter files under {args.llvm_src}")
            print(
                "bindings: "
                + ", ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items()))
                + f", total={len(bindings)}"
            )
            for binding in sorted(
                {b.name: b for b in bindings}.values(), key=lambda b: (b.kind, b.name)
            ):
                where = ", ".join(schema.resolve(binding))
                print(f"  {binding.kind:6s} {binding.name:32s} -> {where}")
            for binding in undeclared:
                print(
                    f"undeclared: {binding.file}:{binding.line}: "
                    f"{binding.kind} {binding.name!r}",
                    file=sys.stderr,
                )
        if undeclared:
            print(
                f"{len(undeclared)} backend bindings have no declaration in the schema",
                file=sys.stderr,
            )
            return 1
        if args.check:
            print(
                f"{len(bindings)} backend bindings, all declared "
                f"({', '.join(f'{k}={v}' for k, v in sorted(by_kind.items()))})"
            )
    else:
        print(
            f"{len(schema.classes)} classes, "
            f"{len({f for c in schema.classes.values() for f in c.fields})} fields, "
            f"{len(schema.defs)} defs, {len(schema.dag_ops)} dag operators"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
