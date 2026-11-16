#!/usr/bin/env python3
"""A small recursive-descent parser for SSDL 1.0 + the recorded errata.

The parser accepts the document's section 5 example.  It is deliberately
syntax-only: names such as `ORR`, `GPR64`, `read_default` are not resolved
here.  The emitter decides which declarations it can lower.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SSDLParseError(Exception):
    def __init__(self, msg: str, line: int | None = None, col: int | None = None):
        where = f"line {line}" + (f":{col}" if col is not None else "")
        super().__init__(f"{where}: {msg}" if line is not None else msg)


@dataclass
class Token:
    kind: str
    value: Any
    line: int
    col: int


@dataclass
class SetDecl:
    name: str
    expr: Any
    line: int


@dataclass
class PredicateDecl:
    name: str
    params: list[str]
    expr: Any = None
    fields: dict[str, Any] = field(default_factory=dict)
    line: int = 0


@dataclass
class PredicateProlog:
    code: str
    line: int


@dataclass
class FeatureDecl:
    name: str
    fields: dict[str, Any]
    line: int


@dataclass
class STIDecl:
    name: str
    fields: dict[str, Any]
    line: int


@dataclass
class STIFunction:
    name: str
    decl: str
    items: list[dict[str, Any]]
    line: int


@dataclass
class TIIPredicate:
    name: str
    statement: dict[str, Any]
    line: int


@dataclass
class MachineDecl:
    name: str
    alias: str | None
    items: list[dict[str, Any]]
    line: int


@dataclass
class ItinerariesDecl:
    namespace: str | None
    items: list[dict[str, Any]]
    line: int


@dataclass
class Program:
    decls: list[Any]


MULTI_PUNCT = ("...", "..", "->", "==", "!=", "<=", ">=", "&&", "||")
SINGLE_PUNCT = set("{}()[]:;,.=<>!+-*/")


def tokenize(text: str) -> list[Token]:
    toks: list[Token] = []
    i = 0
    line = 1
    col = 1
    n = len(text)

    def advance(k: int) -> None:
        nonlocal i, line, col
        for _ in range(k):
            if i < n and text[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        c = text[i]
        if c in " \t\r\n":
            advance(1)
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                advance(1)
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                advance(1)
            continue
        if text.startswith("/*", i):
            start_line, start_col = line, col
            advance(2)
            while i < n and not text.startswith("*/", i):
                advance(1)
            if i >= n:
                raise SSDLParseError("unterminated /* comment", start_line, start_col)
            advance(2)
            continue
        if text.startswith("[{", i):
            start_line, start_col = line, col
            advance(2)
            body_start = i
            depth = 1
            while i < n:
                if text.startswith("[{", i):
                    depth += 1
                    advance(2)
                elif text.startswith("}]", i):
                    depth -= 1
                    if depth == 0:
                        body = text[body_start:i]
                        advance(2)
                        toks.append(Token("CODE", body, start_line, start_col))
                        break
                    advance(2)
                else:
                    advance(1)
            else:
                raise SSDLParseError("unterminated code block", start_line, start_col)
            continue
        if c == '"':
            start_line, start_col = line, col
            advance(1)
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    advance(1)
                    if i >= n:
                        raise SSDLParseError("unterminated string", start_line, start_col)
                    esc = text[i]
                    buf.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(esc, esc))
                    advance(1)
                else:
                    buf.append(text[i])
                    advance(1)
            if i >= n:
                raise SSDLParseError("unterminated string", start_line, start_col)
            advance(1)
            toks.append(Token("STRING", "".join(buf), start_line, start_col))
            continue
        if c.isdigit():
            start = i
            start_line, start_col = line, col
            while i < n and text[i].isdigit():
                advance(1)
            toks.append(Token("INT", int(text[start:i]), start_line, start_col))
            continue
        if c == "-" and i + 1 < n and text[i + 1].isdigit():
            start_line, start_col = line, col
            advance(1)
            start = i
            while i < n and text[i].isdigit():
                advance(1)
            toks.append(Token("INT", -int(text[start:i]), start_line, start_col))
            continue
        matched = None
        for p in MULTI_PUNCT:
            if text.startswith(p, i):
                matched = p
                break
        if matched:
            start_line, start_col = line, col
            advance(len(matched))
            toks.append(Token("PUNCT", matched, start_line, start_col))
            continue
        if c in SINGLE_PUNCT:
            start_line, start_col = line, col
            advance(1)
            toks.append(Token("PUNCT", c, start_line, start_col))
            continue
        if c.isalpha() or c == "_":
            start_line, start_col = line, col
            start = i
            while i < n:
                ch = text[i]
                if ch.isalnum() or ch == "_":
                    advance(1)
                elif ch == "-" and not text.startswith("->", i):
                    advance(1)
                else:
                    break
            toks.append(Token("IDENT", text[start:i], start_line, start_col))
            continue
        raise SSDLParseError(f"unexpected character {c!r}", line, col)
    toks.append(Token("EOF", None, line, col))
    return toks


class Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.p = 0

    # ---- token helpers -------------------------------------------------
    def peek(self, off: int = 0) -> Token:
        return self.toks[min(self.p + off, len(self.toks) - 1)]

    def at(self, value: Any) -> bool:
        return self.peek().value == value

    def at_kind(self, kind: str) -> bool:
        return self.peek().kind == kind

    def next(self) -> Token:
        tok = self.peek()
        self.p += 1
        return tok

    def accept(self, value: Any) -> bool:
        if self.at(value):
            self.p += 1
            return True
        return False

    def expect(self, value: Any) -> Token:
        tok = self.next()
        if tok.value != value:
            raise SSDLParseError(f"expected {value!r}, got {tok.value!r}", tok.line, tok.col)
        return tok

    def expect_ident(self) -> str:
        tok = self.next()
        if tok.kind != "IDENT":
            raise SSDLParseError(f"expected identifier, got {tok.value!r}", tok.line, tok.col)
        return tok.value

    def expect_int(self) -> int:
        tok = self.next()
        if tok.kind != "INT":
            raise SSDLParseError(f"expected integer, got {tok.value!r}", tok.line, tok.col)
        return tok.value

    def expect_string(self) -> str:
        tok = self.next()
        if tok.kind != "STRING":
            raise SSDLParseError(f"expected string, got {tok.value!r}", tok.line, tok.col)
        return tok.value

    def expect_bool(self) -> bool:
        tok = self.next()
        if tok.value is True or tok.value == "true":
            return True
        if tok.value is False or tok.value == "false":
            return False
        raise SSDLParseError(f"expected true/false, got {tok.value!r}", tok.line, tok.col)

    def expect_code(self) -> str:
        tok = self.next()
        if tok.kind != "CODE":
            raise SSDLParseError(f"expected code block, got {tok.value!r}", tok.line, tok.col)
        return tok.value

    # ---- top level -----------------------------------------------------
    def parse(self) -> Program:
        decls = []
        while not self.at_kind("EOF"):
            tok = self.peek()
            if tok.value == "set":
                decls.append(self.parse_set())
            elif tok.value == "predicate":
                decls.append(self.parse_predicate())
            elif tok.value == "predicate_prolog":
                decls.append(self.parse_predicate_prolog())
            elif tok.value == "feature":
                decls.append(self.parse_feature())
            elif tok.value == "sti_decl":
                decls.append(self.parse_sti_decl())
            elif tok.value == "sti_function":
                decls.append(self.parse_sti_function())
            elif tok.value == "tii_predicate":
                decls.append(self.parse_tii_predicate())
            elif tok.value == "machine":
                decls.append(self.parse_machine())
            elif tok.value == "itineraries":
                decls.append(self.parse_itineraries())
            else:
                raise SSDLParseError(f"unexpected top-level token {tok.value!r}", tok.line, tok.col)
        return Program(decls)

    def parse_set(self) -> SetDecl:
        line = self.expect("set").line
        name = self.expect_ident()
        self.expect("=")
        expr = self.parse_set_expr()
        self.expect(";")
        return SetDecl(name, expr, line)

    def parse_set_expr(self):
        if self.at("inst") or self.at("reg"):
            kind = self.next().value
            self.expect("(")
            cond = self.parse_attr_expr()
            self.expect(")")
            return (kind, cond)
        if self.at("["):
            return self.parse_set_list()
        tok = self.peek()
        raise SSDLParseError(f"expected set expression, got {tok.value!r}", tok.line, tok.col)

    def parse_set_list(self):
        self.expect("[")
        items = []
        while not self.at("]"):
            if self.at("..."):
                self.expect("...")
                if self.at("[") or self.at("inst") or self.at("reg"):
                    items.append(("spread", self.parse_set_expr()))
                else:
                    tok = self.peek()
                    items.append(("spread", ("ident", self.expect_ident())))
            elif self.at("["):
                items.append(self.parse_set_list())
            else:
                tok = self.peek()
                items.append(("ident", self.expect_ident()))
            if not self.accept(","):
                break
        self.expect("]")
        return ("set_list", items)

    # ---- attribute expressions ----------------------------------------
    def parse_attr_expr(self):
        return self.parse_attr_or()

    def parse_attr_or(self):
        node = self.parse_attr_and()
        while self.at("||"):
            self.next()
            node = ("binop", "||", node, self.parse_attr_and())
        return node

    def parse_attr_and(self):
        node = self.parse_attr_not()
        while self.at("&&"):
            self.next()
            node = ("binop", "&&", node, self.parse_attr_not())
        return node

    def parse_attr_not(self):
        if self.at("!"):
            self.next()
            return ("unop", "!", self.parse_attr_not())
        return self.parse_attr_cmp()

    def parse_attr_cmp(self):
        node = self.parse_attr_atom()
        if self.peek().value in ("==", "!=", "<", ">", "<=", ">="):
            op = self.next().value
            node = ("binop", op, node, self.parse_attr_atom())
        return node

    def parse_attr_atom(self):
        tok = self.peek()
        if tok.kind == "INT":
            self.next()
            return ("int", tok.value)
        if tok.kind == "STRING":
            self.next()
            return ("string", tok.value)
        if tok.value == "true" or tok.value == "false":
            self.next()
            return ("bool", tok.value == "true")
        if tok.kind == "IDENT":
            name = self.next().value
            if self.at("."):
                self.next()
                return ("attr", name, self.expect_ident())
            return ("ident", name)
        if self.at("("):
            self.next()
            node = self.parse_attr_expr()
            self.expect(")")
            return node
        raise SSDLParseError(f"expected attribute atom, got {tok.value!r}", tok.line, tok.col)

    # ---- predicate expressions ----------------------------------------
    def parse_predicate_expr(self):
        tok = self.peek()
        if tok.value == "true" or tok.value == "false":
            self.next()
            return ("bool", tok.value == "true")
        if tok.value == "not":
            self.next()
            self.expect("(")
            node = self.parse_predicate_expr()
            self.expect(")")
            return ("not", node)
        if tok.value in ("all_of", "any_of"):
            self.next()
            self.expect("(")
            args = []
            while not self.at(")"):
                args.append(self.parse_predicate_expr())
                if not self.accept(","):
                    break
            self.expect(")")
            return ("call", tok.value, args)
        if tok.value == "feature":
            self.next()
            self.expect("(")
            name = self.expect_ident()
            self.expect(")")
            return ("call", "feature", [("ident", name)])
        if tok.value == "(":
            self.next()
            node = self.parse_predicate_expr()
            self.expect(")")
            return node
        if tok.kind == "IDENT":
            name = self.next().value
            if self.at("("):
                self.next()
                args = self.parse_call_args()
                self.expect(")")
                return ("call", name, args)
            return ("ident", name)
        raise SSDLParseError(f"expected predicate expression, got {tok.value!r}", tok.line, tok.col)

    def parse_call_args(self):
        args = []
        while not self.at(")"):
            tok = self.peek()
            if tok.kind == "INT":
                args.append(("int", self.next().value))
            elif tok.kind == "STRING":
                args.append(("string", self.next().value))
            elif tok.kind == "CODE":
                args.append(("code", self.next().value))
            elif tok.value in ("true", "false"):
                args.append(("bool", self.next().value == "true"))
            elif tok.value in ("inst", "reg") or tok.value == "[":
                args.append(self.parse_set_expr())
            elif tok.kind == "IDENT" and self.peek(1).value == "=":
                key = self.next().value
                self.expect("=")
                if self.peek().kind == "STRING":
                    val = ("string", self.next().value)
                else:
                    val = ("ident", self.expect_ident())
                args.append(("named", key, val))
            elif tok.kind == "IDENT" and self.peek(1).value == "(":
                # nested predicate call, e.g. all_of(...)
                args.append(self.parse_predicate_expr())
            else:
                args.append(self.parse_predicate_expr())
            if not self.accept(","):
                break
        return args

    def parse_feature_expr(self):
        tok = self.peek()
        if tok.value in ("all_of", "any_of", "none_of"):
            self.next()
            self.expect("(")
            ids = self.parse_ident_list()
            self.expect(")")
            return ("call", tok.value, [("list", ids)])
        if tok.value == "not":
            self.next()
            self.expect("(")
            node = self.parse_feature_expr()
            self.expect(")")
            return ("not", node)
        raise SSDLParseError(f"expected feature expression, got {tok.value!r}", tok.line, tok.col)

    # ---- generic lists -------------------------------------------------
    def parse_ident_list(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            out.append(self.expect_ident())
            if not self.accept(","):
                break
        self.expect("]")
        return out

    def parse_int_list(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            out.append(self.expect_int())
            if not self.accept(","):
                break
        self.expect("]")
        return out

    def parse_bit_list(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            out.append(self.expect_bool())
            if not self.accept(","):
                break
        self.expect("]")
        return out

    def parse_int_set(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            if self.at("..."):
                self.next()
                out.append(("spread", self.expect_ident()))
            else:
                start = self.expect_int()
                if self.at(".."):
                    self.next()
                    out.append(("range", start, self.expect_int()))
                else:
                    out.append(("int", start))
            if not self.accept(","):
                break
        self.expect("]")
        return ("int_set", out)

    def parse_inst_set(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            if self.at("regex"):
                self.next()
                self.expect("(")
                pat = self.expect_string()
                self.expect(")")
                out.append(("regex", pat))
            else:
                out.append(("ident", self.expect_ident()))
            if not self.accept(","):
                break
        self.expect("]")
        return ("inst_set", out)

    def parse_bool_or_ident(self):
        tok = self.peek()
        if tok.value in ("true", "false"):
            self.next()
            return ("bool", tok.value == "true")
        return ("ident", self.expect_ident())

    # ---- predicate declarations ---------------------------------------
    def parse_predicate(self) -> PredicateDecl:
        line = self.expect("predicate").line
        name = self.expect_ident()
        params: list[str] = []
        if self.at("("):
            self.next()
            while not self.at(")"):
                params.append(self.expect_ident())
                if not self.accept(","):
                    break
            self.expect(")")
        if self.at("="):
            self.next()
            expr = self.parse_predicate_expr()
            self.expect(";")
            return PredicateDecl(name, params, expr=expr, line=line)
        self.expect("{")
        fields: dict[str, Any] = {}
        while not self.at("}"):
            key = self.expect_ident()
            self.expect("=")
            if key == "selector":
                self.expect("cxx_expr")
                self.expect("(")
                fields[key] = self.expect_code()
                self.expect(")")
            elif key == "assembler":
                fields[key] = self.parse_feature_expr()
            elif key == "desc":
                fields[key] = self.expect_string()
            elif key == "recompute_per_function":
                fields[key] = self.expect_bool()
            else:
                fields[key] = self.parse_feature_expr()
            self.expect(";")
        self.expect("}")
        self.accept(";")
        return PredicateDecl(name, params, fields=fields, line=line)

    def parse_predicate_prolog(self) -> PredicateProlog:
        line = self.expect("predicate_prolog").line
        self.expect("(")
        code = self.expect_code()
        self.expect(")")
        self.expect(";")
        return PredicateProlog(code, line)

    # ---- feature -------------------------------------------------------
    def parse_feature(self) -> FeatureDecl:
        line = self.expect("feature").line
        name = self.expect_ident()
        self.expect(":")
        fields: dict[str, Any] = {}
        while not self.at(";"):
            if self.accept(","):
                continue
            key = self.expect_ident()
            self.expect("=")
            if key == "kind" or key == "inline_behavior":
                fields[key] = self.expect_ident()
            elif key == "implies":
                fields[key] = self.parse_ident_list()
            elif key == "bitmask":
                self.expect("{")
                self.expect("group")
                self.expect("=")
                group = self.expect_int()
                self.expect(",")
                self.expect("bit")
                self.expect("=")
                bit = self.expect_int()
                self.expect("}")
                fields[key] = (group, bit)
            elif key == "experimental":
                fields[key] = self.expect_bool()
            elif key in ("version", "field", "value", "desc", "positive_directive", "negative_directive", "name", "mattr"):
                fields[key] = self.expect_string()
            else:
                # be permissive for future fields
                tok = self.peek()
                if tok.kind == "STRING":
                    fields[key] = self.expect_string()
                elif tok.kind == "INT":
                    fields[key] = self.expect_int()
                elif tok.value in ("true", "false"):
                    fields[key] = self.expect_bool()
                else:
                    fields[key] = self.expect_ident()
        self.expect(";")
        return FeatureDecl(name, fields, line)

    # ---- STI -----------------------------------------------------------
    def parse_sti_decl(self) -> STIDecl:
        line = self.expect("sti_decl").line
        name = self.expect_ident()
        if self.accept(";"):
            return STIDecl(name, {}, line)
        self.expect("{")
        fields: dict[str, Any] = {}
        while not self.at("}"):
            key = self.expect_ident()
            self.expect("=")
            if key in ("overrides", "expand_for_mc", "opcode_mask"):
                fields[key] = self.expect_bool()
            elif key == "delegates":
                fields[key] = self.parse_ident_list()
            elif key == "default":
                fields[key] = self.parse_predicate_expr()
            else:
                fields[key] = self.parse_predicate_expr()
            self.expect(";")
        self.expect("}")
        return STIDecl(name, fields, line)

    def parse_sti_function(self) -> STIFunction:
        line = self.expect("sti_function").line
        name = self.expect_ident()
        self.expect(":")
        decl = self.expect_ident()
        self.expect("{")
        items = []
        while not self.at("}"):
            key = self.expect_ident()
            if key in ("equiv_class", "dep_breaking_class"):
                cls_name = self.expect_ident()
                self.expect("(")
                if self.at("[") or self.at("inst") or self.at("reg"):
                    first = self.parse_set_expr()
                else:
                    first = ("ident", self.expect_ident())
                self.expect(",")
                pred = self.parse_predicate_expr()
                third = None
                if self.accept(","):
                    third = self.parse_int_set()
                self.expect(")")
                self.expect(";")
                items.append({"kind": key, "name": cls_name, "opcodes": first, "pred": pred, "operands": third})
            else:
                self.expect("=")
                if key == "delegates":
                    items.append({"kind": "delegates", "value": self.parse_ident_list()})
                elif key == "sched_model":
                    items.append({"kind": "sched_model", "value": self.expect_ident()})
                else:
                    items.append({"kind": key, "value": self.parse_predicate_expr()})
                self.expect(";")
        self.expect("}")
        # Errata S6: the section 5 example omits the trailing ``;`` after the
        # closing brace even though the EBNF in 3.9 requires it.
        self.accept(";")
        return STIFunction(name, decl, items, line)

    # ---- TII -----------------------------------------------------------
    def parse_tii_predicate(self) -> TIIPredicate:
        line = self.expect("tii_predicate").line
        name = self.expect_ident()
        self.expect("=")
        stmt = self.parse_statement()
        self.expect(";")
        return TIIPredicate(name, stmt, line)

    def parse_statement(self):
        if self.at("return"):
            self.next()
            expr = self.parse_predicate_expr()
            self.expect(";")
            return {"kind": "return", "expr": expr}
        if self.at("switch"):
            self.next()
            self.expect("opcode")
            self.expect("{")
            cases = []
            while self.at("case"):
                self.next()
                insts = self.parse_inst_set()
                self.expect(":")
                body = self.parse_statement()
                cases.append({"insts": insts, "body": body})
            self.expect("default")
            self.expect(":")
            default = self.parse_statement()
            self.expect("}")
            return {"kind": "switch", "cases": cases, "default": default}
        tok = self.peek()
        raise SSDLParseError(f"expected statement, got {tok.value!r}", tok.line, tok.col)

    # ---- machine -------------------------------------------------------
    def parse_machine(self) -> MachineDecl:
        line = self.expect("machine").line
        name = self.expect_ident()
        if self.at("="):
            self.next()
            alias = self.expect_ident()
            self.expect(";")
            return MachineDecl(name, alias, [], line)
        self.expect("{")
        items = []
        while not self.at("}"):
            items.append(self.parse_machine_item())
        self.expect("}")
        self.accept(";")
        return MachineDecl(name, None, items, line)

    def parse_machine_item(self):
        tok = self.peek()
        key = tok.value
        if key == "itineraries":
            return {"kind": "itineraries", "decl": self.parse_itineraries()}
        if key == "resource":
            self.next()
            name = self.expect_ident()
            props = self.parse_colon_props()
            self.expect(";")
            return {"kind": "resource", "name": name, "props": props}
        if key == "res_group":
            self.next()
            name = self.expect_ident()
            self.expect("=")
            resources = self.parse_ident_list()
            props = self.parse_colon_props()
            self.expect(";")
            return {"kind": "res_group", "name": name, "resources": resources, "props": props}
        if key in ("write", "read", "func_unit", "bypass", "itin_class"):
            self.next()
            name = self.expect_ident()
            self.expect(";")
            return {"kind": key, "name": name}
        if key == "write_seq":
            self.next()
            name = self.expect_ident()
            self.expect("=")
            writes = self.parse_ident_list()
            repeat = None
            if self.at("repeat"):
                self.next()
                self.expect("=")
                repeat = self.expect_int()
            self.expect(";")
            return {"kind": "write_seq", "name": name, "writes": writes, "repeat": repeat}
        if key == "combo_func":
            self.next()
            name = self.expect_ident()
            self.expect("=")
            funcs = self.parse_ident_list()
            self.expect(";")
            return {"kind": "combo_func", "name": name, "funcs": funcs}
        if key in ("load_queue", "store_queue"):
            self.next()
            self.expect(":")
            self.expect("resource")
            self.expect("=")
            res = self.expect_ident()
            self.expect(";")
            return {"kind": key, "resource": res}
        if key == "reg_file":
            self.next()
            name = self.expect_ident()
            self.expect("{")
            fields = {}
            while not self.at("}"):
                f = self.expect_ident()
                self.expect("=")
                if f in ("num_phys", "max_move_elim_per_cy"):
                    fields[f] = self.expect_int()
                elif f == "classes":
                    fields[f] = self.parse_ident_list()
                elif f == "costs":
                    fields[f] = self.parse_int_list()
                elif f == "allow_move_elim":
                    fields[f] = self.parse_bit_list()
                elif f == "allow_zero_elim_only":
                    fields[f] = self.expect_bool()
                else:
                    fields[f] = self.parse_bool_or_ident()
                self.expect(";")
            self.expect("}")
            return {"kind": "reg_file", "name": name, "fields": fields}
        if key == "retire_unit":
            self.next()
            self.expect("{")
            fields = {}
            while not self.at("}"):
                f = self.expect_ident()
                self.expect("=")
                fields[f] = self.expect_int()
                self.expect(";")
            self.expect("}")
            return {"kind": "retire_unit", "fields": fields}
        if key == "bind":
            self.next()
            write = self.expect_ident()
            self.expect("->")
            resources = self.parse_ident_list()
            props = {}
            while self.accept(","):
                p = self.expect_ident()
                self.expect("=")
                if p in ("latency", "uops"):
                    props[p] = self.expect_int()
                elif p in ("release", "acquire"):
                    props[p] = self.parse_int_list()
                elif p in ("begin_grp", "end_grp", "single", "retire_ooo", "unsupported"):
                    props[p] = self.expect_bool()
                else:
                    props[p] = self.parse_bool_or_ident()
            self.expect(";")
            return {"kind": "bind", "write": write, "resources": resources, "props": props}
        if key == "advance":
            self.next()
            read = self.expect_ident()
            props = self.parse_colon_props()
            self.expect(";")
            return {"kind": "advance", "read": read, "props": props}
        if key == "map":
            self.next()
            insts = self.parse_inst_set()
            self.expect("->")
            rw = self.parse_ident_list()
            unsupported = None
            if self.accept(":"):
                p = self.expect_ident()
                self.expect("=")
                if p == "unsupported":
                    unsupported = self.expect_bool()
                else:
                    raise SSDLParseError(f"unknown map property {p!r}", tok.line, tok.col)
            self.expect(";")
            return {"kind": "map", "insts": insts, "rw": rw, "unsupported": unsupported}
        if key == "map_itin":
            self.next()
            itins = self.parse_ident_list()
            self.expect("->")
            rw = self.parse_ident_list()
            self.expect(";")
            return {"kind": "map_itin", "itins": itins, "rw": rw}
        if key == "alias":
            self.next()
            src = self.expect_ident()
            self.expect("->")
            dst = self.expect_ident()
            self.expect(";")
            return {"kind": "alias", "src": src, "dst": dst}
        if key in ("variant", "read_variant"):
            self.next()
            name = self.expect_ident()
            self.expect("{")
            variadic = None
            cases = []
            default = None
            while not self.at("}"):
                if self.at("variadic"):
                    self.next()
                    self.expect("=")
                    variadic = self.expect_bool()
                    self.expect(";")
                elif self.at("case"):
                    self.next()
                    pred = self.parse_predicate_expr()
                    self.expect(":")
                    selected = self.parse_ident_list()
                    self.expect(";")
                    cases.append({"pred": pred, "selected": selected})
                elif self.at("default"):
                    self.next()
                    self.expect(":")
                    default = self.parse_ident_list()
                    self.expect(";")
                else:
                    t = self.peek()
                    raise SSDLParseError(f"unexpected variant item {t.value!r}", t.line, t.col)
            self.expect("}")
            return {"kind": key, "name": name, "variadic": variadic, "cases": cases, "default": default}
        # machine property
        self.next()
        self.expect("=")
        if key == "default_march":
            val = self.expect_string()
        elif key in ("features", "tune_features", "unsupported_features"):
            val = self.parse_ident_list()
        elif key in ("post_ra_scheduler", "complete_model", "full_inst_rw_overlap_check", "enable_intervals"):
            val = self.expect_bool()
        else:
            val = self.expect_int()
        self.expect(";")
        return {"kind": "prop", "name": key, "value": val}

    def parse_colon_props(self):
        props = {}
        while self.at(":") or self.at(","):
            self.next()
            p = self.expect_ident()
            self.expect("=")
            if p == "super":
                props[p] = self.expect_ident()
            elif p in ("units", "buffer", "cycles"):
                props[p] = self.expect_int()
            elif p in ("writes", "tunables"):
                if p == "writes":
                    props[p] = self.parse_ident_list()
                else:
                    props[p] = self.parse_int_list()
            elif p == "unsupported":
                props[p] = self.expect_bool()
            else:
                props[p] = self.parse_bool_or_ident()
        return props

    # ---- itineraries ---------------------------------------------------
    def parse_itineraries(self) -> ItinerariesDecl:
        line = self.expect("itineraries").line
        namespace = None
        if self.accept(":"):
            self.expect("packetizer_namespace")
            self.expect("=")
            namespace = self.expect_string()
        self.expect("{")
        items = []
        while not self.at("}"):
            key = self.expect_ident()
            if key in ("func_units", "bypasses"):
                self.expect("=")
                items.append({"kind": key, "value": self.parse_ident_list()})
                self.expect(";")
            elif key == "itin":
                name = self.expect_ident()
                self.expect("{")
                fields = {}
                while not self.at("}"):
                    f = self.expect_ident()
                    self.expect("=")
                    if f == "stages":
                        fields[f] = self.parse_stages()
                    elif f == "cycles":
                        fields[f] = self.parse_int_list()
                    elif f == "bypasses":
                        fields[f] = self.parse_ident_list()
                    elif f == "uops":
                        fields[f] = self.expect_int()
                    else:
                        fields[f] = self.parse_bool_or_ident()
                    self.expect(";")
                self.expect("}")
                items.append({"kind": "itin", "name": name, "fields": fields})
            else:
                t = self.peek()
                raise SSDLParseError(f"unexpected itineraries item {key!r}", t.line, t.col)
        self.expect("}")
        return ItinerariesDecl(namespace, items, line)

    def parse_stages(self):
        self.expect("[")
        out = []
        while not self.at("]"):
            self.expect("stage")
            self.expect("(")
            cycles = self.expect_int()
            self.expect(",")
            units = self.parse_ident_list()
            timeinc = None
            kind = None
            if self.accept(","):
                if self.peek().kind == "INT":
                    timeinc = self.expect_int()
                    if self.accept(","):
                        kind = self.expect_ident()
                else:
                    kind = self.expect_ident()
            self.expect(")")
            out.append({"cycles": cycles, "units": units, "timeinc": timeinc, "kind": kind})
            if not self.accept(","):
                break
        self.expect("]")
        return ("stages", out)


def parse(text: str) -> Program:
    return Parser(text).parse()


def parse_file(path: str) -> Program:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())
