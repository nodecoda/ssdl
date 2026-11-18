#!/usr/bin/env python3
"""Compare the scheduling records of two ``llvm-tblgen -print-records`` dumps.

The in-tree side is the ground truth: a dump of the target's TableGen sources.
The generated side is the output of ``emit_td.py`` for the SSDL transcription
of the same model.  It is either a ``-print-records`` dump or the record table
itself (``--records-json``, detected by the ``.json`` suffix); the latter skips
the TableGen round-trip.  Records are matched by the scheduling key that
survives renaming
(``WriteType``, ``ReadType``, ``MatchRW``, resource name), never by record name
alone.  ``defm`` and the SSDL multi-instance lowering both put an instance
prefix on record names, so either side may pass the prefixes that are not part
of the contract (``--strip-prefix`` for the in-tree dump, ``--gen-strip-prefix``
for the generated one); both the keys and the cross-references inside them are
normalized before comparison.

Usage:

    equiv.py intree.rec generated.rec --in-model SiFive7VLEN512Model \
        --gen-model sifive_7_seriesModel \
        --in-processor SIFIVE_7 --gen-processor sifive_7_series
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssdl import expand_records, paths  # noqa: E402
from ssdl.records import (  # noqa: E402
    RecordFile,
    as_bit,
    as_int,
    as_list,
    load,
    load_json,
    unquote,
)


# ---------------------------------------------------------------- predicates
MC_KINDS = {
    "CheckRegOperand",
    "CheckOpcode",
    "CheckAll",
    "AllOf",
    "CheckAny",
    "AnyOf",
    "CheckNot",
    "Not",
    "MCTrue",
    "MCFalse",
    "CheckIsRegOperand",
    "CheckIsImmOperand",
    "CheckSameRegOperand",
    "CheckNumOperands",
    "CheckValidRegOperand",
    "CheckInvalidRegOperand",
    "CheckImmOperand",
    "CheckImmOperand_s",
    "CheckImmOperandRange",
    "CheckZeroOperand",
    "CheckFunctionPredicate",
    "CheckFunctionPredicateWithTII",
    "CheckNonPortable",
    "TIIPredicate",
}


def mc_canon(recs: RecordFile, name: str) -> Any:
    # ``TruePred``/``FalsePred`` live in TargetInstrPredicate.td; a generated
    # records-json table names them but does not define them.
    if name == "TruePred":
        return ("true",)
    if name == "FalsePred":
        return ("false",)
    c = recs.kind(name, MC_KINDS) or recs.cls.get(name, "")
    v = lambda p: recs.value(name, p)  # noqa: E731
    if c == "CheckRegOperand":
        return ("reg", as_int(v("int OpIndex")), v("Register Reg"))
    if c == "CheckOpcode":
        ops = as_list(v("list<Instruction> ValidOpcodes"))
        if not ops:
            ops = as_list(v("list<Instruction> Opcodes"))
        return ("opcode", ops)
    if c in ("CheckAll", "AllOf"):
        return ("all", tuple(mc_canon(recs, x) for x in as_list(v("list<MCInstPredicate> Predicates"))))
    if c in ("CheckAny", "AnyOf"):
        return ("any", tuple(mc_canon(recs, x) for x in as_list(v("list<MCInstPredicate> Predicates"))))
    if c in ("CheckNot", "Not"):
        return ("not", mc_canon(recs, v("MCInstPredicate Pred")))
    if c == "MCTrue":
        return ("true",)
    if c == "MCFalse":
        return ("false",)
    mapper = unquote(v("string FunctionMapper")) or ""
    if c == "CheckIsRegOperand":
        return ("is_reg", as_int(v("int OpIndex")))
    if c == "CheckIsImmOperand":
        return ("is_imm", as_int(v("int OpIndex")))
    if c == "CheckSameRegOperand":
        return (
            "same_reg",
            as_int(v("int FirstIndex")),
            as_int(v("int SecondIndex")),
        )
    if c == "CheckNumOperands":
        return ("num_ops", as_int(v("int NumOps")))
    if c == "CheckValidRegOperand":
        return ("valid_reg", as_int(v("int OpIndex")))
    if c == "CheckInvalidRegOperand":
        return ("invalid_reg", as_int(v("int OpIndex")))
    if c == "CheckZeroOperand":
        return ("imm", as_int(v("int OpIndex")), 0, mapper)
    if c == "CheckImmOperand":
        return ("imm", as_int(v("int OpIndex")), as_int(v("int ImmVal")), mapper)
    if c == "CheckImmOperand_s":
        return ("imm_enum", as_int(v("int OpIndex")), v("string ImmVal"), mapper)
    if c == "CheckImmOperandRange":
        return (
            "imm_range",
            as_int(v("int OpIndex")),
            as_int(v("int StartVal")),
            as_int(v("int EndVal")),
            mapper,
        )
    if c == "CheckFunctionPredicate":
        return ("func", v("string MCInstFnName"), v("string MachineInstrFnName"))
    if c == "CheckFunctionPredicateWithTII":
        return (
            "func_tii",
            v("string MCInstFnName"),
            v("string MachineInstrFnName"),
            unquote(v("string TIIPtrName")),
        )
    if c == "CheckNonPortable":
        code = recs.block(name, "string CodeBlock") or ""
        return ("non_portable", " ".join(code.split()))
    if c == "TIIPredicate":
        return (
            "tii",
            unquote(v("string FunctionName")),
            stmt_canon(recs, v("MCStatement Body")),
        )
    return ("?" + c, tuple(sorted(recs.body.get(name, ()))))


def stmt_canon(recs: RecordFile, name: str | None) -> Any:
    """Canonical form of an ``MCStatement`` (TII predicate bodies)."""
    if not name:
        return None
    c = recs.kind(name, {"MCReturnStatement", "MCOpcodeSwitchStatement",
                         "MCTrue", "MCFalse"}) or recs.cls.get(name, "")
    if c == "MCReturnStatement":
        return ("return", mc_canon(recs, recs.value(name, "MCInstPredicate Pred")))
    if c == "MCOpcodeSwitchStatement":
        cases = []
        for case in as_list(recs.value(name, "list<MCOpcodeSwitchCase> Cases")):
            ops = as_list(recs.value(case, "list<Instruction> Opcodes"))
            stmt = stmt_canon(recs, recs.value(case, "MCStatement CaseStmt"))
            cases.append((ops, stmt))
        default = stmt_canon(recs, recs.value(name, "MCStatement DefaultCase"))
        return ("switch", tuple(cases), default)
    if c in ("MCTrue", "MCFalse"):
        return ("return", ("true",) if c == "MCTrue" else ("false",))
    return ("?" + c, tuple(sorted(recs.body.get(name, ()))))


# A SchedWriteRes and a WriteRes (or SchedReadAdvance and ReadAdvance) are the
# same scheduling object; the SSDL lowering splits ``write W; bind W`` into two
# records, so the class is compared at the binding level, not verbatim.
_BINDING_CLASS = {
    "SchedWriteRes": "WriteRes",
    "WriteRes": "WriteRes",
    "SchedReadAdvance": "ReadAdvance",
    "ReadAdvance": "ReadAdvance",
    # A target may subclass a variant to carry extra fields; it is still the
    # same scheduling object (C1's ``C1NanoStreamingSchedWrite``).
    "SchedWriteVariant": "SchedWriteVariant",
    "SchedReadVariant": "SchedReadVariant",
}


def rw_kind(recs: RecordFile, name: str) -> str:
    cls = recs.kind(name, set(_BINDING_CLASS)) or recs.cls.get(name, "")
    return _BINDING_CLASS.get(cls, cls)


def distribute_mc(node: Any) -> Any:
    """Push an ``MCSchedPredicate`` wrapper into a predicate combinator.

    ``MCSchedPredicate<AnyOf<A, B>>`` and
    ``AnyOfSchedPreds<MCSchedPredicate<A>, MCSchedPredicate<B>>`` expand to the
    same C++ expression; SSDL writes the latter because ``all_of``/``any_of``
    are its scheduling combinators.
    """
    if not isinstance(node, tuple) or not node:
        return node
    if node[0] == "mc":
        inner = node[1]
        if isinstance(inner, tuple) and inner and inner[0] in ("any", "all"):
            return (inner[0], tuple(distribute_mc(("mc", x)) for x in inner[1]))
        if isinstance(inner, tuple) and inner and inner[0] == "not":
            return ("not", distribute_mc(("mc", inner[1])))
        return node
    if node[0] in ("any", "all"):
        return (node[0], tuple(distribute_mc(x) for x in node[1]))
    if node[0] == "not":
        return ("not", distribute_mc(node[1]))
    return node


def pred_canon(recs: RecordFile, name: str | None) -> Any:
    if not name or name == "NoSchedPred":
        return None
    c = recs.cls.get(name, "")
    v = lambda p: recs.value(name, p)  # noqa: E731
    kids = as_list(v("list<SchedPredicateBase> Predicates"))
    if kids:
        inner = tuple(pred_canon(recs, x) for x in kids)
        if c == "NotSchedPred":
            return ("not", inner[0])
        if c == "NoneOfSchedPreds":
            return ("none", inner)
        if c == "AnyOfSchedPreds":
            return ("any", inner)
        return ("all", inner)
    feature = v("SubtargetFeature Feature")
    if feature:
        return ("feature", feature)
    if c == "SchedPredicate":
        code = recs.block(name, "code Predicate") or ""
        return ("sched", " ".join(code.split()))
    mc = v("MCInstPredicate Pred")
    if mc:
        return distribute_mc(("mc", mc_canon(recs, mc)))
    return ("?" + c, tuple(sorted(recs.body.get(name, ()))))


# ---------------------------------------------------------------- scheduling
WRITE_PROPS = (
    ("list<ProcResourceKind> ProcResources", "resources"),
    ("int Latency", "latency"),
    ("int NumMicroOps", "uops"),
    ("list<int> ReleaseAtCycles", "release"),
    ("list<int> AcquireAtCycles", "acquire"),
    ("bit BeginGroup", "begin_group"),
    ("bit EndGroup", "end_group"),
    ("bit SingleIssue", "single"),
    ("bit RetireOOO", "retire_ooo"),
    ("bit Unsupported", "unsupported"),
)


def write_props(recs: RecordFile, name: str, prefix=None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, key in WRITE_PROPS:
        value = recs.value(name, field)
        if key in ("latency", "uops"):
            out[key] = as_int(value, 1)
        elif key in ("release", "acquire"):
            out[key] = tuple(as_int(x) for x in as_list(value))
        elif key == "resources":
            out[key] = tuple(strip_prefix(item, prefix) for item in as_list(value))
        else:
            out[key] = as_bit(value)
    return out


READ_PROPS = (
    ("int Cycles", "cycles"),
    ("list<SchedWrite> ValidWrites", "writes"),
    ("list<int> CycleTunables", "tunables"),
    ("bit Unsupported", "unsupported"),
)


def read_props(recs: RecordFile, name: str, prefix=None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, key in READ_PROPS:
        value = recs.value(name, field)
        if key == "cycles":
            out[key] = as_int(value)
        elif key == "writes":
            out[key] = tuple(strip_prefix(item, prefix) for item in as_list(value))
        elif key == "tunables":
            out[key] = as_list(value)
        else:
            out[key] = as_bit(value)
    return out


@dataclass
class Model:
    writes: dict[str, dict[str, Any]] = field(default_factory=dict)
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)
    variants: dict[str, tuple] = field(default_factory=dict)
    instrw: dict[tuple, tuple] = field(default_factory=dict)
    resources: dict[str, tuple] = field(default_factory=dict)
    props: dict[str, Any] = field(default_factory=dict)
    unsupported_features: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{len(self.writes)} writes, {len(self.reads)} reads, "
            f"{len(self.variants)} variants, {len(self.instrw)} InstRW, "
            f"{len(self.resources)} resources"
        )


ANON_RE = re.compile(r"^.*?anonymous_\d+")
WRES_RE = re.compile(r"^.*?_WRes_")
# The emitter splits a variant subject's own binding off the subject:
# ``<machine>_WRes_<subject>`` / ``<machine>_RA_<read>`` /
# ``<machine>_RAdv_<read>``.  Upstream keeps one record (a SchedWriteRes or a
# SchedReadAdvance) whose name is the subject itself.
RADV_RE = re.compile(r"^(?:.*?_)?(?:RAdv|RA)_")


def _prefixes(prefix) -> tuple[str, ...]:
    if prefix is None:
        return ()
    if isinstance(prefix, str):
        return (prefix,)
    return tuple(item for item in prefix if item)


def strip_prefix(name: str, prefix=None) -> str:
    """Drop any known instance prefix.

    ``defm`` names every record ``<instance><base>``; the SSDL multi-instance
    lowering does the same for model-scoped defs.  The prefix is routing
    information, not part of the scheduling contract, so both sides may list
    the prefixes that should not participate in the comparison.
    """
    for item in sorted(_prefixes(prefix), key=len, reverse=True):
        if not name.startswith(item):
            continue
        rest = name[len(item) :]
        # Upstream ``defm`` concatenates without a separator; the SSDL
        # multi-instance lowering joins with ``_``.  Fold both to one spelling.
        if rest.startswith("_") and not item.endswith("_"):
            rest = rest[1:]
        return rest
    return name


def canon_name(name: str, prefix=None) -> str:
    """Strip names that are not part of the scheduling contract.

    ``defm`` and anonymous defs put the instance prefix and a counter into
    record names; neither carries meaning.  The emitter also renames a
    variant subject's own binding to ``<machine>_WRes_<write>`` because an
    aliased subject cannot carry a WriteRes.
    """
    return RADV_RE.sub("", WRES_RE.sub("", ANON_RE.sub("", strip_prefix(name, prefix))))


# (``-print-records`` field, SSDL property, TableGen default).  Defaults come
# from ``SchedMachineModel`` in ``TargetSchedule.td``: an omitted field and an
# explicit default are the same machine model.
MODEL_PROPS = (
    ("int IssueWidth", "issue_width", -1),
    ("int MicroOpBufferSize", "micro_op_buffer_size", -1),
    ("int LoopMicroOpBufferSize", "loop_micro_op_buffer", -1),
    ("int LoadLatency", "load_latency", -1),
    ("int HighLatency", "high_latency", -1),
    ("int MispredictPenalty", "mispredict_penalty", -1),
    ("bit PostRAScheduler", "post_ra_scheduler", False),
    ("bit CompleteModel", "complete_model", True),
    ("bit FullInstRWOverlapCheck", "full_inst_rw_overlap_check", True),
    ("bit EnableIntervals", "enable_intervals", False),
    ("bit NoModel", "no_model", False),
)


def build(recs: RecordFile, model: str, prefix=None) -> Model:
    """Build the comparable model; ``prefix`` is a name or a prefix sequence."""
    out = Model()
    bound: set[str] = set()
    used_resources: set[str] = set()
    scoped = list(recs.scoped_to(model))
    alias_targets: set[str] = set()
    for name in scoped:
        if recs.value(name, "SchedReadWrite MatchRW") is not None:
            alias_targets.add(recs.value(name, "SchedReadWrite AliasRW"))
    for name in scoped:
        if recs.cls.get(name) in ("WriteRes", "SchedWriteRes"):
            used_resources.update(
                strip_prefix(item, prefix)
                for item in as_list(recs.value(name, "list<ProcResourceKind> ProcResources"))
            )
    # A bare SchedWrite that has a WriteRes binding somewhere in this model is
    # a binding in every respect; upstream often writes it as one record of a
    # SchedWriteRes subclass and then aliases the vocabulary write to it.
    bound_writes: set[str] = set()
    bound_reads: set[str] = set()
    for name in scoped:
        write_type = recs.value(name, "SchedWrite WriteType")
        if write_type is not None and recs.value(name, "int Latency") is not None:
            bound_writes.add(write_type)
        read_type = recs.value(name, "SchedRead ReadType")
        if read_type is not None and recs.value(name, "int Cycles") is not None:
            bound_reads.add(read_type)
    for name in scoped:
        if recs.value(name, "SchedWrite WriteType") is not None:
            key = canon_name(recs.value(name, "SchedWrite WriteType"), prefix)
            # A binding (any WriteRes subclass) outranks the bare subject
            # declaration: the emitter splits an aliased subject into ``X``
            # plus ``<machine>_WRes_X``, and the latter carries the payload.
            if recs.value(name, "int Latency") is not None:
                out.writes[key] = write_props(recs, name, prefix)
                bound.add(key)
            elif key not in bound:
                out.writes[key] = write_props(recs, name, prefix)
        elif recs.value(name, "int Latency") is not None:
            key = canon_name(name, prefix)
            out.writes[key] = write_props(recs, name, prefix)
            bound.add(key)
        if recs.value(name, "SchedRead ReadType") is not None:
            out.reads[recs.value(name, "SchedRead ReadType")] = read_props(recs, name, prefix)
        elif recs.cls.get(name) == "SchedReadAdvance":
            out.reads[canon_name(name, prefix)] = read_props(recs, name, prefix)
        if recs.value(name, "SchedReadWrite MatchRW") is not None:
            match = recs.value(name, "SchedReadWrite MatchRW")
            variant = recs.value(name, "SchedReadWrite AliasRW")
            cases = []
            for var in as_list(recs.value(variant, "list<SchedVar> Variants")):
                pred = pred_canon(recs, recs.value(var, "SchedPredicateBase Predicate"))
                selected = tuple(
                    canon_name(sel, prefix)
                    for sel in as_list(recs.value(var, "list<SchedReadWrite> Selected"))
                )
                cases.append((pred, selected))
            # Include the target class so an alias to a variant cannot look
            # like an alias to a WriteSequence just because both have no
            # SchedVars.  An alias to a *binding* (upstream's one-record
            # SchedWriteRes / SchedReadAdvance) is the same schedule as an
            # alias to the bare write/read that this model binds separately.
            alias_kind = rw_kind(recs, variant)
            if alias_kind == "SchedWrite" and variant in bound_writes:
                alias_kind = "WriteRes"
            elif alias_kind == "SchedRead" and variant in bound_reads:
                alias_kind = "ReadAdvance"
            out.variants[canon_name(match, prefix)] = (alias_kind, tuple(cases))
        elif (
            recs.kind(name, {"SchedWriteVariant", "SchedReadVariant"}) is not None
            and name not in alias_targets
        ):
            cases = []
            for var in as_list(recs.value(name, "list<SchedVar> Variants")):
                pred = pred_canon(recs, recs.value(var, "SchedPredicateBase Predicate"))
                selected = tuple(
                    canon_name(sel, prefix)
                    for sel in as_list(recs.value(var, "list<SchedReadWrite> Selected"))
                )
                cases.append((pred, selected))
            out.variants[canon_name(name, prefix)] = (
                recs.kind(name, {"SchedWriteVariant", "SchedReadVariant"}) or "",
                tuple(cases),
            )
        if recs.value(name, "dag Instrs") is not None:
            rws = tuple(
                canon_name(item, prefix)
                for item in as_list(recs.value(name, "list<SchedReadWrite> OperandReadWrites"))
            )
            out.instrw[(recs.value(name, "dag Instrs"), rws)] = (
                as_bit(recs.value(name, "bit Unsupported")),
            )
        units = recs.value(name, "int NumUnits")
        if units is not None:
            out.resources[strip_prefix(name, prefix)] = (
                as_int(units),
                as_int(recs.value(name, "int BufferSize"), -1),
                strip_prefix(recs.value(name, "ProcResourceKind Super"), prefix),
            )
    # Target-level resources are not scoped; include the ones this model's
    # writes actually name.
    for name in recs.body:
        key = strip_prefix(name, prefix)
        if key in out.resources:
            continue
        units = recs.value(name, "int NumUnits")
        if units is None or key not in used_resources:
            continue
        out.resources[key] = (
            as_int(units),
            as_int(recs.value(name, "int BufferSize"), -1),
            strip_prefix(recs.value(name, "ProcResourceKind Super"), prefix),
        )
    # The machine model def itself does not carry ``SchedModel = ...``; it is
    # the record every other entry points at.
    if model in recs.body:
        for prefix, key, default in MODEL_PROPS:
            value = recs.value(model, prefix)
            if value is None:
                out.props[key] = default
            else:
                out.props[key] = as_int(value) if prefix.startswith("int") else as_bit(value)
        out.unsupported_features = as_list(
            recs.value(model, "list<Predicate> UnsupportedFeatures")
        )
    return out


# ---------------------------------------------------------------- processor
@dataclass
class Processor:
    name: str
    sched_model: str
    features: tuple[str, ...]
    tune_features: tuple[str, ...]
    default_march: str
    itinerary: str

    def summary(self) -> str:
        return (
            f"{self.name!r} -> {self.sched_model}, "
            f"{len(self.features)} features, {len(self.tune_features)} tune features"
        )


def feature_names(recs: RecordFile, names: tuple[str, ...]) -> tuple[str, ...]:
    """Feature identity is the ``-mattr`` name, not the def name."""
    out = []
    for name in names:
        value = recs.value(name, "string Name")
        out.append(unquote(value) if value is not None else name)
    return tuple(out)


def build_processor(recs: RecordFile, name: str) -> Processor:
    # SSDL identifiers are ``[A-Za-z_]\w*``, so a host processor name such as
    # ``sifive-7-series`` can only be spelled ``sifive_7_series``.  Fold the
    # separator on both sides; the processor record itself is still selected
    # by ``--in-processor``/``--gen-processor``.
    return Processor(
        name=unquote(recs.value(name, "string Name")).replace("-", "_"),
        sched_model=recs.value(name, "SchedMachineModel SchedModel") or "",
        features=feature_names(recs, as_list(recs.value(name, "list<SubtargetFeature> Features"))),
        tune_features=feature_names(
            recs, as_list(recs.value(name, "list<SubtargetFeature> TuneFeatures"))
        ),
        default_march=unquote(recs.value(name, "string DefaultMarch")),
        itinerary=recs.value(name, "ProcessorItineraries ProcItin") or "",
    )


# ---------------------------------------------------------------- comparison
def diff(where: str, a: Any, b: Any, diffs: list[str], limit: int) -> None:
    if a == b:
        return
    if len(diffs) < limit:
        diffs.append(f"{where}:\n  intree: {a}\n  generated: {b}")
    else:
        diffs.append("")


def compare(
    intree: Model,
    generated: Model,
    limit: int = 20,
    intree_cpu: Processor | None = None,
    generated_cpu: Processor | None = None,
) -> int:
    diffs: list[str] = []
    for kind, left, right in (
        ("write", intree.writes, generated.writes),
        ("read", intree.reads, generated.reads),
        ("variant", intree.variants, generated.variants),
        ("resource", intree.resources, generated.resources),
    ):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                diffs.append(f"{kind} {key}: only in generated")
            elif key not in right:
                diffs.append(f"{kind} {key}: only in intree")
            else:
                diff(f"{kind} {key}", left[key], right[key], diffs, limit)
    for key in sorted(intree.props):
        diff(f"machine prop {key}", intree.props[key], generated.props.get(key), diffs, limit)
    if set(intree.instrw) != set(generated.instrw):
        diffs.append("InstRW sets differ")

    intree_cpu_text = ""
    if intree_cpu is not None and generated_cpu is not None:
        for field_name in ("name", "features", "tune_features", "default_march", "itinerary"):
            diff(
                f"processor {field_name}",
                getattr(intree_cpu, field_name),
                getattr(generated_cpu, field_name),
                diffs,
                limit,
            )
        intree_cpu_text = f", {intree_cpu.summary()}"

    print(f"intree:    {intree.summary()}{intree_cpu_text}")
    print(f"generated: {generated.summary()}{intree_cpu_text if intree_cpu_text else ''}")
    print(
        "scope:     writes, reads, variants, InstRW, resources, machine props"
        + (", processor name/features/tunes/march/itinerary" if intree_cpu else "")
    )
    if intree.unsupported_features:
        print(
            "note:      UnsupportedFeatures = "
            f"[{', '.join(intree.unsupported_features)}] is not compared: "
            "its predicates belong to the host target, not to the SSDL model"
        )
    if diffs:
        print(f"MISMATCH ({len(diffs)} entries shown)")
        for text in diffs:
            if text:
                print(text)
        return 1
    print("records match")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("intree", help="records dump of the in-tree model")
    ap.add_argument("generated", help="records dump of the generated model")
    ap.add_argument("--in-model", required=True)
    ap.add_argument("--gen-model", required=True)
    ap.add_argument("--in-processor", help="in-tree ProcessorModel def to compare")
    ap.add_argument(
        "--in-mcpu",
        help="in-tree ProcessorModel selected by its -mcpu name (AArch64 "
        "processor defs are anonymous)",
    )
    ap.add_argument("--gen-processor", help="generated ProcessorModel def to compare")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument(
        "--strip-prefix",
        action="append",
        default=None,
        help="instance prefix the in-tree defm put on record names, e.g. "
        "VLEN512SiFive7 (repeatable)",
    )
    ap.add_argument(
        "--gen-strip-prefix",
        action="append",
        default=None,
        help="instance prefix the SSDL multi-instance lowering put on generated "
        "record names, e.g. small (repeatable)",
    )
    ap.add_argument(
        "--target",
        choices=("riscv", "aarch64"),
        default="riscv",
        help="target whose TD schema expands --records-json (default: riscv)",
    )
    ap.add_argument(
        "--llvm-src",
        default=None,
        help="LLVM checkout used to expand a generated records-json table "
        "(default: $SSDL_LLVM_SRC)",
    )
    args = ap.parse_args(argv)

    intree_recs = load(args.intree)
    if args.generated.endswith(".json"):
        src = args.llvm_src or paths.llvm_src()
        if src is None:
            print(
                "equiv: --llvm-src or SSDL_LLVM_SRC is required to expand a "
                "records-json table",
                file=sys.stderr,
            )
            return 2
        generated_recs = expand_records.expand_table(
            args.generated, expand_records.schema_for_target(src, args.target)
        )
    else:
        generated_recs = load(args.generated)
    if args.in_processor and args.in_mcpu:
        ap.error("--in-processor and --in-mcpu are mutually exclusive")
    intree_processor = args.in_processor
    if args.in_mcpu is not None:
        matches = [
            name
            for name, cls in intree_recs.cls.items()
            if cls == "ProcessorModel"
            and unquote(intree_recs.value(name, "string Name")) == args.in_mcpu
        ]
        if len(matches) != 1:
            print(
                f"equiv: {args.in_mcpu!r} names {len(matches)} in-tree processors",
                file=sys.stderr,
            )
            return 2
        intree_processor = matches[0]
    intree_cpu = (
        build_processor(intree_recs, intree_processor) if intree_processor else None
    )
    generated_cpu = (
        build_processor(generated_recs, args.gen_processor) if args.gen_processor else None
    )
    if (intree_cpu is None) != (generated_cpu is None):
        ap.error("--in-processor and --gen-processor must be given together")
    return compare(
        build(intree_recs, args.in_model, args.strip_prefix),
        build(generated_recs, args.gen_model, args.gen_strip_prefix),
        args.limit,
        intree_cpu,
        generated_cpu,
    )


if __name__ == "__main__":
    sys.exit(main())
