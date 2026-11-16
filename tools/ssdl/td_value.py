#!/usr/bin/env python3
"""TableGen values as a typed tree.

A record's field value (and a class argument) is a TableGen expression.  The
record layer keeps it as text so the emitter stays a printer, but anything that
consumes the table -- the type checker, and a future host-side record loader --
needs the structure.  This module owns that structure:

    IntValue  1            StringValue  "sifive-7-series"
    BitValue  true/false   CodeValue    [{ ... }]
    IdentValue W_ALU       ListValue    [A, B]
    DagValue  (instrs ADD, SUB)        ApplyValue CheckSameRegOperand<1, 2>
    UnsetValue ?           UnknownValue <could not parse; text preserved>

``parse_value`` and ``render`` are inverses for the expressions the emitter
writes, and ``to_json_node``/``from_json_node`` are the serialised form used by
``record_ir.RecordSet.dumps`` so the JSON table is typed rather than a string
blob.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- Init tree
@dataclass(frozen=True)
class Init:
    """A TableGen value expression."""


@dataclass(frozen=True)
class IntValue(Init):
    value: int


@dataclass(frozen=True)
class BitValue(Init):
    value: bool


@dataclass(frozen=True)
class StringValue(Init):
    value: str


@dataclass(frozen=True)
class CodeValue(Init):
    text: str


@dataclass(frozen=True)
class IdentValue(Init):
    name: str


@dataclass(frozen=True)
class ListValue(Init):
    items: tuple[Init, ...]


@dataclass(frozen=True)
class DagValue(Init):
    op: str
    args: tuple[Init, ...]


@dataclass(frozen=True)
class ApplyValue(Init):
    name: str
    args: tuple[Init, ...]


@dataclass(frozen=True)
class UnsetValue(Init):
    pass


@dataclass(frozen=True)
class UnknownValue(Init):
    text: str


class ValueParseError(Exception):
    pass


class ValueParser:
    def __init__(self, text: str):
        self.text = text
        self.i = 0

    def error(self, message: str) -> ValueError:
        return ValueParseError(f"{message} in {self.text!r} at {self.i}")

    def skip_ws(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def at(self, ch: str) -> bool:
        self.skip_ws()
        return self.text.startswith(ch, self.i)

    def take(self, ch: str) -> None:
        if not self.at(ch):
            raise self.error(f"expected {ch!r}")
        self.i += 1

    def ident(self) -> str:
        self.skip_ws()
        start = self.i
        while self.i < len(self.text) and (
            self.text[self.i].isalnum() or self.text[self.i] == "_"
        ):
            self.i += 1
        if start == self.i:
            raise self.error("expected an identifier")
        return self.text[start:self.i]

    def string(self) -> str:
        self.take('"')
        out: list[str] = []
        while True:
            if self.i >= len(self.text):
                raise self.error("unterminated string")
            ch = self.text[self.i]
            self.i += 1
            if ch == "\\" and self.i < len(self.text):
                esc = self.text[self.i]
                self.i += 1
                out.append({"n": "\n", "t": "\t"}.get(esc, esc))
            elif ch == '"':
                return "".join(out)
            else:
                out.append(ch)

    def code(self) -> str:
        # [{} ... }] with nesting, as in predicates.py
        self.take("[")
        self.take("{")
        depth = 1
        start = self.i
        while self.i < len(self.text):
            if self.text.startswith("[{", self.i):
                depth += 1
                self.i += 2
                continue
            if self.text.startswith("}]", self.i):
                depth -= 1
                self.i += 2
                if depth == 0:
                    return self.text[start:self.i - 2]
                continue
            self.i += 1
        raise self.error("unterminated code block")

    def value(self) -> Init:
        self.skip_ws()
        if self.i >= len(self.text):
            raise self.error("expected a value")
        ch = self.text[self.i]
        if ch == "?":
            self.i += 1
            return UnsetValue()
        if ch == '"':
            return StringValue(self.string())
        if ch == "[":
            if self.text.startswith("[{", self.i):
                return CodeValue(self.code())
            return self.list_value()
        if ch == "(":
            return self.dag()
        if ch.isdigit() or (ch == "-" and self.i + 1 < len(self.text)):
            start = self.i
            self.i += 1
            while self.i < len(self.text) and self.text[self.i].isdigit():
                self.i += 1
            return IntValue(int(self.text[start:self.i]))
        if ch.isalpha() or ch == "_":
            name = self.ident()
            if name == "true":
                return BitValue(True)
            if name == "false":
                return BitValue(False)
            if self.at("<"):
                return ApplyValue(name, self.type_args())
            return IdentValue(name)
        raise self.error(f"unsupported character {ch!r}")

    def list_value(self) -> ListValue:
        self.take("[")
        items: list[Init] = []
        while not self.at("]"):
            items.append(self.value())
            if self.at(","):
                self.i += 1
                continue
            break
        self.take("]")
        return ListValue(tuple(items))

    def dag(self) -> DagValue:
        self.take("(")
        op = self.ident()
        args: list[Init] = []
        while not self.at(")"):
            args.append(self.value())
            if self.at(","):
                self.i += 1
                continue
            break
        self.take(")")
        return DagValue(op, tuple(args))

    def type_args(self) -> tuple[Init, ...]:
        self.take("<")
        args: list[Init] = []
        while not self.at(">"):
            args.append(self.value())
            if self.at(","):
                self.i += 1
                continue
            break
        self.take(">")
        return tuple(args)


def parse_value(text: str) -> Init:
    text = text.strip()
    if not text:
        return UnknownValue(text)
    parser = ValueParser(text)
    try:
        value = parser.value()
    except ValueParseError:
        return UnknownValue(text)
    parser.skip_ws()
    if parser.i != len(parser.text):
        return UnknownValue(text)
    return value


def render(value: Init) -> str:
    if isinstance(value, IntValue):
        return str(value.value)
    if isinstance(value, BitValue):
        return "true" if value.value else "false"
    if isinstance(value, StringValue):
        return json.dumps(value.value)
    if isinstance(value, CodeValue):
        return "[{" + value.text + "}]"
    if isinstance(value, IdentValue):
        return value.name
    if isinstance(value, ListValue):
        return "[" + ", ".join(render(item) for item in value.items) + "]"
    if isinstance(value, DagValue):
        # The operator is not an argument: `(instrs ADD, SUB)`.
        args = "".join(
            (" " if index == 0 else ", ") + render(arg)
            for index, arg in enumerate(value.args)
        )
        return "(" + value.op + args + ")"
    if isinstance(value, ApplyValue):
        return value.name + "<" + ", ".join(render(a) for a in value.args) + ">"
    if isinstance(value, UnsetValue):
        return "?"
    return value.text  # type: ignore[attr-defined]


NODE_KINDS = {
    IntValue: "int",
    BitValue: "bit",
    StringValue: "string",
    CodeValue: "code",
    IdentValue: "ident",
    ListValue: "list",
    DagValue: "dag",
    ApplyValue: "apply",
    UnsetValue: "unset",
    UnknownValue: "unknown",
}


def to_json_node(value: Init) -> dict:
    """Serialise a value as a typed JSON node."""
    kind = NODE_KINDS[type(value)]
    if isinstance(value, IntValue):
        return {"kind": "int", "value": value.value}
    if isinstance(value, BitValue):
        return {"kind": "bit", "value": value.value}
    if isinstance(value, StringValue):
        return {"kind": "string", "value": value.value}
    if isinstance(value, CodeValue):
        return {"kind": "code", "value": value.text}
    if isinstance(value, IdentValue):
        return {"kind": "ident", "name": value.name}
    if isinstance(value, ListValue):
        return {"kind": "list", "items": [to_json_node(item) for item in value.items]}
    if isinstance(value, DagValue):
        return {
            "kind": "dag",
            "op": value.op,
            "args": [to_json_node(arg) for arg in value.args],
        }
    if isinstance(value, ApplyValue):
        return {
            "kind": "apply",
            "class": value.name,
            "args": [to_json_node(arg) for arg in value.args],
        }
    if isinstance(value, UnsetValue):
        return {"kind": "unset"}
    return {"kind": "unknown", "text": value.text}  # type: ignore[attr-defined]


def from_json_node(node: Any) -> Init:
    """Accept a typed node, or a bare string from an older table."""
    if isinstance(node, str):
        return parse_value(node)
    if not isinstance(node, dict):
        return UnknownValue(str(node))
    kind = node.get("kind")
    if kind == "int":
        return IntValue(int(node["value"]))
    if kind == "bit":
        return BitValue(bool(node["value"]))
    if kind == "string":
        return StringValue(str(node["value"]))
    if kind == "code":
        return CodeValue(str(node["value"]))
    if kind == "ident":
        return IdentValue(str(node["name"]))
    if kind == "list":
        return ListValue(tuple(from_json_node(item) for item in node.get("items", ())))
    if kind == "dag":
        return DagValue(str(node["op"]), tuple(from_json_node(a) for a in node.get("args", ())))
    if kind == "apply":
        return ApplyValue(
            str(node["class"]), tuple(from_json_node(a) for a in node.get("args", ()))
        )
    if kind == "unset":
        return UnsetValue()
    return UnknownValue(str(node.get("text", node)))
