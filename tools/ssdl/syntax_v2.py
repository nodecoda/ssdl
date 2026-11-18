#!/usr/bin/env python3
"""Surface v2 syntax front end (Phase 2 skeleton).

The semantic IR is unchanged.  This module parses the v2 surface and renders
the equivalent v1 statement text, so the existing elaborator, spelling rules,
runtime lowering and emitter are reused verbatim.  Keeping the transpiler
small is deliberate: the v2 parser is a *front end*, not a second semantic
implementation.

Implemented in this slice:

- ``set instructions|dimension|relation``
- ``feature`` with typed fields, host and bitmask blocks
- ``predicate`` expression form (``sched_code(host [{...}])`` included)
- ``extern fn``
- ``machine both|model|cpu|family|instance|alias``
- machine items with one record-body rule: resource/res_group/write/read/
  write_seq/combo_func/queues/reg_file/retire_unit/bind/advance/map/
  map_itin/alias/variant/read_variant/itineraries
- v1 field separators are rejected with a v2 hint, never accepted
- record bodies are shape-checked against ``field_types.ENTITY_FIELDS``

``predicate sched|select|sti|tii`` and ``predicate prolog`` are implemented.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

if __package__ in (None, ""):
    import field_types  # type: ignore
    import parser  # type: ignore
else:
    from . import field_types, parser


class V2Error(Exception):
    pass


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


@dataclass(frozen=True)
class Field:
    """One record-body field with its source position.

    Iterating a field yields ``(key, value)`` so the existing tuple-based
    helpers keep working, while diagnostics can still point at the line.
    """

    key: str
    value: str
    line: int = 0
    col: int = 0

    def __iter__(self):
        yield self.key
        yield self.value
_PROPS = set(field_types.ENTITY_FIELDS["machine"])


_NORMALISE = (
    (r"\s*\.\s*", "."),
    (r"\[\s+", "["),
    (r"\s+\]", "]"),
    (r"\(\s+", "("),
    (r"\s+\)", ")"),
    (r"\s+,", ","),
    (r",\s*", ", "),
    (r"-\s+(\d)", r"-\1"),
    (r"([A-Za-z0-9_)\]])\s+\(", r"\1("),
)


def _normalise(text: str) -> str:
    """Collapse whitespace in the token stream, but never inside a string.

    The rendered token text is a mix of identifiers and JSON string literals.
    Applying the spacing rules to the whole line would rewrite prose inside a
    ``desc = "..."`` field (``instruction (mips.ccmov)`` became
    ``instruction(mips.ccmov)``), so the rules run per string-free segment.
    """
    parts: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        chunk = "".join(buf)
        buf.clear()
        for pattern, replacement in _NORMALISE:
            chunk = re.sub(pattern, replacement, chunk)
        parts.append(chunk)

    i = 0
    n = len(text)
    while i < n:
        if text[i] == '"':
            flush()
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            parts.append(text[i:j])
            i = j
            continue
        buf.append(text[i])
        i += 1
    flush()
    return "".join(parts)


def _render_tokens(tokens: list[parser.Token]) -> str:
    # ``host [{ ... }]`` is the v2 spelling; the v1 flat text keeps ``[{...}]``.
    kept: list[parser.Token] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.value == "host"
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "CODE"
        ):
            index += 1
            continue
        kept.append(token)
        index += 1
    parts = []
    for token in kept:
        if token.kind == "STRING":
            parts.append(json.dumps(token.value))
        elif token.kind == "CODE":
            parts.append("[{" + str(token.value) + "}]")
        else:
            parts.append(str(token.value))
    return _normalise(" ".join(parts)).strip()


class _Cursor:
    def __init__(self, tokens: list[parser.Token]):
        self.tokens = tokens
        self.index = 0
        self.decl_line = 1

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

    def record_body(self) -> list[Field]:
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
        fields: list[Field] = []
        while not self.at("}"):
            key_token = self.peek()
            key = self.ident()
            self.expect("=")
            value = self.expr({";"})
            self.expect(";")
            fields.append(Field(key, value, key_token.line, key_token.col))
        self.expect("}")
        return fields


def _props(fields: list[tuple[str, str]]) -> str:
    return ", ".join(f"{key} = {value}" for key, value in fields)


def _fields(
    body: list[Field],
    allowed: set[str] | None = None,
    where: str = "",
    types: dict[str, str] | None = None,
) -> dict[str, str]:
    """Validate a record body against the entity's field table.

    ``types`` is the single source of truth when supplied: the allowed set is
    derived from it and every value is shape-checked.  ``allowed`` remains for
    the few generic bodies that do not have an entity kind.
    """
    if types is not None:
        allowed = set(types)
    if allowed is None:
        raise V2Error(f"{where}: internal error: no field table")
    seen: set[str] = set()
    for field in body:
        key = field.key
        if key in seen:
            raise V2Error(f"{where}: repeated field {key!r}")
        if key not in allowed:
            raise V2Error(f"{where}: unknown field {key!r}")
        seen.add(key)
        if types is not None:
            problem = field_types.value_error(types[key], field.value)
            if problem is not None:
                raise V2Error(
                    f"line {field.line}:{field.col}: {where}.{key}: {problem}"
                )
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
            cursor.expect("{")
            while not cursor.at("}"):
                key = cursor.ident()
                cursor.expect("=")
                if key == "projections":
                    cursor.expect("{")
                    while not cursor.at("}"):
                        projection = cursor.ident()
                        cursor.expect("=")
                        values = cursor.list_tokens()
                        cursor.expect(";")
                        props.append((projection, values))
                    cursor.expect("}")
                    continue
                item = cursor.expr({";"})
                cursor.expect(";")
                if key in ("name_format", "units"):
                    props.append((key, item))
                else:
                    raise V2Error(f"set dimension {name}: unknown field {key!r}")
            cursor.expect("}")
        else:
            cursor.accept(";")
        out.append(f"set {name} = {value} : {_props(props)};")
        return
    if kind == "relation":
        cursor.accept(";")
        out.append(f"set {name} : kind = relation = {value};")
        return
    raise V2Error(f"line: unknown set kind {kind!r}")


def _host_block(cursor: _Cursor, where: str) -> str:
    if not cursor.at("host"):
        token = cursor.peek()
        raise V2Error(
            f"line {token.line}:{token.col}: {where} needs host [{ ... }]"
        )
    cursor.next()
    token = cursor.next()
    if token.kind != "CODE":
        raise V2Error(
            f"line {token.line}:{token.col}: {where} needs host [{ ... }], "
            f"got {token.value!r}"
        )
    return "[{" + str(token.value) + "}]"


def _check_host(tokens: list[parser.Token], where: str) -> None:
    for index, token in enumerate(tokens):
        if token.kind == "CODE" and (
            index == 0 or tokens[index - 1].value != "host"
        ):
            raise V2Error(
                f"line {token.line}:{token.col}: {where}: a code block must be "
                "written host [{ ... }]"
            )


def _literal(cursor: _Cursor, where: str) -> str:
    token = cursor.peek()
    if token.kind == "STRING":
        cursor.next()
        return json.dumps(token.value)
    if token.value in ("true", "false"):
        cursor.next()
        return json.dumps(str(token.value))
    if token.kind == "INT":
        cursor.next()
        return json.dumps(str(token.value))
    raise V2Error(
        f"line {token.line}:{token.col}: {where} needs a bool/int/string literal, "
        f"got {token.value!r}"
    )


def _feature(cursor: _Cursor, out: list[str]) -> None:
    cursor.expect("feature")
    kind_token = cursor.peek()
    if (
        kind_token.kind == "IDENT"
        and cursor.index + 1 < len(cursor.tokens)
        and cursor.tokens[cursor.index + 1].value == ":"
    ):
        raise V2Error(
            f"line {kind_token.line}:{kind_token.col}: feature: write the kind "
            "after the keyword, not 'NAME : kind'"
        )
    feature_kind = cursor.ident()
    if feature_kind not in ("plain", "extension", "tune"):
        raise V2Error(
            f"line {kind_token.line}:{kind_token.col}: feature needs a kind right "
            "after the keyword: feature plain|extension|tune NAME { ... }"
        )
    name = cursor.ident()
    if cursor.at(":"):
        token = cursor.peek()
        raise V2Error(
            f"line {token.line}:{token.col}: feature: write the kind after the "
            "keyword, not 'NAME : kind'"
        )
    cursor.expect("{")
    fields: list[tuple[str, str]] = []
    while not cursor.at("}"):
        key = cursor.ident()
        if key in ("bool", "int", "string"):
            field_name = cursor.ident()
            cursor.expect("=")
            value = _literal(cursor, f"feature {name} field {field_name}")
            cursor.expect(";")
            fields.append(("field", json.dumps(field_name)))
            fields.append(("value", value))
        elif key == "host":
            host = _fields(cursor.record_body(), {"field", "value"}, f"feature {name} host")
            cursor.expect(";")
            if "field" not in host or "value" not in host:
                raise V2Error(f"feature {name}: host needs field and value")
            fields.append(("field", host["field"]))
            fields.append(("value", host["value"]))
        elif key == "bitmask":
            if not cursor.at("="):
                token = cursor.peek()
                raise V2Error(
                    f"line {token.line}:{token.col}: bitmask needs '=' before "
                    "the field body: bitmask = { group = 0; bit = 1; }"
                )
            cursor.expect("=")
            mask = _fields(
                cursor.record_body(), {"group", "bit"}, f"feature {name} bitmask"
            )
            cursor.expect(";")
            if "group" not in mask or "bit" not in mask:
                raise V2Error(f"feature {name}: bitmask needs group and bit")
            fields.append(("bitmask", f"{{ group = {mask['group']}, bit = {mask['bit']} }}"))
        elif key == "kind":
            token = cursor.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: feature: kind moved to the header: "
                "feature plain|extension|tune NAME { ... }"
            )
        else:
            cursor.expect("=")
            fields.append((key, cursor.expr({";"})))
            cursor.expect(";")
    cursor.expect("}")
    parts = [("kind", feature_kind)] + fields
    out.append(f"feature {name} : " + ", ".join(f"{k} = {v}" for k, v in parts) + ";")


def _reject_none_of(tokens: list[parser.Token], where: str) -> None:
    """`none_of` is a feature-list combinator, not a schedule predicate."""
    for index, token in enumerate(tokens[:-1]):
        if (
            token.kind == "IDENT"
            and token.value == "none_of"
            and tokens[index + 1].value == "("
        ):
            raise V2Error(
                f"{where}: none_of is only valid in a select `assembler` list; "
                "write all_of(not(P)) for a schedule predicate"
            )


def _predicate(cursor: _Cursor, out: list[str]) -> None:
    cursor.expect("predicate")
    if cursor.peek().value == "prolog":
        cursor.next()
        cursor.expect("=")
        code = _host_block(cursor, "predicate prolog")
        cursor.expect(";")
        out.append(f"predicate_prolog({code});")
        return

    kind_token = cursor.peek()
    pred_kind = cursor.ident()
    if pred_kind not in ("sched", "select", "sti", "tii"):
        raise V2Error(
            f"line {kind_token.line}:{kind_token.col}: predicate needs a kind "
            "right after the keyword: predicate sched|select|sti|tii NAME ..."
        )
    name = cursor.ident()
    params: list[str] = []
    if cursor.at("("):
        cursor.expect("(")
        while not cursor.at(")"):
            param = cursor.ident()
            if cursor.accept(":"):
                param_kind = cursor.ident()
                if param_kind not in ("INT", "BOOL"):
                    raise V2Error(
                        f"predicate {name}: parameter {param} needs INT or BOOL, "
                        f"got {param_kind!r}"
                    )
            params.append(param)
            if not cursor.accept(","):
                break
        cursor.expect(")")
    head = f"predicate {name}"
    if params:
        head += "(" + ", ".join(params) + ")"

    if pred_kind == "select":
        _predicate_select(cursor, out, head)
        return
    if pred_kind == "sti":
        _predicate_sti(cursor, out, name)
        return
    if pred_kind == "tii":
        _predicate_tii(cursor, out, name)
        return

    cursor.expect("=")
    tokens = cursor.collect({";"})
    cursor.expect(";")
    _check_host(tokens, head)
    _reject_none_of(tokens, head)
    out.append(f"{head} = {_render_tokens(tokens)};")


def _predicate_select(cursor: _Cursor, out: list[str], head: str) -> None:
    cursor.expect("{")
    fields: dict[str, str] = {}
    while not cursor.at("}"):
        key = cursor.ident()
        cursor.expect("=")
        if key == "when":
            fields["selector"] = f"cxx_expr({_host_block(cursor, f'{head}.when')})"
            cursor.expect(";")
        elif key == "during":
            raise V2Error(
                f"line {cursor.peek().line}:{cursor.peek().col}: "
                "'during' was renamed to 'assembler' (the assembler-availability "
                "predicate)"
            )
        elif key in ("assembler", "desc", "recompute_per_function"):
            fields[key] = cursor.expr({";"})
            cursor.expect(";")
        else:
            token = cursor.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: {head}: unknown select field {key!r}"
            )
    cursor.expect("}")
    if "selector" not in fields and "assembler" not in fields:
        raise V2Error(f"{head}: select needs when and/or assembler")
    body = " ".join(f"{key} = {value};" for key, value in fields.items())
    out.append(f"{head} {{ {body} }}")


def _predicate_sti(cursor: _Cursor, out: list[str], name: str) -> None:
    allowed = {"default", "overrides", "expand_for_mc", "opcode_mask", "delegates"}
    cursor.expect("{")
    fields: list[tuple[str, str]] = []
    impls: list[tuple[str, str, list[str]]] = []
    while not cursor.at("}"):
        key = cursor.ident()
        if key == "impl":
            impl_name = cursor.ident()
            cursor.expect(":")
            machine = cursor.ident()
            cursor.expect("{")
            lines: list[str] = []
            while not cursor.at("}"):
                if cursor.at("sched_model"):
                    cursor.next()
                    cursor.expect("=")
                    value = cursor.expr({";"})
                    cursor.expect(";")
                    if value != machine:
                        raise V2Error(
                            f"predicate {name}: impl {impl_name} is scoped to "
                            f"{machine!r} but sched_model = {value!r}"
                        )
                    continue
                statement = cursor.collect({";"})
                cursor.expect(";")
                lines.append(_render_tokens(statement) + ";")
            cursor.expect("}")
            impls.append((impl_name, machine, lines))
            continue
        cursor.expect("=")
        if key not in allowed:
            token = cursor.peek()
            raise V2Error(
                f"line {token.line}:{token.col}: predicate {name}: "
                f"unknown sti field {key!r}"
            )
        fields.append((key, cursor.expr({";"})))
        cursor.expect(";")
    cursor.expect("}")
    decl = " ".join(f"{key} = {value};" for key, value in fields)
    out.append(f"sti_decl {name} {{ {decl} }}")
    for impl_name, machine, lines in impls:
        body = " ".join(lines + [f"sched_model = {machine};"])
        out.append(f"sti_function {impl_name} : {name} {{ {body} }}")


def _predicate_tii(cursor: _Cursor, out: list[str], name: str) -> None:
    cursor.expect("{")
    method: str | None = None
    while not cursor.at("switch"):
        key = cursor.peek()
        field = cursor.ident()
        if field != "method":
            raise V2Error(
                f"line {key.line}:{key.col}: predicate {name}: "
                f"unknown tii field {field!r}"
            )
        cursor.expect("=")
        method = cursor.string()
        cursor.expect(";")
    cursor.expect("switch")
    cursor.expect("opcode")
    cursor.expect("{")
    start = cursor.index - 1
    depth = 1
    while depth:
        token = cursor.next()
        if token.kind == "EOF":
            raise V2Error(f"predicate {name}: unterminated switch opcode")
        if token.value == "{":
            depth += 1
        elif token.value == "}":
            depth -= 1
    body = _render_tokens(cursor.tokens[start:cursor.index])
    cursor.expect("}")  # predicate body
    if method is None:
        out.append(f"tii_predicate {name} = switch opcode {body};")
    else:
        method_text = json.dumps(method)
        out.append(
            f"tii_predicate {name} {{ method = {method_text}; "
            f"switch opcode {body} }}"
        )


def _extern(cursor: _Cursor, out: list[str]) -> None:
    tokens = cursor.collect({";"})
    cursor.expect(";")
    values = [str(token.value) for token in tokens]
    if "from" in values:
        index = len(values) - 1 - values[::-1].index("from")
        if index + 1 >= len(tokens) or tokens[index + 1].kind != "STRING":
            raise V2Error(
                "extern: 'from' takes the profile key as a string, e.g. "
                'extern fn cycles(...) -> INT from "cycles";'
            )
        key = tokens[index + 1].value
        out.append(f"{_render_tokens(tokens[:index])} : {key};")
        return
    if "->" in values:
        after_type = values[values.index("->") + 2 :]
        if ":" in after_type:
            raise V2Error(
                "extern: write the profile key after 'from', not ':' "
                '(extern fn cycles(...) -> INT from "cycles";)'
            )
    out.append(_render_tokens(tokens) + ";")


def _dim_index(cursor: _Cursor) -> str:
    if not cursor.at("["):
        return ""
    tokens = cursor.collect({";", "{"})
    return _render_tokens(tokens)


def _machine_item(cursor: _Cursor, out: list[str]) -> None:
    key_token = cursor.peek()
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
        _fields(body, None, f"resource {name}", field_types.ENTITY_FIELDS["resource"])
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
        _fields(body, None, f"res_group {name}", field_types.ENTITY_FIELDS["res_group"])
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
        fields = _fields(
            body, None, f"write_seq {name}", field_types.ENTITY_FIELDS["write_seq"]
        )
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
        fields = _fields(body, None, key, field_types.ENTITY_FIELDS[key])
        resource = fields.get("resource")
        if resource is None:
            raise V2Error(f"{key}: missing resource field")
        out.append(f"{key} : resource = {resource};")
        return
    if key == "reg_file":
        name = cursor.ident()
        body = cursor.record_body()
        cursor.accept(";")
        _fields(body, None, f"reg_file {name}", field_types.ENTITY_FIELDS["reg_file"])
        fields = " ".join(f"{field} = {value};" for field, value in body)
        out.append(f"reg_file {name} {{ {fields} }}")
        return
    if key == "retire_unit":
        if any(line.startswith("retire_unit ") for line in out):
            raise V2Error("retire_unit: at most one per machine")
        body = cursor.record_body()
        cursor.accept(";")
        _fields(
            body, None, "retire_unit", field_types.ENTITY_FIELDS["retire_unit"]
        )
        fields = " ".join(f"{field} = {value};" for field, value in body)
        out.append(f"retire_unit {{ {fields} }}")
        return
    if key == "bind":
        subject = cursor.ident()
        index = _dim_index(cursor)
        body = cursor.record_body()
        cursor.accept(";")
        fields = _fields(
            body, None, f"bind {subject}", field_types.ENTITY_FIELDS["bind"]
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
        _fields(
            body, None, f"advance {subject}", field_types.ENTITY_FIELDS["advance"]
        )
        tail = " : ".join(f"{field} = {value}" for field, value in body)
        suffix = f" : {tail}" if tail else ""
        out.append(f"advance {subject}{index}{suffix};")
        return
    if key == "map":
        insts = cursor.list_tokens({"{", "->"})
        if cursor.accept("->"):
            # Dense shorthand: the operand position is the list position, which
            # is how the v1 surface and ``InstRW`` read.  The block form stays
            # for sparse positions and for ``unsupported``.
            values: list[str] = []
            cursor.expect("[")
            while not cursor.at("]"):
                token = cursor.peek()
                if token.kind != "IDENT":
                    raise V2Error(
                        f"line {token.line}:{token.col}: map values are sched_rw "
                        f"names (a write or a read), got {token.value!r}"
                    )
                at = cursor.peek()
                value = cursor.ident() + _dim_index(cursor)
                if "[" in value:
                    raise V2Error(
                        f"line {at.line}:{at.col}: map: operand {len(values)} names "
                        f"{value!r}, but a map target cannot carry a dimension "
                        "index; write one map per axis member"
                    )
                values.append(value)
                if not cursor.accept(","):
                    break
            cursor.expect("]")
            cursor.accept(";")
            if not values:
                raise V2Error("map: the operand list is empty")
            out.append(f"map {insts} -> [{', '.join(values)}];")
            return
        cursor.expect("{")
        entries = {}
        unsupported = None
        while not cursor.at("}"):
            token = cursor.peek()
            if token.value == "unsupported":
                cursor.next()
                cursor.expect("=")
                unsupported = cursor.expr({";"})
                problem = field_types.value_error(
                    field_types.ENTITY_FIELDS["map"]["unsupported"], unsupported
                )
                if problem is not None:
                    raise V2Error(
                        f"line {token.line}:{token.col}: map.unsupported: {problem}"
                    )
                cursor.expect(";")
                continue
            if token.kind != "INT":
                raise V2Error(
                    f"line {token.line}:{token.col}: map: operand index must be "
                    f"an integer, got {token.value!r}"
                )
            index = cursor.number()
            cursor.expect("=")
            if cursor.peek().kind != "IDENT":
                token = cursor.peek()
                raise V2Error(
                    f"line {token.line}:{token.col}: map values are sched_rw "
                    "names (a write or read), not expressions"
                )
            at = cursor.peek()
            value = cursor.ident() + _dim_index(cursor)
            if "[" in value:
                # The family scanner expands ``name[axis]`` only in statement
                # heads; a map target is a value position, so a dimension index
                # there would reach the flat parser as a stray bracket.  Refuse
                # it here, where the line and column still mean something.
                raise V2Error(
                    f"line {at.line}:{at.col}: map: operand {index} names "
                    f"{value!r}, but a map target cannot carry a dimension "
                    "index; write one map per axis member"
                )
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
        itins = cursor.list_tokens({";", "{", "->"})
        cursor.expect("->")
        rw_token = cursor.peek()
        rw = cursor.list_tokens()
        if "[" in rw[1:-1]:
            raise V2Error(
                f"line {rw_token.line}:{rw_token.col}: map_itin: a target "
                "cannot carry a dimension index; write one map_itin per axis "
                "member"
            )
        cursor.expect(";")
        out.append(f"map_itin {itins} -> {rw};")
        return
    if key == "alias":
        raise V2Error(
            f"line {key_token.line}:{key_token.col}: 'alias' inside a machine "
            "is the SchedAlias relation; write 'sched_alias' (the CPU alias is "
            "'machine alias X = Y;')"
        )
    if key == "sched_alias":
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
            field_token = cursor.peek()
            field = cursor.ident()
            if field == "variadic":
                cursor.expect("=")
                value = cursor.expr({";"})
                problem = field_types.value_error(
                    field_types.ENTITY_FIELDS["variant"]["variadic"], value
                )
                if problem is not None:
                    raise V2Error(
                        f"line {field_token.line}:{field_token.col}: "
                        f"{key} {subject}.variadic: {problem}"
                    )
                lines.append(f"variadic = {value};")
                cursor.expect(";")
            elif field == "case":
                pred_tokens = cursor.collect({":"}, ternary=True)
                _check_host(pred_tokens, f"{key} {subject}")
                _reject_none_of(pred_tokens, f"{key} {subject}")
                pred = _render_tokens(pred_tokens)
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
    if key == "role":
        raise V2Error(
            "line: the machine role is a keyword; write "
            "machine model|cpu|family|instance NAME"
        )
    if key not in _PROPS:
        raise V2Error(
            f"line {key_token.line}:{key_token.col}: unknown machine item {key!r}"
        )
    problem = field_types.value_error(
        field_types.ENTITY_FIELDS["machine"][key], value
    )
    if problem is not None:
        raise V2Error(
            f"line {key_token.line}:{key_token.col}: machine.{key}: {problem}"
        )
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
                itin_token = cursor.peek()
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
                        if "index" in keys:
                            raise V2Error(
                                "stage: 'index' was renamed to 'cycles' "
                                "(the stage's cycle count)"
                            )
                        if "latency" in keys:
                            raise V2Error(
                                "stage: 'latency' was renamed to 'timeinc' "
                                "(the time increment before the next stage)"
                            )
                        _fields(
                            body, None, "stage", field_types.ENTITY_FIELDS["stage"]
                        )
                        stage_fields = dict(body)
                        cycles = stage_fields.get("cycles")
                        units = stage_fields.get("units")
                        if cycles is None or units is None:
                            raise V2Error("stage: cycles and units are required")
                        extra = ""
                        if "timeinc" in stage_fields:
                            extra += f", {stage_fields['timeinc']}"
                            if "kind" in stage_fields:
                                extra += f", {stage_fields['kind']}"
                        elif "kind" in stage_fields:
                            raise V2Error("stage: kind requires timeinc")
                        stages.append(f"stage({cycles}, {units}{extra})")
                        cursor.accept(",")
                    cursor.expect("]")
                    itin_lines.append(f"stages = [{', '.join(stages)}];")
                else:
                    if itin_field == "cycles":
                        raise V2Error(
                            f"line {itin_token.line}:{itin_token.col}: "
                            "itin 'cycles' was renamed to 'operand_cycles' "
                            "(InstrItinData's operand cycles, not stage "
                            "occupancy)"
                        )
                    if itin_field not in field_types.ENTITY_FIELDS["itin"]:
                        raise V2Error(f"itin {name}: unknown field {itin_field!r}")
                    value = cursor.expr({";"})
                    problem = field_types.value_error(
                        field_types.ENTITY_FIELDS["itin"][itin_field], value
                    )
                    if problem is not None:
                        raise V2Error(
                            f"line {itin_token.line}:{itin_token.col}: "
                            f"itin {name}.{itin_field}: {problem}"
                        )
                    emitted = "cycles" if itin_field == "operand_cycles" else itin_field
                    itin_lines.append(f"{emitted} = {value};")
                cursor.expect(";")
            cursor.expect("}")
            cursor.accept(";")
            lines.append(f"itin {name} {{ {' '.join(itin_lines)} }}")
        else:
            field_token = cursor.peek()
            cursor.expect("=")
            if field == "packetizer_namespace":
                prefix.append(
                    f"packetizer_namespace = {json.dumps(cursor.string())}"
                )
                cursor.expect(";")
                continue
            value = cursor.expr({";"})
            cursor.expect(";")
            if field == "sched_model":
                sched_model = value
            elif field in ("func_units", "bypasses"):
                problem = field_types.value_error(
                    field_types.ENTITY_FIELDS["itineraries"][field], value
                )
                if problem is not None:
                    raise V2Error(
                        f"line {field_token.line}:{field_token.col}: "
                        f"itineraries.{field}: {problem}"
                    )
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




class LineMap:
    """Map a generated v1 line back to the v2 source line that produced it."""

    def __init__(
        self,
        v1_to_v2: tuple[int, ...],
        item_lines: dict[int, list[int | None]] | None = None,
    ):
        self.v1_to_v2 = v1_to_v2
        self.item_lines = item_lines or {}

    def map_line(self, line: int) -> int:
        if not self.v1_to_v2:
            return line
        if 1 <= line <= len(self.v1_to_v2):
            return self.v1_to_v2[line - 1]
        return self.v1_to_v2[-1]


class _Out(list):
    """A line list that remembers the v2 declaration line of each line."""

    def __init__(self, cursor: _Cursor):
        super().__init__()
        self.cursor = cursor
        self.lines: list[int] = []
        # v1 declaration line -> v2 line of each machine item, in order.  A
        # machine collapses to one v1 line, so the line map alone cannot point
        # at the item that produced a diagnostic.
        self.item_lines: dict[int, list[int | None]] = {}

    def append(self, text: str) -> None:
        super().append(text)
        self.lines.append(self.cursor.decl_line)


def _transpile(text: str) -> _Out:
    tokens = parser.tokenize(text, floor_div=True)
    cursor = _Cursor(tokens)
    out = _Out(cursor)
    while cursor.peek().kind != "EOF":
        if cursor.accept(";"):
            continue
        token = cursor.peek()
        cursor.decl_line = token.line
        if token.value in ("set",):
            _set(cursor, out)
        elif token.value == "extern":
            _extern(cursor, out)
        elif token.value == "machine":
            cursor.next()
            role = _machine_role(cursor, token)
            if role == "alias":
                name = cursor.ident()
                cursor.expect("=")
                target = cursor.ident()
                cursor.expect(";")
                out.append(f"machine {name} = {target};")
                continue
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
            body_lines: list[int | None] = []
            if role in ("both", "model", "cpu"):
                body.append(f"role = {role};")
                body_lines.append(None)
            while not cursor.at("}"):
                item_token = cursor.peek()
                before = len(body)
                override = cursor.accept("override")
                if override:
                    item_token = cursor.peek()
                _machine_item(cursor, body)
                if override:
                    if len(body) != before + 1:
                        raise V2Error(
                            f"line {item_token.line}:{item_token.col}: "
                            "'override' prefixes one complete item "
                            "(bind/resource/res_group/advance/variant/reg_file/"
                            "write/read)"
                        )
                    body[before] = "override " + body[before]
                body_lines.extend([item_token.line] * (len(body) - before))
                if cursor.at(":") or cursor.at(","):
                    token = cursor.peek()
                    raise V2Error(
                        f"line {token.line}:{token.col}: {token.value!r} is a v1 "
                        "field separator; v2 writes a field body: "
                        "name { field = value; }"
                    )
            cursor.expect("}")
            cursor.accept(";")
            out.item_lines[len(out) + 1] = body_lines
            out.append(f"machine {name}{params}{inherit} {{ {' '.join(body)} }}")
        elif token.value == "itineraries":
            _itineraries(cursor, out)
        elif token.value == "feature":
            _feature(cursor, out)
        elif token.value == "predicate":
            _predicate(cursor, out)
        else:
            raise V2Error(
                f"line {token.line}:{token.col}: unexpected top-level token {token.value!r}"
            )
    return out


def to_v1(text: str) -> str:
    """Transpile v2 surface text to v1 family source."""
    return "\n".join(_transpile(text)) + "\n"


def to_v1_mapped(text: str) -> tuple[str, LineMap]:
    """Like :func:`to_v1`, but also return the v1-to-v2 line map."""
    out = _transpile(text)
    return "\n".join(out) + "\n", LineMap(tuple(out.lines), out.item_lines)


MACHINE_ROLES = ("both", "alias", "family", "instance", "model", "cpu")


def _machine_role(cursor: _Cursor, token: parser.Token) -> str:
    """Read the optional role word; absent means ``both``.

    A role word is only a role when a name follows it.  ``machine both { }``
    therefore names a machine ``both`` instead of turning ``{`` into a name.
    """
    first = cursor.peek()
    if first.kind != "IDENT" or str(first.value) not in MACHINE_ROLES:
        return "both"
    index = cursor.index + 1
    if index >= len(cursor.tokens):
        return "both"
    after = cursor.tokens[index]
    if after.kind != "IDENT":
        return "both"
    cursor.next()
    return str(first.value)


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
