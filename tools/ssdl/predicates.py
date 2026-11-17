#!/usr/bin/env python3
"""Lower SSDL predicates, variants, STI predicates and TII predicates to TD.

The lowering follows ``2026-11-16-ssdl-v1.1-draft.md`` §4:

  * MC expressions become ``MCInstPredicate`` subclasses;
  * SCHED expressions become ``SchedPredicateBase`` subclasses, with MC
    children wrapped in ``MCSchedPredicate``;
  * SEL (block-form) predicates become ``Predicate<...>`` and optionally
    ``AssemblerPredicate<...>``.

Parameterized predicates are macros: calls are substituted inline and no
record is emitted for the declaration itself.  Parameterless predicates get a
named record and are referenced by name.

Set arguments (``opcode(SomeSet)``, ``reg_is(i, ZeroRegs)``) need the host
attribute schema; see ``sets.py``.  Inline instruction/register lists work
without a schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

if __package__ in (None, ""):
    from parser import (  # type: ignore
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        SetDecl,
        STIDecl,
        STIFunction,
        TIIPredicate,
    )
    from record_ir import Base, DuplicateRecord, Record, RecordSet  # type: ignore
    from sets import SetError, SetEvaluator
    from tdutil import join_list, td_code, td_def_name, td_str
else:
    from .parser import (
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        SetDecl,
        STIDecl,
        STIFunction,
        TIIPredicate,
    )
    from .record_ir import Base, DuplicateRecord, Record, RecordSet
    from .sets import SetError, SetEvaluator
    from .tdutil import join_list, td_code, td_def_name, td_str


class PredicateError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


MC = "mc"
SCHED = "sched"
SEL = "sel"

ATOM_PREDICATES = {
    "opcode",
    "pseudo",
    "is_reg",
    "is_vreg",
    "is_not_vreg",
    "is_imm",
    "is_valid_reg",
    "is_invalid_reg",
    "is_zero",
    "reg_is",
    "imm_is",
    "imm_is_enum",
    "imm_lt",
    "imm_gt",
    "imm_le",
    "imm_ge",
    "imm_range",
    "reg_simple",
    "imm_simple",
    "num_ops",
    "same_reg",
}

COMBINERS = {"all_of", "any_of"}


def _bool(value: bool) -> str:
    return "true" if value else "false"


# Three STI hooks are fixed virtuals on `TargetSubtargetInfo` and already have
# declaration records in `llvm/Target/TargetInstrPredicate.td:425-437`.  A DSL
# `sti_decl` for one of these names selects the hook; it must not declare a
# second record, or -gen-subtarget reports a duplicate predicate name (and can
# recurse through the delegation graph).
BUILTIN_STI_DECLS = {
    "isZeroIdiom": "IsZeroIdiomDecl",
    "isDependencyBreaking": "IsDepBreakingDecl",
    "isOptimizableRegisterMove": "IsOptimizableRegisterMoveDecl",
}

# Fixed fields of the same declarations.  `TargetInstrPredicate.td:426-435`
# scopes `UpdatesOpcodeMask = true` over the first two only, so
# `isOptimizableRegisterMove` must not request an opcode mask.
BUILTIN_STI_FIXED = {
    "isZeroIdiom": {"opcode_mask": True, "delegates": []},
    "isDependencyBreaking": {
        "opcode_mask": True,
        "delegates": ["IsZeroIdiomDecl"],
    },
    "isOptimizableRegisterMove": {"opcode_mask": False, "delegates": []},
}


@dataclass
class PredicateLowerer:
    program: Program
    feature_defs: dict[str, str]
    sets: SetEvaluator | None
    sorts: dict[str, str]
    machine_models: dict[str, str]
    reserved_names: set[str] = field(default_factory=set)
    # The table `emit_td.py` collects; predicate records share it with the
    # machine records so `--records-json` describes the whole module.
    records: RecordSet = field(default_factory=RecordSet)

    def __post_init__(self) -> None:
        self.predicates: dict[str, PredicateDecl] = {
            d.name: d for d in self.program.decls if isinstance(d, PredicateDecl)
        }
        self.set_names = {
            d.name for d in self.program.decls if isinstance(d, SetDecl)
        }
        self.sti_decls: dict[str, STIDecl] = {
            d.name: d for d in self.program.decls if isinstance(d, STIDecl)
        }
        self.sti_decl_defs: dict[str, str] = {
            name: BUILTIN_STI_DECLS.get(name, td_def_name(name) + "Decl")
            for name in self.sti_decls
        }
        self.named_defs: dict[str, str] = {}
        # A DepBreakingClass/InstructionEquivalenceClass is a TableGen def: two
        # STIPredicate functions can share it (the section 5 example declares
        # OrrXzrXzr in both isZeroIdiom and isDependencyBreaking), but the same
        # name with different contents is still an error.
        self.class_defs: dict[str, str] = {}
        self.sti_functions: dict[str, tuple[str | None, list[str]]] = {}
        self.global_defs: list[tuple[str, str]] = []
        self.def_names: set[str] = set(self.reserved_names)
        self.errors: list[str] = []
        self.stack: list[str] = []

    # -- record table ----------------------------------------------------
    def define(
        self,
        where: str,
        name: str,
        cls: str | None = None,
        args: tuple[str, ...] | list[str] = (),
        fields: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
        *,
        bases: tuple | list = (),
        style: str = "block",
    ) -> str:
        """Register a predicate-side definition and return its TableGen text."""
        all_bases: list[Base] = []
        if cls is not None:
            all_bases.append(Base(cls, tuple(args)))
        for base in bases:
            all_bases.append(base if isinstance(base, Base) else Base.parse(base))
        record = Record(
            name=name,
            bases=tuple(all_bases),
            fields=tuple((field, str(value)) for field, value in fields),
            style=style,
            origin=where,
        )
        try:
            self.records.add(name, list(all_bases), record.fields,
                             style=style, origin=where)
        except DuplicateRecord:
            pass
        return record.render()

    # -- diagnostics -----------------------------------------------------
    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def finish(self) -> None:
        if self.sets is not None:
            try:
                self.sets.finish()
            except SetError as exc:
                self.errors.extend(exc.errors)
        if self.errors:
            raise PredicateError(self.errors)

    def register_def(self, name: str, where: str) -> None:
        if name in self.def_names:
            self.error(where, f"emitted def {name!r} already exists")
            return
        self.def_names.add(name)

    # -- driver ----------------------------------------------------------
    def run(self) -> list[str]:
        self.lower_early()
        self.lower_late()
        self.finish()
        return [text for _, text in self.global_defs]

    def lower_early(self) -> list[str]:
        """Defs that can precede the machine blocks.

        Named predicate records, predicate prologs, STI declarations and STI
        equivalence classes do not reference a ``SchedMachineModel`` yet, so
        they can be emitted before the machines that use them.
        """
        for decl in self.program.decls:
            if isinstance(decl, PredicateDecl) and not decl.params:
                self.ensure_named(decl.name)
        for decl in self.program.decls:
            if isinstance(decl, PredicateProlog):
                self.lower_prolog(decl)
        for decl in self.program.decls:
            if isinstance(decl, STIDecl):
                self.lower_sti_decl(decl)
        for decl in self.program.decls:
            if isinstance(decl, STIFunction):
                self.lower_sti_classes(decl)
        return [text for _, text in self.global_defs]

    def lower_late(self) -> list[str]:
        """Defs that must follow the machines (``STIPredicate.SchedModel``)."""
        start = len(self.global_defs)
        for decl in self.program.decls:
            if isinstance(decl, STIFunction):
                self.lower_sti_predicate(decl)
        for decl in self.program.decls:
            if isinstance(decl, TIIPredicate):
                self.lower_tii(decl)
        return [text for _, text in self.global_defs[start:]]

    # -- named predicates ------------------------------------------------
    def ensure_named(self, name: str) -> str:
        if name in self.named_defs:
            return self.named_defs[name]
        if name not in self.predicates:
            self.error(f"predicate {name}", "unknown predicate")
            return td_def_name(name)
        decl = self.predicates[name]
        if decl.params:
            self.error(
                f"predicate {name}",
                "parameterized predicates are macros; call them with arguments",
            )
            return td_def_name(name)
        if name in self.stack:
            self.error(f"predicate {name}", "recursive predicate lowering")
            return td_def_name(name)
        self.stack.append(name)
        def_name = td_def_name(name)
        where = f"predicate {name}"
        if decl.expr is None:
            text = self.lower_block_predicate(decl, def_name, where)
        else:
            sort = self.sorts.get(name, MC)
            if sort == SEL:
                self.error(where, "block-form predicates cannot have an expression body")
                text = self.define(where, def_name, "FalsePred")
            elif sort == SCHED:
                body = self.lower_sched(decl.expr, {}, where)
                text = self.define(
                    where, def_name, bases=[self.predicate_base(body, SCHED)]
                )
            else:
                body = self.lower_mc(decl.expr, {}, where)
                text = self.define(
                    where, def_name, bases=[self.predicate_base(body, MC)]
                )
        self.stack.pop()
        self.register_def(def_name, where)
        self.named_defs[name] = def_name
        self.global_defs.append((def_name, text))
        return def_name

    @staticmethod
    def predicate_base(body: str, sort: str) -> str:
        """Turn a lowered predicate expression into a valid base class.

        ``TruePred``/``FalsePred`` and other named predicates are records, not
        classes, so ``def P : SomeDef;`` would be rejected by TableGen.  A
        one-element sequence keeps the meaning and is always a class.
        """
        if "<" in body:
            return body
        if sort == SCHED:
            return f"AllOfSchedPreds<[{body}]>"
        return f"CheckAll<[{body}]>"

    def named_call(self, name: str, args: list, env: dict[str, Any], where: str) -> str:
        if name not in self.predicates:
            self.error(where, f"unknown predicate {name!r}")
            return "FalsePred"
        decl = self.predicates[name]
        if not decl.params:
            if args:
                self.error(where, f"predicate {name} takes no arguments")
            return self.ensure_named(name)
        if len(args) != len(decl.params):
            self.error(
                where,
                f"predicate {name} expects {len(decl.params)} argument(s), got {len(args)}",
            )
            return "FalsePred"
        if decl.expr is None:
            self.error(where, f"predicate {name} is a selection predicate macro")
            return "FalsePred"
        new_env = dict(env)
        for param, arg in zip(decl.params, args):
            new_env[param] = arg
        sort = self.sorts.get(name, MC)
        if sort == SCHED:
            return self.lower_sched(decl.expr, new_env, f"{where}.{name}")
        return self.lower_mc(decl.expr, new_env, f"{where}.{name}")

    @staticmethod
    def resolve_param(arg, env: dict[str, Any] | None):
        """Follow macro parameters used as set/entity arguments."""
        seen: set[str] = set()
        while env and arg[0] == "ident" and arg[1] in env and arg[1] not in seen:
            seen.add(arg[1])
            arg = env[arg[1]]
        return arg

    def operand_index(self, arg, env: dict[str, Any], where: str) -> int:
        tag = arg[0]
        if tag == "int":
            if arg[1] < 0:
                self.error(where, f"operand index must be non-negative, got {arg[1]}")
                return 0
            return arg[1]
        if tag == "ident":
            name = arg[1]
            if name in env:
                return self.operand_index(env[name], env, where)
            self.error(where, f"unbound operand parameter {name!r}")
            return 0
        if tag == "string":
            self.error(where, f"expected an operand index, got string {arg[1]!r}")
            return 0
        self.error(where, f"expected an operand index, got {arg!r}")
        return 0

    # -- MC predicates ---------------------------------------------------
    def lower_mc(self, expr, env: dict[str, Any], where: str) -> str:
        tag = expr[0]
        if tag == "bool":
            return "TruePred" if expr[1] else "FalsePred"
        if tag == "not":
            return f"CheckNot<{self.lower_mc(expr[1], env, where)}>"
        if tag == "ident":
            name = expr[1]
            if name in env:
                self.error(where, f"predicate parameter {name!r} was used as a predicate")
                return "FalsePred"
            if self.sorts.get(name) == SCHED:
                self.error(
                    where,
                    f"predicate {name!r} is SCHED; an MC context requires an MC predicate",
                )
                return "FalsePred"
            return self.ensure_named(name)
        if tag != "call":
            self.error(where, f"unsupported predicate expression {expr!r}")
            return "FalsePred"

        name, args = expr[1], expr[2]
        if name in COMBINERS:
            if not args:
                return "TruePred" if name == "all_of" else "FalsePred"
            lowered = [self.lower_mc(arg, env, where) for arg in args]
            cls = "CheckAll" if name == "all_of" else "CheckAny"
            return f"{cls}<{join_list(lowered)}>"
        if name == "mc":
            return self.lower_mc(args[0], env, where)
        if name == "sched":
            self.error(
                where,
                "sched(...) is a SCHED predicate; an MC context requires an MC predicate",
            )
            return "FalsePred"
        if name == "feature":
            self.error(where, "feature(...) is a SCHED predicate; wrap it in sched(...) or use a variant")
            return "FalsePred"
        if name == "sched_code":
            self.error(where, "sched_code(...) is a SCHED predicate")
            return "FalsePred"
        if name in self.predicates:
            if self.sorts.get(name) == SCHED:
                self.error(
                    where,
                    f"predicate {name!r} is SCHED; an MC context requires an MC predicate",
                )
                return "FalsePred"
            return self.named_call(name, args, env, where)
        if name == "opcode" or name == "pseudo":
            names = self.inst_list(args[0], where, env)
            if not names:
                return "FalsePred"
            cls = "CheckOpcode" if name == "opcode" else "CheckPseudo"
            return f"{cls}<{join_list(names)}>"
        if name in ("is_reg", "is_vreg", "is_not_vreg", "is_imm"):
            index = self.operand_index(args[0], env, where)
            cls = {
                "is_reg": "CheckIsRegOperand",
                "is_vreg": "CheckIsVRegOperand",
                "is_not_vreg": "CheckIsNotVRegOperand",
                "is_imm": "CheckIsImmOperand",
            }[name]
            return f"{cls}<{index}>"
        if name in ("is_valid_reg", "is_invalid_reg", "is_zero", "reg_simple", "imm_simple"):
            index = self.operand_index(args[0], env, where)
            cls = {
                "is_valid_reg": "CheckValidRegOperand",
                "is_invalid_reg": "CheckInvalidRegOperand",
                "is_zero": "CheckZeroOperand",
                "reg_simple": "CheckRegOperandSimple",
                "imm_simple": "CheckImmOperandSimple",
            }[name]
            self.reject_mapper(args[1:], where, name)
            return f"{cls}<{index}>"
        if name == "reg_is":
            index = self.operand_index(args[0], env, where)
            self.reject_mapper(args[2:], where, name)
            regs = self.reg_list(args[1], where, env)
            if not regs:
                return "FalsePred"
            checks = [f"CheckRegOperand<{index}, {reg}>" for reg in regs]
            return checks[0] if len(checks) == 1 else f"CheckAny<{join_list(checks)}>"
        if name in ("imm_is", "imm_lt", "imm_gt", "imm_le", "imm_ge"):
            index = self.operand_index(args[0], env, where)
            value = self.int_arg(args[1], where, name)
            self.reject_mapper(args[2:], where, name)
            cls = {
                "imm_is": "CheckImmOperand",
                "imm_lt": "CheckImmOperandLT",
                "imm_gt": "CheckImmOperandGT",
                "imm_le": "CheckImmOperandLE",
                "imm_ge": "CheckImmOperandGE",
            }[name]
            return f"{cls}<{index}, {value}>"
        if name == "imm_is_enum":
            index = self.operand_index(args[0], env, where)
            enum = self.string_arg(args[1], where, name)
            self.reject_mapper(args[2:], where, name)
            return f"CheckImmOperand_s<{index}, {td_str(enum)}>"
        if name == "imm_range":
            index = self.operand_index(args[0], env, where)
            start = self.int_arg(args[1], where, name)
            end = self.int_arg(args[2], where, name)
            self.reject_mapper(args[3:], where, name)
            return f"CheckImmOperandRange<{index}, {start}, {end}>"
        if name == "num_ops":
            return f"CheckNumOperands<{self.int_arg(args[0], where, name)}>"
        if name == "same_reg":
            first = self.operand_index(args[0], env, where)
            second = self.operand_index(args[1], env, where)
            return f"CheckSameRegOperand<{first}, {second}>"
        if name == "func":
            mc_fn = self.string_arg(args[0], where, name)
            mi_fn = self.string_arg(args[1], where, name)
            return f"CheckFunctionPredicate<{td_str(mc_fn)}, {td_str(mi_fn)}>"
        if name == "func_tii":
            mc_fn = self.string_arg(args[0], where, name)
            mi_fn = self.string_arg(args[1], where, name)
            ptr = "TII"
            for extra in args[2:]:
                if extra[0] == "named" and extra[1] == "tii":
                    ptr = self.string_arg(extra[2], where, name)
                else:
                    self.error(where, f"func_tii: unexpected argument {extra!r}")
            return (
                f"CheckFunctionPredicateWithTII<{td_str(mc_fn)}, {td_str(mi_fn)}, "
                f"{td_str(ptr)}>"
            )
        if name == "non_portable":
            code = self.code_arg(args[0], where, name)
            return f"CheckNonPortable<{td_str(code)}>"
        self.error(where, f"unsupported predicate {name!r}")
        return "FalsePred"

    def reject_mapper(self, extras: list, where: str, name: str) -> None:
        for extra in extras:
            if extra[0] == "named" and extra[1] == "map":
                self.error(where, f"{name}: map= is not lowered yet")
            else:
                self.error(where, f"{name}: unexpected argument {extra!r}")

    # -- SCHED predicates ------------------------------------------------
    def expr_sort(self, expr, env: dict[str, Any]) -> str:
        tag = expr[0]
        if tag == "bool":
            return MC
        if tag == "not":
            return self.expr_sort(expr[1], env)
        if tag == "ident":
            name = expr[1]
            if name in env:
                return MC
            return self.sorts.get(name, MC)
        if tag != "call":
            return MC
        name, args = expr[1], expr[2]
        if name in ("feature", "sched_code", "sched"):
            return SCHED
        if name == "mc":
            return MC
        if name in COMBINERS:
            if not args:
                return MC
            return MC if all(self.expr_sort(arg, env) == MC for arg in args) else SCHED
        if name in self.predicates:
            return self.sorts.get(name, MC)
        return MC

    def to_sched(self, expr, env: dict[str, Any], where: str) -> str:
        sort = self.expr_sort(expr, env)
        if sort == SCHED:
            return self.lower_sched(expr, env, where)
        if sort == SEL:
            self.error(where, "selection predicates cannot be used as scheduling predicates")
            return "NoSchedPred"
        return f"MCSchedPredicate<{self.lower_mc(expr, env, where)}>"

    def lower_sched(self, expr, env: dict[str, Any], where: str) -> str:
        tag = expr[0]
        if tag == "bool":
            arm = "TruePred" if expr[1] else "FalsePred"
            return f"MCSchedPredicate<{arm}>"
        if tag == "not":
            child = expr[1]
            if self.expr_sort(child, env) == SCHED:
                return f"NotSchedPred<{self.to_sched(child, env, where)}>"
            return f"MCSchedPredicate<CheckNot<{self.lower_mc(child, env, where)}>>"
        if tag == "ident":
            name = expr[1]
            if name in env:
                self.error(where, f"predicate parameter {name!r} was used as a predicate")
                return "NoSchedPred"
            sort = self.sorts.get(name, MC)
            if sort == SCHED:
                return self.ensure_named(name)
            return f"MCSchedPredicate<{self.ensure_named(name)}>"
        if tag != "call":
            self.error(where, f"unsupported scheduling predicate {expr!r}")
            return "NoSchedPred"

        name, args = expr[1], expr[2]
        if name == "feature":
            feat = self.feature_def(args[0], where, env)
            return f"FeatureSchedPredicate<{feat}>"
        if name == "sched_code":
            code = self.code_arg(args[0], where, name)
            return f"SchedPredicate<{td_code(code)}>"
        if name == "mc":
            return f"MCSchedPredicate<{self.lower_mc(args[0], env, where)}>"
        if name == "sched":
            return self.to_sched(args[0], env, where)
        if name in COMBINERS:
            if not args:
                arm = "TruePred" if name == "all_of" else "FalsePred"
                return f"MCSchedPredicate<{arm}>"
            sorts = [self.expr_sort(arg, env) for arg in args]
            if all(sort == MC for sort in sorts):
                lowered = [self.lower_mc(arg, env, where) for arg in args]
                cls = "CheckAll" if name == "all_of" else "CheckAny"
                return f"MCSchedPredicate<{cls}<{join_list(lowered)}>>"
            lowered = [self.to_sched(arg, env, where) for arg in args]
            cls = "AllOfSchedPreds" if name == "all_of" else "AnyOfSchedPreds"
            return f"{cls}<{join_list(lowered)}>"
        if name in self.predicates:
            sort = self.sorts.get(name, MC)
            lowered = self.named_call(name, args, env, where)
            if sort == MC:
                return f"MCSchedPredicate<{lowered}>"
            return lowered
        if name in ATOM_PREDICATES:
            return f"MCSchedPredicate<{self.lower_mc(expr, env, where)}>"
        self.error(where, f"unsupported scheduling predicate {name!r}")
        return "NoSchedPred"

    def feature_def(self, arg, where: str, env: dict[str, Any] | None = None) -> str:
        arg = self.resolve_param(arg, env)
        name = arg[1] if arg[0] == "ident" else str(arg)
        if name not in self.feature_defs:
            self.error(where, f"unknown feature {name!r}")
            return td_def_name(name)
        return self.feature_defs[name]

    # -- block-form (SEL) predicates -------------------------------------
    def lower_block_predicate(self, decl: PredicateDecl, def_name: str, where: str) -> str:
        fields = decl.fields
        selector = fields.get("selector")
        if selector is None:
            self.error(where, "block-form predicate requires 'selector'")
            selector = "false"
        bases = [f"Predicate<{td_str(selector.strip())}>"]
        assembler = fields.get("assembler")
        if assembler is not None:
            dag = self.lower_feature_expr(assembler, where)
            desc = fields.get("desc", "")
            if desc:
                bases.append(f"AssemblerPredicate<{dag}, {td_str(desc)}>")
            else:
                bases.append(f"AssemblerPredicate<{dag}>")
        return self.define(
            where,
            def_name,
            bases=bases,
            fields=[("RecomputePerFunction", "true")]
            if fields.get("recompute_per_function")
            else [],
        )

    def lower_feature_expr(self, expr, where: str) -> str:
        if expr[0] == "not":
            return f"(not {self.lower_feature_expr(expr[1], where)})"
        if expr[0] != "call":
            self.error(where, f"unsupported feature expression {expr!r}")
            return "(all_of)"
        name, args = expr[1], expr[2]
        if name not in ("all_of", "any_of", "none_of"):
            self.error(where, f"unsupported feature combinator {name!r}")
            return "(all_of)"
        ids = args[0][1] if args and args[0][0] == "list" else []
        if not ids:
            self.error(where, f"{name}() with no features is not lowered")
            return f"({name})"
        feats = [self.feature_def(("ident", feat), where) for feat in ids]
        return f"({name} {', '.join(feats)})"

    # -- set/instruction arguments ---------------------------------------
    def inst_list(
        self, arg, where: str, env: dict[str, Any] | None = None
    ) -> list[str]:
        arg = self.resolve_param(arg, env)
        if self.sets is not None:
            return self.sets.resolve(arg, "inst", where)
        return self.simple_entity_list(arg, "inst", where)

    def reg_list(
        self, arg, where: str, env: dict[str, Any] | None = None
    ) -> list[str]:
        arg = self.resolve_param(arg, env)
        if self.sets is not None:
            return self.sets.resolve(arg, "reg", where)
        return self.simple_entity_list(arg, "reg", where)

    def simple_entity_list(self, arg, kind: str, where: str) -> list[str]:
        if arg[0] == "ident":
            name = arg[1]
            if name in self.set_names:
                self.error(where, f"set {name!r} requires the attribute schema (--attrs)")
                return []
            return [name]
        if arg[0] == "set_list":
            out = []
            for item in arg[1]:
                if item[0] != "ident":
                    self.error(where, f"set expressions require the attribute schema (--attrs)")
                    return []
                name = item[1]
                if name in self.set_names:
                    self.error(where, f"set {name!r} requires the attribute schema (--attrs)")
                    return []
                out.append(name)
            return out
        self.error(where, f"set expressions require the attribute schema (--attrs)")
        return []

    # -- STI declarations ------------------------------------------------
    def lower_sti_decl(self, decl: STIDecl) -> None:
        where = f"sti_decl {decl.name}"
        def_name = self.sti_decl_defs[decl.name]
        if decl.name in BUILTIN_STI_DECLS:
            self.check_builtin_sti_decl(decl, where)
            return
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", decl.name):
            self.error(where, f"{decl.name!r} is not a valid C++ predicate name")
        default = "FalsePred"
        if "default" in decl.fields:
            default = self.lower_mc(decl.fields["default"], {}, where)
        overrides = decl.fields.get("overrides", True)
        expand = decl.fields.get("expand_for_mc", True)
        opcode_mask = decl.fields.get("opcode_mask", False)
        delegates = []
        for name in decl.fields.get("delegates", []):
            if name not in self.sti_decl_defs:
                self.error(where, f"unknown delegate {name!r}")
            else:
                delegates.append(self.sti_decl_defs[name])
        text = self.define(
            where,
            def_name,
            "STIPredicateDecl",
            [
                td_str(decl.name),
                default,
                _bool(overrides),
                _bool(expand),
                _bool(opcode_mask),
                join_list(delegates),
            ],
        )
        self.register_def(def_name, where)
        self.global_defs.append((def_name, text))

    def check_builtin_sti_decl(self, decl: STIDecl, where: str) -> None:
        """Validate a declaration of one of the fixed `TargetSubtargetInfo` hooks.

        Their signatures live in C++, so the attributes the DSL cannot change
        are rejected rather than silently ignored.
        """
        fields = decl.fields
        fixed = BUILTIN_STI_FIXED[decl.name]
        expected = {
            "overrides": True,
            "expand_for_mc": True,
            "opcode_mask": fixed["opcode_mask"],
        }
        for key, value in expected.items():
            given = fields.get(key)
            if given is None or given == value:
                continue
            self.error(
                where,
                f"{decl.name!r} is a fixed TargetSubtargetInfo hook; "
                f"{key} must be {value!r} (got {given!r})",
            )
        # The C++ declaration already says `FalsePred`; writing `false` is the
        # same predicate, any other expression is not representable.
        given_default = fields.get("default")
        if given_default is not None and given_default != ("bool", False):
            self.error(
                where,
                f"{decl.name!r} is a fixed TargetSubtargetInfo hook; "
                f"default must be FalsePred (got {given_default!r})",
            )
        # `delegates` are written with DSL names but the fixed signature lists
        # the emitted STIPredicateDecl records.
        given_delegates = list(fields.get("delegates", []))
        resolved_delegates = [
            self.sti_decl_defs.get(name, name) for name in given_delegates
        ]
        want = fixed["delegates"]
        if resolved_delegates != want:
            self.error(
                where,
                f"{decl.name!r} has a fixed delegation "
                f"{[w.removesuffix('Decl') for w in want]!r}; got {given_delegates!r}",
            )

    def lower_sti_classes(self, decl: STIFunction) -> None:
        """Lower the equivalence classes of an ``sti_function``.

        The ``STIPredicate`` record itself is emitted in the late phase because
        it binds ``SchedModel`` and therefore has to follow the machine model it
        names.
        """
        where = f"sti_function {decl.name}"
        if decl.decl not in self.sti_decl_defs:
            self.error(where, f"unknown STI declaration {decl.decl!r}")
        model_def = None
        class_defs: list[str] = []
        for item in decl.items:
            if item["kind"] in ("equiv_class", "dep_breaking_class"):
                class_name = td_def_name(item["name"])
                opcodes = self.inst_list(item["opcodes"], f"{where}.{item['name']}")
                if not opcodes:
                    self.error(where, f"{item['name']}: empty opcode list")
                pred = self.lower_mc(item["pred"], {}, f"{where}.{item['name']}")
                operands = self.int_set(item["operands"], f"{where}.{item['name']}")
                base = (
                    "DepBreakingClass"
                    if item["kind"] == "dep_breaking_class"
                    else "InstructionEquivalenceClass"
                )
                args = [join_list(opcodes), pred]
                if operands:
                    args.append(join_list([str(n) for n in operands]))
                text = self.define(
                    f"{where}.{item['name']}", class_name, base, args
                )
                previous = self.class_defs.get(class_name)
                if previous is not None:
                    if previous != text:
                        self.error(
                            f"{where}.{item['name']}",
                            f"equivalence class {class_name!r} is already "
                            "declared with different contents",
                        )
                    else:
                        class_defs.append(class_name)
                    continue
                self.register_def(class_name, f"{where}.{item['name']}")
                self.class_defs[class_name] = text
                self.global_defs.append((class_name, text))
                class_defs.append(class_name)
            elif item["kind"] == "sched_model":
                name = item["value"]
                if name not in self.machine_models:
                    self.error(where, f"unknown machine {name!r}")
                else:
                    model_def = self.machine_models[name]
            elif item["kind"] == "delegates":
                self.error(
                    where,
                    "sti_function.delegates has no landing spot on STIPredicate (M7)",
                )
        self.sti_functions[decl.name] = (model_def, class_defs)

    def lower_sti_predicate(self, decl: STIFunction) -> None:
        where = f"sti_function {decl.name}"
        model_def, class_defs = self.sti_functions.get(decl.name, (None, []))
        fn_name = td_def_name(decl.name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", decl.name):
            self.error(where, f"{decl.name!r} is not a valid TableGen/C++ name")
        decl_def = self.sti_decl_defs.get(decl.decl, "FalsePred")
        sti = self.define(
            where,
            fn_name,
            "STIPredicate",
            [decl_def, join_list(class_defs)],
            [("SchedModel", model_def)] if model_def is not None else [],
        )
        self.register_def(fn_name, where)
        self.global_defs.append((fn_name, sti))

    def int_set(self, arg, where: str) -> list[int]:
        if arg is None:
            return []
        if arg[0] != "int_set":
            self.error(where, f"expected an int list, got {arg!r}")
            return []
        out: list[int] = []
        for item in arg[1]:
            if item[0] == "int":
                out.append(item[1])
            elif item[0] == "range":
                start, end = item[1], item[2]
                if start > end:
                    self.error(where, f"empty range [{start}..{end}]")
                else:
                    out.extend(range(start, end + 1))
            else:
                self.error(where, f"int-set spread {item!r} is not lowered")
        return out

    # -- TII predicates --------------------------------------------------
    def lower_tii(self, decl: TIIPredicate) -> None:
        where = f"tii_predicate {decl.name}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", decl.name):
            self.error(where, f"{decl.name!r} is not a valid C++ predicate name")
        body = self.lower_statement(decl.statement, where)
        def_name = td_def_name(decl.name)
        text = self.define(
            where, def_name, "TIIPredicate", [td_str(decl.name), body]
        )
        self.register_def(def_name, where)
        self.global_defs.append((def_name, text))

    def lower_statement(self, stmt: dict, where: str) -> str:
        if stmt["kind"] == "return":
            return f"MCReturnStatement<{self.lower_mc(stmt['expr'], {}, where)}>"
        cases = []
        for index, case in enumerate(stmt.get("cases", [])):
            names = []
            for entry in case["insts"][1]:
                if entry[0] == "regex":
                    self.error(where, "MCOpcodeSwitchCase requires concrete instructions, not regex(...)")
                    continue
                names.append(entry[1])
            if not names:
                self.error(where, f"switch case {index} has no instructions")
            body = self.lower_statement(case["body"], where)
            cases.append(f"MCOpcodeSwitchCase<{join_list(names)}, {body}>")
        default = self.lower_statement(stmt["default"], where)
        return f"MCOpcodeSwitchStatement<{join_list(cases)}, {default}>"

    # -- prolog ----------------------------------------------------------
    def lower_prolog(self, decl: PredicateProlog) -> None:
        index = sum(1 for name, _ in self.global_defs if name.startswith("SSDLPredicateProlog"))
        def_name = "SSDLPredicateProlog" if index == 0 else f"SSDLPredicateProlog{index}"
        text = self.define(
            "predicate_prolog", def_name, "PredicateProlog", [td_code(decl.code)]
        )
        self.register_def(def_name, "predicate_prolog")
        self.global_defs.append((def_name, text))

    # -- small helpers ---------------------------------------------------
    def int_arg(self, arg, where: str, name: str) -> int:
        if arg[0] == "int":
            return arg[1]
        self.error(where, f"{name}: expected an integer, got {arg!r}")
        return 0

    def string_arg(self, arg, where: str, name: str) -> str:
        if arg[0] == "string":
            return arg[1]
        if arg[0] == "ident":
            return arg[1]
        self.error(where, f"{name}: expected a string, got {arg!r}")
        return ""

    def code_arg(self, arg, where: str, name: str) -> str:
        if arg[0] == "code":
            return arg[1]
        self.error(where, f"{name}: expected a code block, got {arg!r}")
        return ""
