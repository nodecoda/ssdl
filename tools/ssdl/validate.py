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
    import field_types as field_types_mod  # type: ignore
    import predicate_types as predicate_types_mod  # type: ignore
    from parser import FAMILY_SOURCE, MachineDecl, PredicateDecl, Program, STIDecl, STIFunction, TIIPredicate, ItinerariesDecl, coerce_layer, expand_source_mapped, parse  # type: ignore
    import syntax_v2  # type: ignore
else:
    from .diagnose import ERROR, NOTE, diagnose, render_findings
    from .family_expand import check_coverage
    from . import field_types as field_types_mod
    from . import predicate_types as predicate_types_mod
    from . import syntax_v2
    from .parser import (
        FAMILY_SOURCE,
        ItinerariesDecl,
        MachineDecl,
        expand_source_mapped,
        parse,
        PredicateDecl,
        Program,
        STIDecl,
        STIFunction,
        TIIPredicate,
        coerce_layer,
    )


MC = "mc"
SCHED = "sched"
SEL = "sel"

# Atom predicates map onto MCInstPredicate subclasses.  The list mirrors the
# grammar in the 1.1 draft; ``func``/``func_tii`` are included because they are
# addressed in section 3.6 even though their norm mapping is still open.
ATOM_PREDICATES = set(predicate_types_mod.SIGNATURES)

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


def _python_error(expected: str, value) -> str | None:
    """Check an IR AST value against the shared v2 field table."""
    if expected.startswith("enum:"):
        enum = tuple(expected[len("enum:") :].split("|"))
        if value not in enum:
            return f"expects one of {', '.join(enum)}, got {value!r}"
        return None
    if expected == field_types_mod.INT_EXPR:
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else f"expects an integer expression, got {value!r}"
    if expected == field_types_mod.INT_LIST:
        ok = isinstance(value, list) and all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        )
        return None if ok else f"expects an integer list, got {value!r}"
    if expected == field_types_mod.BOOL:
        return None if isinstance(value, bool) else f"expects true or false, got {value!r}"
    if expected == field_types_mod.BOOL_LIST:
        ok = isinstance(value, list) and all(isinstance(item, bool) for item in value)
        return None if ok else f"expects a true/false list, got {value!r}"
    if expected in (field_types_mod.IDENT, field_types_mod.STRING):
        return None if isinstance(value, str) else f"expects a name, got {value!r}"
    if expected in (field_types_mod.IDENT_LIST, field_types_mod.SCHED_RW_LIST):
        ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
        return None if ok else f"expects a name list, got {value!r}"
    return None


def _typed_item_values(item: dict) -> tuple[str | None, dict]:
    """Return (entity kind, field/value dict) for one machine item."""
    kind = item["kind"]
    if kind == "prop":
        return "machine", {item["name"]: item.get("value")}
    if kind in ("resource", "res_group", "advance"):
        return kind, dict(item.get("props", {}))
    if kind == "write_seq":
        return kind, {"repeat": item.get("repeat")}
    if kind in ("load_queue", "store_queue"):
        return kind, {"resource": item.get("resource")}
    if kind in ("reg_file", "retire_unit"):
        return kind, dict(item.get("fields", {}))
    if kind == "bind":
        values = dict(item.get("props", {}))
        values["resources"] = item.get("resources")
        return kind, values
    if kind in ("variant", "read_variant"):
        if item.get("variadic") is None:
            return None, {}
        return "variant", {"variadic": item.get("variadic")}
    return None, {}


class SortChecker:
    def __init__(self, program: Program):
        self.program = program
        self.predicates: dict[str, PredicateDecl] = {}
        # A TII predicate is an MCInstPredicate too, but the canonical way to
        # use one in a scheduling model is ``mc(Name)`` -> MCSchedPredicate.
        self.tii_names: set[str] = {
            d.name for d in program.decls if isinstance(d, TIIPredicate)
        }
        self.machines = {
            d.name: d for d in program.decls if isinstance(d, MachineDecl) and d.alias is None
        }
        self.errors: list[str] = []
        self.sorts: dict[str, str] = {}
        self.stack: list[str] = []
        self.current_line: int | None = None

        for name in sorted(self.tii_names & set(
            d.name for d in program.decls if isinstance(d, PredicateDecl)
        )):
            self.error(
                f"predicate {name}",
                f"{name!r} is declared both as a predicate and as a tii predicate",
            )

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
    def error(
        self, where: str, message: str, line: int | None = None
    ) -> None:
        line = line or self.current_line
        prefix = f"line {line}: " if line else ""
        self.errors.append(f"{prefix}{where}: {message}")

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
        if name in self.tii_names:
            return MC
        if name not in self.predicates:
            removed = predicate_types_mod.REMOVED.get(name)
            if removed is not None:
                self.error(
                    where, f"{name}() was removed from the surface: {removed}"
                )
                return MC
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
            # ``mc(TII)`` is the MCSchedPredicate wrapper, not an MC predicate.
            if args[0][0] == "ident" and args[0][1] in self.tii_names:
                return SCHED
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
            for problem in predicate_types_mod.check_call(name, args):
                self.error(where, problem)
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
            self.current_line = getattr(decl, "line", None)
            if isinstance(decl, TIIPredicate):
                self.check_statement(decl.statement, f"tii_predicate {decl.name}")
            elif isinstance(decl, STIDecl):
                if "default" in decl.fields:
                    self.require_mc(decl.fields["default"], f"sti_decl {decl.name}.default")
            elif isinstance(decl, STIFunction):
                for item in decl.items:
                    self.current_line = item.get("line") or decl.line
                    if item["kind"] in ("equiv_class", "dep_breaking_class"):
                        self.require_mc(
                            item["pred"],
                            f"sti_function {decl.name}.{item['name']}",
                        )
            elif isinstance(decl, MachineDecl):
                self.check_item_fields(decl.items, f"machine {decl.name}")
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
                    self.current_line = item.get("line") or decl.line
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
                        self.check_itin_fields(
                            itin, f"machine {decl.name}.itineraries"
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
                    self.check_itin_fields(decl, "itineraries")
        return self.errors

    def check_item_fields(self, items: list[dict], where: str) -> None:
        """Apply the shared field table to IR machine items."""
        for item in items:
            self.current_line = item.get("line")
            kind, values = _typed_item_values(item)
            if kind is None:
                continue
            table = field_types_mod.ENTITY_FIELDS.get(kind)
            if table is None:
                continue
            subject = (
                item.get("name")
                or item.get("write")
                or item.get("read")
                or kind
            )
            for field, value in values.items():
                if value is None or field not in table:
                    continue
                problem = _python_error(table[field], value)
                if problem is not None:
                    site = (
                        f"{where}.{field}"
                        if subject == field
                        else f"{where}.{subject}.{field}"
                    )
                    self.error(site, problem)

    def check_itin_fields(self, itin: ItinerariesDecl, where: str) -> None:
        """Check itinerary fields and stage bodies against the same table."""
        for item in itin.items:
            self.current_line = item.get("line")
            kind = item["kind"]
            if kind in ("func_units", "bypasses"):
                problem = _python_error(
                    field_types_mod.IDENT_LIST, item.get("value")
                )
                if problem is not None:
                    self.error(f"{where}.{kind}", problem)
                continue
            if kind != "itin":
                continue
            fields = item.get("fields", {})
            for field, value in fields.items():
                if field == "stages":
                    for stage in value[1]:
                        for stage_field, expected in (
                            field_types_mod.ENTITY_FIELDS["stage"].items()
                        ):
                            stage_value = stage.get(stage_field)
                            if stage_value is None:
                                continue
                            problem = _python_error(expected, stage_value)
                            if problem is not None:
                                self.error(
                                    f"{where}.{item['name']}.stage.{stage_field}",
                                    problem,
                                )
                    continue
                expected = field_types_mod.ENTITY_FIELDS["itin"].get(field)
                if expected is None:
                    continue
                problem = _python_error(expected, value)
                if problem is not None:
                    self.error(f"{where}.{item['name']}.{field}", problem)

    def check_itin_names(
        self, itin: ItinerariesDecl, where: str, declared_classes: set[str]
    ) -> None:
        seen: set[str] = set()
        for item in itin.items:
            if item["kind"] != "itin":
                continue
            self.current_line = item.get("line")
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
        "--syntax",
        type=coerce_layer,
        metavar="{v2,ir}",
        default="v2",
        help="input layer: v2 surface (default) or internal IR text",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="also print notes (semantic context, not a problem)",
    )
    args = ap.parse_args(argv)
    try:
        raw = Path(args.input).read_text(encoding="utf-8")
        marker = syntax_v2.to_ir(raw) if args.syntax == "v2" else raw
        flat, source_map = expand_source_mapped(raw, syntax=args.syntax)
        program = parse(flat, source_map=source_map)
    except Exception as exc:
        print(f"{args.input}: {exc}", file=sys.stderr)
        return 2
    # Coverage is a property of the expanded text, not of the AST: it compares
    # the write and bind member sets that the axes produced.
    coverage = check_coverage(flat) if FAMILY_SOURCE.search(marker) else []
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
