#!/usr/bin/env python3
"""Consumer semantics for the TableGen fields that SSDL lowers to.

The keyword reference answers a grep question: which C++ line reads a field.
That is not the question an SSDL author has.  A field can be read by
``SubtargetEmitter`` and still reach only llvm-mca; a field can be declared,
documented in a ``.td`` comment, and never read at all.  This module records the
second kind of fact:

* ``consumers`` -- runtime subsystems that observe the field after lowering
* ``emitted`` -- whether the field reaches the generated ``.inc`` tables
* ``default_expansion`` -- what the TableGen default really means at runtime
* ``evidence`` -- repo-relative ``path:line`` proving the claim
* ``role`` -- effect / selector / validation / dead / declaration

The table is curated, not inferred.  Grep reachability is not consumption: a
string binding in an emitter only proves that the emitter looked at the record,
not that any scheduler or analysis uses the result.  ``check.py`` resolves every
key against the parsed TD schema and every evidence line against the checkout,
so a stale claim fails the build instead of becoming folklore.

Paths are relative to the monorepo root (the parent of ``llvm/``).  The audit
was performed on the checkout below; re-audit when the base commit moves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

VERIFIED_AT = "700e551714cc"

# Runtime subsystems that can observe a lowered field.
CODEGEN = "codegen"  # machine scheduler, target passes, cost heuristics
MCA = "mca"  # llvm-mca
TTI = "tti"  # TargetTransformInfo: unrolling, inlining, branch cost
ISEL = "isel"  # SelectionDAG and GlobalISel pattern selection
ASM = "asm"  # asm matcher, decoder, compressor
TARGETPARSER = "targetparser"  # RISCVISAInfo / clang driver tables

RUNTIME_CONSUMERS = frozenset({CODEGEN, MCA, TTI, ISEL, ASM, TARGETPARSER})

# What the field itself does.
ROLE_EFFECT = "effect"  # runtime-observable value
ROLE_SELECTOR = "selector"  # chooses which record applies; no runtime field
ROLE_VALIDATION = "validation"  # TableGen-time check only
ROLE_DEAD = "dead"  # declared but no consumer
ROLE_DECLARATION = "declaration"  # changes generated signature/shape only

ROLES = frozenset(
    {ROLE_EFFECT, ROLE_SELECTOR, ROLE_VALIDATION, ROLE_DEAD, ROLE_DECLARATION}
)


@dataclass(frozen=True)
class Effect:
    layer: str
    role: str
    consumers: frozenset[str]
    emitted: bool
    default_expansion: str
    evidence: tuple[str, ...]
    note: str = ""

    def to_json(self) -> dict:
        return {
            "layer": self.layer,
            "role": self.role,
            "consumers": sorted(self.consumers),
            "emitted": self.emitted,
            "default_expansion": self.default_expansion,
            "evidence": list(self.evidence),
            "verified_at": VERIFIED_AT,
            "note": self.note,
        }


def _fx(
    layer: str,
    role: str,
    consumers: Iterable[str],
    emitted: bool,
    default: str,
    *evidence: str,
    note: str = "",
) -> Effect:
    return Effect(
        layer=layer,
        role=role,
        consumers=frozenset(consumers),
        emitted=emitted,
        default_expansion=default,
        evidence=tuple(evidence),
        note=note,
    )


FIELD_EFFECTS: dict[str, Effect] = {
    # ---------------------------------------------------- SchedMachineModel
    "SchedMachineModel.IssueWidth": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "-1 -> DefaultIssueWidth (1); 0 is a real value and divides by zero",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1542",
        "llvm/lib/CodeGen/TargetSchedule.cpp:55",
        "llvm/lib/MCA/Stages/InOrderIssueStage.cpp:55",
        note="Hard per-cycle issue constraint shared by the machine scheduler and MCA.",
    ),
    "SchedMachineModel.MicroOpBufferSize": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "-1 -> DefaultMicroOpBufferSize (0, in-order); 0/1/>1 are distinct modes",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1543",
        "llvm/lib/CodeGen/MachineScheduler.cpp:3130",
        "llvm/lib/MCA/HardwareUnits/RetireControlUnit.cpp:24",
        note="MCA uses it as the reorder-buffer fallback only when ReorderBufferSize is 0.",
    ),
    "SchedMachineModel.LoopMicroOpBufferSize": _fx(
        "sched", ROLE_EFFECT, {TTI}, True,
        "-1 -> DefaultLoopMicroOpBufferSize (0)",
        "llvm/include/llvm/CodeGen/BasicTTIImpl.h:755",
        note="Unroll threshold only; neither MachineScheduler nor llvm-mca reads it.",
    ),
    "SchedMachineModel.LoadLatency": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "-1 -> DefaultLoadLatency (4); fallback when the sched class has no latency",
        "llvm/lib/CodeGen/TargetInstrInfo.cpp:1813",
        note="Real load latency must still be repeated in WriteRes.Latency; MCA ignores this.",
    ),
    "SchedMachineModel.HighLatency": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "-1 -> DefaultHighLatency (10)",
        "llvm/lib/CodeGen/TargetInstrInfo.cpp:1815",
        note="Requires the target to override isHighLatencyDef; MCA ignores this.",
    ),
    "SchedMachineModel.MispredictPenalty": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "-1 -> DefaultMispredictPenalty (10)",
        "llvm/lib/CodeGen/EarlyIfConversion.cpp:1249",
        "llvm/include/llvm/CodeGen/BasicTTIImpl.h:847",
        note="Branch-cost heuristics only; the machine scheduler and MCA do not read it.",
    ),
    "SchedMachineModel.Itineraries": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "NoItineraries -> null; a second, legacy scheduling mechanism",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1050",
        "llvm/lib/CodeGen/TargetSchedule.cpp:194",
        "llvm/tools/llvm-mca/llvm-mca.cpp:446",
        note="llvm-mca rejects CPUs that provide itineraries instead of a sched model.",
    ),
    "SchedMachineModel.PostRAScheduler": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "false",
        "llvm/lib/CodeGen/TargetSubtargetInfo.cpp:65",
        "llvm/lib/CodeGen/PostRASchedulerList.cpp:276",
        note="Gates the legacy post-RA list scheduler only.",
    ),
    "SchedMachineModel.CompleteModel": _fx(
        "sched", ROLE_VALIDATION, set(), True,
        "true; TableGen completeness gate, runtime check is debug-only",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1919",
        "llvm/lib/CodeGen/TargetSchedule.cpp:226",
        note="Release builds never enforce the runtime assert.",
    ),
    "SchedMachineModel.FullInstRWOverlapCheck": _fx(
        "sched", ROLE_VALIDATION, set(), False,
        "true; only downgrades a TableGen error",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1040",
        note="Never reaches MCSchedModel.",
    ),
    "SchedMachineModel.UnsupportedFeatures": _fx(
        "sched", ROLE_VALIDATION, set(), False,
        "[]; filters instructions out of the completeness check only",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1165",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1926",
        note="Does not remove resources or sched classes from the generated tables.",
    ),
    "SchedMachineModel.NoModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "false; TableGen bookkeeping for the synthetic NoSchedModel",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:525",
        note="Never reaches C++.",
    ),
    "SchedMachineModel.EnableIntervals": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "false; ForceEnableIntervals overrides it",
        "llvm/lib/CodeGen/MachineScheduler.cpp:2907",
        "llvm/lib/CodeGen/TargetSchedule.cpp:341",
        note="Chip-level resource intervals in MachineScheduler.",
    ),
    # ---------------------------------------------- ProcResourceUnits/Group
    "ProcResourceUnits.Kind": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "required; also the name base of ProcResource<num>",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:908",
        note="Resolution only; the record name is what becomes the resource name.",
    ),
    "ProcResourceUnits.NumUnits": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; 0 yields a zero resource factor",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:913",
        "llvm/lib/CodeGen/TargetSchedule.cpp:57",
        "llvm/lib/MCA/InstrBuilder.cpp:183",
        note="Affects resource LCM/factors, per-instance scheduling, and MCA masks.",
    ),
    "ProcResourceUnits.Super": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "unset -> SuperIdx 0 (no super-resource)",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:896",
        "llvm/lib/MCA/InstrBuilder.cpp:102",
        "llvm/lib/CodeGen/MachineScheduler.cpp:2874",
        note="MCA charges super-resource cycles; CodeGen uses group sub-unit masks.",
    ),
    "ProcResourceUnits.BufferSize": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "-1 -> OoO port (no dispatch hazard); 0 barrier; 1 in-order; >1 station",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:900",
        "llvm/lib/MC/MCSchedule.cpp:227",
        "llvm/lib/MCA/HardwareUnits/ResourceManager.cpp:78",
        note="MCA scales >1 values; CodeGen uses them unscaled.",
    ),
    "ProcResGroup.Resources": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; group NumUnits is derived by summing members",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:735",
        "llvm/lib/CodeGen/MachineScheduler.cpp:2874",
        "llvm/lib/MCA/InstrBuilder.cpp:183",
        note="Group members can overlap only when a supergroup contains both.",
    ),
    "ProcResGroup.BufferSize": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "-1 -> same semantics as ProcResourceUnits.BufferSize",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:919",
        "llvm/lib/MC/MCSchedule.cpp:227",
        note="A group can set BufferSize but has no Super field.",
    ),
    "ProcResourceUnits.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:937",
        note="Scoping selector, not a runtime field.",
    ),
    "ProcResGroup.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:937",
        note="Scoping selector, not a runtime field.",
    ),
    # ------------------------------------------------------------ WriteRes
    "ProcWriteResources.Latency": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "1; summed across a WriteSequence; -1 means invalid/infinite",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1210",
        "llvm/lib/CodeGen/TargetSchedule.cpp:203",
        "llvm/lib/MCA/InstrBuilder.cpp:237",
        note="MCA substitutes MaxLatency for -1.",
    ),
    "ProcWriteResources.NumMicroOps": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "1; summed over every write in the class",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1211",
        "llvm/lib/CodeGen/TargetSchedule.cpp:91",
        note="Must fit 13 bits (InvalidNumMicroOps sentinel).",
    ),
    "ProcWriteResources.ReleaseAtCycles": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "[] -> 1 cycle per resource, NOT Latency",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1254",
        "llvm/lib/CodeGen/MachineScheduler.cpp:2850",
        "llvm/lib/MCA/InstrBuilder.cpp:76",
        note="0 means the resource must be available but is not consumed.",
    ),
    "ProcWriteResources.AcquireAtCycles": _fx(
        "sched", ROLE_EFFECT, {CODEGEN}, True,
        "[] -> 0",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1259",
        "llvm/lib/CodeGen/MachineScheduler.cpp:1582",
        note="llvm-mca ignores acquire completely; it builds CycleSegment(0, ReleaseAtCycle).",
    ),
    "ProcWriteResources.BeginGroup": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; OR-reduced over all writes of the class",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1212",
        "llvm/lib/CodeGen/TargetSchedule.cpp:69",
    ),
    "ProcWriteResources.EndGroup": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; OR-reduced over all writes of the class",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1213",
        "llvm/lib/CodeGen/TargetSchedule.cpp:80",
    ),
    "ProcWriteResources.SingleIssue": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; syntactic sugar for BeginGroup|EndGroup",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1214",
        note="No separate runtime field; there is nothing to model independently.",
    ),
    "ProcWriteResources.RetireOOO": _fx(
        "sched", ROLE_EFFECT, {MCA}, True,
        "false",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1216",
        "llvm/lib/MCA/InstrBuilder.cpp:713",
        note="MCA-only: in-order retire model. CodeGen never reads MCSchedClassDesc::RetireOOO.",
    ),
    "ProcWriteResources.Unsupported": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; true emits InvalidNumMicroOps instead of deleting the class",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1206",
        "llvm/lib/MCA/InstrBuilder.cpp:581",
        note="CodeGen silently falls back; llvm-mca hard-errors on the instruction.",
    ),
    "ProcWriteResources.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:937",
        note="Chooses the WriteRes record for a processor.",
    ),
    # -------------------------------------------------------- ReadAdvance
    "ProcReadAdvance.Cycles": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; negative values increase latency",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1345",
        "llvm/lib/CodeGen/TargetSchedule.cpp:214",
        "llvm/lib/MCA/HardwareUnits/RegisterFile.cpp:532",
        note="CodeGen clamps an advance larger than the latency to 0; MCA models cross-domain stalls.",
    ),
    "ProcReadAdvance.ValidWrites": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "[] -> WriteResourceID 0, applies to every write (no bypass)",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1329",
        "llvm/lib/CodeGen/TargetSchedule.cpp:214",
        note="Non-empty values must name actual SchedWrites of the model.",
    ),
    "ProcReadAdvance.CycleTunables": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "[]; folded into Cycles at emit time (Cycles + tunable)",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1337",
        note="Not a separate runtime field; per-write additive delta.",
    ),
    "ProcReadAdvance.Unsupported": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; true invalidates the whole sched class",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1324",
        note="Same invalid-class path as WriteRes.Unsupported.",
    ),
    "ReadAdvance.ReadType": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "required when used as ReadAdvance",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:2145",
        note="Two ReadAdvances for one SchedRead in a model is fatal.",
    ),
    "ProcReadAdvance.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1319",
    ),
    # ------------------------------------- InstRW / ItinRW / Alias / Sequence
    "InstRW.OperandReadWrites": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; replaces the instruction's SchedRW for that CPU",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1150",
        note="Highest-priority per-instruction override.",
    ),
    "InstRW.Instrs": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; opcode or regex DAG expanded at emit",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:186",
        note="Overlapping coverage for one instruction/model is fatal.",
    ),
    "InstRW.Unsupported": _fx(
        "sched", ROLE_DEAD, set(), False,
        "false; no consumer",
        "llvm/include/llvm/Target/TargetSchedule.td:469",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1206",
        note="Emitter reads Unsupported only on WriteRes and ReadAdvance, never on InstRW.",
    ),
    "InstRW.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1150",
    ),
    "ItinRW.MatchedItinClasses": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; only consulted when the class has no Writes",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1178",
        note="Legacy itinerary-to-SchedRW bridge.",
    ),
    "ItinRW.OperandReadWrites": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; same expansion as InstRW once matched",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1178",
    ),
    "ItinRW.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> applies to every model",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1178",
    ),
    "SchedAlias.MatchRW": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "required; the SchedReadWrite being replaced",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:628",
        note="Resolution is per processor; aliasing an alias is fatal.",
    ),
    "SchedAlias.AliasRW": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "required; the replacement (sequence or variant allowed)",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:661",
    ),
    "SchedAlias.SchedModel": _fx(
        "sched", ROLE_SELECTOR, set(), False,
        "unset -> generic alias for every model",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:628",
    ),
    "WriteSequence.Writes": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; expanded and summed into the class",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:749",
        note="Latency is additive per element; a trailing variadic variant distributes.",
    ),
    "WriteSequence.Repeat": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "1; re-emits the whole list N times",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:749",
    ),
    "WriteSequence.SchedModel": _fx(
        "sched", ROLE_DEAD, set(), False,
        "ignored",
        "llvm/include/llvm/Target/TargetSchedule.td:244",
        note="The in-tree comment says it silences warnings but is ignored.",
    ),
    "SchedVar.Predicate": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679",
        note="Predicate language determines MCA visibility; see SchedPredicateBase notes.",
    ),
    "SchedVar.Selected": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679",
    ),
    "SchedVar.SchedModel": _fx(
        "sched", ROLE_DEAD, set(), False,
        "ignored",
        "llvm/include/llvm/Target/TargetSchedule.td:430",
    ),
    "SchedVariant.Variants": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1679",
    ),
    "SchedVariant.Variadic": _fx(
        "sched", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "false; true distributes writes over variadic operands, latency not additive",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1486",
    ),
    "SchedVariant.SchedModel": _fx(
        "sched", ROLE_DEAD, set(), False,
        "ignored",
        "llvm/include/llvm/Target/TargetSchedule.td:437",
    ),
    # ------------------------------------------------------- STI predicates
    "STIPredicateDecl.Name": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "required; names the generated function",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:294",
    ),
    "STIPredicateDecl.DefaultReturnValue": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "FalsePred (unmatched opcode returns false)",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:565",
    ),
    "STIPredicateDecl.OverridesBaseClassMember": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "true; declares a virtual override in TargetSubtargetInfo",
        "llvm/include/llvm/Target/TargetInstrPredicate.td:395",
        note="Changes the generated declaration, not a runtime value.",
    ),
    "STIPredicateDecl.ExpandForMC": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "true; emits an MC-layer equivalent",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:489",
        note="With true, CheckPseudo and CheckNonPortable silently become false in MC.",
    ),
    "STIPredicateDecl.UpdatesOpcodeMask": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "false; adds an in/out APInt mask to the generated signature",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:489",
        "llvm/lib/MCA/InstrBuilder.cpp:757",
        note="The mask is only consumed by llvm-mca.",
    ),
    "STIPredicateDecl.Delegates": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "[]; delegated predicates run first and may set the mask",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:504",
        note="Delegation is expansion-time, not a runtime field of its own.",
    ),
    "InstructionEquivalenceClass.Opcodes": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; a switch case per opcode",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:527",
        note="Duplicate opcode coverage in one predicate/CPU is fatal.",
    ),
    "InstructionEquivalenceClass.Predicate": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; the body of the opcode case",
        "llvm/utils/TableGen/Common/PredicateExpander.cpp:527",
    ),
    "InstructionEquivalenceClass.OperandIndices": _fx(
        "predicate", ROLE_EFFECT, {MCA}, True,
        "[] -> zero-width mask, meaning all explicit uses are independent",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:282",
        "llvm/lib/MCA/InstrBuilder.cpp:757",
        note="Mask bits use MCA use-index space (defs excluded, implicit appended), not raw operand indices.",
    ),
    "TIIPredicate.Body": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN, MCA}, True,
        "required; expanded into both MachineInstr and MCInst helpers",
        "llvm/utils/TableGen/InstrInfoEmitter.cpp:662",
        "llvm/utils/TableGen/InstrInfoEmitter.cpp:869",
        note="Also a real target API: RISCVInstrInfo.cpp calls generated TII predicates directly.",
    ),
    "PredicateProlog.Code": _fx(
        "predicate", ROLE_EFFECT, {CODEGEN}, True,
        "required; spliced into resolveSchedClass only",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1949",
        note="Not spliced into the MC variant resolver; prolog symbols are unavailable to llvm-mca.",
    ),
    # ------------------------------------------- isel / assembler predicates
    "Predicate.CondString": _fx(
        "predicate", ROLE_EFFECT, {ISEL}, True,
        "required; raw C++ expression in DAGISel scope",
        "llvm/utils/TableGen/DAGISelMatcherEmitter.cpp:1347",
        note="RecomputePerFunction is ignored here; SelectionDAG always recomputes.",
    ),
    "Predicate.AssemblerMatcherPredicate": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "false; true opts the predicate into the asm matcher",
        "llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:25",
    ),
    "Predicate.AssemblerCondDag": _fx(
        "predicate", ROLE_EFFECT, {ASM}, True,
        "required when used as an assembler predicate",
        "llvm/utils/TableGen/AsmMatcherEmitter.cpp:4013",
        note="Expanded to one feature bit per predicate; all_of/any_of/none_of only.",
    ),
    "Predicate.PredicateName": _fx(
        "predicate", ROLE_EFFECT, {ASM}, True,
        "empty; diagnostics text",
        "llvm/utils/TableGen/AsmMatcherEmitter.cpp:3004",
    ),
    "Predicate.RecomputePerFunction": _fx(
        "predicate", ROLE_EFFECT, {ISEL}, True,
        "false; SelectionDAG ignores it, GlobalISel honors it",
        "llvm/utils/TableGen/Common/GlobalISel/GlobalISelMatchTableExecutorEmitter.cpp:36",
        note="The in-tree comment only documents the SelectionDAG behavior.",
    ),
    "AssemblerPredicate.AssemblerMatcherPredicate": _fx(
        "predicate", ROLE_DECLARATION, set(), False,
        "true",
        "llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:25",
    ),
    "AssemblerPredicate.AssemblerCondDag": _fx(
        "predicate", ROLE_EFFECT, {ASM}, True,
        "required; all_of/any_of/none_of over features",
        "llvm/utils/TableGen/Common/SubtargetFeatureInfo.cpp:187",
    ),
    "AssemblerPredicate.PredicateName": _fx(
        "predicate", ROLE_EFFECT, {ASM}, True,
        "empty; diagnostics text",
        "llvm/utils/TableGen/AsmMatcherEmitter.cpp:3004",
    ),
    # ----------------------------------------------------- features / CPU
    "SubtargetFeature.Name": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, ASM, TARGETPARSER}, True,
        "required; the -mattr name and the key in SubtargetFeatureKV",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:206",
        note="One name-to-enum table serves plain, extension, and tune features.",
    ),
    "SubtargetFeature.FieldName": _fx(
        "feature", ROLE_EFFECT, {CODEGEN}, False,
        "required; maps the feature to a Subtarget field",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:2107",
        note="Build-time mapping; no per-feature runtime field.",
    ),
    "SubtargetFeature.Value": _fx(
        "feature", ROLE_EFFECT, {CODEGEN}, False,
        "required; non-bool values become enum/int with max-wins codegen",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:2107",
    ),
    "SubtargetFeature.Desc": _fx(
        "feature", ROLE_DECLARATION, set(), False,
        "required; help text only",
        "llvm/lib/MC/MCSubtargetInfo.cpp:113",
        note="Never affects feature bits or code generation.",
    ),
    "SubtargetFeature.Implies": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, TARGETPARSER}, True,
        "[]; emitted as a direct list, not transitively unrolled",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:252",
        "llvm/lib/MC/MCSubtargetInfo.cpp:38",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:873",
        note="Two independent closures exist: MC bitset closure and RISCVISAInfo closure.",
    ),
    "SubtargetFeature.InlineBehavior": _fx(
        "feature", ROLE_EFFECT, {TTI}, True,
        "InlineDefault for plain features; RISCVTuneFeature defaults to InlineIgnore",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:2126",
        "llvm/include/llvm/CodeGen/BasicTTIImpl.h:400",
        note="Emitted into three bitsets; orthogonal to kind.",
    ),
    "RISCVExtension.MajorVersion": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; part of the TargetParser extension table",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:45",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:263",
        note="MC sees only the boolean feature bit; version never enters SubtargetFeatureKV.",
    ),
    "RISCVExtension.MinorVersion": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; part of the TargetParser extension table",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:45",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:263",
    ),
    "RISCVExtension.Experimental": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "false; true requires -menable-experimental-extensions and an explicit version",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:41",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:376",
        note="Also prefixes the -mattr name with experimental-.",
    ),
    "RISCVExtensionBitmask.GroupID": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; selects the __riscv_feature_bits group",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:249",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:1173",
    ),
    "RISCVExtensionBitmask.BitPos": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; selects the bit inside the group",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:250",
        "llvm/lib/TargetParser/RISCVISAInfo.cpp:1173",
    ),
    "RISCVTuneFeature.PositiveDirectiveName": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; accepted by -mtune",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:289",
        "llvm/lib/TargetParser/RISCVTargetParser.cpp:253",
        note="Consumed by the clang driver and TargetParser, not by RISCVSubtarget.",
    ),
    "RISCVTuneFeature.NegativeDirectiveName": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "required; accepted by -mtune",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:290",
        "llvm/lib/TargetParser/RISCVTargetParser.cpp:253",
        note="RISCVSimpleTuneFeature synthesizes no-<name>.",
    ),
    "Processor.Name": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, ASM, TARGETPARSER}, True,
        "required; the -mcpu name",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:388",
        "llvm/lib/MC/MCSubtargetInfo.cpp:438",
    ),
    "Processor.SchedModel": _fx(
        "feature", ROLE_SELECTOR, set(), False,
        "NoSchedModel unless supplied",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:388",
        note="Chooses the scheduling model for the CPU.",
    ),
    "Processor.ProcItin": _fx(
        "feature", ROLE_EFFECT, {CODEGEN}, True,
        "NoItineraries unless supplied",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:1050",
        "llvm/lib/CodeGen/TargetSchedule.cpp:194",
    ),
    "Processor.Features": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, TARGETPARSER}, True,
        "required",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:388",
        note="Architectural features; also feeds the TargetParser CPU table.",
    ),
    "Processor.TuneFeatures": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, TARGETPARSER}, True,
        "[]",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:388",
        "llvm/lib/MC/MCSubtargetInfo.cpp:200",
        note="Merged into the same bitset as Features; only declaration-time checks separate them.",
    ),
    "ProcessorAlias.Name": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, TARGETPARSER}, True,
        "required; the alternate -mcpu name",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:315",
        "llvm/lib/MC/MCSubtargetInfo.cpp:438",
        note="Not resolved away by TableGen; a separate runtime alias table exists.",
    ),
    "ProcessorAlias.Alias": _fx(
        "feature", ROLE_EFFECT, {CODEGEN, TARGETPARSER}, True,
        "required; the canonical CPU name",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:315",
        "llvm/lib/MC/MCSubtargetInfo.cpp:438",
    ),
    "RISCVProcessorModel.DefaultMarch": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "empty; synthesized from Features when absent",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:196",
        "llvm/lib/TargetParser/RISCVTargetParser.cpp:109",
        note="Not a ProcessorModel field; consumed by TargetParser/driver, never RISCVSubtarget.",
    ),
    "RISCVProcessorModel.ConfigurableTuneFeatures": _fx(
        "feature", ROLE_EFFECT, {TARGETPARSER}, False,
        "[]; tune features the driver may toggle",
        "llvm/utils/TableGen/Basic/RISCVTargetDefEmitter.cpp:357",
        "llvm/lib/TargetParser/RISCVTargetParser.cpp:201",
    ),
    # ------------------------------------------------------------ itineraries
    "ProcessorItineraries.FU": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; functional unit table",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:516",
        note="Legacy itinerary path; llvm-mca rejects CPUs that use it.",
    ),
    "ProcessorItineraries.BP": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; bypass table",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:530",
    ),
    "ProcessorItineraries.IID": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; per-class itinerary data",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1112",
    ),
    "ProcessorItineraries.PacketizerNamespace": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "empty; one shared packetizer automaton",
        "llvm/utils/TableGen/DFAPacketizerEmitter.cpp:223",
        note="Non-empty keeps conflicting FU indices in separate automata.",
    ),
    "InstrItinData.TheClass": _fx(
        "itinerary", ROLE_SELECTOR, set(), False,
        "required; the InstrItinClass being described",
        "llvm/utils/TableGen/Common/CodeGenSchedule.cpp:1121",
    ),
    "InstrItinData.NumMicroOps": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "1; 0 means a dynamically determined number",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:640",
        "llvm/lib/CodeGen/TargetSchedule.cpp:91",
    ),
    "InstrItinData.Stages": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; reservation stages",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:426",
    ),
    "InstrItinData.OperandCycles": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "[]; per-operand cycle counts",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:471",
        "llvm/lib/CodeGen/TargetSchedule.cpp:194",
    ),
    "InstrItinData.Bypasses": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "[]; per-operand forwarding paths",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:487",
    ),
    "InstrStage.Cycles": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; stage length in cycles",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:435",
    ),
    "InstrStage.Units": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; functional units usable by the stage",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:440",
    ),
    "InstrStage.TimeInc": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "-1 -> Cycles",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:449",
    ),
    "InstrStage.Kind": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "Required (0); Reserved (1)",
        "llvm/utils/TableGen/SubtargetEmitter.cpp:452",
    ),
    "ComboFuncData.TheComboFunc": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; combined unit identity",
        "llvm/utils/TableGen/DFAPacketizerEmitter.cpp:154",
    ),
    "ComboFuncData.FuncList": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; members of the combination",
        "llvm/utils/TableGen/DFAPacketizerEmitter.cpp:156",
    ),
    "ComboFuncUnits.CFD": _fx(
        "itinerary", ROLE_EFFECT, {CODEGEN}, True,
        "required; combination table",
        "llvm/utils/TableGen/DFAPacketizerEmitter.cpp:144",
    ),
}


def lookup(class_name: str, field_name: str) -> Effect | None:
    """Exact ``Class.Field`` lookup; inheritance is the caller's job."""
    return FIELD_EFFECTS.get(f"{class_name}.{field_name}")


def key_parts(key: str) -> tuple[str, str]:
    if "." not in key:
        raise ValueError(f"effect key must be Class.Field, got {key!r}")
    return tuple(key.split(".", 1))  # type: ignore[return-value]
