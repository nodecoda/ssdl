#!/usr/bin/env python3
"""Effect-aware diagnostics for SSDL sources.

``effects.py`` records what a lowered field does.  This module turns those
facts into source-level findings, so an author does not have to read the
emitter to learn that, for example, ``retire_ooo`` only changes llvm-mca or
that ``InstRW.Unsupported`` is dead.

Levels:

  error    the construct cannot have the effect it appears to ask for
  warning  a real effect, but not in the subsystem the source implies
  note     useful context, hidden unless the caller asks for it

The pass is independent of lowering.  ``emit_td`` folds error findings into
its own error list and exposes the rest; ``validate.py`` reports them next to
the sort errors.
"""
from __future__ import annotations

from dataclasses import dataclass

if __package__ in (None, ""):
    from effects import FIELD_EFFECTS  # type: ignore
    from parser import (  # type: ignore
        FeatureDecl,
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        STIDecl,
        STIFunction,
        TIIPredicate,
    )
else:
    from .effects import FIELD_EFFECTS
    from .parser import (
        FeatureDecl,
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        STIDecl,
        STIFunction,
        TIIPredicate,
    )

ERROR = "error"
WARNING = "warning"
NOTE = "note"

# Atoms that become ``false`` when a predicate is expanded for MCInst.
MC_DEGRADED_ATOMS = frozenset({"pseudo", "non_portable"})


@dataclass(frozen=True)
class Finding:
    level: str
    where: str
    message: str
    effect: str | None = None
    evidence: tuple[str, ...] = ()

    def render(self) -> str:
        suffix = f" [{self.effect}]" if self.effect else ""
        return f"{self.where}: {self.level}: {self.message}{suffix}"


def render_findings(findings: list[Finding]) -> list[str]:
    """Render one line per distinct message, folding repeats into ``(xN)``.

    A real model sets the same field on thousands of writes, so a per-write
    line is noise.  The first occurrence keeps its ``where`` prefix; the count
    says how many sites the message covers.
    """
    order: list[tuple[str, str, str | None]] = []
    first: dict[tuple[str, str, str | None], Finding] = {}
    count: dict[tuple[str, str, str | None], int] = {}
    for finding in findings:
        key = (finding.level, finding.message, finding.effect)
        if key not in first:
            first[key] = finding
            count[key] = 0
            order.append(key)
        count[key] += 1
    lines = []
    for key in order:
        line = first[key].render()
        if count[key] > 1:
            line += f" (x{count[key]})"
        lines.append(line)
    return lines


def _f(
    level: str,
    where: str,
    message: str,
    key: str | None = None,
) -> Finding:
    effect = FIELD_EFFECTS.get(key) if key else None
    return Finding(
        level=level,
        where=where,
        message=message,
        effect=key,
        evidence=effect.evidence if effect else (),
    )


# ---------------------------------------------------------------- predicates
def _atoms(
    expr,
    predicates: dict[str, PredicateDecl],
    seen: set[str] | None = None,
) -> set[str]:
    """Atom predicate names reachable from ``expr``, following named refs."""
    if not expr:
        return set()
    tag = expr[0]
    if tag == "bool":
        return set()
    if tag == "not":
        return _atoms(expr[1], predicates, seen)
    if tag == "ident":
        return _named_atoms(expr[1], predicates, seen)
    if tag != "call":
        return set()

    name, args = expr[1], expr[2]
    if name in ("all_of", "any_of"):
        out: set[str] = set()
        for arg in args:
            out.update(_atoms(arg, predicates, seen))
        return out
    if name in ("mc", "sched", "not"):
        return _atoms(args[0], predicates, seen) if args else set()
    if name == "feature":
        return set()
    if name in predicates:
        return _named_atoms(name, predicates, seen)
    return {name}


def _named_atoms(
    name: str,
    predicates: dict[str, PredicateDecl],
    seen: set[str] | None = None,
) -> set[str]:
    decl = predicates.get(name)
    if decl is None or decl.expr is None:
        return {name}
    visited = set() if seen is None else seen
    if name in visited:
        return set()
    visited.add(name)
    return _atoms(decl.expr, predicates, visited)


def _degraded(atoms: set[str]) -> list[str]:
    return sorted(atoms & MC_DEGRADED_ATOMS)


# ------------------------------------------------------------------ machines
_MACHINE_PROPS = {
    "loop_micro_op_buffer": (
        WARNING,
        "loop_micro_op_buffer is a TTI unroll threshold; "
        "MachineScheduler and llvm-mca do not read it",
        "SchedMachineModel.LoopMicroOpBufferSize",
    ),
    "load_latency": (
        NOTE,
        "load_latency is a CodeGen fallback only; llvm-mca ignores it and "
        "the real load latency belongs in bind latency",
        "SchedMachineModel.LoadLatency",
    ),
    "high_latency": (
        NOTE,
        "high_latency is a CodeGen fallback only and needs isHighLatencyDef; "
        "llvm-mca ignores it",
        "SchedMachineModel.HighLatency",
    ),
    "mispredict_penalty": (
        NOTE,
        "mispredict_penalty feeds branch-cost heuristics; "
        "the machine scheduler and llvm-mca do not read it",
        "SchedMachineModel.MispredictPenalty",
    ),
    "post_ra_scheduler": (
        NOTE,
        "post_ra_scheduler only gates the legacy post-RA list scheduler; "
        "the pre-RA MachineScheduler and llvm-mca ignore it",
        "SchedMachineModel.PostRAScheduler",
    ),
    "enable_intervals": (
        NOTE,
        "enable_intervals only affects CodeGen chip-level resource intervals; "
        "llvm-mca ignores it",
        "SchedMachineModel.EnableIntervals",
    ),
    "complete_model": (
        NOTE,
        "complete_model gates the TableGen completeness check; "
        "the runtime assert is debug-only",
        "SchedMachineModel.CompleteModel",
    ),
    "full_inst_rw_overlap_check": (
        NOTE,
        "full_inst_rw_overlap_check is validation-only and never reaches MCSchedModel",
        "SchedMachineModel.FullInstRWOverlapCheck",
    ),
    "unsupported_features": (
        NOTE,
        "unsupported_features only filters the completeness check; "
        "it does not remove sched classes or resources",
        "SchedMachineModel.UnsupportedFeatures",
    ),
}

_BIND_PROPS = {
    "retire_ooo": (
        WARNING,
        "retire_ooo is read only by llvm-mca for its in-order retire model; "
        "CodeGen never reads it",
        "ProcWriteResources.RetireOOO",
    ),
    "acquire": (
        NOTE,
        "acquire is read only by CodeGen; llvm-mca builds "
        "CycleSegment(0, release) and ignores it",
        "ProcWriteResources.AcquireAtCycles",
    ),
    "single": (
        NOTE,
        "single is sugar for begin_grp + end_grp; there is no separate runtime field",
        "ProcWriteResources.SingleIssue",
    ),
    "unsupported": (
        NOTE,
        "unsupported marks the sched class invalid: CodeGen falls back, "
        "llvm-mca hard-errors on the instruction",
        "ProcWriteResources.Unsupported",
    ),
}

_SCHED_RECORD_KINDS = {
    "bind",
    "advance",
    "map",
    "map_itin",
    "variant",
    "read_variant",
    "alias",
}


def _machine(
    decl: MachineDecl,
    predicates: dict[str, PredicateDecl],
) -> list[Finding]:
    out: list[Finding] = []
    where = f"machine {decl.name}"
    has_sched = False
    has_itin = False

    for item in decl.items:
        kind = item["kind"]
        if kind == "prop":
            name = item["name"]
            rule = _MACHINE_PROPS.get(name)
            if rule:
                level, message, key = rule
                out.append(_f(level, f"{where}.{name}", message, key))
            continue
        if kind in _SCHED_RECORD_KINDS:
            has_sched = True
        if kind == "itineraries":
            has_itin = True
            continue
        if kind == "bind":
            out.extend(_bind(item, where))
            continue
        if kind == "map":
            if item.get("unsupported") is True:
                out.append(
                    _f(
                        WARNING,
                        f"{where}.map",
                        "map ... : unsupported is a dead field (the emitter never "
                        "reads InstRW.Unsupported); setting it has no effect. "
                        "Mark the bound write unsupported instead, or drop the "
                        "property",
                        "InstRW.Unsupported",
                    )
                )
            elif item.get("unsupported") is False:
                out.append(
                    _f(
                        NOTE,
                        f"{where}.map",
                        "InstRW.Unsupported is a dead field; explicit false has no effect",
                        "InstRW.Unsupported",
                    )
                )
            continue
        if kind in ("variant", "read_variant"):
            for index, case in enumerate(item["cases"]):
                atoms = _degraded(_atoms(case["pred"], predicates))
                if atoms:
                    out.append(
                        _f(
                            WARNING,
                            f"{where}.{item['name']}.case[{index}]",
                            f"case uses {', '.join(atoms)}; llvm-mca expands these "
                            "to false and silently uses the default variant",
                            "SchedVar.Predicate",
                        )
                    )

    if has_itin:
        if has_sched:
            out.append(
                _f(
                    NOTE,
                    f"{where}.itineraries",
                    "itineraries are CodeGen legacy tables: CodeGen consults them "
                    "before the sched model, while llvm-mca ignores them and uses "
                    "the sched model",
                    "SchedMachineModel.Itineraries",
                )
            )
        else:
            out.append(
                _f(
                    WARNING,
                    f"{where}.itineraries",
                    "llvm-mca rejects CPUs that provide only itineraries; "
                    "add SchedMachineModel write/bind tables for MCA",
                    "SchedMachineModel.Itineraries",
                )
            )
    return out


def _bind(item: dict, where: str) -> list[Finding]:
    out: list[Finding] = []
    props = item["props"]
    label = f"{where}.bind {item['write']}"
    for name, (level, message, key) in _BIND_PROPS.items():
        if name in props:
            out.append(_f(level, f"{label}.{name}", message, key))
    latency = props.get("latency")
    if (
        "release" not in props
        and isinstance(latency, int)
        and latency > 1
    ):
        out.append(
            _f(
                NOTE,
                label,
                "release is omitted: ReleaseAtCycles defaults to one cycle per "
                "resource, not to latency; set release explicitly if the resource "
                "is occupied longer",
                "ProcWriteResources.ReleaseAtCycles",
            )
        )
    return out


# ---------------------------------------------------------------------- STI
def _sti_decl(decl: STIDecl, predicates: dict[str, PredicateDecl]) -> list[Finding]:
    out: list[Finding] = []
    where = f"sti_decl {decl.name}"
    expand = decl.fields.get("expand_for_mc", True)
    default = decl.fields.get("default")
    if expand and default is not None:
        atoms = _degraded(_atoms(default, predicates))
        if atoms:
            out.append(
                _f(
                    WARNING,
                    where,
                    f"default uses {', '.join(atoms)}; the MC version of this "
                    "predicate expands them to false",
                    "STIPredicateDecl.ExpandForMC",
                )
            )
    return out


def _sti_function(
    decl: STIFunction,
    sti_decls: dict[str, STIDecl],
    predicates: dict[str, PredicateDecl],
) -> list[Finding]:
    out: list[Finding] = []
    where = f"sti_function {decl.name}"
    target = sti_decls.get(decl.decl)
    expand = target.fields.get("expand_for_mc", True) if target else True
    mask_declared = bool(target and target.fields.get("opcode_mask"))
    mask_fed = False

    for item in decl.items:
        if item["kind"] not in ("equiv_class", "dep_breaking_class"):
            continue
        label = f"{where}.{item['name']}"
        atoms = _degraded(_atoms(item["pred"], predicates))
        if expand and atoms:
            out.append(
                _f(
                    WARNING,
                    label,
                    f"predicate uses {', '.join(atoms)}; the MC version expands "
                    "them to false, so llvm-mca will not see this class. "
                    "Set expand_for_mc = false or use a portable predicate",
                    "STIPredicateDecl.ExpandForMC",
                )
            )
        operands = item.get("operands")
        if item["kind"] == "dep_breaking_class":
            if operands:
                mask_fed = True
            else:
                out.append(
                    _f(
                        NOTE,
                        label,
                        "empty broken-dependency list means every explicit input "
                        "operand is independent",
                        "InstructionEquivalenceClass.OperandIndices",
                    )
                )

    if mask_declared and not mask_fed:
        out.append(
            _f(
                NOTE,
                where,
                "opcode_mask only receives bits from a dep_breaking_class with "
                "explicit operand indices; otherwise the mask is cleared",
                "STIPredicateDecl.UpdatesOpcodeMask",
            )
        )
    return out


def _tii(decl: TIIPredicate, predicates: dict[str, PredicateDecl]) -> list[Finding]:
    atoms: set[str] = set()

    def walk(stmt: dict) -> None:
        if stmt["kind"] == "return":
            atoms.update(_atoms(stmt["expr"], predicates))
            return
        for case in stmt.get("cases", []):
            walk(case["body"])
        walk(stmt["default"])

    walk(decl.statement)
    degraded = _degraded(atoms)
    if not degraded:
        return []
    return [
        _f(
            WARNING,
            f"tii_predicate {decl.name}",
            f"body uses {', '.join(degraded)}; the generated MC helper expands "
            "them to false, so only MachineInstr callers see them",
            "TIIPredicate.Body",
        )
    ]


# ----------------------------------------------------------------- features
def _feature(decl: FeatureDecl) -> list[Finding]:
    fields = decl.fields
    kind = fields.get("kind", "plain")
    where = f"feature {decl.name}"
    out: list[Finding] = []
    if kind == "extension" and ("version" in fields or "bitmask" in fields):
        out.append(
            _f(
                NOTE,
                where,
                "extension version and bitmask live in RISCVISAInfo/TargetParser; "
                "MC sees only the boolean feature bit",
                "RISCVExtension.MajorVersion",
            )
        )
    if kind == "tune" and (
        "positive_directive" in fields or "negative_directive" in fields
    ):
        out.append(
            _f(
                NOTE,
                where,
                "tune directives are consumed by the clang driver/TargetParser, "
                "not by RISCVSubtarget",
                "RISCVTuneFeature.PositiveDirectiveName",
            )
        )
    return out


# ------------------------------------------------------------------ driver
def diagnose(program: Program) -> list[Finding]:
    out: list[Finding] = []
    predicates = {
        d.name: d for d in program.decls if isinstance(d, PredicateDecl)
    }
    sti_decls = {d.name: d for d in program.decls if isinstance(d, STIDecl)}
    for decl in program.decls:
        if isinstance(decl, MachineDecl) and decl.alias is None:
            out.extend(_machine(decl, predicates))
        elif isinstance(decl, STIDecl):
            out.extend(_sti_decl(decl, predicates))
        elif isinstance(decl, STIFunction):
            out.extend(_sti_function(decl, sti_decls, predicates))
        elif isinstance(decl, TIIPredicate):
            out.extend(_tii(decl, predicates))
        elif isinstance(decl, PredicateProlog):
            out.append(
                _f(
                    NOTE,
                    "predicate_prolog",
                    "prolog code is spliced only into resolveSchedClass; "
                    "its symbols are unavailable to the MC variant resolver",
                    "PredicateProlog.Code",
                )
            )
        elif isinstance(decl, FeatureDecl):
            out.extend(_feature(decl))
    return out


def main() -> int:
    import argparse
    import sys

    if __package__ in (None, ""):
        from parser import parse_file  # type: ignore
    else:
        from .parser import parse_file

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--verbose", action="store_true", help="include notes")
    args = ap.parse_args()
    program = parse_file(args.input)
    findings = diagnose(program)
    shown = [
        finding
        for finding in findings
        if args.verbose or finding.level != NOTE
    ]
    for line in render_findings(shown):
        print(line, file=sys.stderr)
    return 1 if any(f.level == ERROR for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
