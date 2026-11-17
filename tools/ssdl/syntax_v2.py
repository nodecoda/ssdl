#!/usr/bin/env python3
"""Surface v2 syntax front end (Phase 2 skeleton).

The semantic IR is unchanged.  This module parses the v2 surface and renders
the equivalent v1 statement text, so the existing elaborator, spelling rules,
runtime lowering and emitter are reused verbatim.  Keeping the transpiler
small is deliberate: the v2 parser is a *front end*, not a second semantic
implementation.

Implemented in this slice:

- ``set instructions|dimension|relation``
- ``extern fn``
- ``machine family|instance|alias``
- machine items with one record-body rule: resource/res_group/write/read/
  write_seq/combo_func/queues/reg_file/retire_unit/bind/advance/map/
  map_itin/alias/variant/read_variant/itineraries
- v1 field separators are rejected with a v2 hint, never accepted

Not yet in this slice (Phase 3/4): ``machine model|cpu``, ``predicate``,
``feature``, typed feature fields, ``set dimension { projections = ... }``.
They raise a clear ``V2Error`` instead of silently falling back.
"""
from __future__ import annotations

import json
import re

from . import parser


class V2Error(Exception):
    pass


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_PROPS = {
    "features", "tune_features", "unsupported_features", "default_march",
    "issue_width", "micro_op_buffer_size", "loop_micro_op_buffer",
    "load_latency", "high_latency", "mispredict_penalty",
    "post_ra_scheduler", "complete_model", "full_inst_rw_overlap_check",
    "enable_intervals",
}


def _render_tokens(tokens: list[parser.Token]) -> str:
    parts = []
    for token in tokens:
        if token.kind == "STRING":
            parts.append(json.dumps(token.value))
        else:
            parts.append(str(token.value))
    text = " ".join(parts)
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*", ", ", text)
    text = re.sub(r"-\s+(\d)", r"-\1", text)
    text = re.sub(r"([A-Za-z0-9_)\]])\s+\(", r"\1(", text)
    return text.strip()


class _Cursor:
    def __init__(self, tokens: list[parser.Token]):
        self.tokens = tokens
        self.index = 0

    def peek(self) -> parser.Token:
        return self.tokens[self.index]

    def at(self, value) -> bool:
        return self.peek().value == value

    def next(self) -> parser.Token:
        token = self.peek()
        self.index += 1
        return token

    def accept(self, value) -> bool:
        if self.at(value):
            self.index += 1
            return True
        return False

    def expect(self, value) -> parser.Token:
        token = self.next()
        if token.value != value:
            raise V2Error(
                f"line {token.line}:{token.col}: expected {value!r}, got {token.value!r}"
            )
        return token

    def ident(self) -> str:
        token = self.next()
        if token.kind not in ("IDENT",) or not _IDENT.match(str(token.value)):
            raise V2Error(
                f"line {token.line}:{token.col}: expected identifier, got {token.value!r}"
            )
        return str(token.value)

    def string(self) -> str:
        token = self.next()
        if token.kind != "STRING":
            raise V2Error(
                f"line {token.line}:{token.col}: expected string, got {token.value!r}"
            )
        return token.value

    def number(self) -> int:
        token = self.next()
        if token.kind != "INT":
            raise V2Error(
                f"line {token.line}:{token.col}: expected integer, got {token.value!r}"
            )
        return int(token.value)

    def collect(self, stops: set[str], ternary: bool = False) -> list[parser.Token]:
        start = self.index
        depth = 0
        pending = 0
        while True:
            token = self.peek()
            if token.kind == "EOF":
                break
            value = token.value
            if depth == 0:
                if ternary and value == "?":
                    pending += 1
                elif ternary and value == ":" and pending:
                    pending -= 1
                elif value in stops and pending == 0:
                    break
            if value in ("(", "[", "{"):
                depth += 1
            elif value in (")", "]", "}"):
                depth -= 1
            self.index += 1
        tokens = self.tokens[start:self.index]
        if not tokens:
            token = self.peek()
            raise V2Error(f"line {token.line}:{token.col}: expected expression")
        return tokens

    def expr(self, stops: set[str], ternary: bool = False) -> str:
        return _render_tokens(self.collect(stops, ternary=ternary))

    def list_tokens(self, stops: set[str] | None = None) -> str:
        tokens = self.collect(stops or {";", "{"})
        return _render_tokens(tokens)

    def record_body(self) -> list[tuple[str, str]]:
        if self.at(":") or self.at(","):
            token = self.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: {token.value!r} is a v1 field "
                "separator; v2 has one field-body form: name { field = value; }"
            )
        if not self.at("{"):
            token = self.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: expected '{{' to open a field "
                f"body (name {{ field = value; }}), got {token.value!r}"
            )
        self.next()  # consume '{'
        fields = []
        while not self.at("}"):
            key = self.ident()
            self.expect("=")
            value = self.expr({";"})
            self.expect(";")
            fields.append((key, value))
        self.expect("}")
        return fields


def _props(fields: list[tuple[str, str]]) -> str:
    return ", ".join(f"{key} = {value}" for key, value in fields)


def _fields(
    body: list[tuple[str, str]], allowed: set[str], where: str
) -> dict[str, str]:
    """Validate a record body against the entity's field table."""
    seen: set[str] = set()
    for key, _ in body:
        if key in seen:
            raise V2Error(f"{where}: repeated field {key!r}")
        if key not in allowed:
            raise V2Error(f"{where}: unknown field {key!r}")
        seen.add(key)
    return dict(body)


def _set(cursor: _Cursor, out: list[str]) -> None:
    cursor.expect("set")
    if cursor.peek().value not in ("instructions", "dimension", "relation"):
        token = cursor.peek()
        raise V2Error(
            f"line {token.line}:{token.col}: v2 requires a set kind: "
            "set instructions|dimension|relation NAME = ..."
        )
    kind = cursor.ident()
    name = cursor.ident()
    cursor.expect("=")
    # ':' must stop the value list: it is a v1 property separator, not v2.
    value = cursor.list_tokens({";", "{", ":"})
    if kind == "instructions":
        cursor.accept(";")
        out.append(f"set {name} = {value};")
        return
    if kind == "dimension":
        if cursor.at(":") or cursor.at(","):
            token = cursor.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: {token.value!r} is a v1 set "
                "property separator; v2 writes a body: "
                "set dimension X = [...] { name_format = \"...\"; }"
            )
        props = [("kind", "dimension")]
        if cursor.at("{"):
            body = cursor.record_body()
            for key, item in body:
                if key == "name_format":
                    props.append((key, item))
                elif key == "units":
                    props.append((key, item))
                elif key == "projections":
                    raise V2Error(
                        f"set dimension {name}: projections are the Phase 3 slice"
                    )
                else:
                    raise V2Error(
                        f"set dimension {name}: unknown field {key!r}"
                    )
        else:
            cursor.accept(";")
        out.append(f"set {name} = {value} : {_props(props)};")
        return
    if kind == "relation":
        cursor.accept(";")
        out.append(f"set {name} : kind = relation = {value};")
        return
    raise V2Error(f"line: unknown set kind {kind!r}")


def _extern(cursor: _Cursor, out: list[str]) -> None:
    tokens = cursor.collect({";"})
    cursor.expect(";")
    out.append(_render_tokens(tokens) + ";")


def _dim_index(cursor: _Cursor) -> str:
    if not cursor.at("["):
        return ""
    tokens = cursor.collect({";", "{"})
    return _render_tokens(tokens)


def _machine_item(cursor: _Cursor, out: list[str]) -> None:
    key = cursor.ident()
    if key == "itineraries":
        # back up: _itineraries expects to consume the keyword itself
        cursor.index -= 1
        _itineraries(cursor, out)
        return
    if key == "resource":
        name = cursor.ident()
        index = _dim_index(cursor)
        body = cursor.record_body() if cursor.at("{") else []
        cursor.accept(";")
        _fields(body, {"super", "units", "buffer", "name"}, f"resource {name}")
        suffix = f" : {_props(body)}" if body else ""
        out.append(f"resource {name}{index}{suffix};")
        return
    if key == "res_group":
        name = cursor.ident()
        index = _dim_index(cursor)
        cursor.expect("=")
        members = cursor.list_tokens()
        body = cursor.record_body() if cursor.at("{") else []
        cursor.accept(";")
        _fields(body, {"buffer"}, f"res_group {name}")
        suffix = f" : {_props(body)}" if body else ""
        out.append(f"res_group {name}{index} = {members}{suffix};")
        return
    if key in ("write", "read", "func_unit", "bypass", "itin_class"):
        name = cursor.ident()
        index = _dim_index(cursor)
        if index and key not in ("write", "read"):
            raise V2Error(f"{key} {name}: dim_index is only allowed on write/read")
        cursor.expect(";")
        out.append(f"{key} {name}{index};")
        return
    if key == "write_seq":
        name = cursor.ident()
        index = _dim_index(cursor)
        cursor.expect("=")
        members = cursor.list_tokens()
        body = cursor.record_body() if cursor.at("{") else []
        cursor.accept(";")
        fields = _fields(body, {"repeat"}, f"write_seq {name}")
        repeat = f" repeat = {fields['repeat']}" if "repeat" in fields else ""
        out.append(f"write_seq {name}{index} = {members}{repeat};")
        return
    if key == "combo_func":
        name = cursor.ident()
        cursor.expect("=")
        members = cursor.list_tokens()
        cursor.expect(";")
        out.append(f"combo_func {name} = {members};")
        return
    if key in ("load_queue", "store_queue"):
        body = cursor.record_body()
        cursor.accept(";")
        fields = _fields(body, {"resource"}, key)
        resource = fields.get("resource")
        if resource is None:
            raise V2Error(f"{key}: missing resource field")
        out.append(f"{key} : resource = {resource};")
        return
    if key == "reg_file":
        name = cursor.ident()
        body = cursor.record_body()
        cursor.accept(";")
        _fields(
            body,
            {
                "num_phys", "classes", "costs", "allow_move_elim",
                "max_move_elim_per_cy", "allow_zero_elim_only",
            },
            f"reg_file {name}",
        )
        fields = " ".join(f"{field} = {value};" for field, value in body)
        out.append(f"reg_file {name} {{ {fields} }}")
        return
    if key == "retire_unit":
        body = cursor.record_body()
        cursor.accept(";")
        _fields(body, {"buffer", "max_per_cycle"}, "retire_unit")
        fields = " ".join(f"{field} = {value};" for field, value in body)
        out.append(f"retire_unit {{ {fields} }}")
        return
    if key == "bind":
        subject = cursor.ident()
        index = _dim_index(cursor)
        body = cursor.record_body()
        cursor.accept(";")
        fields = _fields(
            body,
            {
                "resources", "latency", "uops", "release", "acquire",
                "begin_grp", "end_grp", "single", "retire_ooo", "unsupported",
            },
            f"bind {subject}",
        )
        resources = fields.pop("resources", None)
        if resources is None:
            raise V2Error(f"bind {subject}: missing resources field")
        tail = ", ".join(f"{field} = {value}" for field, value in fields.items())
        suffix = f", {tail}" if tail else ""
        out.append(f"bind {subject}{index} -> {resources}{suffix};")
        return
    if key == "advance":
        subject = cursor.ident()
        index = _dim_index(cursor)
        body = cursor.record_body()
        cursor.accept(";")
        _fields(body, {"cycles", "writes", "tunables", "unsupported"}, f"advance {subject}")
        tail = " : ".join(f"{field} = {value}" for field, value in body)
        suffix = f" : {tail}" if tail else ""
        out.append(f"advance {subject}{index}{suffix};")
        return
    if key == "map":
        insts = cursor.list_tokens()
        cursor.expect("{")
        entries = {}
        unsupported = None
        while not cursor.at("}"):
            token = cursor.peek()
            if token.value == "unsupported":
                cursor.next()
                cursor.expect("=")
                unsupported = cursor.expr({";"})
                cursor.expect(";")
                continue
            if token.kind != "INT":
                raise V2Error(
                    f"line {token.line}:{token.col}: map: operand index must be "
                    f"an integer, got {token.value!r}"
                )
            index = cursor.number()
            cursor.expect("=")
            value = cursor.expr({";"})
            cursor.expect(";")
            if index in entries:
                raise V2Error(f"map: duplicate operand index {index}")
            entries[index] = value
        cursor.expect("}")
        cursor.accept(";")
        if sorted(entries) != list(range(len(entries))):
            raise V2Error("map: operand indices must be contiguous from 0")
        rw = ", ".join(entries[i] for i in range(len(entries)))
        suffix = f" : unsupported = {unsupported}" if unsupported is not None else ""
        out.append(f"map {insts} -> [{rw}]{suffix};")
        return
    if key == "map_itin":
        itins = cursor.list_tokens()
        cursor.expect("->")
        rw = cursor.list_tokens()
        cursor.expect(";")
        out.append(f"map_itin {itins} -> {rw};")
        return
    if key == "alias":
        src = cursor.ident()
        cursor.expect("->")
        dst = cursor.ident()
        cursor.expect(";")
        out.append(f"alias {src} -> {dst};")
        return
    if key in ("variant", "read_variant"):
        subject = cursor.ident()
        index = _dim_index(cursor)
        cursor.expect("{")
        lines = []
        while not cursor.at("}"):
            field = cursor.ident()
            if field == "variadic":
                cursor.expect("=")
                lines.append(f"variadic = {cursor.expr({';'})};")
                cursor.expect(";")
            elif field == "case":
                pred = cursor.expr({":"}, ternary=True)
                cursor.expect(":")
                selected = cursor.list_tokens()
                cursor.expect(";")
                lines.append(f"case {pred}: {selected};")
            elif field == "default":
                cursor.expect(":")
                selected = cursor.list_tokens()
                cursor.expect(";")
                lines.append(f"default: {selected};")
            else:
                raise V2Error(f"{key} {subject}: unknown field {field!r}")
        cursor.expect("}")
        cursor.accept(";")
        body = " ".join(lines)
        out.append(f"{key} {subject}{index} {{ {body} }}")
        return
    # machine property
    cursor.expect("=")
    value = cursor.expr({";"})
    cursor.expect(";")
    if key == "model":
        raise V2Error("line: machine cpu/model is Phase 3; use family/instance for now")
    if key not in _PROPS:
        raise V2Error(f"line: unknown machine item {key!r}")
    out.append(f"{key} = {value};")

def _itineraries(cursor: _Cursor, out: list[str]) -> None:
    cursor.expect("itineraries")
    prefix: list[str] = []
    if cursor.accept(":"):
        cursor.expect("packetizer_namespace")
        cursor.expect("=")
        prefix.append(f"packetizer_namespace = {json.dumps(cursor.string())}")
    cursor.expect("{")
    lines: list[str] = []
    sched_model = None
    while not cursor.at("}"):
        field = cursor.ident()
        if field == "itin":
            name = cursor.ident()
            cursor.expect("{")
            itin_lines = []
            while not cursor.at("}"):
                itin_field = cursor.ident()
                cursor.expect("=")
                if itin_field == "stages":
                    cursor.expect("[")
                    stages = []
                    while not cursor.at("]"):
                        cursor.expect("stage")
                        body = cursor.record_body()
                        keys = [key for key, _ in body]
                        duplicated = {key for key in keys if keys.count(key) > 1}
                        if duplicated:
                            raise V2Error(
                                f"stage: repeated field {sorted(duplicated)[0]!r}"
                            )
                        unknown = set(keys) - {"index", "units", "latency", "kind"}
                        if unknown:
                            raise V2Error(
                                f"stage: unknown field {sorted(unknown)[0]!r}"
                            )
                        stage_fields = dict(body)
                        index = stage_fields.get("index")
                        units = stage_fields.get("units")
                        if index is None or units is None:
                            raise V2Error("stage: index and units are required")
                        extra = ""
                        if "latency" in stage_fields:
                            extra += f", {stage_fields['latency']}"
                            if "kind" in stage_fields:
                                extra += f", {stage_fields['kind']}"
                        elif "kind" in stage_fields:
                            raise V2Error("stage: kind requires latency")
                        stages.append(f"stage({index}, {units}{extra})")
                        cursor.accept(",")
                    cursor.expect("]")
                    itin_lines.append(f"stages = [{', '.join(stages)}];")
                else:
                    itin_lines.append(f"{itin_field} = {cursor.expr({';'})};")
                cursor.expect(";")
            cursor.expect("}")
            cursor.accept(";")
            lines.append(f"itin {name} {{ {' '.join(itin_lines)} }}")
        else:
            cursor.expect("=")
            value = cursor.expr({";"})
            cursor.expect(";")
            if field == "sched_model":
                sched_model = value
            elif field in ("func_units", "bypasses"):
                lines.append(f"{field} = {value};")
            else:
                raise V2Error(f"itineraries: unknown field {field!r}")
    cursor.expect("}")
    cursor.accept(";")
    props = list(prefix)
    if sched_model is not None:
        props.insert(0, f"sched_model = {sched_model}")
    head = " : " + " : ".join(props) if props else ""
    out.append(f"itineraries{head} {{ {' '.join(lines)} }}")




def to_v1(text: str) -> str:
    """Transpile v2 surface text to v1 family source."""
    tokens = parser.tokenize(text, floor_div=True)
    cursor = _Cursor(tokens)
    out: list[str] = []
    while cursor.peek().kind != "EOF":
        if cursor.accept(";"):
            continue
        token = cursor.peek()
        if token.value in ("set",):
            _set(cursor, out)
        elif token.value == "extern":
            _extern(cursor, out)
        elif token.value == "machine":
            cursor.next()
            role = cursor.ident()
            if role == "alias":
                name = cursor.ident()
                cursor.expect("=")
                target = cursor.ident()
                cursor.expect(";")
                out.append(f"machine {name} = {target};")
                continue
            if role not in ("family", "instance"):
                raise V2Error(
                    f"line {token.line}: machine {role} is Phase 3; "
                    "use machine family/instance for now"
                )
            name = cursor.ident()
            params = ""
            if role == "family" and cursor.at("("):
                params = _param_list(cursor)
            inherit = ""
            if role == "instance":
                cursor.expect(":")
                family = cursor.ident()
                cursor.expect("(")
                args = cursor.collect({")"})
                cursor.expect(")")
                inherit = f" : {family}({_render_tokens(args)})"
            cursor.expect("{")
            body: list[str] = []
            while not cursor.at("}"):
                _machine_item(cursor, body)
                if cursor.at(":") or cursor.at(","):
                    token = cursor.peek()
                    raise V2Error(
                        f"line {token.line}:{token.col}: {token.value!r} is a v1 "
                        "field separator; v2 writes a field body: "
                        "name { field = value; }"
                    )
            cursor.expect("}")
            cursor.accept(";")
            out.append(f"machine {name}{params}{inherit} {{ {' '.join(body)} }}")
        elif token.value == "itineraries":
            _itineraries(cursor, out)
        elif token.value in ("predicate", "feature"):
            raise V2Error(
                f"line {token.line}: {token.value} is Phase 4; "
                "the Phase 2 front end covers family/instance/entities"
            )
        else:
            raise V2Error(
                f"line {token.line}:{token.col}: unexpected top-level token {token.value!r}"
            )
    return "\n".join(out) + "\n"


def _param_list(cursor: _Cursor) -> str:
    cursor.expect("(")
    params = []
    while not cursor.at(")"):
        name = cursor.ident()
        cursor.expect(":")
        kind = cursor.ident()
        if kind not in ("INT", "BOOL"):
            raise V2Error(f"parameter {name}: expected INT or BOOL, got {kind!r}")
        params.append(f"{name}: {kind}")
        if not cursor.accept(","):
            break
    cursor.expect(")")
    return "(" + ", ".join(params) + ")"
