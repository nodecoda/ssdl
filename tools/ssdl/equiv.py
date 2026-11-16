#!/usr/bin/env python3
"""Compare the scheduling records of two ``llvm-tblgen -print-records`` dumps.

The in-tree side is the ground truth: a dump of the target's TableGen sources.
The generated side is the output of ``emit_td.py`` for the SSDL transcription
of the same model.  Records are matched by the scheduling key that survives
renaming (``WriteType``, ``ReadType``, ``MatchRW``, resource name), never by
record name alone.

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

from ssdl.records import (  # noqa: E402
    RecordFile,
    as_bit,
    as_int,
    as_list,
    load,
    unquote,
)


# ---------------------------------------------------------------- predicates
def mc_canon(recs: RecordFile, name: str) -> Any:
    c = recs.cls.get(name, "")
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
    return ("?" + c, tuple(sorted(recs.body.get(name, ()))))


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
    mc = v("MCInstPredicate Pred")
    if mc:
        return ("mc", mc_canon(recs, mc))
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


def write_props(recs: RecordFile, name: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prefix, key in WRITE_PROPS:
        value = recs.value(name, prefix)
        if key in ("latency", "uops"):
            out[key] = as_int(value, 1)
        elif key in ("release", "acquire"):
            out[key] = tuple(as_int(x) for x in as_list(value))
        elif key == "resources":
            out[key] = as_list(value)
        else:
            out[key] = as_bit(value)
    return out


READ_PROPS = (
    ("int Cycles", "cycles"),
    ("list<SchedWrite> ValidWrites", "writes"),
    ("list<int> CycleTunables", "tunables"),
    ("bit Unsupported", "unsupported"),
)


def read_props(recs: RecordFile, name: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prefix, key in READ_PROPS:
        value = recs.value(name, prefix)
        if key == "cycles":
            out[key] = as_int(value)
        elif key in ("writes", "tunables"):
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


def canon_name(name: str, prefix: str | None = None) -> str:
    """Strip names that are not part of the scheduling contract.

    ``defm`` and anonymous defs put the instance prefix and a counter into
    record names; neither carries meaning.  The emitter also renames a
    variant subject's own binding to ``<machine>_WRes_<write>`` because an
    aliased subject cannot carry a WriteRes.
    """
    if prefix and name.startswith(prefix):
        name = name[len(prefix):]
    return WRES_RE.sub("", ANON_RE.sub("", name))


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


def build(recs: RecordFile, model: str, prefix: str | None = None) -> Model:
    out = Model()
    bound: set[str] = set()
    for name in recs.scoped_to(model):
        if recs.value(name, "SchedWrite WriteType") is not None:
            key = canon_name(recs.value(name, "SchedWrite WriteType"), prefix)
            if recs.cls.get(name) == "WriteRes":
                # A binding outranks the bare subject declaration: the emitter
                # splits an aliased subject into ``X`` plus
                # ``<machine>_WRes_X``, and the latter carries the payload.
                out.writes[key] = write_props(recs, name)
                bound.add(key)
            elif key not in bound:
                out.writes[key] = write_props(recs, name)
        elif recs.cls.get(name) == "SchedWriteRes":
            key = canon_name(name, prefix)
            out.writes[key] = write_props(recs, name)
            bound.add(key)
        if recs.value(name, "SchedRead ReadType") is not None:
            out.reads[recs.value(name, "SchedRead ReadType")] = read_props(recs, name)
        elif recs.cls.get(name) == "SchedReadAdvance":
            out.reads[canon_name(name, prefix)] = read_props(recs, name)
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
            # SchedVars.
            out.variants[canon_name(match, prefix)] = (recs.cls.get(variant, ""), tuple(cases))
        if recs.value(name, "dag Instrs") is not None:
            rws = as_list(recs.value(name, "list<SchedReadWrite> OperandReadWrites"))
            out.instrw[(recs.value(name, "dag Instrs"), rws)] = (
                as_bit(recs.value(name, "bit Unsupported")),
            )
        units = recs.value(name, "int NumUnits")
        if units is not None:
            out.resources[name] = (
                as_int(units),
                as_int(recs.value(name, "int BufferSize"), -1),
                recs.value(name, "ProcResourceKind Super"),
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
    ap.add_argument("--gen-processor", help="generated ProcessorModel def to compare")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument(
        "--strip-prefix",
        default=None,
        help="instance prefix the in-tree defm put on record names, e.g. VLEN512SiFive7",
    )
    args = ap.parse_args(argv)

    intree_recs = load(args.intree)
    generated_recs = load(args.generated)
    intree_cpu = (
        build_processor(intree_recs, args.in_processor) if args.in_processor else None
    )
    generated_cpu = (
        build_processor(generated_recs, args.gen_processor) if args.gen_processor else None
    )
    if (intree_cpu is None) != (generated_cpu is None):
        ap.error("--in-processor and --gen-processor must be given together")
    return compare(
        build(intree_recs, args.in_model, args.strip_prefix),
        build(generated_recs, args.gen_model),
        args.limit,
        intree_cpu,
        generated_cpu,
    )


if __name__ == "__main__":
    sys.exit(main())
