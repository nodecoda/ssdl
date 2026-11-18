#!/usr/bin/env python3
"""Signatures for the atom predicates.

The v2 grammar has one closed set of atom predicates.  Before this module the
arity and argument kinds were only enforced while lowering, which meant a
typo such as ``is_reg(true)`` survived validation and failed late with a
``lowering:`` location.  The table below is the single description of each
call; ``validate`` uses it directly and the test suite asserts that the
lowering side knows the same names.
"""
from __future__ import annotations

from dataclasses import dataclass

OPERAND = "operand"
INT = "int"
STRING = "string"
IDENT = "ident"
CODE = "code"
INST_SET = "inst_set"
REG_OR_SET = "reg_or_set"


@dataclass(frozen=True)
class Signature:
    positional: tuple[str, ...]
    extras: tuple[str, ...] = ()


#: name -> positional argument kinds (plus allowed ``name =`` extras)
SIGNATURES: dict[str, Signature] = {
    "opcode": Signature((INST_SET,)),
    "pseudo": Signature((INST_SET,)),
    "is_reg": Signature((OPERAND,)),
    "is_vreg": Signature((OPERAND,)),
    "is_not_vreg": Signature((OPERAND,)),
    "is_imm": Signature((OPERAND,)),
    "is_valid_reg": Signature((OPERAND,)),
    "is_invalid_reg": Signature((OPERAND,)),
    "is_zero": Signature((OPERAND,)),
    "reg_is": Signature((OPERAND, REG_OR_SET), ("map",)),
    "imm_is": Signature((OPERAND, INT), ("map",)),
    "imm_is_enum": Signature((OPERAND, STRING), ("map",)),
    "imm_lt": Signature((OPERAND, INT), ("map",)),
    "imm_gt": Signature((OPERAND, INT), ("map",)),
    "imm_le": Signature((OPERAND, INT), ("map",)),
    "imm_ge": Signature((OPERAND, INT), ("map",)),
    "imm_range": Signature((OPERAND, INT, INT), ("map",)),
    "reg_simple": Signature((OPERAND,), ("map",)),
    "imm_simple": Signature((OPERAND,), ("map",)),
    "num_ops": Signature((INT,)),
    "same_reg": Signature((OPERAND, OPERAND)),
    "func": Signature((STRING, STRING)),
    "func_tii": Signature((STRING, STRING), ("tii",)),
    "non_portable": Signature((CODE,)),
}


_KIND_LABEL = {
    OPERAND: "an operand index",
    INT: "an integer",
    STRING: "a string",
    IDENT: "an identifier",
    CODE: "a code block",
    INST_SET: "an instruction set",
    REG_OR_SET: "a register set",
}


def _matches(kind: str, arg) -> bool:
    tag = arg[0]
    if kind == OPERAND:
        if tag == "int":
            return arg[1] >= 0
        return tag == "ident"
    if kind == INT:
        return tag == "int"
    if kind == STRING:
        return tag in ("string", "ident")
    if kind == IDENT:
        return tag == "ident"
    if kind == CODE:
        return tag == "code"
    if kind in (INST_SET, REG_OR_SET):
        return tag in ("ident", "set_list", "call")
    return False


def check_call(name: str, args: list) -> list[str]:
    """Return signature problems for one atom-predicate call."""
    signature = SIGNATURES.get(name)
    if signature is None:
        return []
    problems: list[str] = []
    positional = [arg for arg in args if arg[0] != "named"]
    named = [arg for arg in args if arg[0] == "named"]
    expected = len(signature.positional)
    if len(positional) != expected:
        problems.append(
            f"{name}: expects {expected} positional argument(s), got {len(positional)}"
        )
    for index, (kind, arg) in enumerate(zip(signature.positional, positional)):
        if not _matches(kind, arg):
            if kind == OPERAND and arg[0] == "int":
                problems.append(
                    f"{name}: operand index must be non-negative, got {arg[1]}"
                )
            else:
                problems.append(
                    f"{name}: argument {index} expects {_KIND_LABEL[kind]}, "
                    f"got {arg!r}"
                )
    for arg in named:
        extra = arg[1]
        if extra not in signature.extras:
            problems.append(f"{name}: unexpected named argument {extra!r}")
            continue
        value = arg[2]
        if extra == "map":
            if value[0] not in ("ident", "call", "set_list", "string"):
                problems.append(
                    f"{name}: map= expects a mapper name, got {value!r}"
                )
        elif extra == "tii":
            if value[0] not in ("string", "ident"):
                problems.append(f"{name}: tii= expects a string, got {value!r}")
    return problems
