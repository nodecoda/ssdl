#!/usr/bin/env python3
"""Generate an SSDL machine from ``llvm-tblgen -print-records`` output.

SSDL has no metaprogramming (``foreach``/``constructor``) *inside the
language*, by design: the expanded declarations are the artifact.  Producing
those declarations from the target's own TableGen records is an external
generator, which the spec explicitly allows.  This file is that generator.

It is a transcriber, not an optimiser: every model-scoped record becomes the
SSDL form that ``emit_td.py`` lowers back to a record with the same
scheduling identity.  The output is **IR text**, the internal layer: read it
with ``--syntax ir``, or run ``to_v2.py`` to get a surface source.  ``equiv.py`` checks that round trip against the
in-tree dump.

Usage:

    from_records.py riscv.rec --model SiFive7VLEN512Model \
        --processor SIFIVE_7 -o sifive7-vlen512.ssdl

    from_records.py riscv.rec --model SiFive7VLEN512Model \
        --machine sifive-7-series -o sifive7-vlen512.ssdl

``--processor`` also copies the processor's ``features``/``tune_features``
and ``default_march``; ``--machine`` defaults to the processor's ``-mcpu``
name, which is why names with ``-`` are accepted by the parser.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

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

RESERVED_SSDL = {
    "ReadDefault": "read_default",
    "NoReadAdvance": "no_read_advance",
    "NoWrite": "write_none",
}

INLINE = {
    "InlineIgnore": "ignore",
    "InlineDefault": "default",
    "InlineInverse": "inverse",
    "InlineMustMatch": "must_match",
}


class GeneratorError(Exception):
    pass


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


READ_KINDS = {"SchedRead", "SchedReadAdvance", "ReadAdvance", "SchedReadVariant"}
WRITE_KINDS = {
    "SchedWrite",
    "SchedWriteRes",
    "WriteRes",
    "WriteSequence",
    "SchedWriteVariant",
}


def is_read(recs: RecordFile, name: str) -> bool:
    return recs.kind(name, READ_KINDS) is not None or recs.has(
        name, "SchedRead ReadType"
    )


def is_write(recs: RecordFile, name: str) -> bool:
    return recs.kind(name, WRITE_KINDS) is not None or recs.has(
        name, "SchedWrite WriteType"
    )


# ---------------------------------------------------------------- predicates
class Predicates:
    """Render ``SchedPredicateBase``/``MCInstPredicate`` trees as SSDL text."""

    def __init__(self, recs: RecordFile) -> None:
        self.recs = recs
        self.features: list[str] = []
        # TII predicates are named defs, not inline expressions: a scheduling
        # predicate refers to them as mc(Name).  Rendered text is emitted once.
        self.tii_text: dict[str, str] = {}

    def is_tii(self, name: str | None) -> bool:
        return bool(name) and self.recs.kind(name, {"TIIPredicate"}) is not None

    def sched(self, name: str | None) -> str | None:
        if not name or name == "NoSchedPred":
            return None
        recs = self.recs
        cls = recs.cls.get(name, "")
        kids = as_list(recs.value(name, "list<SchedPredicateBase> Predicates"))
        if kids:
            inner = [self.sched(k) for k in kids]
            if cls == "NotSchedPred":
                return f"not({inner[0]})"
            if cls == "NoneOfSchedPreds":
                return "not(" + "any_of(" + ", ".join(inner) + "))"
            if cls == "AnyOfSchedPreds":
                return "any_of(" + ", ".join(inner) + ")"
            return "all_of(" + ", ".join(inner) + ")"
        feature = recs.value(name, "SubtargetFeature Feature")
        if feature:
            self.features.append(feature)
            return f"feature({feature})"
        mc = recs.value(name, "MCInstPredicate Pred")
        if mc:
            if self.is_tii(mc):
                self.want_tii(mc)
                return f"mc({mc})"
            return self.mc(mc)
        if recs.kind(name, {"SchedPredicate"}) is not None:
            code = recs.block(name, "code Predicate")
            if code is None:
                raise GeneratorError(f"SchedPredicate {name} has no code block")
            return f"sched_code({code})"
        raise GeneratorError(f"unsupported scheduling predicate {name} ({cls})")

    # -- TII predicates --------------------------------------------------
    def want_tii(self, name: str) -> None:
        if name in self.tii_text:
            return
        recs = self.recs
        method = unquote(recs.value(name, "string FunctionName")) or name
        body = recs.value(name, "MCStatement Body")
        if body is None:
            raise GeneratorError(f"TIIPredicate {name} has no Body")
        statement = self.statement(body)
        if method == name:
            text = f"tii_predicate {name} = {statement};"
        else:
            text = (
                f"tii_predicate {name} {{ method = {quote(method)}; {statement} }}"
            )
        self.tii_text[name] = text

    def statement(self, name: str) -> str:
        recs = self.recs
        kind = recs.kind(name, {"MCReturnStatement", "MCOpcodeSwitchStatement",
                                "MCTrue", "MCFalse"})
        cls = kind or recs.cls.get(name, "")
        if cls == "MCReturnStatement":
            pred = recs.value(name, "MCInstPredicate Pred")
            if pred is None:
                raise GeneratorError(f"MCReturnStatement {name} has no Pred")
            return f"return {self.mc(pred)};"
        if cls == "MCOpcodeSwitchStatement":
            cases = []
            for case in as_list(recs.value(name, "list<MCOpcodeSwitchCase> Cases")):
                ops = as_list(recs.value(case, "list<Instruction> Opcodes"))
                stmt = recs.value(case, "MCStatement CaseStmt")
                if not ops or stmt is None:
                    raise GeneratorError(f"MCOpcodeSwitchCase {case} is incomplete")
                cases.append(f"case [{', '.join(ops)}]: {self.statement(stmt)}")
            default = recs.value(name, "MCStatement DefaultCase")
            if default is None:
                raise GeneratorError(f"MCOpcodeSwitchStatement {name} has no default")
            cases.append(f"default: {self.statement(default)}")
            return "switch opcode { " + " ".join(cases) + " }"
        if cls in ("MCTrue", "MCFalse"):
            return f"return {'true' if cls == 'MCTrue' else 'false'};"
        raise GeneratorError(f"unsupported MC statement {name} ({cls})")

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
        "CheckImmOperandLT",
        "CheckImmOperandGT",
        "CheckImmOperandLE",
        "CheckImmOperandGE",
        "CheckZeroOperand",
        "CheckFunctionPredicate",
        "CheckFunctionPredicateWithTII",
        "CheckNonPortable",
    }

    def mc(self, name: str) -> str:
        recs = self.recs
        # A TIIPredicate is a named MCInstPredicate: scheduling references it
        # through ``mc(<name>)`` and the def is emitted once, by name.
        if self.is_tii(name):
            self.want_tii(name)
            return f"mc({name})"
        cls = recs.kind(name, self.MC_KINDS) or recs.cls.get(name, "")
        if cls == "CheckRegOperand":
            index = as_int(recs.value(name, "int OpIndex"))
            register = recs.value(name, "Register Reg")
            if register is None:
                raise GeneratorError(f"CheckRegOperand {name} has no register")
            return f"reg_is({index}, {register})"
        if cls == "CheckOpcode":
            ops = as_list(recs.value(name, "list<Instruction> ValidOpcodes"))
            if not ops:
                ops = as_list(recs.value(name, "list<Instruction> Opcodes"))
            if not ops:
                raise GeneratorError(f"CheckOpcode {name} has no opcodes")
            return "opcode([" + ", ".join(ops) + "])"
        if cls in ("CheckAll", "AllOf"):
            kids = as_list(recs.value(name, "list<MCInstPredicate> Predicates"))
            return "all_of(" + ", ".join(self.mc(k) for k in kids) + ")"
        if cls in ("CheckAny", "AnyOf"):
            kids = as_list(recs.value(name, "list<MCInstPredicate> Predicates"))
            return "any_of(" + ", ".join(self.mc(k) for k in kids) + ")"
        if cls in ("CheckNot", "Not"):
            return "not(" + self.mc(recs.value(name, "MCInstPredicate Pred")) + ")"
        if cls == "MCTrue":
            return "true"
        if cls == "MCFalse":
            return "false"
        if cls == "CheckIsRegOperand":
            return f"is_reg({as_int(recs.value(name, 'int OpIndex'))})"
        if cls == "CheckIsImmOperand":
            return f"is_imm({as_int(recs.value(name, 'int OpIndex'))})"
        if cls == "CheckSameRegOperand":
            return (
                f"same_reg({as_int(recs.value(name, 'int FirstIndex'))}, "
                f"{as_int(recs.value(name, 'int SecondIndex'))})"
            )
        if cls == "CheckNumOperands":
            return f"num_ops({as_int(recs.value(name, 'int NumOps'))})"
        if cls == "CheckValidRegOperand":
            return f"is_valid_reg({as_int(recs.value(name, 'int OpIndex'))})"
        if cls == "CheckInvalidRegOperand":
            return f"is_invalid_reg({as_int(recs.value(name, 'int OpIndex'))})"
        if cls in (
            "CheckImmOperand",
            "CheckImmOperand_s",
            "CheckImmOperandRange",
            "CheckImmOperandLT",
            "CheckImmOperandGT",
            "CheckImmOperandLE",
            "CheckImmOperandGE",
            "CheckZeroOperand",
        ):
            index = as_int(recs.value(name, "int OpIndex"))
            mapper = unquote(recs.value(name, "string FunctionMapper"))
            tail = f", mapper = {quote(mapper)}" if mapper else ""
            if cls == "CheckImmOperand_s":
                value = recs.value(name, "string ImmVal") or '""'
                return f"imm_is_enum({index}, {value}{tail})"
            if cls == "CheckImmOperandRange":
                start = as_int(recs.value(name, "int StartVal"))
                end = as_int(recs.value(name, "int EndVal"))
                return f"imm_range({index}, {start}, {end}{tail})"
            if cls == "CheckZeroOperand" and not mapper:
                return f"is_zero({index})"
            value = as_int(recs.value(name, "int ImmVal"))
            atom = {
                "CheckImmOperandLT": "imm_lt",
                "CheckImmOperandGT": "imm_gt",
                "CheckImmOperandLE": "imm_le",
                "CheckImmOperandGE": "imm_ge",
            }.get(cls, "imm_is")
            return f"{atom}({index}, {value}{tail})"
        if cls == "CheckFunctionPredicate":
            mc_fn = recs.value(name, "string MCInstFnName") or '""'
            mi_fn = recs.value(name, "string MachineInstrFnName") or '""'
            return f"func({mc_fn}, {mi_fn})"
        if cls == "CheckFunctionPredicateWithTII":
            mc_fn = recs.value(name, "string MCInstFnName") or '""'
            mi_fn = recs.value(name, "string MachineInstrFnName") or '""'
            ptr = unquote(recs.value(name, "string TIIPtrName"))
            if ptr and ptr != "TII":
                return f"func_tii({mc_fn}, {mi_fn}, tii = {quote(ptr)})"
            return f"func_tii({mc_fn}, {mi_fn})"
        if cls == "CheckNonPortable":
            code = recs.block(name, "string CodeBlock")
            if code is None:
                raise GeneratorError(f"CheckNonPortable {name} has no CodeBlock")
            return f"non_portable({code})"
        raise GeneratorError(f"unsupported MC predicate {name} ({cls})")


# ---------------------------------------------------------------- features
class Features:
    def __init__(self, recs: RecordFile) -> None:
        self.recs = recs
        self.wanted: list[str] = []

    def want(self, name: str) -> None:
        if name not in self.wanted:
            self.wanted.append(name)
            for implied in as_list(self.recs.value(name, "list<SubtargetFeature> Implies")):
                self.want(implied)

    def render(self, name: str) -> str:
        recs = self.recs
        cls = recs.cls.get(name, "")
        value = lambda field: recs.value(name, field)  # noqa: E731
        mattr = unquote(value("string Name"))
        desc = unquote(value("string Desc"))
        inline = INLINE.get(value("InlineBehavior") or "")
        inline_text = f", inline_behavior = {inline}" if inline else ""
        implies = as_list(value("list<SubtargetFeature> Implies"))
        implies_text = (
            ", implies = [" + ", ".join(implies) + "]" if implies else ""
        )

        if value("string PositiveDirectiveName") is not None:
            positive = unquote(value("string PositiveDirectiveName"))
            negative = unquote(value("string NegativeDirectiveName"))
            if mattr != positive:
                raise GeneratorError(
                    f"tune feature {name}: Name {mattr!r} differs from "
                    f"PositiveDirectiveName {positive!r}; SSDL has one field"
                )
            return (
                f"feature {name} : kind = tune, positive_directive = {quote(positive)}, "
                f"negative_directive = {quote(negative)}, "
                f"field = {quote(unquote(value('string FieldName')))}, "
                f"value = {quote(unquote(value('string Value')))}, "
                f"desc = {quote(desc)}{implies_text}{inline_text};"
            )

        if "RISCVExtension" in cls or value("int MajorVersion") is not None:
            major = as_int(value("int MajorVersion"))
            minor = as_int(value("int MinorVersion"))
            match = re.fullmatch(r"'[^']*' \((.*)\)", desc)
            inner = match.group(1) if match else desc
            experimental = as_bit(value("bit Experimental"))
            fields = [
                "kind = extension",
                f"version = {quote(f'{major}.{minor}')}",
                f"field = {quote(unquote(value('string FieldName')))}",
                f"value = {quote(unquote(value('string Value')))}",
                f"desc = {quote(inner)}",
            ]
            if experimental:
                fields.append("experimental = true")
            elif implies:
                fields.append("implies = [" + ", ".join(implies) + "]")
            group = value("int GroupID")
            bit = value("int BitPos")
            if group is not None and bit is not None:
                fields.append(f"bitmask = {{ group = {as_int(group)}, bit = {as_int(bit)} }}")
            return "feature " + name + " : " + ", ".join(fields) + ";"

        return (
            f"feature {name} : kind = plain, mattr = {quote(mattr)}, "
            f"field = {quote(unquote(value('string FieldName')))}, "
            f"value = {quote(unquote(value('string Value')))}, "
            f"desc = {quote(desc)}{implies_text}{inline_text};"
        )


# ---------------------------------------------------------------- generator
def generate(recs: RecordFile, model: str, machine: str, processor: str | None,
             unsupported_features: bool) -> str:
    if model not in recs.body:
        raise GeneratorError(f"model {model!r} is not in the record dump")
    itineraries = recs.value(model, "ProcessorItineraries Itineraries")
    if itineraries not in (None, "NoItineraries"):
        raise GeneratorError(
            f"model {model!r} uses {itineraries!r}; itineraries are not transcribed yet"
        )

    selected = recs.scoped_to(model)
    aliases = [n for n in selected if recs.has(n, "SchedReadWrite MatchRW")]
    variants: dict[str, tuple[str, list[str]]] = {}
    for name in aliases:
        target = recs.value(name, "SchedReadWrite AliasRW")
        kind = recs.kind(target, {"SchedWriteVariant", "SchedReadVariant"})
        if kind == "SchedWriteVariant":
            variants[recs.value(name, "SchedReadWrite MatchRW")] = ("variant", target)
        elif kind == "SchedReadVariant":
            variants[recs.value(name, "SchedReadWrite MatchRW")] = ("read_variant", target)

    # Every SchedReadWrite an InstRW names.  Some upstream defs appear here
    # directly instead of through an alias: a bare SchedWriteVariant is a
    # SchedWrite, and a SchedReadAdvance is a SchedRead.
    referenced: list[str] = []
    for name in selected:
        if recs.value(name, "dag Instrs") is not None:
            referenced.extend(
                as_list(recs.value(name, "list<SchedReadWrite> OperandReadWrites"))
            )
    alias_targets = {
        recs.value(name, "SchedReadWrite AliasRW") for name in aliases
    }
    for name in list(selected) + referenced:
        cls = recs.kind(name, {"SchedWriteVariant", "SchedReadVariant"})
        if cls is not None:
            if name in alias_targets or name in variants:
                continue
            kind = "variant" if cls == "SchedWriteVariant" else "read_variant"
            variants[name] = (kind, name)

    read_advances = {
        name
        for name in list(referenced) + selected
        if recs.kind(name, {"SchedReadAdvance"}) is not None
        and not recs.has(name, "SchedRead ReadType")
    }

    preds = Predicates(recs)
    features = Features(recs)

    # Variants first: their predicate trees and selected names drive which
    # features/writes/reads/advances the model needs.
    variant_cases: dict[str, list[tuple[str | None, list[str]]]] = {}
    rewritten: dict[str, str] = {}
    for subject, (kind, variant) in variants.items():
        cases: list[tuple[str | None, list[str]]] = []
        for sv in as_list(recs.value(variant, "list<SchedVar> Variants")):
            pred = preds.sched(recs.value(sv, "SchedPredicateBase Predicate"))
            picked = list(as_list(recs.value(sv, "list<SchedReadWrite> Selected")))
            cases.append((pred, picked))
        if kind == "read_variant":
            # A read variant normally picks one concrete read advance and the
            # SSDL subject carries the ``advance``; that is what the emitter
            # produces for ``read_variant X { ... X ... }``.  Fold only when
            # *every* case agrees -- AArch64 A55 picks a different advance per
            # case (1 cycle shifted, 2 otherwise) and must keep both.
            picked_all = {entry for _, picked in cases for entry in picked}
            if len(picked_all) == 1:
                only = picked_all.pop()
                if only in read_advances:
                    rewritten[subject] = only
                    cases = [
                        (pred, [subject if entry == only else entry for entry in picked])
                        for pred, picked in cases
                    ]
        variant_cases[subject] = cases

    writes: list[str] = []
    reads: list[str] = []

    def want_write(name: str) -> None:
        if name in RESERVED_SSDL or name in writes:
            return
        writes.append(name)

    def want_read(name: str) -> None:
        if name in RESERVED_SSDL or name in reads:
            return
        reads.append(name)

    for subject, kind_cases in variant_cases.items():
        if kind_cases and variants[subject][0] == "variant":
            want_write(subject)
        else:
            want_read(subject)
        for _, picked in kind_cases:
            for entry in picked:
                if entry == subject:
                    continue
                (want_write if is_write(recs, entry) else want_read)(entry)
    # A rewritten concrete advance is expressed as ``advance <subject>``; it
    # must not also appear as a read of its own (the emitter would name both
    # records the same).
    folded = set(rewritten.values())
    for name in sorted(read_advances - folded):
        want_read(name)

    bindings: list[tuple[str, str]] = []
    for name in selected:
        if recs.has(name, "SchedWrite WriteType"):
            key = recs.value(name, "SchedWrite WriteType")
            want_write(key)
            bindings.append((key, name))
        elif recs.kind(name, {"SchedWriteRes"}) is not None:
            want_write(name)
            bindings.append((name, name))

    advances: list[tuple[str, str]] = []
    for name in selected:
        if recs.has(name, "SchedRead ReadType"):
            read = recs.value(name, "SchedRead ReadType")
            want_read(read)
            advances.append((read, name))
    for subject, concrete in rewritten.items():
        advances.append((subject, concrete))
        want_read(subject)
    seen_advances = {read for read, _ in advances}
    for name in sorted(read_advances - folded):
        if name not in seen_advances:
            advances.append((name, name))

    for name in selected:
        if recs.value(name, "dag Instrs") is not None:
            for entry in as_list(recs.value(name, "list<SchedReadWrite> OperandReadWrites")):
                if entry in RESERVED_SSDL:
                    continue
                (want_write if is_write(recs, entry) else want_read)(entry)

    # Aliases to non-variant targets (for example a WriteSequence).
    plain_aliases: list[tuple[str, str]] = []
    for name in aliases:
        src = recs.value(name, "SchedReadWrite MatchRW")
        dst = recs.value(name, "SchedReadWrite AliasRW")
        if src in variants:
            continue
        plain_aliases.append((src, dst))
        (want_write if is_write(recs, dst) else want_read)(dst)
        # The source is the alias subject; it needs its own declaration too.
        (want_write if is_write(recs, src) else want_read)(src)

    # The processor record is what SSDL calls the machine.
    feature_props: list[str] = []
    if processor is not None:
        if processor not in recs.body:
            raise GeneratorError(f"processor {processor!r} is not in the record dump")
        if recs.value(processor, "SchedMachineModel SchedModel") != model:
            raise GeneratorError(f"processor {processor!r} does not use {model!r}")
        march = unquote(recs.value(processor, "string DefaultMarch"))
        for list_name, ssdl_name in (
            ("list<SubtargetFeature> Features", "features"),
            ("list<SubtargetFeature> TuneFeatures", "tune_features"),
        ):
            names = as_list(recs.value(processor, list_name))
            for feature in names:
                features.want(feature)
            if names:
                feature_props.append(f"    {ssdl_name} = [{', '.join(names)}];")
        if march:
            feature_props.append(f"    default_march = {quote(march)};")

    for name in preds.features:
        features.want(name)

    out: list[str] = [
        f"# Generated by tools/ssdl/from_records.py from {model}.",
        "# This is the external generator permitted by the spec: the SSDL text",
        "# below is fully expanded, and the language itself has no foreach.",
        "",
    ]
    # Emit implied features first: some TableGen versions resolve a def's
    # template arguments before later top-level defs exist.
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for implied in as_list(recs.value(name, "list<SubtargetFeature> Implies")):
            visit(implied)
        ordered.append(name)

    for name in sorted(features.wanted):
        visit(name)
    for name in ordered:
        out.append(features.render(name))
    out.append("")
    # TII predicates are named records referenced from variant cases; they
    # must precede the machine text that names them.
    for text in preds.tii_text.values():
        out.append(text)
    if preds.tii_text:
        out.append("")
    out.append(f"machine {machine} {{")
    out.extend(feature_props)

    props = (
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
    )
    for prefix, ssdl_name, default in props:
        value = recs.value(model, prefix)
        if value is None:
            continue
        if prefix.startswith("int"):
            number = as_int(value)
            if number != default:
                out.append(f"    {ssdl_name} = {number};")
        else:
            bit = as_bit(value)
            if bit != default:
                out.append(f"    {ssdl_name} = {'true' if bit else 'false'};")

    unsupported = as_list(recs.value(model, "list<Predicate> UnsupportedFeatures"))
    if unsupported_features and unsupported:
        # Predicate def names are target vocabulary: transcribe them verbatim.
        out.append(f"    unsupported_features = [{', '.join(unsupported)}];")

    resources = [n for n in selected if recs.value(n, "int NumUnits") is not None]
    groups = [n for n in selected if recs.has(n, "list<ProcResource> Resources")]
    # A57 (and friends) declare ProcResource outside `let SchedModel`; SSDL
    # requires them inside the machine, so collect what this model binds.
    wanted_res: set[str] = set()
    for _, binding in bindings:
        wanted_res.update(
            as_list(recs.value(binding, "list<ProcResourceKind> ProcResources"))
        )
    pending = list(wanted_res)
    while pending:
        name = pending.pop()
        super_name = recs.value(name, "ProcResourceKind Super")
        if super_name and super_name != "?" and super_name not in wanted_res:
            wanted_res.add(super_name)
            pending.append(super_name)
        for member in as_list(recs.value(name, "list<ProcResource> Resources")):
            if member not in wanted_res:
                wanted_res.add(member)
                pending.append(member)
    for name in sorted(wanted_res):
        if name in resources or name in groups:
            continue
        if recs.value(name, "int NumUnits") is not None:
            resources.append(name)
        elif recs.has(name, "list<ProcResource> Resources"):
            groups.append(name)
    for name in sorted(resources):
        parts = [f"units = {as_int(recs.value(name, 'int NumUnits'))}"]
        super_name = recs.value(name, "ProcResourceKind Super")
        if super_name and super_name != "?":
            parts.append(f"super = {super_name}")
        buffer = as_int(recs.value(name, "int BufferSize"), -1)
        if buffer != -1:
            parts.append(f"buffer = {buffer}")
        out.append(f"    resource {name} : " + ", ".join(parts) + ";")
    for name in sorted(groups):
        members = as_list(recs.value(name, "list<ProcResource> Resources"))
        text = f"    res_group {name} = [{', '.join(members)}]"
        buffer = as_int(recs.value(name, "int BufferSize"), -1)
        if buffer != -1:
            text += f" : buffer = {buffer}"
        out.append(text + ";")

    sequences = [n for n in writes if recs.cls.get(n) == "WriteSequence"]
    for name in sorted(writes):
        if name in sequences:
            continue
        out.append(f"    write {name};")
    for name in sequences:
        seq = as_list(recs.value(name, "list<SchedWrite> Writes"))
        repeat = as_int(recs.value(name, "int Repeat"), 1)
        text = f"    write_seq {name} = [{', '.join(seq)}]"
        if repeat != 1:
            text += f" repeat = {repeat}"
        out.append(text + ";")
    for name in sorted(reads):
        out.append(f"    read {name};")

    for subject, name in bindings:
        parts = []
        for prefix, key in (
            ("int Latency", "latency"),
            ("int NumMicroOps", "uops"),
        ):
            value = recs.value(name, prefix)
            if value is not None:
                parts.append(f"{key} = {as_int(value)}")
        for prefix, key in (
            ("list<int> ReleaseAtCycles", "release"),
            ("list<int> AcquireAtCycles", "acquire"),
        ):
            value = as_list(recs.value(name, prefix))
            if value:
                parts.append(f"{key} = [{', '.join(value)}]")
        for prefix, key in (
            ("bit BeginGroup", "begin_grp"),
            ("bit EndGroup", "end_grp"),
            ("bit SingleIssue", "single"),
            ("bit RetireOOO", "retire_ooo"),
            ("bit Unsupported", "unsupported"),
        ):
            if as_bit(recs.value(name, prefix)):
                parts.append(f"{key} = true")
        resources_text = as_list(recs.value(name, "list<ProcResourceKind> ProcResources"))
        text = f"    bind {subject} -> [{', '.join(resources_text)}]"
        if parts:
            text += ", " + ", ".join(parts)
        out.append(text + ";")

    seen_advance: set[str] = set()
    for read, name in advances:
        if read in seen_advance:
            continue
        seen_advance.add(read)
        cycles = as_int(recs.value(name, "int Cycles"))
        text = f"    advance {read} : cycles = {cycles}"
        valid = as_list(recs.value(name, "list<SchedWrite> ValidWrites"))
        if valid:
            text += f" : writes = [{', '.join(valid)}]"
        tunables = as_list(recs.value(name, "list<int> CycleTunables"))
        if tunables:
            text += f" : tunables = [{', '.join(tunables)}]"
        if as_bit(recs.value(name, "bit Unsupported")):
            text += " : unsupported = true"
        out.append(text + ";")

    for src, dst in plain_aliases:
        out.append(f"    alias {src} -> {dst};")

    for subject, (kind, _) in variants.items():
        out.append(f"    {kind} {subject} {{")
        for pred, picked in variant_cases[subject]:
            names = [RESERVED_SSDL.get(x, x) for x in picked]
            if pred is None:
                out.append(f"        default: [{', '.join(names)}];")
            else:
                out.append(f"        case {pred}: [{', '.join(names)}];")
        out.append("    }")

    seen_maps: set[tuple] = set()
    for name in selected:
        if recs.value(name, "dag Instrs") is None:
            continue
        dag = recs.value(name, "dag Instrs")
        rw = [
            RESERVED_SSDL.get(x, x)
            for x in as_list(
                recs.value(name, "list<SchedReadWrite> OperandReadWrites")
            )
        ]
        match = re.fullmatch(r"\(instrs (.*)\)", dag)
        if match:
            insts = [x.strip() for x in match.group(1).split(",") if x.strip()]
        else:
            patterns = re.findall(r'"([^"]*)"', dag)
            insts = [f"regex({quote(p)})" for p in patterns]
        if not insts:
            raise GeneratorError(f"InstRW {name}: unsupported dag {dag}")
        # AArch64 carries genuinely duplicated InstRW defs (A64FX and the
        # ThunderX2/T3 models each repeat an identical regex mapping).  One
        # map says the same thing; the language still rejects duplicates a
        # human writes twice.
        key = (tuple(insts), tuple(rw), as_bit(recs.value(name, "bit Unsupported")))
        if key in seen_maps:
            continue
        seen_maps.add(key)
        text = f"    map [{', '.join(insts)}] -> [{', '.join(rw)}]"
        if as_bit(recs.value(name, "bit Unsupported")):
            text += " : unsupported = true"
        out.append(text + ";")

    out.append("}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("records", help="llvm-tblgen -print-records dump")
    ap.add_argument("--model", required=True, help="SchedMachineModel def name")
    ap.add_argument("--machine", help="SSDL machine name (default: --processor -mcpu name)")
    ap.add_argument("--processor", help="ProcessorModel def whose features are copied")
    ap.add_argument(
        "--mcpu",
        help="ProcessorModel selected by its -mcpu name (AArch64 models are "
        "anonymous, so --processor cannot name them)",
    )
    ap.add_argument("--unsupported-features", action="store_true",
                    help="transcribe UnsupportedFeatures (needs the host predicate defs)")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    recs = load(args.records)
    processor = args.processor
    if args.mcpu is not None:
        if processor is not None:
            ap.error("--processor and --mcpu are mutually exclusive")
        matches = [
            name
            for name, cls in recs.cls.items()
            if cls == "ProcessorModel"
            and unquote(recs.value(name, "string Name")) == args.mcpu
        ]
        if not matches:
            print(f"from_records: no ProcessorModel is named {args.mcpu!r}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(
                f"from_records: {args.mcpu!r} names {len(matches)} processors",
                file=sys.stderr,
            )
            return 1
        processor = matches[0]
    processor_name = None
    if processor:
        processor_name = unquote(recs.value(processor, "string Name"))
    machine = args.machine or processor_name
    if not machine:
        ap.error("--machine is required when --processor is not given")
    try:
        text = generate(
            recs, args.model, machine, processor, args.unsupported_features
        )
    except GeneratorError as exc:
        print(f"from_records: {exc}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
