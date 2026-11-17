#!/usr/bin/env python3
"""Migrate v1 SSDL sources to the v2 surface syntax.

``to_v2`` is the inverse of :func:`syntax_v2.to_v1`: it reads a v1 source
(plain v1.3, or a family/dimension source as emitted by ``family_mine.py``)
and prints the equivalent v2 source.  Round tripping is checked by
``tools/ssdl/check.py``: ``to_v1(to_v2(text))`` must expand to the same
family text and lower to the same records.

The migrator reuses ``family_expand``'s logical-statement scanner, so it is
layout independent in exactly the same way the family expander is: statements
may be wrapped anywhere, and comments / strings / ``[{ ... }]`` blocks are
opaque.  Conversion is statement-local; no semantic resolution happens here.

Only the syntax moves.  Constructs whose v2 spelling does not exist yet are
rejected with a ``ToV2Error`` that names the construct instead of silently
producing a source that means something else.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    from family_expand import scan_statements  # type: ignore
else:
    from .family_expand import scan_statements


class ToV2Error(Exception):
    pass


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_OPENERS = ("reg_file", "retire_unit", "variant", "read_variant")


def _first_word(text: str) -> str:
    match = IDENT_RE.match(text.strip())
    return match.group(0) if match else ""


def _opens_block(text: str) -> bool:
    return text.strip().endswith("{")


def _take_block(statements: list[str], index: int) -> tuple[str, list[str], int]:
    """Split ``statements[index]`` (a block header) from its flat body.

    The returned body still contains the braces of nested blocks; only the
    brace that closes the header is removed.  Callers that care about nesting
    recurse with :func:`_take_block` on the body.
    """
    head = statements[index].strip()
    if not _opens_block(head):
        raise ToV2Error(f"line does not open a block: {head!r}")
    depth = 0
    body: list[str] = []
    i = index
    while i < len(statements):
        text = statements[i].strip()
        if i > index:
            body.append(text)
        if _opens_block(text):
            depth += 1
        elif text == "}":
            depth -= 1
            if depth == 0:
                return head, body[:-1], i + 1
        i += 1
    raise ToV2Error(f"unterminated block: {head!r}")


def _split_top(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside ``[] () {}``, strings and code blocks."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if text.startswith("[{", i):
            close = text.find("}]", i + 2)
            if close < 0:
                raise ToV2Error("unterminated code block")
            buf.append(text[i : close + 2])
            i = close + 2
            continue
        # The separator check runs first so an opening bracket can itself be
        # the separator (``_find_top(body, "{")`` for a block predicate).
        if depth == 0 and text.startswith(separator, i):
            parts.append("".join(buf))
            buf = []
            i += len(separator)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        buf.append(char)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_top(text: str, separator: str) -> int:
    parts = _split_top(text, separator)
    if len(parts) == 1:
        return -1
    return len(parts[0])


def _hostify(text: str) -> str:
    """Prefix every bare ``[{ ... }]`` block with the v2 ``host`` marker."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("[{", i):
            if not re.search(r"\bhost\s*$", "".join(out)):
                out.append("host ")
            out.append("[{")
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _list_items(text: str) -> list[str]:
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise ToV2Error(f"expected a list, got {text!r}")
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [item.strip() for item in _split_top(inner, ",") if item.strip()]


def _fields(text: str) -> list[tuple[str, str]]:
    """``k = v, k = v`` -> [(k, v), ...]."""
    out = []
    for part in _split_top(text, ","):
        part = part.strip()
        if not part:
            continue
        halves = _split_top(part, "=")
        if len(halves) < 2:
            raise ToV2Error(f"expected 'field = value', got {part!r}")
        out.append((halves[0].strip(), "=".join(halves[1:]).strip()))
    return out


def _prop_fields(text: str) -> list[tuple[str, str]]:
    """Split v1 colon-separated properties, each of which may use commas.

    ``resource P : super = Q : units = 1`` and
    ``resource P : units = 1, buffer = 4`` are both v1 spellings; v2 has one
    field body, so the migrator has to normalise both.
    """
    out: list[tuple[str, str]] = []
    for chunk in _split_top(text, ":"):
        out.extend(_fields(chunk))
    return out


def _set(text: str) -> list[str]:
    body = text.strip()[len("set") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    relation = re.match(r"^(\S+)\s*:\s*kind\s*=\s*relation\s*=\s*(.*)$", body)
    if relation:
        return [f"set relation {relation.group(1)} = {relation.group(2)};"]
    halves = _split_top(body, ":")
    left = halves[0].strip()
    props_text = ":".join(halves[1:]).strip() if len(halves) > 1 else ""
    eq = _find_top(left, "=")
    if eq < 0:
        raise ToV2Error(f"set declaration without '=': {text!r}")
    name = left[:eq].strip()
    value = left[eq + 1 :].strip()
    props = _fields(props_text) if props_text else []
    kind = "dimension" if props else None
    rest = props
    if props and props[0][0] == "kind":
        kind = props[0][1]
        rest = props[1:]
    if kind == "dimension":
        direct = [(k, v) for k, v in rest if k in ("name_format", "units")]
        extra = [(k, v) for k, v in rest if k not in ("name_format", "units")]
        lines = [f"set dimension {name} = {value}"]
        fields = [f"{k} = {v};" for k, v in direct]
        if extra:
            fields.append(
                "projections = { " + " ".join(f"{k} = {v};" for k, v in extra) + " }"
            )
        if fields:
            lines.append("{ " + " ".join(fields) + " }")
        return [" ".join(lines) + ";"]
    if kind is not None:
        raise ToV2Error(f"unknown set kind {kind!r} in {text!r}")
    return [f"set instructions {name} = {value};"]


def _feature(text: str) -> list[str]:
    body = text.strip()[len("feature") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    colon = _find_top(body, ":")
    if colon < 0:
        raise ToV2Error(f"feature without a field list: {text!r}")
    name = body[:colon].strip()
    fields = []
    kind = "plain"
    for key, value in _fields(body[colon + 1 :]):
        if key == "kind":
            kind = value
        elif key == "bitmask" and value.startswith("{"):
            inner = value.strip()[1:-1].strip()
            mask = " ".join(f"{k} = {v};" for k, v in _fields(inner))
            fields.append(f"bitmask = {{ {mask} }};")
        else:
            fields.append(f"{key} = {value};")
    return [f"feature {kind} {name} {{ {' '.join(fields)} }};"]


def _predicate(text: str) -> list[str]:
    body = text.strip()[len("predicate") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    brace = _find_top(body, "{")
    if brace >= 0:
        head = body[:brace].strip()
        inner = body[brace + 1 :].strip()
        if inner.endswith("}"):
            inner = inner[:-1].strip()
        out = []
        for part in _split_top(inner, ";"):
            part = part.strip()
            if not part:
                continue
            key, value = _split_top(part, "=")[:2]
            key = key.strip()
            value = value.strip()
            if key == "selector":
                match = re.match(r"^cxx_expr\s*\((.*)\)$", value, re.S)
                if not match:
                    raise ToV2Error(f"selector must be cxx_expr([{{...}}]): {value!r}")
                out.append(f"when = {_hostify(match.group(1))};")
            elif key == "assembler":
                out.append(f"during = {value};")
            elif key in ("desc", "recompute_per_function"):
                out.append(f"{key} = {value};")
            else:
                raise ToV2Error(f"unknown predicate field {key!r}")
        return [f"predicate select {head} {{ {' '.join(out)} }};"]
    eq = _find_top(body, "=")
    if eq < 0:
        raise ToV2Error(f"predicate without '=': {text!r}")
    name = body[:eq].strip()
    expr = _hostify(body[eq + 1 :].strip())
    return [f"predicate sched {name} = {expr};"]


def _predicate_prolog(text: str) -> list[str]:
    body = text.strip()[len("predicate_prolog") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    match = re.match(r"^\((.*)\)$", body, re.S)
    if not match:
        raise ToV2Error(f"predicate_prolog expects ([{{...}}]): {text!r}")
    return [f"predicate prolog = {_hostify(match.group(1))};"]


def _tii_predicate(text: str) -> list[str]:
    body = text.strip()[len("tii_predicate") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    eq = _find_top(body, "=")
    if eq < 0:
        raise ToV2Error(f"tii_predicate without '=': {text!r}")
    name = body[:eq].strip()
    statement = body[eq + 1 :].strip()
    if not statement.startswith("switch"):
        raise ToV2Error(
            f"tii_predicate {name}: v2 spells the TII hook as a switch; "
            "a bare return has no v2 form yet"
        )
    return [f"predicate tii {name} {{ {statement} }};"]


def _resource(text: str) -> list[str]:
    body = text.strip()[len("resource") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    colon = _find_top(body, ":")
    if colon < 0:
        return [f"resource {body};"]
    name = body[:colon].strip()
    fields = " ".join(f"{k} = {v};" for k, v in _prop_fields(body[colon + 1 :]))
    return [f"resource {name} {{ {fields} }};"]


def _res_group(text: str) -> list[str]:
    body = text.strip()[len("res_group") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    colon = _find_top(body, ":")
    props = ""
    if colon >= 0:
        fields = " ".join(f"{k} = {v};" for k, v in _prop_fields(body[colon + 1 :]))
        props = f" {{ {fields} }}"
        body = body[:colon].strip()
    eq = _find_top(body, "=")
    if eq < 0:
        raise ToV2Error(f"res_group without '=': {text!r}")
    return [f"res_group {body[:eq].strip()} = {body[eq + 1:].strip()}{props};"]


def _write_seq(text: str) -> list[str]:
    body = text.strip()[len("write_seq") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    match = re.match(r"^(.*?)\s+repeat\s*=\s*(\d+)$", body, re.S)
    if not match:
        return [f"write_seq {body};"]
    return [f"write_seq {match.group(1).strip()} {{ repeat = {match.group(2)}; }};"]


def _queue(text: str, keyword: str) -> list[str]:
    body = text.strip()[len(keyword) :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    match = re.match(r"^:\s*resource\s*=\s*(\S+)$", body)
    if not match:
        raise ToV2Error(f"{keyword} expects ': resource = NAME': {text!r}")
    return [f"{keyword} {{ resource = {match.group(1)}; }};"]


def _bind(text: str) -> list[str]:
    body = text.strip()[len("bind") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    arrow = _find_top(body, "->")
    if arrow < 0:
        raise ToV2Error(f"bind without '->': {text!r}")
    subject = body[:arrow].strip()
    rest = body[arrow + 2 :].strip()
    resources, *tail = _split_top(rest, ",")
    lines = [f"resources = {resources.strip()};"]
    for part in tail:
        key, value = _split_top(part, "=")[:2]
        lines.append(f"{key.strip()} = {value.strip()};")
    return [f"bind {subject} {{ {' '.join(lines)} }};"]


def _advance(text: str) -> list[str]:
    body = text.strip()[len("advance") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    parts = _split_top(body, ":")
    subject = parts[0].strip()
    fields = []
    for part in parts[1:]:
        key, value = _split_top(part, "=")[:2]
        fields.append(f"{key.strip()} = {value.strip()};")
    return [f"advance {subject} {{ {' '.join(fields)} }};"]


def _map(text: str) -> list[str]:
    body = text.strip()[len("map") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    parts = _split_top(body, ":")
    mapping = parts[0].strip()
    unsupported = None
    for part in parts[1:]:
        key, value = _split_top(part, "=")[:2]
        if key.strip() != "unsupported":
            raise ToV2Error(f"map: unknown property {key.strip()!r}")
        unsupported = value.strip()
    arrow = _find_top(mapping, "->")
    if arrow < 0:
        raise ToV2Error(f"map without '->': {text!r}")
    insts = mapping[:arrow].strip()
    rw = mapping[arrow + 2 :].strip()
    fields = [f"{index} = {item};" for index, item in enumerate(_list_items(rw))]
    if unsupported is not None:
        fields.append(f"unsupported = {unsupported};")
    return [f"map {insts} {{ {' '.join(fields)} }};"]


def _stage_args(text: str) -> str:
    match = re.match(r"^stage\s*\((.*)\)$", text.strip(), re.S)
    if not match:
        raise ToV2Error(f"expected stage(...), got {text!r}")
    args = [arg.strip() for arg in _split_top(match.group(1), ",") if arg.strip()]
    if len(args) < 2:
        raise ToV2Error(f"stage needs a cycle count and units: {text!r}")
    fields = [f"cycles = {args[0]};", f"units = {args[1]};"]
    if len(args) >= 3:
        fields.append(f"timeinc = {args[2]};")
    if len(args) >= 4:
        fields.append(f"kind = {args[3]};")
    return "stage { " + " ".join(fields) + " }"


def _stages(text: str) -> str:
    body = text.strip()[len("stages") :].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    eq = _find_top(body, "=")
    value = body[eq + 1 :].strip() if eq >= 0 else body
    items = ", ".join(_stage_args(item) for item in _list_items(value))
    return f"stages = [{items}];"


def _convert_itin_fields(body: list[str]) -> list[str]:
    return [_stages(text) if _first_word(text) == "stages" else text for text in body]


def _convert_itin_body(body: list[str], indent: str = "    ") -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(body):
        text = body[i]
        if _opens_block(text):
            head, inner, i = _take_block(body, i)
            if _first_word(head) == "itin":
                out.append(indent + head)
                out.extend(indent + line for line in _convert_itin_fields(inner))
                out.append(indent + "}")
            else:
                out.append(indent + head)
                out.extend(indent + line for line in inner)
                out.append(indent + "}")
            continue
        out.append(indent + text)
        i += 1
    return out


def _itineraries(head: str, body: list[str], indent: str = "") -> list[str]:
    plain = head.strip()
    if not plain.endswith("{"):
        raise ToV2Error(f"unrecognised itineraries header: {head!r}")
    plain = plain[:-1].strip()
    rest = plain[len("itineraries") :].strip()
    keep: list[str] = []
    body = list(body)
    for prop in _split_top(rest, ":") if rest else []:
        prop = prop.strip()
        if not prop:
            continue
        key, value = _split_top(prop, "=")[:2]
        if key.strip() == "sched_model":
            # v2 spells sched_model as a body field, not a ':' property.
            body.insert(0, f"sched_model = {value.strip()};")
        else:
            keep.append(prop)
    out_head = "itineraries" + ("" if not keep else " : " + " : ".join(keep)) + " {"
    lines = [indent + out_head]
    lines.extend(_convert_itin_body(body, indent + "    "))
    lines.append(indent + "}")
    return lines


def _machine_item(text: str) -> list[str]:
    word = _first_word(text)
    if word == "resource":
        return _resource(text)
    if word == "res_group":
        return _res_group(text)
    if word in ("write", "read", "func_unit", "bypass", "itin_class", "combo_func", "alias", "map_itin"):
        return [text]
    if word == "write_seq":
        return _write_seq(text)
    if word in ("load_queue", "store_queue"):
        return _queue(text, word)
    if word == "bind":
        return _bind(text)
    if word == "advance":
        return _advance(text)
    if word == "map":
        return _map(text)
    if word == "variant" or word == "read_variant":
        raise ToV2Error(f"{word} must be a block, got {text!r}")
    if word == "itineraries":
        raise ToV2Error("itineraries must be a block")
    # A machine property.  Everything the v1 parser knows is also a v2 field.
    return [text]


def _convert_machine_body(body: list[str], indent: str = "    ") -> tuple[list[str], str | None]:
    lines: list[str] = []
    role: str | None = None
    i = 0
    while i < len(body):
        text = body[i]
        if _opens_block(text):
            head, inner, i = _take_block(body, i)
            word = _first_word(head)
            if word == "itineraries":
                lines.extend(_itineraries(head, inner, indent))
            elif word in _OPENERS:
                lines.append(indent + head)
                for line in inner:
                    rendered = _hostify(line) if _first_word(line) == "case" else line
                    lines.append(indent + "    " + rendered)
                lines.append(indent + "}")
            else:
                lines.append(indent + head)
                lines.extend(indent + "    " + line for line in inner)
                lines.append(indent + "}")
            continue
        match = re.match(r"^role\s*=\s*(both|model|cpu)\s*;$", text)
        if match:
            role = match.group(1)
            i += 1
            continue
        lines.extend(indent + line for line in _machine_item(text))
        i += 1
    return lines, role


def _machine(head: str, body: list[str]) -> list[str]:
    lines, role = _convert_machine_body(body)
    family = re.match(r"^machine\s+([A-Za-z_][A-Za-z0-9_-]*)\s*\((.*)\)\s*\{$", head, re.S)
    instance = re.match(
        r"^machine\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\((.*)\)\s*\{$",
        head, re.S,
    )
    plain = re.match(r"^machine\s+([A-Za-z_][A-Za-z0-9_-]*)\s*\{$", head)
    if instance:
        out_head = f"machine instance {instance.group(1)} : {instance.group(2)}({instance.group(3)}) {{"
    elif family:
        out_head = f"machine family {family.group(1)}({family.group(2)}) {{"
    elif plain:
        out_head = f"machine {role or 'both'} {plain.group(1)} {{"
    else:
        raise ToV2Error(f"unrecognised machine header: {head!r}")
    return [out_head, *lines, "}"]


def _machine_alias(text: str) -> list[str] | None:
    match = re.match(
        r"^machine\s+([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*([A-Za-z_][A-Za-z0-9_-]*)\s*;$",
        text.strip(),
    )
    if not match:
        return None
    return [f"machine alias {match.group(1)} = {match.group(2)};"]


def _sti_nodes(nodes: list[tuple[str, list[str] | None]]) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, list[str]]]]]:
    """Collect STI declarations and impls so they can be grouped under one predicate."""
    decls: dict[str, list[str]] = {}
    by_decl: dict[str, list[tuple[str, list[str]]]] = {}
    for head, body in nodes:
        if body is None:
            continue
        word = _first_word(head)
        if word == "sti_decl":
            match = re.match(r"^sti_decl\s+([A-Za-z_][A-Za-z0-9_-]*)", head)
            if not match:
                raise ToV2Error(f"unrecognised sti_decl header: {head!r}")
            decls[match.group(1)] = body
        elif word == "sti_function":
            match = re.match(
                r"^sti_function\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*([A-Za-z_][A-Za-z0-9_-]*)",
                head,
            )
            if not match:
                raise ToV2Error(f"unrecognised sti_function header: {head!r}")
            by_decl.setdefault(match.group(2), []).append((match.group(1), body))
    return decls, by_decl


def _emit_sti(name: str, fields: list[str], impls: list[tuple[str, list[str]]]) -> list[str]:
    parts = [field for field in fields]
    for impl_name, body in impls:
        machine = None
        statements = []
        for text in body:
            match = re.match(r"^sched_model\s*=\s*(\S+)\s*;$", text)
            if match:
                machine = match.group(1)
            else:
                statements.append(_hostify(text))
        if machine is None:
            raise ToV2Error(
                f"sti_function {impl_name}: no sched_model, so v2 has no machine "
                "to scope the impl to"
            )
        parts.append(f"impl {impl_name} : {machine} {{ {' '.join(statements)} }}")
    return [f"predicate sti {name} {{ {' '.join(parts)} }};"]


def _balance(text: str) -> int:
    """Brace delta of one statement, ignoring strings and code blocks."""
    delta = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
            i += 1
            continue
        if text.startswith("[{", i):
            close = text.find("}]", i + 2)
            i = n if close < 0 else close + 2
            continue
        if char == "{":
            delta += 1
        elif char == "}":
            delta -= 1
        i += 1
    return delta


def _merge_tii(statements: list[str]) -> list[str]:
    """A ``tii_predicate`` switch is one statement even though it spans braces."""
    out: list[str] = []
    i = 0
    while i < len(statements):
        statement = statements[i]
        if _first_word(statement) == "tii_predicate" and _opens_block(statement):
            parts = [statement]
            depth = _balance(statement)
            i += 1
            while i < len(statements) and depth > 0:
                parts.append(statements[i])
                depth += _balance(statements[i])
                i += 1
            out.append(" ".join(parts))
            continue
        out.append(statement)
        i += 1
    return out


def to_v2(text: str) -> str:
    statements = _merge_tii(
        [statement for _, statement in scan_statements(text) if statement]
    )
    nodes: list[tuple[str, list[str] | None]] = []
    i = 0
    while i < len(statements):
        statement = statements[i]
        if _opens_block(statement):
            head, body, i = _take_block(statements, i)
            nodes.append((head, body))
        else:
            nodes.append((statement, None))
            i += 1

    decls, impls = _sti_nodes(nodes)
    emitted_sti: set[str] = set()
    out: list[str] = []
    for head, body in nodes:
        word = _first_word(head)
        if word == "sti_function":
            continue  # emitted with its predicate
        if word == "sti_decl":
            match = re.match(r"^sti_decl\s+([A-Za-z_][A-Za-z0-9_-]*)", head)
            assert match
            name = match.group(1)
            out.extend(_emit_sti(name, body or [], impls.get(name, [])))
            emitted_sti.add(name)
            continue
        if body is not None:
            if word == "machine":
                out.extend(_machine(head, body))
            elif word == "itineraries":
                out.extend(_itineraries(head, body))
            elif word == "predicate":
                merged = head[:-1].strip() + " { " + " ".join(body) + " }"
                out.extend(_predicate(merged))
            else:
                raise ToV2Error(f"no v2 form for top-level block {head!r}")
            continue
        alias = _machine_alias(head)
        if alias is not None:
            out.extend(alias)
            continue
        if word == "set":
            out.extend(_set(head))
        elif word == "feature":
            out.extend(_feature(head))
        elif word == "predicate":
            out.extend(_predicate(head))
        elif word == "predicate_prolog":
            out.extend(_predicate_prolog(head))
        elif word == "tii_predicate":
            out.extend(_tii_predicate(head))
        elif word == "extern":
            out.append(head)
        elif word == "machine":
            raise ToV2Error(f"machine without a body: {head!r}")
        else:
            raise ToV2Error(f"unhandled top-level statement: {head!r}")

    for name in impls:
        if name not in emitted_sti:
            raise ToV2Error(f"sti_function targets undeclared sti_decl {name!r}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="v1 SSDL source")
    ap.add_argument("-o", "--output", help="write v2 source here (default: stdout)")
    args = ap.parse_args(argv)
    try:
        text = Path(args.input).read_text(encoding="utf-8")
        result = to_v2(text)
    except (OSError, ToV2Error) as exc:
        print(f"{args.input}: {exc}", file=sys.stderr)
        return 2
    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
    else:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
