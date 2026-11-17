#!/usr/bin/env python3
"""Normative checks for the SSDL 1.1 predicate sorts and itinerary binding.

SSDL 1.0 had one ``predicate_expr`` covering both LLVM predicate hierarchies
(``MCInstPredicate`` and ``SchedPredicateBase``) and left top-level
``itineraries`` unattached.  The 1.1 draft fixes both:

  * expression predicates have a sort, inferred from their body:
      MC     -> MCInstPredicate / CheckAll / CheckAny / ...
      SCHED  -> SchedPredicateBase / AllOfSchedPreds / FeatureSchedPredicate
      SEL    -> block-form ``selector``/``assembler`` predicates
  * MC expressions are auto-promoted to SCHED only where a scheduling
    predicate is required (``variant`` cases); MC contexts reject SCHED.
  * a top-level ``itineraries`` block must name ``sched_model``; a block inside
    ``machine`` must not.

This pass is independent of lowering: the emitter can skip a construct while
this validator still proves that its predicate layer is well-sorted.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    from diagnose import ERROR, NOTE, diagnose, render_findings  # type: ignore
    from family_expand import check_coverage  # type: ignore
    from parser import FAMILY_SOURCE, MachineDecl, PredicateDecl, Program, STIDecl, STIFunction, TIIPredicate, ItinerariesDecl, expand_source, parse  # type: ignore
else:
    from .diagnose import ERROR, NOTE, diagnose, render_findings
    from .family_expand import check_coverage
    from .parser import (
        FAMILY_SOURCE,
        ItinerariesDecl,
        MachineDecl,
        expand_source,
        parse,
        PredicateDecl,
        Program,
        STIDecl,
        STIFunction,
        TIIPredicate,
    )


MC = "mc"
SCHED = "sched"
SEL = "sel"

# Atom predicates map onto MCInstPredicate subclasses.  The list mirrors the
# grammar in the 1.1 draft; ``func``/``func_tii`` are included because they are
# addressed in section 3.6 even though their norm mapping is still open.
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
    "func",
    "func_tii",
    "non_portable",
}

COMBINERS = {"all_of", "any_of"}

RESERVED = ATOM_PREDICATES | COMBINERS | {
    "feature",
    "sched_code",
    "mc",
    "sched",
    "not",
    "true",
    "false",
}


class SortChecker:
    def __init__(self, program: Program):
        self.program = program
        self.predicates: dict[str, PredicateDecl] = {}
        self.machines = {
            d.name: d for d in program.decls if isinstance(d, MachineDecl) and d.alias is None
        }
        self.errors: list[str] = []
        self.sorts: dict[str, str] = {}
        self.stack: list[str] = []

        for decl in program.decls:
            if not isinstance(decl, PredicateDecl):
                continue
            if decl.name in RESERVED:
                self.error(
                    f"predicate {decl.name}",
                    f"{decl.name!r} is a reserved predicate name",
                )
                continue
            if decl.name in self.predicates:
                self.error(f"predicate {decl.name}", "duplicate predicate name")
                continue
            self.predicates[decl.name] = decl

    # -- diagnostics -----------------------------------------------------
    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    # -- sort inference --------------------------------------------------
    def declaration_sort(self, name: str) -> str:
        if name in self.sorts:
            return self.sorts[name]
        decl = self.predicates[name]
        if decl.expr is None:
            self.sorts[name] = SEL
            return SEL
        if name in self.stack:
            chain = " -> ".join(self.stack + [name])
            self.error(f"predicate {name}", f"recursive predicate reference: {chain}")
            return MC
        self.stack.append(name)
        sort = self.infer(decl.expr, f"predicate {name}")
        self.stack.pop()
        self.sorts[name] = sort
        return sort

    def check_call_arity(self, where: str, name: str, args: list) -> None:
        decl = self.predicates[name]
        if len(args) != len(decl.params):
            self.error(
                where,
                f"predicate {name} expects {len(decl.params)} argument(s), got {len(args)}",
            )

    def named_sort(self, where: str, name: str, args: list | None) -> str:
        if name not in self.predicates:
            self.error(where, f"unknown predicate {name!r}; declare it or use an atom predicate")
            return MC
        self.check_call_arity(where, name, args or [])
        sort = self.declaration_sort(name)
        if sort == SEL:
            self.error(
                where,
                f"{name!r} is a selection predicate (block form) and cannot be used "
                "in a scheduling predicate expression",
            )
            return MC
        return sort

    def infer(self, expr, where: str) -> str:
        kind = expr[0]
        if kind == "bool":
            return MC
        if kind == "not":
            return self.infer(expr[1], where)
        if kind == "ident":
            return self.named_sort(where, expr[1], None)
        if kind != "call":
            self.error(where, f"expected a predicate expression, got {expr!r}")
            return MC

        name, args = expr[1], expr[2]
        if name == "feature":
            if len(args) != 1:
                self.error(where, "feature(...) takes exactly one feature name")
            return SCHED
        if name == "sched_code":
            if len(args) != 1 or args[0][0] != "code":
                self.error(where, "sched_code(...) takes exactly one code block")
            return SCHED
        if name == "mc":
            if len(args) != 1:
                self.error(where, "mc(...) takes exactly one predicate expression")
                return MC
            inner = self.infer(args[0], where)
            if inner == SCHED:
                self.error(where, "mc(...) requires an MC predicate, got a SCHED predicate")
            return MC
        if name == "sched":
            if len(args) != 1:
                self.error(where, "sched(...) takes exactly one predicate expression")
            else:
                self.infer(args[0], where)
            return SCHED
        if name in COMBINERS:
            sorts = [self.infer(arg, where) for arg in args]
            return MC if all(s == MC for s in sorts) else SCHED
        if name in ATOM_PREDICATES:
            return MC
        return self.named_sort(where, name, args)

    # -- contexts --------------------------------------------------------
    def require_mc(self, expr, where: str) -> None:
        sort = self.infer(expr, where)
        if sort != MC:
            self.error(where, f"requires an MC predicate, got {sort.upper()}")

    def check_variant_case(self, expr, where: str) -> None:
        sort = self.infer(expr, where)
        if sort == SEL:
            self.error(where, "variant cases require MC or SCHED predicates, got SEL")
        # MC is legal here: the norm wraps it in MCSchedPredicate.

    def check_statement(self, stmt: dict, where: str) -> None:
        if stmt["kind"] == "return":
            self.require_mc(stmt["expr"], where)
            return
        for case in stmt.get("cases", []):
            self.check_statement(case["body"], where)
        self.check_statement(stmt["default"], where)

    def run(self) -> list[str]:
        for name in self.predicates:
            self.declaration_sort(name)

        for decl in self.program.decls:
            if isinstance(decl, TIIPredicate):
                self.check_statement(decl.statement, f"tii_predicate {decl.name}")
            elif isinstance(decl, STIDecl):
                if "default" in decl.fields:
                    self.require_mc(decl.fields["default"], f"sti_decl {decl.name}.default")
            elif isinstance(decl, STIFunction):
                for item in decl.items:
                    if item["kind"] in ("equiv_class", "dep_breaking_class"):
                        self.require_mc(
                            item["pred"],
                            f"sti_function {decl.name}.{item['name']}",
                        )
            elif isinstance(decl, MachineDecl):
                itin_classes: set[str] = set()
                for item in decl.items:
                    if item["kind"] == "itin_class":
                        if item["name"] in itin_classes:
                            self.error(
                                f"machine {decl.name}.itin_class",
                                f"duplicate itinerary class {item['name']!r}",
                            )
                        itin_classes.add(item["name"])
                for item in decl.items:
                    if item["kind"] in ("variant", "read_variant"):
                        for index, case in enumerate(item["cases"]):
                            self.check_variant_case(
                                case["pred"],
                                f"machine {decl.name}.{item['name']}.case[{index}]",
                            )
                    elif item["kind"] == "itineraries":
                        itin = item["decl"]
                        if itin.sched_model is not None:
                            self.error(
                                f"machine {decl.name}.itineraries",
                                "must not set sched_model; the containing machine owns the itinerary",
                            )
                        self.check_itin_names(
                            itin, f"machine {decl.name}.itineraries", itin_classes
                        )
            elif isinstance(decl, ItinerariesDecl):
                if decl.sched_model is None:
                    self.error(
                        "itineraries",
                        "top-level itineraries must set 'sched_model = IDENT'",
                    )
                elif decl.sched_model not in self.machines:
                    self.error(
                        "itineraries",
                        f"sched_model {decl.sched_model!r} is not a machine definition",
                    )
                else:
                    owner = self.machines[decl.sched_model]
                    classes = {
                        item["name"]
                        for item in owner.items
                        if item["kind"] == "itin_class"
                    }
                    self.check_itin_names(decl, "itineraries", classes)
        return self.errors

    def check_itin_names(
        self, itin: ItinerariesDecl, where: str, declared_classes: set[str]
    ) -> None:
        seen: set[str] = set()
        for item in itin.items:
            if item["kind"] != "itin":
                continue
            name = item["name"]
            if name in seen:
                self.error(where, f"duplicate itin {name!r}")
            elif name in declared_classes:
                self.error(
                    where,
                    f"itin {name!r} collides with an itin_class declaration",
                )
            seen.add(name)


def validate_all(program: Program) -> tuple[list[str], list]:
    """Sort errors plus effect findings.

    ``validate`` keeps its historical list-of-strings contract; this entry
    point is for callers that also want warnings and notes.
    """
    errors = SortChecker(program).run()
    findings = diagnose(program)
    errors.extend(f.render() for f in findings if f.level == ERROR)
    return errors, findings


def validate(program: Program) -> list[str]:
    errors, _ = validate_all(program)
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="SSDL source file")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="also print notes (semantic context, not a problem)",
    )
    args = ap.parse_args(argv)
    try:
        raw = Path(args.input).read_text(encoding="utf-8")
        flat = expand_source(raw)
        program = parse(flat)
    except Exception as exc:
        print(f"{args.input}: {exc}", file=sys.stderr)
        return 2
    # Coverage is a property of the expanded text, not of the AST: it compares
    # the write and bind member sets that the axes produced.
    coverage = check_coverage(flat) if FAMILY_SOURCE.search(raw) else []
    errors, findings = validate_all(program)
    errors = coverage + errors
    for error in errors:
        print(error, file=sys.stderr)
    shown = [
        finding
        for finding in findings
        if finding.level != ERROR
        and (args.verbose or finding.level != NOTE)
    ]
    for line in render_findings(shown):
        print(line, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
