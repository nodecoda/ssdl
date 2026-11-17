#!/usr/bin/env python3
"""Lower the lowerable subset of SSDL to TableGen.

The emitter deliberately covers the part of SSDL that maps one-to-one onto
existing TableGen records:

  feature   -> SubtargetFeature / RISCVExtension / RISCVTuneFeature
  machine   -> SchedMachineModel + RISCVProcessorModel / ProcessorAlias
  resource  -> ProcResource / ProcResGroup
  write/read-> SchedWrite / SchedRead
  bind      -> WriteRes
  advance   -> ReadAdvance
  map       -> InstRW
  variant   -> SchedWriteVariant / SchedReadVariant + SchedAlias
  reg_file  -> RegisterFile
  queues    -> LoadQueue / StoreQueue, RetireControlUnit
  itineraries -> FuncUnit / Bypass / InstrItinClass / InstrItinData /
                 ProcessorItineraries
  predicate / predicate_prolog / sti_decl / sti_function / tii_predicate
            -> MCInstPredicate / SchedPredicateBase / Predicate /
               AssemblerPredicate / PredicateProlog / STIPredicate /
               TIIPredicate (see ``predicates.py``)

``set`` declarations are compile-time only and are evaluated against a host
attribute schema (``sets.py``); they emit no records.  Constructs that still
have no landing spot (``write_seq``, ``map_itin``, scheduling aliases,
``sti_function.delegates``) are reported as "skipped"; with ``--strict`` every
skip is an error, which is how the test suite proves which parts are open.

Record names are deterministic so that ``llvm-tblgen -print-records`` output can
be compared against a hand-written .td file.
"""
from __future__ import annotations

import argparse
import re
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Any

if __package__ in (None, ""):
    import validate as validate_mod  # type: ignore
    from record_ir import Base, DuplicateRecord, Record, RecordSet  # type: ignore
    from predicates import PredicateError, PredicateLowerer  # type: ignore
    from sets import AttributeSchema, SetEvaluator  # type: ignore
    from tdutil import join_list, td_def_name, td_str  # type: ignore
else:
    from . import validate as validate_mod
    from .record_ir import Base, DuplicateRecord, Record, RecordSet
    from .predicates import PredicateError, PredicateLowerer
    from .sets import AttributeSchema, SetEvaluator
    from .tdutil import join_list, td_def_name, td_str

if __package__ in (None, ""):
    from diagnose import (  # type: ignore
        ERROR as FINDING_ERROR,
        NOTE as FINDING_NOTE,
        diagnose as diagnose_program,
        render_findings,
    )
    from parser import (  # type: ignore
        FeatureDecl,
        ItinerariesDecl,
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        SetDecl,
        STIDecl,
        STIFunction,
        TIIPredicate,
        parse_file,
    )
else:
    from .diagnose import (
        ERROR as FINDING_ERROR,
        NOTE as FINDING_NOTE,
        diagnose as diagnose_program,
        render_findings,
    )
    from .parser import (
        FeatureDecl,
        ItinerariesDecl,
        MachineDecl,
        PredicateDecl,
        PredicateProlog,
        Program,
        SetDecl,
        STIDecl,
        STIFunction,
        TIIPredicate,
        parse_file,
    )


class SSDLEmitError(Exception):
    def __init__(self, diagnostics: list["Diagnostic"]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(d.render() for d in diagnostics))


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" | "skip"
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.level}: {self.message}"


@dataclass
class FeatureInfo:
    decl: FeatureDecl
    kind: str
    def_name: str
    text: str = ""


@dataclass
class MachineInfo:
    decl: MachineDecl
    def_name: str
    text: str = ""
    # Target-global records this machine declares (SchedWrite/SchedRead,
    # WriteSequence, FuncUnit/Bypass/InstrItinClass).  They are collected
    # across machines and emitted once; see ``Emitter.collect_global_vocabulary``.
    globals: list[str] = field(default_factory=list)


class _Unset:
    """Marker type for `define(scope=...)`: use the current machine scope."""


_UNSET = _Unset()

INLINE_BEHAVIOR = {
    "ignore": "InlineIgnore",
    "default": "InlineDefault",
    "inverse": "InlineInverse",
    "must_match": "InlineMustMatch",
}

FEATURE_FIELDS = {
    "plain": {"mattr", "field", "value", "desc", "implies", "inline_behavior"},
    "extension": {"version", "field", "value", "desc", "implies", "experimental", "bitmask"},
    "tune": {
        "positive_directive",
        "negative_directive",
        "field",
        "value",
        "desc",
        "implies",
        "inline_behavior",
    },
}

FEATURE_REQUIRED = {
    "plain": ("mattr", "field", "value"),
    "extension": ("version",),
    "tune": ("positive_directive", "field", "value"),
}

RESERVED_RW = {
    "read_default": "ReadDefault",
    "no_read_advance": "NoReadAdvance",
    "write_none": "NoWrite",
}

SCHED_MODEL_PROPS = {
    "issue_width": "IssueWidth",
    "micro_op_buffer_size": "MicroOpBufferSize",
    "loop_micro_op_buffer": "LoopMicroOpBufferSize",
    "load_latency": "LoadLatency",
    "high_latency": "HighLatency",
    "mispredict_penalty": "MispredictPenalty",
    "post_ra_scheduler": "PostRAScheduler",
    "complete_model": "CompleteModel",
    "full_inst_rw_overlap_check": "FullInstRWOverlapCheck",
    "enable_intervals": "EnableIntervals",
}


class Emitter:
    def __init__(
        self,
        program: Program,
        strict: bool = False,
        attrs: AttributeSchema | None = None,
        sorts: dict[str, str] | None = None,
    ):
        self.program = program
        self.strict = strict
        self.attrs = attrs
        self.sorts = sorts
        self.errors: list[Diagnostic] = []
        self.skips: list[Diagnostic] = []
        # Effect-aware findings from ``diagnose.py``.  Error findings are
        # folded into ``errors``; warnings and notes are advisory.
        self.findings: list = []
        self.features: dict[str, FeatureInfo] = {}
        self.machines: dict[str, MachineInfo] = {}
        # ProcessorItineraries emitted for a top-level block, keyed by the
        # machine name its ``sched_model`` names.
        self.top_itineraries: dict[str, str] = {}
        self.top_itin_text: list[str] = []
        # Scheduling vocabulary is target-global: instructions name these
        # records through ``Sched<[...]>``, so they cannot be per-machine.
        self.global_rw: dict[str, tuple[str, str]] = {}
        self.global_texts: list[str] = []
        # Def name -> declaring machine, for resources and res groups, so a
        # cross-machine collision can explain the rule instead of just firing.
        self.resource_defs: dict[str, str] = {}
        # The record table this lowering denotes.  Every `def` is registered
        # here and rendered from here, so `--records-json` cannot drift from
        # the printed TableGen.
        self.records = RecordSet()
        self.current_scope: str | None = None

    # -- diagnostics -----------------------------------------------------
    def error(self, where: str, message: str) -> None:
        self.errors.append(Diagnostic("error", where, message))

    def skip(self, where: str, message: str) -> None:
        diag = Diagnostic("skip", where, message)
        if self.strict:
            self.errors.append(Diagnostic("error", where, message))
        else:
            self.skips.append(diag)

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
        suffix: str = "",
        scope: str | None | _Unset = _UNSET,
    ) -> str:
        """Register a definition in the record table and return its text.

        ``cls``/``args`` describe the usual single-class def; ``bases`` adds
        further instantiations (``feature`` with a bitmask) or replaces the
        class list entirely when ``cls`` is None.  A duplicate name keeps the
        first record and still renders: the emitters already report genuine
        collisions (``seen_defs``, ``global_text``) with better messages, and
        a repeated identically-rendered global def is deduplicated later.
        """
        all_bases: list[Base] = []
        if cls is not None:
            all_bases.append(Base(cls, tuple(args)))
        for base in bases:
            all_bases.append(base if isinstance(base, Base) else Base.parse(base))
        record = Record(
            name=name,
            bases=tuple(all_bases),
            fields=tuple((field, str(value)) for field, value in fields),
            scope=self.current_scope if isinstance(scope, _Unset) else scope,
            style=style,
            suffix=suffix,
            origin=where,
        )
        try:
            self.records.add(name, list(all_bases), record.fields,
                             scope=record.scope, style=style, suffix=suffix,
                             origin=where)
        except DuplicateRecord:
            pass
        return record.render()

    # -- driver ----------------------------------------------------------
    def run(self) -> str:
        self.findings = diagnose_program(self.program)
        for finding in self.findings:
            if finding.level == FINDING_ERROR:
                self.error(finding.where, finding.message)

        self.collect_features()
        self.declare_machines()
        self.collect_global_vocabulary()
        self.lower_top_itineraries()

        self.set_evaluator = (
            SetEvaluator(self.program, self.attrs) if self.attrs is not None else None
        )
        if self.set_evaluator is not None:
            # Evaluate every declared set eagerly so an unused cycle or type
            # error is still diagnosed.
            for decl in self.program.decls:
                if isinstance(decl, SetDecl):
                    self.set_evaluator.expand_set(decl.name, f"set {decl.name}")
        elif any(isinstance(d, SetDecl) for d in self.program.decls):
            for decl in self.program.decls:
                if isinstance(decl, SetDecl):
                    self.skip(
                        f"set {decl.name}",
                        "requires the host attribute schema (--attrs); sets emit no records",
                    )

        self.predicates = PredicateLowerer(
            program=self.program,
            feature_defs={name: info.def_name for name, info in self.features.items()},
            sets=self.set_evaluator,
            sorts=self.collect_sorts(),
            machine_models={
                name: f"{info.def_name}Model"
                for name, info in self.machines.items()
                if info.decl.alias is None
            },
            reserved_names=self.reserved_def_names(),
            records=self.records,
        )
        early = self.predicates.lower_early()

        self.lower_machines()
        late = self.predicates.lower_late()
        try:
            self.predicates.finish()
        except PredicateError as exc:
            for message in exc.errors:
                self.error("lowering", message)

        if self.errors:
            raise SSDLEmitError(self.errors)

        chunks: list[str] = []
        for info in self.features.values():
            chunks.append(info.text)
        chunks.extend(self.top_itin_text)
        chunks.extend(early)
        chunks.extend(self.global_texts)
        for info in self.machines.values():
            chunks.append(info.text)
        chunks.extend(late)
        return "\n".join(c for c in chunks if c).rstrip() + "\n"

    def collect_sorts(self) -> dict[str, str]:
        if self.sorts is not None:
            return self.sorts
        checker = validate_mod.SortChecker(self.program)
        checker.run()  # sort errors are reported by ``validate.py``; lowering
        return checker.sorts  # needs only the inferred map.

    def reserved_def_names(self) -> set[str]:
        """Def names the machine lowering will emit.

        The predicate lowerer refuses to emit a record whose name is already
        taken; feeding it the names the emitter itself plans to declare keeps
        that check from firing on legitimate predicate names.
        """
        names: set[str] = set()
        for info in self.features.values():
            names.add(info.def_name)
        for info in self.machines.values():
            names.add(info.def_name)
            if info.decl.alias is not None:
                continue
            names.add(f"{info.def_name}Model")
            for item in info.decl.items:
                kind = item.get("kind")
                if kind in ("resource", "res_group", "func_unit", "bypass",
                            "itin_class", "reg_file"):
                    names.add(item["name"])
                elif kind == "write":
                    names.add(item["name"])
                    names.add(f"{info.def_name}_WR_{item['name']}")
                elif kind == "read":
                    names.add(item["name"])
                    names.add(f"{info.def_name}_RA_{item['name']}")
                elif kind == "retire_unit":
                    names.add(f"{info.def_name}RetireUnit")
                elif kind == "write_seq":
                    names.add(item["name"])
                elif kind in ("variant", "read_variant"):
                    names.add(f"{info.def_name}_{item['name']}Variant")
                    names.add(f"{info.def_name}_SSDLVariantAlias_{item['name']}")
                elif kind == "alias":
                    names.add(
                        td_def_name(
                            f"{info.def_name}_SchedAlias_{item['src']}_{item['dst']}"
                        )
                    )
                elif kind == "map_itin":
                    names.add(
                        td_def_name(
                            f"{info.def_name}_ITINRW_" + "_".join(item["itins"])
                        )
                        if item["itins"]
                        else td_def_name(f"{info.def_name}_ITINRW_")
                    )
                elif kind in ("load_queue", "store_queue"):
                    prefix = "LoadQueue_" if kind == "load_queue" else "StoreQueue_"
                    names.add(f"{info.def_name}_" + prefix + item["resource"])
                elif kind == "itineraries":
                    names.update(self.itinerary_names(item["decl"], info.def_name))
        for text in self.top_itin_text:
            for name in re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
                names.add(name)
        return names

    # -- features --------------------------------------------------------
    def collect_features(self) -> None:
        for decl in self.program.decls:
            if not isinstance(decl, FeatureDecl):
                continue
            where = f"feature {decl.name}"
            if decl.name in self.features:
                self.error(where, "duplicate feature name")
                continue
            kind = decl.fields.get("kind", "plain")
            if kind not in FEATURE_FIELDS:
                self.error(where, f"unknown kind {kind!r}; expected plain, extension or tune")
                continue
            allowed = FEATURE_FIELDS[kind]
            for key in decl.fields:
                if key != "kind" and key not in allowed:
                    self.error(
                        where,
                        f"field {key!r} is not allowed for kind = {kind} "
                        f"(allowed: {', '.join(sorted(allowed))})",
                    )
            for key in FEATURE_REQUIRED[kind]:
                if key not in decl.fields:
                    self.error(where, f"kind = {kind} requires field {key!r}")

            if kind == "extension":
                def_name = decl.name if decl.name.startswith("Feature") else "Feature" + decl.name
            else:
                def_name = decl.name
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", def_name):
                self.error(where, f"{def_name!r} is not a valid TableGen def name")
                continue
            self.features[decl.name] = FeatureInfo(decl, kind, def_name)

        for info in self.features.values():
            info.text = self.lower_feature(info)

    def _resolve_features(self, where: str, names: Any) -> list[str]:
        out = []
        for name in names:
            if name not in self.features:
                self.error(where, f"unknown feature {name!r}")
                continue
            out.append(self.features[name].def_name)
        return out

    def _inline_arg(self, where: str, fields: dict[str, Any]) -> str | None:
        if "inline_behavior" not in fields:
            return None
        behavior = fields["inline_behavior"]
        if behavior not in INLINE_BEHAVIOR:
            self.error(
                where,
                f"unknown inline_behavior {behavior!r}; "
                f"expected one of {', '.join(sorted(INLINE_BEHAVIOR))}",
            )
            return None
        return INLINE_BEHAVIOR[behavior]

    def lower_feature(self, info: FeatureInfo) -> str:
        decl = info.decl
        fields = decl.fields
        where = f"feature {decl.name}"
        desc = fields.get("desc", "")
        implies = self._resolve_features(where, fields.get("implies", []))
        if info.kind in ("extension", "tune"):
            for name in fields.get("implies", []):
                target = self.features.get(name)
                if target is not None and target.kind != info.kind:
                    self.error(
                        where,
                        f"kind = {info.kind} can only imply kind = {info.kind}, "
                        f"but {name!r} is kind = {target.kind}",
                    )
        inline = self._inline_arg(where, fields)

        if info.kind == "plain":
            mattr = fields.get("mattr")
            if mattr is None:
                return ""
            args = [
                td_str(mattr),
                td_str(fields.get("field", "")),
                td_str(fields.get("value", "")),
                td_str(desc),
            ]
            if implies:
                args.append(join_list(implies))
            if inline is not None:
                if not implies:
                    args.append("[]")
                args.append(inline)
            return self.define(where, info.def_name, "SubtargetFeature", args)

        if info.kind == "extension":
            version = fields.get("version")
            if version is None:
                return ""
            match = re.fullmatch(r"(\d+)\.(\d+)", version)
            if not match:
                self.error(where, f"version {version!r} must be MAJOR.MINOR")
                return ""
            major, minor = match.group(1), match.group(2)
            experimental = bool(fields.get("experimental", False))
            if experimental and fields.get("implies"):
                self.error(
                    where,
                    "experimental extensions with implies need RISCVExperimentalExtension, "
                    "whose implies list is list<RISCVExtension>; not lowered",
                )
            cls = "RISCVExperimentalExtension" if experimental else "RISCVExtension"
            args = [major, minor, td_str(desc)]
            if implies:
                args.append(join_list(implies))
            elif "field" in fields:
                # fieldname/value are positional after implies; keep the
                # default implies list so they do not shift into its slot.
                args.append("[]")
            if "field" in fields:
                args.append(td_str(fields["field"]))
                args.append(td_str(fields.get("value", "true")))
            elif "value" in fields:
                self.error(where, "value requires field for kind = extension")
            bases = [Base(cls, tuple(args))]
            mask = fields.get("bitmask")
            if mask is not None:
                group, bit = mask
                if not 0 <= group <= 7:
                    self.error(where, f"bitmask group {group} does not fit bits<3> (0..7)")
                if bit < 0:
                    self.error(where, f"bitmask bit {bit} must be non-negative")
                bases.append(Base("RISCVExtensionBitmask", (str(group), str(bit))))
            return self.define(where, info.def_name, bases=bases)

        # kind == tune
        positive = fields.get("positive_directive")
        if positive is None:
            return ""
        negative = fields.get("negative_directive", "no-" + positive)
        args = [
            td_str(positive),
            td_str(positive),
            td_str(negative),
            td_str(fields.get("field", "")),
            td_str(fields.get("value", "")),
            td_str(desc),
        ]
        if implies:
            args.append(join_list(implies))
        if inline is not None:
            if not implies:
                args.append("[]")
            args.append(inline)
        return self.define(where, info.def_name, "RISCVTuneFeature", args)

    # -- itineraries -----------------------------------------------------
    def itinerary_def_name(self, decl: ItinerariesDecl, owner_def: str) -> str:
        if decl.sched_model is not None:
            return td_def_name(decl.sched_model) + "Itineraries"
        return owner_def + "Itineraries"

    def itin_class_name(self, name: str) -> str:
        base = td_def_name(name)
        return base if base.startswith("IIC_") else "IIC_" + base

    def itinerary_names(self, decl: ItinerariesDecl, owner_def: str) -> set[str]:
        names = {self.itinerary_def_name(decl, owner_def)}
        for item in decl.items:
            if item["kind"] in ("func_units", "bypasses"):
                names.update(item["value"])
            elif item["kind"] == "itin":
                names.add(self.itin_class_name(item["name"]))
        return names

    def lower_stage(self, stage: dict[str, Any], where: str, units: set[str]) -> str:
        for unit in stage["units"]:
            if unit not in units:
                self.error(where, f"stage names unknown functional unit {unit!r}")
        args = [str(stage["cycles"]), join_list(stage["units"])]
        if stage["timeinc"] is not None or stage["kind"] is not None:
            args.append(str(stage["timeinc"]) if stage["timeinc"] is not None else "-1")
        if stage["kind"] is not None:
            kind = {"required": "Required", "reserved": "Reserved"}.get(stage["kind"])
            if kind is None:
                self.error(where, f"unknown stage kind {stage['kind']!r}")
                kind = "Required"
            args.append(kind)
        return f"InstrStage<{', '.join(args)}>"

    def lower_itineraries(
        self,
        decl: ItinerariesDecl,
        owner_def: str,
        where: str,
        declared_units: set[str],
        declared_bypasses: set[str],
    ) -> tuple[list[str], str]:
        out: list[str] = []
        func_units: list[str] = []
        bypasses: list[str] = []
        itins: list[dict[str, Any]] = []
        for item in decl.items:
            if item["kind"] == "func_units":
                func_units = list(item["value"])
            elif item["kind"] == "bypasses":
                bypasses = list(item["value"])
            elif item["kind"] == "itin":
                itins.append(item)

        known_units = set(func_units) | set(declared_units)
        known_bypasses = set(bypasses) | set(declared_bypasses)
        for unit in func_units:
            if unit not in declared_units:
                out.append(self.define(where, unit, "FuncUnit", scope=None))
        for bypass in bypasses:
            if bypass not in declared_bypasses:
                out.append(self.define(where, bypass, "Bypass", scope=None))

        iid: list[str] = []
        seen_itins: set[str] = set()
        for item in itins:
            name = item["name"]
            if name in seen_itins:
                self.error(where, f"duplicate itin {name!r}")
                continue
            seen_itins.add(name)
            cls = self.itin_class_name(name)
            out.append(self.define(where, cls, "InstrItinClass", scope=None))
            fields = item["fields"]
            raw_stages = fields.get("stages", ("stages", []))
            stages = [self.lower_stage(stage, f"{where}.{name}", known_units) for stage in raw_stages[1]]
            cycles = [int(x) for x in fields.get("cycles", [])]
            itin_bypasses = list(fields.get("bypasses", []))
            for bypass in itin_bypasses:
                if bypass not in known_bypasses:
                    self.error(where, f"itin {name}: unknown bypass {bypass!r}")
            uops = int(fields.get("uops", 1))
            args = [cls, join_list(stages)]
            if cycles or itin_bypasses or uops != 1:
                args.append(join_list([str(c) for c in cycles]))
            if itin_bypasses or uops != 1:
                args.append(join_list(itin_bypasses))
            if uops != 1:
                args.append(str(uops))
            iid.append(f"InstrItinData<{', '.join(args)}>")

        def_name = self.itinerary_def_name(decl, owner_def)
        fields = (
            [("PacketizerNamespace", td_str(decl.namespace))]
            if decl.namespace
            else []
        )
        out.append(
            self.define(
                where,
                def_name,
                "ProcessorItineraries",
                [join_list(func_units), join_list(bypasses), join_list(iid)],
                fields,
                scope=None,
            )
        )
        return out, def_name

    def lower_top_itineraries(self) -> None:
        for decl in self.program.decls:
            if not isinstance(decl, ItinerariesDecl):
                continue
            where = "itineraries"
            if decl.sched_model is None:
                self.error(where, "top-level itineraries must set 'sched_model = IDENT'")
                continue
            if decl.sched_model not in self.machines:
                self.error(
                    where, f"sched_model {decl.sched_model!r} is not a declared machine"
                )
                continue
            info = self.machines[decl.sched_model]
            if info.decl.alias is not None:
                self.error(
                    where,
                    f"sched_model {decl.sched_model!r} is not a machine definition",
                )
                continue
            if self.machine_role(info.decl) == "cpu":
                self.error(
                    where,
                    f"sched_model {decl.sched_model!r} is a cpu, not a model",
                )
                continue
            if decl.sched_model in self.top_itineraries:
                self.error(
                    where,
                    f"duplicate top-level itineraries for machine {decl.sched_model!r}",
                )
                continue
            texts, def_name = self.lower_itineraries(
                decl, info.def_name, f"itineraries({decl.sched_model})", set(), set()
            )
            self.top_itin_text.extend(texts)
            self.top_itineraries[decl.sched_model] = def_name

    # -- machines --------------------------------------------------------
    def declare_machines(self) -> None:
        for decl in self.program.decls:
            if not isinstance(decl, MachineDecl):
                continue
            if decl.name in self.machines:
                self.error(f"machine {decl.name}", "duplicate machine name")
                continue
            self.machines[decl.name] = MachineInfo(decl, td_def_name(decl.name))

    def collect_global_vocabulary(self) -> None:
        """Collect the target-level scheduling vocabulary.

        Upstream defines ``SchedWrite``/``SchedRead`` once per target and lets
        every instruction name them through ``Sched<[...]>`` (see
        ``RISCVSchedule.td``); only the ``WriteRes``/``ReadAdvance`` bindings are
        per model.  Machine blocks are the only place SSDL can declare them, so
        they are hoisted here and emitted exactly once.
        """
        for info in self.machines.values():
            if info.decl.alias is not None:
                continue
            for item in info.decl.items:
                kind = item.get("kind")
                if kind not in ("write", "read", "write_seq"):
                    continue
                name = item["name"]
                owner = self.global_rw.get(name)
                if owner is not None and owner[0] != kind:
                    self.error(
                        f"machine {info.decl.name}",
                        f"{name!r} is already declared as a {owner[0]} by "
                        f"{owner[1]}",
                    )
                    continue
                self.global_rw.setdefault(name, (kind, f"machine {info.decl.name}"))
            for item in info.decl.items:
                if item.get("kind") not in ("resource", "res_group"):
                    continue
                name = item["name"]
                previous = self.resource_defs.get(name)
                if previous is not None:
                    self.error(
                        f"machine {info.decl.name}",
                        f"resource {name!r} is already defined by {previous!r}; "
                        "resource names are global TableGen defs, so give each "
                        "model's resources distinct names",
                    )
                else:
                    self.resource_defs[name] = f"machine {info.decl.name}"

    def lower_machines(self) -> None:
        # Alias targets may be declared later, so resolve in a second pass.
        seen_defs: dict[str, str] = {}
        global_text: dict[str, str] = {}
        global_owner: dict[str, str] = {}
        for info in self.machines.values():
            lowered, globals_ = self.lower_machine(info)
            for chunk in globals_:
                match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)", chunk)
                if match is None:  # pragma: no cover - emitter always names defs
                    continue
                def_name = match.group(1)
                if def_name in global_text:
                    if global_text[def_name] != chunk:
                        kind = self.global_rw.get(def_name, ("declaration", ""))[0]
                        self.error(
                            f"machine {info.decl.name}",
                            f"conflicting declaration of scheduling {kind} "
                            f"{def_name!r}: {global_owner[def_name]} declares it "
                            "differently",
                        )
                    continue
                global_text[def_name] = chunk
                global_owner[def_name] = f"machine {info.decl.name}"
                self.global_texts.append(chunk)
            for def_name in re.findall(
                r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)", lowered, re.MULTILINE
            ):
                if def_name in seen_defs:
                    if def_name in self.resource_defs:
                        continue  # already reported with the resource-specific hint
                    self.error(
                        f"machine {info.decl.name}",
                        f"emitted def {def_name!r} collides with {seen_defs[def_name]!r}",
                    )
                seen_defs[def_name] = f"machine {info.decl.name}"
            info.text = lowered

    def resolve_rw(
        self, entries: list[str], writes: set[str], reads: set[str], where: str
    ) -> list[str]:
        resolved = []
        for entry in entries:
            if entry in RESERVED_RW:
                resolved.append(RESERVED_RW[entry])
            elif entry in writes or entry in reads:
                resolved.append(entry)
            else:
                self.error(where, f"unknown SchedReadWrite {entry!r}")
        return resolved

    def machine_role(self, decl) -> str:
        for item in decl.items:
            if item.get("kind") == "prop" and item.get("name") == "role":
                return item.get("value", "both")
        return "both"

    def lower_machine(self, info: MachineInfo) -> tuple[str, list[str]]:
        decl = info.decl
        where = f"machine {decl.name}"
        if decl.alias is not None:
            if decl.alias not in self.machines:
                self.error(where, f"alias target {decl.alias!r} is not a declared machine")
                return "", []
            return (
                self.define(
                    where,
                    info.def_name,
                    "ProcessorAlias",
                    [td_str(decl.name), td_str(decl.alias)],
                    scope=None,
                ),
                [],
            )

        props: dict[str, Any] = {}
        items = {
            "resource": [],
            "res_group": [],
            "write": [],
            "read": [],
            "write_seq": [],
            "func_unit": [],
            "bypass": [],
            "itin_class": [],
            "combo_func": [],
            "bind": [],
            "advance": [],
            "map": [],
            "map_itin": [],
            "alias": [],
            "variant": [],
            "read_variant": [],
            "reg_file": [],
            "retire_unit": [],
            "load_queue": [],
            "store_queue": [],
            "itineraries": [],
        }
        for item in decl.items:
            kind = item["kind"]
            if kind == "prop":
                props[item["name"]] = item["value"]
            elif kind in items:
                items[kind].append(item)
            else:  # pragma: no cover - parser only produces known kinds
                self.error(where, f"unknown machine item {kind!r}")

        for key in props:
            if key not in SCHED_MODEL_PROPS and key not in (
                "features",
                "tune_features",
                "default_march",
                "unsupported_features",
                "role",
                "model",
            ):
                self.error(where, f"unknown machine property {key!r}")

        role = props.get("role", "both")
        if role not in ("both", "model", "cpu"):
            self.error(where, f"unknown machine role {role!r}")
            role = "both"
        model_name = props.get("model")
        if role == "cpu":
            if model_name is None:
                self.error(where, "a cpu must name its model: model = IDENT")
            elif model_name not in self.machines:
                self.error(where, f"model {model_name!r} is not a declared machine")
            elif self.machine_role(self.machines[model_name].decl) == "cpu":
                self.error(where, f"model {model_name!r} is itself a cpu")
        elif model_name is not None:
            self.error(where, "'model =' is only allowed on a cpu")
        if role == "model":
            for key in ("features", "tune_features", "default_march"):
                if key in props:
                    self.error(where, f"{key} belongs on a cpu, not a model")
        if role == "cpu":
            for key in SCHED_MODEL_PROPS:
                if key in props:
                    self.error(where, f"{key} belongs on a model, not a cpu")
            if "unsupported_features" in props:
                self.error(where, "unsupported_features belongs on a model, not a cpu")
            for kind, values in items.items():
                if values:
                    self.error(where, f"a cpu cannot declare {kind}")

        skip_messages = {
            "combo_func": "combo_func has no attachment point on ProcessorItineraries",
        }
        for kind, message in skip_messages.items():
            for item in items[kind]:
                name = item.get("name") or item.get("kind")
                self.skip(f"{where}.{name}", message)

        # Records that carry `SchedModel` must be scoped to this model.  A
        # single `let SchedModel = ... in { ... }` cannot contain SchedWrite /
        # SchedRead defs (they have no such field), so the output is split:
        #   global  - target-level vocabulary: SchedWrite/SchedRead,
        #             WriteSequence, FuncUnit/Bypass/InstrItinClass
        #   prelude - ProcessorItineraries (no SchedModel, but model-specific)
        #   scoped  - ProcResource, WriteRes, ReadAdvance, InstRW, SchedAlias,
        #             SchedVariant, RegisterFile, RetireControlUnit, queues
        global_out: list[str] = []
        prelude: list[str] = []
        scoped: list[str] = []
        out = scoped
        # Machine-scoped records live in one TableGen def namespace.  Upstream
        # disambiguates them with `defm` (SiFive7PipeA, SiFive7IDivRemSched
        # WriteVariant); we do the same with the machine def name as prefix so
        # two models can be described from the same vocabulary.
        prefix = info.def_name + "_"
        # Everything below until the machine model record is scoped to this
        # model through one `let SchedModel = ... in {}` block.
        self.current_scope = f"{info.def_name}Model"

        seen_res: set[str] = set()
        for item in items["resource"]:
            name = item["name"]
            if name in seen_res:
                self.error(where, f"duplicate resource {name!r}")
                continue
            seen_res.add(name)
            res_props = item["props"]
            for key in res_props:
                if key not in ("units", "buffer", "super"):
                    self.error(where, f"resource {name}: unknown property {key!r}")
            units = res_props.get("units", 1)
            body = []
            if "super" in res_props:
                super_name = res_props["super"]
                if super_name not in seen_res and not any(
                    r["name"] == super_name for r in items["resource"]
                ):
                    self.error(where, f"resource {name}: unknown super {super_name!r}")
                body.append(("Super", str(super_name)))
            if "buffer" in res_props:
                body.append(("BufferSize", str(res_props["buffer"])))
            out.append(self.define(where, name, "ProcResource", [str(units)], body))

        for item in items["res_group"]:
            name = item["name"]
            if name in seen_res:
                self.error(where, f"duplicate resource {name!r}")
                continue
            seen_res.add(name)
            members = []
            for member in item["resources"]:
                if member not in seen_res:
                    self.error(where, f"res_group {name}: unknown resource {member!r}")
                members.append(member)
            res_props = item["props"]
            for key in res_props:
                if key not in ("buffer",):
                    self.error(where, f"res_group {name}: unknown property {key!r}")
            body = []
            if "buffer" in res_props:
                body.append(("BufferSize", str(res_props["buffer"])))
            out.append(
                self.define(where, name, "ProcResGroup", [join_list(members)], body)
            )

        writes: set[str] = {
            name for name, (kind, _) in self.global_rw.items() if kind != "read"
        }
        reads: set[str] = {
            name for name, (kind, _) in self.global_rw.items() if kind == "read"
        }
        # A variant subject is rewritten as `SchedAlias<subject, Variant>`, so
        # the subject itself can no longer carry WriteRes/ReadAdvance.  Upstream
        # (RISCV SiFive7, AArch64) keeps the subject as pure vocabulary and
        # points every SchedVar at a concrete record.  SSDL lets a branch name
        # the subject itself ("keep this model's own resources"); materialise
        # that record once so such a branch stays acyclic in llvm-tblgen.
        variant_writes = {item["name"] for item in items["variant"]}
        variant_reads = {item["name"] for item in items["read_variant"]}
        concrete_rw: dict[str, str] = {}
        alias_srcs = {item["src"] for item in items["alias"]}
        alias_dsts = {item["dst"] for item in items["alias"]}
        for subject in sorted(variant_writes | variant_reads):
            kind = "variant" if subject in variant_writes else "read_variant"
            if subject in alias_srcs:
                self.error(
                    where,
                    f"{subject!r} is both an alias source and a {kind} subject; "
                    "a SchedAlias can have only one match per processor",
                )
            if subject in alias_dsts:
                self.error(
                    where,
                    f"{subject!r} is both an alias target and a {kind} subject; "
                    "TableGen cannot alias an alias",
                )
        local_writes: set[str] = set()
        for item in items["write"]:
            if item["name"] in local_writes:
                self.error(where, f"duplicate write {item['name']!r}")
                continue
            local_writes.add(item["name"])
            global_out.append(self.define(where, item["name"], "SchedWrite", scope=None))
        local_reads: set[str] = set()
        for item in items["read"]:
            if item["name"] in local_reads:
                self.error(where, f"duplicate read {item['name']!r}")
                continue
            local_reads.add(item["name"])
            global_out.append(self.define(where, item["name"], "SchedRead", scope=None))

        # WriteSequence is itself a SchedWrite, so collect the names first to
        # allow a sequence to refer to another sequence declared later.
        write_seqs: dict[str, dict[str, Any]] = {}
        for item in items["write_seq"]:
            name = item["name"]
            if name in local_writes or name in write_seqs:
                self.error(where, f"duplicate write {name!r}")
                continue
            owner = self.global_rw.get(name)
            if owner is not None and owner[0] != "write_seq":
                self.error(
                    where,
                    f"{name!r} is already declared as a {owner[0]} by {owner[1]}",
                )
                continue
            write_seqs[name] = item
        writes.update(write_seqs)
        for name, item in write_seqs.items():
            seq = []
            for member in item["writes"]:
                if member not in writes:
                    self.error(where, f"write_seq {name}: unknown write {member!r}")
                seq.append(member)
            args = [join_list(seq)]
            repeat = item.get("repeat")
            if repeat is not None and repeat != 1:
                args.append(str(repeat))
            global_out.append(
                self.define(where, name, "WriteSequence", args, scope=None)
            )

        declared_units: set[str] = set()
        for item in items["func_unit"]:
            if item["name"] in declared_units:
                self.error(where, f"duplicate func_unit {item['name']!r}")
            declared_units.add(item["name"])
            global_out.append(self.define(where, item["name"], "FuncUnit", scope=None))
        declared_bypasses: set[str] = set()
        for item in items["bypass"]:
            if item["name"] in declared_bypasses:
                self.error(where, f"duplicate bypass {item['name']!r}")
            declared_bypasses.add(item["name"])
            global_out.append(self.define(where, item["name"], "Bypass", scope=None))
        for item in items["itin_class"]:
            global_out.append(
                self.define(where, item["name"], "InstrItinClass", scope=None)
            )

        for item in items["alias"]:
            src, dst = item["src"], item["dst"]
            resolved = self.resolve_rw([src, dst], writes, reads, f"{where}.alias")
            if len(resolved) != 2:
                continue
            def_name = td_def_name(f"{prefix}SchedAlias_{src}_{dst}")
            out.append(
                self.define(where, def_name, "SchedAlias", [resolved[0], resolved[1]])
            )

        bound: set[str] = set()
        for item in items["bind"]:
            bind_props = item["props"]
            write = item["write"]
            if write not in writes:
                self.error(where, f"bind: unknown write {write!r}")
                continue
            if write in bound:
                self.error(where, f"bind: duplicate binding for write {write!r}")
                continue
            bound.add(write)
            resources = []
            for res in item["resources"]:
                if res not in seen_res:
                    self.error(where, f"bind {write}: unknown resource {res!r}")
                resources.append(res)
            for key in bind_props:
                if key not in (
                    "latency",
                    "uops",
                    "release",
                    "acquire",
                    "begin_grp",
                    "end_grp",
                    "single",
                    "retire_ooo",
                    "unsupported",
                ):
                    self.error(where, f"bind {write}: unknown property {key!r}")
            body = []
            for key, field in (
                ("latency", "Latency"),
                ("uops", "NumMicroOps"),
                ("release", "ReleaseAtCycles"),
                ("acquire", "AcquireAtCycles"),
                ("begin_grp", "BeginGroup"),
                ("end_grp", "EndGroup"),
                ("single", "SingleIssue"),
                ("retire_ooo", "RetireOOO"),
                ("unsupported", "Unsupported"),
            ):
                if key in bind_props:
                    value = bind_props[key]
                    if isinstance(value, bool):
                        value = "true" if value else "false"
                    elif isinstance(value, list):
                        value = join_list([str(v) for v in value])
                    body.append((field, value))
            resources_text = join_list(resources)
            if write in variant_writes:
                # Variant subjects are aliased away, so their own binding has
                # to survive as a standalone SchedWriteRes (see the note above).
                out.append(
                    self.define(
                        where,
                        f"{prefix}WRes_{write}",
                        "SchedWriteRes",
                        [resources_text],
                        body,
                    )
                )
                concrete_rw[write] = f"{prefix}WRes_{write}"
            else:
                out.append(
                    self.define(
                        where,
                        f"{prefix}WR_{write}",
                        "WriteRes",
                        [write, resources_text],
                        body,
                    )
                )

        advanced: set[str] = set()
        for item in items["advance"]:
            adv_props = item["props"]
            read = item["read"]
            if read not in reads:
                self.error(where, f"advance: unknown read {read!r}")
                continue
            if read in advanced:
                self.error(where, f"advance: duplicate advance for read {read!r}")
                continue
            advanced.add(read)
            for key in adv_props:
                if key not in ("cycles", "writes", "tunables", "unsupported"):
                    self.error(where, f"advance {read}: unknown property {key!r}")
            if "cycles" not in adv_props:
                self.error(where, f"advance {read}: cycles is required (LLVM ReadAdvance has no default)")
                continue
            valid_writes = []
            for w in adv_props.get("writes", []):
                if w not in writes:
                    self.error(where, f"advance {read}: unknown write {w!r}")
                valid_writes.append(w)
            tunables = adv_props.get("tunables", [])
            if read in variant_reads:
                # Same reasoning as for writes: the subject becomes an alias,
                # so its advance has to live on a standalone SchedReadAdvance.
                args = [str(adv_props["cycles"])]
                if valid_writes or tunables:
                    args.append(join_list(valid_writes))
                if tunables:
                    args.append(join_list([str(t) for t in tunables]))
                out.append(
                    self.define(
                        where,
                        f"{prefix}RAdv_{read}",
                        "SchedReadAdvance",
                        args,
                        [("Unsupported", "true")] if adv_props.get("unsupported") else [],
                        style="inline",
                    )
                )
                concrete_rw[read] = f"{prefix}RAdv_{read}"
            else:
                args = [read, str(adv_props["cycles"])]
                if valid_writes or tunables:
                    args.append(join_list(valid_writes))
                if tunables:
                    args.append(join_list([str(t) for t in tunables]))
                out.append(
                    self.define(
                        where,
                        f"{prefix}RA_{read}",
                        "ReadAdvance",
                        args,
                        [("Unsupported", "true")] if adv_props.get("unsupported") else [],
                        style="inline",
                    )
                )

        map_names: set[str] = set()
        for index, item in enumerate(items["map"]):
            _, inst_entries = item["insts"]
            insts = inst_entries
            rw = item["rw"]
            if not insts:
                self.error(where, "map: empty instruction list")
                continue
            if not rw:
                self.error(where, "map: empty SchedReadWrite list")
                continue
            idents = [i for i in insts if i[0] == "ident"]
            regexes = [i for i in insts if i[0] == "regex"]
            resolved = []
            for entry in rw:
                if entry in RESERVED_RW:
                    resolved.append(RESERVED_RW[entry])
                elif entry in writes or entry in reads:
                    resolved.append(entry)
                else:
                    self.error(where, f"map: unknown SchedReadWrite {entry!r}")

            # InstRW carries one dag.  A mixed map is therefore split into one
            # (instrs ...) record and one (instregex ...) record; the 1.1 norm
            # requires this split rather than rejecting the map.
            mixed = bool(idents and regexes)
            groups: list[tuple[str | None, list[tuple[str, str]]]] = []
            if mixed:
                groups.append(("instrs", idents))
                groups.append(("regex", regexes))
            else:
                groups.append((None, insts))

            for group_kind, entries in groups:
                pieces: list[str] = []
                if group_kind == "regex" or (group_kind is None and regexes):
                    patterns = []
                    for _, pattern in entries:
                        patterns.append(td_str(pattern))
                        sanitized = re.sub(r"[^A-Za-z0-9_]", "", pattern) or f"{index}"
                        pieces.append("REGEX_" + sanitized)
                    dag = "(instregex " + ", ".join(patterns) + ")"
                else:
                    names = []
                    for _, name in entries:
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                            self.error(where, f"map: {name!r} is not a TableGen instruction name")
                        names.append(name)
                        pieces.append(name)
                    dag = "(instrs " + ", ".join(names) + ")"
                def_name = prefix + "IRW_" + "_".join(pieces)
                if mixed:
                    def_name += "_" + group_kind
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", def_name):
                    def_name = f"{prefix}IRW_map_{index}"
                if def_name in map_names:
                    self.error(where, f"map: duplicate emitted record name {def_name!r}")
                    continue
                map_names.add(def_name)
                fields = (
                    [("Unsupported", "true" if item["unsupported"] else "false")]
                    if item.get("unsupported") is not None
                    else []
                )
                out.append(
                    self.define(
                        where,
                        def_name,
                        "InstRW",
                        [join_list(resolved), dag],
                        fields,
                        style="inline",
                    )
                )

        # A read with no `advance` already means "read as usual", which is
        # exactly ReadDefault; that is the concrete record for a self-reference.
        for read in variant_reads:
            concrete_rw.setdefault(read, "ReadDefault")

        variant_edges: dict[str, set[str]] = {}
        variant_subjects = variant_writes | variant_reads
        for kind in ("variant", "read_variant"):
            for item in items[kind]:
                name = item["name"]
                cls = "SchedWriteVariant" if kind == "variant" else "SchedReadVariant"
                target_ok = name in writes if kind == "variant" else name in reads
                if not target_ok:
                    expected = "write" if kind == "variant" else "read"
                    self.error(
                        f"{where}.{name}",
                        f"{kind} must name a declared {expected}",
                    )
                    continue
                if item["default"] is None:
                    self.error(
                        f"{where}.{name}",
                        "variant requires an explicit default case (P1-12)",
                    )
                    continue

                # A self-reference selects the model's own binding; every other
                # name is used as written (chained through its own alias).
                def concretise(
                    entries: list[str], subject: str = name
                ) -> list[str]:
                    return [
                        concrete_rw.get(entry, entry) if entry == subject else entry
                        for entry in entries
                    ]

                def resolve(entries: list[str], subject: str = name) -> list[str]:
                    return self.resolve_rw(
                        entries, writes, reads, f"{where}.{subject}"
                    )

                cases = []
                resolved_all: list[str] = []
                for case in item["cases"]:
                    selected = resolve(case["selected"])
                    resolved_all.extend(selected)
                    pred = self.predicates.to_sched(
                        case["pred"], {}, f"{where}.{name}"
                    ) if self.predicates is not None else "NoSchedPred"
                    cases.append(f"SchedVar<{pred}, {join_list(concretise(selected))}>")
                default = resolve(item["default"])
                resolved_all.extend(default)
                cases.append(
                    f"SchedVar<NoSchedPred, {join_list(concretise(default))}>"
                )

                if name in resolved_all and name not in concrete_rw:
                    expected = "write" if kind == "variant" else "read"
                    self.error(
                        f"{where}.{name}",
                        f"{name!r} selects itself but this machine does not "
                        f"describe its {expected} resources; add a `bind` or "
                        "name a distinct SchedReadWrite",
                    )
                variant_edges[name] = {
                    other
                    for other in resolved_all
                    if other in variant_subjects and other != name
                }
                variadic = bool(item.get("variadic"))
                out.append(
                    self.define(
                        where,
                        f"{prefix}{name}Variant",
                        cls,
                        [join_list(cases)],
                        [("Variadic", "true")] if variadic else [],
                        suffix=";" if variadic else "",
                    )
                )
                out.append(
                    self.define(
                        where,
                        f"{prefix}SSDLVariantAlias_{name}",
                        "SchedAlias",
                        [name, f"{prefix}{name}Variant"],
                    )
                )

        # Chained references are legal, but a cycle makes llvm-tblgen expand
        # variants forever inside inferFromRW; reject it with a location.
        state: dict[str, int] = {}

        def visit_variant(node: str, stack: list[str]) -> None:
            state[node] = 1
            stack.append(node)
            for nxt in sorted(variant_edges.get(node, ())):
                if state.get(nxt) == 1:
                    cycle = " -> ".join(stack[stack.index(nxt):] + [nxt])
                    self.error(where, f"variant cycle: {cycle}")
                elif state.get(nxt, 0) == 0:
                    visit_variant(nxt, stack)
            stack.pop()
            state[node] = 2

        for node in sorted(variant_edges):
            if state.get(node, 0) == 0:
                visit_variant(node, [])

        for item in items["reg_file"]:
            name = item["name"]
            fields = item["fields"]
            for key in fields:
                if key not in (
                    "num_phys",
                    "classes",
                    "costs",
                    "allow_move_elim",
                    "max_move_elim_per_cy",
                    "allow_zero_elim_only",
                ):
                    self.error(where, f"reg_file {name}: unknown field {key!r}")
            if "num_phys" not in fields:
                self.error(where, f"reg_file {name}: num_phys is required")
                continue
            args = [str(fields["num_phys"])]
            classes = list(fields.get("classes", []))
            costs = list(fields.get("costs", []))
            allow = list(fields.get("allow_move_elim", []))
            max_moves = fields.get("max_move_elim_per_cy")
            zero_only = fields.get("allow_zero_elim_only")
            if costs and len(costs) != len(classes):
                self.error(
                    where,
                    f"reg_file {name}: costs has {len(costs)} entries for "
                    f"{len(classes)} classes",
                )
            if allow and len(allow) != len(classes):
                self.error(
                    where,
                    f"reg_file {name}: allow_move_elim has {len(allow)} entries "
                    f"for {len(classes)} classes",
                )
            if classes or costs or allow or max_moves is not None or zero_only:
                args.append(join_list(classes))
            if costs or allow or max_moves is not None or zero_only:
                args.append(join_list([str(c) for c in costs]))
            if allow or max_moves is not None or zero_only:
                args.append(join_list(["true" if b else "false" for b in allow]))
            if max_moves is not None or zero_only:
                args.append(str(max_moves if max_moves is not None else 0))
            if zero_only:
                args.append("true" if zero_only else "false")
            out.append(self.define(where, name, "RegisterFile", args))

        for item in items["retire_unit"]:
            fields = item["fields"]
            for key in fields:
                if key not in ("buffer", "max_per_cycle"):
                    self.error(where, f"retire_unit: unknown field {key!r}")
            if "buffer" not in fields:
                self.error(where, "retire_unit: buffer is required")
                continue
            if "max_per_cycle" not in fields:
                self.error(
                    where,
                    "retire_unit: max_per_cycle is required (RetireControlUnit "
                    "takes both arguments)",
                )
                continue
            out.append(
                self.define(
                    where,
                    f"{info.def_name}RetireUnit",
                    "RetireControlUnit",
                    [str(fields["buffer"]), str(fields["max_per_cycle"])],
                )
            )

        for kind, cls, queue_prefix in (
            ("load_queue", "LoadQueue", "LoadQueue_"),
            ("store_queue", "StoreQueue", "StoreQueue_"),
        ):
            for item in items[kind]:
                res = item["resource"]
                if res not in seen_res:
                    self.error(where, f"{kind}: unknown resource {res!r}")
                out.append(
                    self.define(where, f"{prefix}{queue_prefix}{res}", cls, [res])
                )

        itineraries_name = self.top_itineraries.get(decl.name)
        for item in items["itineraries"]:
            if itineraries_name is not None:
                self.error(
                    where,
                    "itineraries are declared both top-level and inside the machine",
                )
                break
            texts, itineraries_name = self.lower_itineraries(
                item["decl"],
                info.def_name,
                f"{where}.itineraries",
                declared_units,
                declared_bypasses,
            )
            prelude.extend(texts)

        if items["map_itin"]:
            itin_classes = {item["name"] for item in items["itin_class"]}
            declared_itins: dict[str, str] = {}
            for block in items["itineraries"]:
                for entry in block["decl"].items:
                    if entry["kind"] == "itin":
                        declared_itins[entry["name"]] = self.itin_class_name(entry["name"])

            def resolve_itin(name: str) -> str | None:
                if name in itin_classes:
                    return name
                if name in declared_itins:
                    return declared_itins[name]
                base = name[4:] if name.startswith("IIC_") else name
                if base in itin_classes:
                    return base
                if base in declared_itins:
                    return declared_itins[base]
                return None

            for item in items["map_itin"]:
                resolved = self.resolve_rw(item["rw"], writes, reads, f"{where}.map_itin")
                classes = []
                for name in item["itins"]:
                    cls = resolve_itin(name)
                    if cls is None:
                        self.error(
                            where,
                            f"map_itin: unknown itinerary class {name!r}",
                        )
                        continue
                    classes.append(cls)
                if not classes:
                    self.error(where, "map_itin: empty itinerary class list")
                    continue
                def_name = td_def_name(f"{prefix}ITINRW_" + "_".join(classes))
                out.append(
                    self.define(
                        where, def_name, "ItinRW", [join_list(resolved), join_list(classes)]
                    )
                )

        # The machine model record is not itself scoped: it *is* the scope.
        self.current_scope = None
        model_text = None
        if role != "cpu":
            model_fields: list[tuple[str, str]] = []
            if itineraries_name is not None:
                model_fields.append(("Itineraries", itineraries_name))
            for key, field in SCHED_MODEL_PROPS.items():
                if key in props:
                    value = props[key]
                    if isinstance(value, bool):
                        value = "true" if value else "false"
                    model_fields.append((field, str(value)))
            if "unsupported_features" in props:
                preds = [f"Has{name}" for name in props["unsupported_features"]]
                model_fields.append(("UnsupportedFeatures", join_list(preds)))
            model_text = self.define(
                where, f"{info.def_name}Model", "SchedMachineModel", (), model_fields
            )

        processor_texts: list[str] = []
        if role == "model":
            features, tune, march = [], [], None
        else:
            features = props.get("features", [])
            tune = props.get("tune_features", [])
            march = props.get("default_march")
        if role == "cpu" and model_name is not None:
            sched_model_name = f"{td_def_name(model_name)}Model"
        else:
            sched_model_name = f"{info.def_name}Model"
        for name in features:
            if name in self.features and self.features[name].kind == "tune":
                self.error(where, f"tune feature {name!r} belongs in tune_features, not features")
        for name in tune:
            # TuneFeatures is list<SubtargetFeature>: upstream mixes plain
            # features (TuneSiFive7) with RISCVTuneFeature records.  Only an
            # architectural extension is misplaced here.
            if name in self.features and self.features[name].kind == "extension":
                self.error(
                    where,
                    f"extension feature {name!r} belongs in features, not tune_features",
                )
        fargs = self._resolve_features(where, features)
        targs = self._resolve_features(where, tune)
        if role == "model":
            pass
        elif features:
            args = [td_str(decl.name), sched_model_name, join_list(fargs)]
            if tune or march:
                args.append(join_list(targs))
            if march is not None:
                args.append(td_str(march))
            processor_texts.append(
                self.define(where, info.def_name, "RISCVProcessorModel", args)
            )
        else:
            if march is not None:
                self.error(where, "default_march requires at least one feature")
            args = [td_str(decl.name), sched_model_name]
            if tune:
                args.append(join_list(targs))
            processor_texts.append(
                self.define(where, info.def_name, "RISCVTuneProcessorModel", args)
            )

        chunks = list(prelude) + ([model_text] if model_text else [])
        if scoped:
            body = textwrap.indent("\n\n".join(scoped), "  ")
            chunks.append(f"let SchedModel = {info.def_name}Model in {{\n{body}\n}}")
        chunks.extend(processor_texts)
        return "\n\n".join(chunks), global_out


def emit(
    program: Program,
    strict: bool = False,
    includes: tuple[str, ...] = (),
    header_comment: str | None = None,
    attrs: AttributeSchema | None = None,
    sorts: dict[str, str] | None = None,
    target_include: str = "RISCV.td",
    records: RecordSet | None = None,
    findings: list | None = None,
) -> tuple[str, list[Diagnostic]]:
    """Lower ``program`` and prepend the includes the output needs to load.

    ``target_include`` is the target description that must be loaded first.
    It is ``RISCV.td`` for real use; the validation fixture points it at a
    minimal stub so ``-gen-subtarget`` can be exercised in CI.
    """
    emitter = Emitter(program, strict=strict, attrs=attrs, sorts=sorts)
    body = emitter.run()
    if records is not None:
        records.records.extend(emitter.records.records)
        records.by_name.update(emitter.records.by_name)
    if findings is not None:
        findings.extend(
            f for f in emitter.findings if f.level != FINDING_ERROR
        )
    header = []
    if header_comment:
        header.append(header_comment)
    header.append(f'include "{target_include}"')
    for include in includes:
        header.append(f'include "{include}"')
    return "\n".join(header) + "\n\n" + body, emitter.skips


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="SSDL source file")
    ap.add_argument("-o", "--output", help="write TableGen to this file (default: stdout)")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="treat every construct the emitter cannot lower as an error",
    )
    ap.add_argument(
        "--include",
        dest="includes",
        action="append",
        default=[],
        help="extra TableGen file to include (repeatable)",
    )
    ap.add_argument(
        "--attrs",
        default=None,
        help="JSON attribute schema used to evaluate set declarations",
    )
    ap.add_argument(
        "--target-include",
        default="RISCV.td",
        help="target description included first (default: RISCV.td)",
    )
    ap.add_argument(
        "--records-json",
        default=None,
        help="write the record table this lowering denotes (see record_ir.py)",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="also print notes (semantic context, not a problem)",
    )
    args = ap.parse_args(argv)

    try:
        attrs = AttributeSchema.load(args.attrs) if args.attrs else None
    except (OSError, ValueError) as exc:
        print(f"{args.attrs}: {exc}", file=sys.stderr)
        return 2

    try:
        program = parse_file(args.input)
    except Exception as exc:  # parse errors carry line/column
        print(f"{args.input}: {exc}", file=sys.stderr)
        return 2

    # Diagnose before lowering: a dead-field error should stop emission even
    # if the rest of the model is well formed, and warnings should survive an
    # unrelated lowering failure.
    findings = diagnose_program(program)
    errors = [f for f in findings if f.level == FINDING_ERROR]
    if errors:
        for line in render_findings(errors):
            print(line, file=sys.stderr)
        return 1

    records = RecordSet()
    try:
        text, skips = emit(
            program,
            records=records,
            strict=args.strict,
            includes=tuple(args.includes),
            header_comment=f"// Generated from {args.input} by tools/ssdl/emit_td.py.",
            attrs=attrs,
            target_include=args.target_include,
        )
    except SSDLEmitError as exc:
        for diag in exc.diagnostics:
            print(diag.render(), file=sys.stderr)
        return 1

    for diag in skips:
        print(diag.render(), file=sys.stderr)
    shown = [
        finding
        for finding in findings
        if args.verbose or finding.level != FINDING_NOTE
    ]
    for line in render_findings(shown):
        print(line, file=sys.stderr)

    if args.records_json:
        with open(args.records_json, "w", encoding="utf-8") as f:
            f.write(records.dumps())

    output = text
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
