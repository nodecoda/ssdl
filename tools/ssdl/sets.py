#!/usr/bin/env python3
"""Set evaluation for SSDL 1.1.

SSDL 1.1 does not define a universal attribute universe.  A host supplies one
in JSON::

    {
      "order": {"inst": ["ADD", ...], "reg": ["X0", ...]},
      "inst": {"ADD": {"is_pseudo": false, ...}, ...},
      "reg":  {"X0":  {"is_zero": true, ...}, ...}
    }

The evaluator implements the contract in ``2026-11-16-ssdl-v1.1-draft.md`` §5:
typed comparisons, errors for unknown/missing attributes, stable host order,
stable deduplication, spread expansion, and cycle detection.
"""
from __future__ import annotations

import json
from typing import Any

if __package__ in (None, ""):
    from parser import Program, SetDecl  # type: ignore
else:
    from .parser import Program, SetDecl


class SetError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class AttributeSchema:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.kinds = {"inst", "reg"}
        for kind in self.kinds:
            if kind not in data or not isinstance(data[kind], dict):
                raise ValueError(f"attribute schema is missing {kind!r} entities")
        order = data.get("order", {})
        self.orders: dict[str, list[str]] = {}
        for kind in self.kinds:
            names = order.get(kind, list(data[kind]))
            self.orders[kind] = names

    @classmethod
    def load(cls, path: str) -> "AttributeSchema":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def entities(self, kind: str) -> list[str]:
        return self.orders[kind]

    def attrs(self, kind: str, entity: str) -> dict[str, Any]:
        try:
            return self.data[kind][entity]
        except KeyError:
            raise KeyError(f"unknown {kind} entity {entity!r}")


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "string"
    return "invalid"


class SetEvaluator:
    def __init__(self, program: Program, schema: AttributeSchema):
        self.program = program
        self.schema = schema
        self.sets: dict[str, SetDecl] = {
            d.name: d for d in program.decls if isinstance(d, SetDecl)
        }
        self.memo: dict[str, tuple[str, list[str]]] = {}
        self.stack: list[str] = []
        self.errors: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def finish(self) -> None:
        if self.errors:
            raise SetError(self.errors)

    # -- sets ------------------------------------------------------------
    def expand_set(self, name: str, where: str) -> tuple[str, list[str]]:
        if name in self.memo:
            return self.memo[name]
        if name not in self.sets:
            self.error(where, f"unknown set {name!r}")
            return "inst", []
        if name in self.stack:
            chain = " -> ".join(self.stack + [name])
            self.error(where, f"recursive set reference: {chain}")
            return "inst", []
        self.stack.append(name)
        kind, names = self.expand_expr(self.sets[name].expr, f"set {name}")
        self.stack.pop()
        self.memo[name] = (kind, names)
        return kind, names

    # -- expressions -----------------------------------------------------
    def expand_expr(self, expr, where: str) -> tuple[str, list[str]]:
        if expr[0] in ("inst", "reg"):
            kind, cond = expr
            names = [
                entity
                for entity in self.schema.entities(kind)
                if self.eval_cond(cond, entity, kind, where)
            ]
            return kind, _dedupe(names)
        if expr[0] == "set_list":
            kind: str | None = None
            out: list[str] = []
            for item in expr[1]:
                item_kind, item_names = self.expand_item(item, where)
                if kind is None:
                    kind = item_kind
                elif kind != item_kind:
                    self.error(where, f"cannot mix inst and reg in one set (got {kind} and {item_kind})")
                out.extend(item_names)
            if kind is None:
                self.error(where, "cannot infer the entity kind of an empty bare list")
                kind = "inst"
            return kind, _dedupe(out)
        self.error(where, f"unsupported set expression {expr!r}")
        return "inst", []

    def expand_item(self, item, where: str) -> tuple[str, list[str]]:
        tag = item[0]
        if tag == "ident":
            name = item[1]
            if name in self.sets:
                self.error(
                    where,
                    f"{name!r} is a set; write '...{name}' to splice it",
                )
                return "inst", []
            return self.entity_kind(name, where), [name]
        if tag == "spread":
            inner = item[1]
            if inner[0] == "ident" and inner[1] in self.sets:
                return self.expand_set(inner[1], where)
            return self.expand_expr(inner, where)
        if tag == "set_list":
            return self.expand_expr(item, where)
        self.error(where, f"unsupported set item {item!r}")
        return "inst", []

    def entity_kind(self, name: str, where: str) -> str:
        kinds = [k for k in ("inst", "reg") if name in self.schema.data[k]]
        if len(kinds) == 1:
            return kinds[0]
        if not kinds:
            self.error(where, f"unknown instruction or register {name!r}")
        else:
            self.error(where, f"{name!r} is both an instruction and a register; use inst(...) or reg(...)")
        return "inst"

    # -- conditions ------------------------------------------------------
    def eval_cond(self, expr, entity: str, kind: str, where: str) -> bool:
        tag = expr[0]
        if tag == "binop":
            op, left, right = expr[1], expr[2], expr[3]
            if op in ("&&", "||"):
                lv = self.eval_cond(left, entity, kind, where)
                rv = self.eval_cond(right, entity, kind, where)
                return (lv and rv) if op == "&&" else (lv or rv)
            lv, lt = self.eval_atom(left, entity, kind, where, allow_attr=True)
            rv, rt = self.eval_atom(right, entity, kind, where, allow_attr=False)
            if op in ("==", "!="):
                if lt != rt:
                    self.error(
                        where,
                        f"cannot compare {lt} with {rt} for {kind} {entity!r}",
                    )
                    return False
                result = lv == rv
                return result if op == "==" else not result
            if lt != "int" or rt != "int":
                self.error(where, f"operator {op} requires int operands, got {lt} and {rt}")
                return False
            if op == "<":
                return lv < rv
            if op == ">":
                return lv > rv
            if op == "<=":
                return lv <= rv
            return lv >= rv
        if tag == "unop" and expr[1] == "!":
            value = self.eval_cond(expr[2], entity, kind, where)
            return not value
        value, value_type = self.eval_atom(expr, entity, kind, where, allow_attr=True)
        if value_type != "bool":
            self.error(where, f"condition for {kind} {entity!r} is {value_type}, not bool")
            return False
        return value

    def eval_atom(
        self,
        atom,
        entity: str,
        kind: str,
        where: str,
        *,
        allow_attr: bool,
    ) -> tuple[Any, str]:
        tag = atom[0]
        if tag == "int":
            return atom[1], "int"
        if tag == "string":
            return atom[1], "string"
        if tag == "bool":
            return atom[1], "bool"
        if tag == "ident":
            name = atom[1]
            if not allow_attr:
                return name, "string"
            attrs = self.schema.attrs(kind, entity)
            if name not in attrs:
                self.error(
                    where,
                    f"attribute {name!r} is not declared for {kind} {entity!r}",
                )
                return False, "bool"
            value = attrs[name]
            if value is None:
                self.error(where, f"attribute {name!r} has no value for {kind} {entity!r}")
                return False, "bool"
            return value, _type_of(value)
        if tag == "attr":
            ent, name = atom[1], atom[2]
            if ent != entity:
                self.error(
                    where,
                    f"attribute reference {ent}.{name} does not match entity {entity!r}",
                )
            attrs = self.schema.attrs(kind, entity)
            if name not in attrs:
                self.error(
                    where,
                    f"attribute {name!r} is not declared for {kind} {entity!r}",
                )
                return False, "bool"
            value = attrs[name]
            return value, _type_of(value)
        self.error(where, f"unsupported attribute expression {atom!r}")
        return False, "bool"

    # -- convenience -----------------------------------------------------
    def resolve(self, arg, kind: str, where: str) -> list[str]:
        """Resolve an opcode/register argument to entity names."""
        if arg[0] == "ident":
            name = arg[1]
            if name in self.sets:
                set_kind, names = self.expand_set(name, where)
                if set_kind != kind:
                    self.error(where, f"set {name!r} is a {set_kind} set, expected {kind}")
                return names
            if name not in self.schema.data[kind]:
                self.error(where, f"unknown {kind} {name!r}")
                return []
            return [name]
        if arg[0] in ("set_list", "inst", "reg"):
            expr_kind, names = self.expand_expr(arg, where)
            if expr_kind != kind:
                self.error(where, f"expected a {kind} set, got {expr_kind}")
            return names
        self.error(where, f"unsupported {kind} argument {arg!r}")
        return []


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def load_schema(path: str) -> AttributeSchema:
    return AttributeSchema.load(path)
