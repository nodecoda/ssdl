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

Everything else (``set``, the predicate body forms, ``sti_*``, ``tii_predicate``,
``variant``, ``reg_file``, ``itineraries`` ...) parses but is reported as
"skipped" rather than lowered.  With ``--strict`` every skipped construct is an
error, which is how the test suite proves that the section 5 example is *not*
lowerable as written.

Record names are deterministic so that ``llvm-tblgen -print-records`` output can
be compared against a hand-written .td file.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Any

if __package__ in (None, ""):
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


def td_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def td_def_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not out or out[0].isdigit():
        out = "_" + out
    return out


def join_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


class Emitter:
    def __init__(self, program: Program, strict: bool = False):
        self.program = program
        self.strict = strict
        self.errors: list[Diagnostic] = []
        self.skips: list[Diagnostic] = []
        self.features: dict[str, FeatureInfo] = {}
        self.machines: dict[str, MachineInfo] = {}

    # -- diagnostics -----------------------------------------------------
    def error(self, where: str, message: str) -> None:
        self.errors.append(Diagnostic("error", where, message))

    def skip(self, where: str, message: str) -> None:
        diag = Diagnostic("skip", where, message)
        if self.strict:
            self.errors.append(Diagnostic("error", where, message))
        else:
            self.skips.append(diag)

    # -- driver ----------------------------------------------------------
    def run(self) -> str:
        self.collect_features()
        self.collect_machines()
        # Collect the global "not lowered" diagnostics before bailing out so a
        # failing file reports every unlowered construct, not just the first
        # class of error.
        for decl in self.program.decls:
            if isinstance(decl, SetDecl):
                self.skip(f"set {decl.name}", "attribute-filtered sets are not lowered (norm 4.1)")
            elif isinstance(decl, PredicateDecl):
                self.skip(f"predicate {decl.name}", "predicates are not lowered by the minimal emitter")
            elif isinstance(decl, PredicateProlog):
                self.skip("predicate_prolog", "predicate prologs are not lowered by the minimal emitter")
            elif isinstance(decl, STIDecl):
                self.skip(f"sti_decl {decl.name}", "STI declarations are not lowered by the minimal emitter")
            elif isinstance(decl, STIFunction):
                self.skip(f"sti_function {decl.name}", "STI functions are not lowered by the minimal emitter")
            elif isinstance(decl, TIIPredicate):
                self.skip(f"tii_predicate {decl.name}", "TII predicates are not lowered by the minimal emitter")
            elif isinstance(decl, ItinerariesDecl):
                self.skip("itineraries", "itineraries are not lowered by the minimal emitter")
            elif isinstance(decl, (FeatureDecl, MachineDecl)):
                pass
            else:  # pragma: no cover - parser only produces the above
                self.error(type(decl).__name__, "unknown declaration kind")

        if self.errors:
            raise SSDLEmitError(self.errors)

        chunks: list[str] = []
        for info in self.features.values():
            chunks.append(info.text)
        for info in self.machines.values():
            chunks.append(info.text)
        return "\n".join(c for c in chunks if c).rstrip() + "\n"

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
            return f"def {info.def_name} : SubtargetFeature<{', '.join(args)}>;"

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
            if "field" in fields:
                args.append(td_str(fields["field"]))
                args.append(td_str(fields.get("value", "true")))
            elif "value" in fields:
                self.error(where, "value requires field for kind = extension")
            bases = [f"{cls}<{', '.join(args)}>"]
            mask = fields.get("bitmask")
            if mask is not None:
                group, bit = mask
                if not 0 <= group <= 7:
                    self.error(where, f"bitmask group {group} does not fit bits<3> (0..7)")
                if bit < 0:
                    self.error(where, f"bitmask bit {bit} must be non-negative")
                bases.append(f"RISCVExtensionBitmask<{group}, {bit}>")
            return f"def {info.def_name} : {', '.join(bases)};"

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
        return f"def {info.def_name} : RISCVTuneFeature<{', '.join(args)}>;"

    # -- machines --------------------------------------------------------
    def collect_machines(self) -> None:
        for decl in self.program.decls:
            if not isinstance(decl, MachineDecl):
                continue
            if decl.name in self.machines:
                self.error(f"machine {decl.name}", "duplicate machine name")
                continue
            self.machines[decl.name] = MachineInfo(decl, td_def_name(decl.name))

        # Alias targets may be declared later, so resolve in a second pass.
        seen_defs: dict[str, str] = {}
        for info in self.machines.values():
            lowered = self.lower_machine(info)
            for def_name in re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)", lowered, re.MULTILINE):
                if def_name in seen_defs:
                    self.error(
                        f"machine {info.decl.name}",
                        f"emitted def {def_name!r} collides with {seen_defs[def_name]!r}",
                    )
                seen_defs[def_name] = f"machine {info.decl.name}"
            info.text = lowered

    def lower_machine(self, info: MachineInfo) -> str:
        decl = info.decl
        where = f"machine {decl.name}"
        if decl.alias is not None:
            if decl.alias not in self.machines:
                self.error(where, f"alias target {decl.alias!r} is not a declared machine")
                return ""
            return (
                f"def {info.def_name} : ProcessorAlias<{td_str(decl.name)}, "
                f"{td_str(decl.alias)}>;"
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
            ):
                self.error(where, f"unknown machine property {key!r}")

        skip_messages = {
            "write_seq": "write_seq is not lowered by the minimal emitter",
            "func_unit": "func_unit is not lowered by the minimal emitter",
            "bypass": "bypass is not lowered by the minimal emitter",
            "itin_class": "itin_class is not lowered by the minimal emitter",
            "combo_func": "combo_func is not lowered by the minimal emitter",
            "map_itin": "map_itin is not lowered by the minimal emitter",
            "alias": "scheduling aliases are not lowered by the minimal emitter",
            "variant": "SchedWriteVariant is not lowered by the minimal emitter",
            "read_variant": "SchedReadVariant is not lowered by the minimal emitter",
            "reg_file": "reg_file is not lowered by the minimal emitter",
            "retire_unit": "retire_unit is not lowered by the minimal emitter",
            "load_queue": "load_queue is not lowered by the minimal emitter",
            "store_queue": "store_queue is not lowered by the minimal emitter",
            "itineraries": "itineraries are not lowered by the minimal emitter",
        }
        for kind, message in skip_messages.items():
            for item in items[kind]:
                name = item.get("name") or item.get("kind")
                self.skip(f"{where}.{name}", message)

        out: list[str] = []

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
                body.append(f"  let Super = {super_name};")
            if "buffer" in res_props:
                body.append(f"  let BufferSize = {res_props['buffer']};")
            if body:
                out.append(f"def {name} : ProcResource<{units}> {{\n" + "\n".join(body) + "\n}")
            else:
                out.append(f"def {name} : ProcResource<{units}>;")

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
                body.append(f"  let BufferSize = {res_props['buffer']};")
            if body:
                out.append(
                    f"def {name} : ProcResGroup<{join_list(members)}> {{\n"
                    + "\n".join(body)
                    + "\n}"
                )
            else:
                out.append(f"def {name} : ProcResGroup<{join_list(members)}>;")

        writes: set[str] = set()
        reads: set[str] = set()
        for item in items["write"]:
            if item["name"] in writes:
                self.error(where, f"duplicate write {item['name']!r}")
            writes.add(item["name"])
            out.append(f"def {item['name']} : SchedWrite;")
        for item in items["read"]:
            if item["name"] in reads:
                self.error(where, f"duplicate read {item['name']!r}")
            reads.add(item["name"])
            out.append(f"def {item['name']} : SchedRead;")

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
                    body.append(f"  let {field} = {value};")
            if body:
                out.append(
                    f"def WR_{write} : WriteRes<{write}, {join_list(resources)}> {{\n"
                    + "\n".join(body)
                    + "\n}"
                )
            else:
                out.append(f"def WR_{write} : WriteRes<{write}, {join_list(resources)}>;")

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
            if "unsupported" in adv_props:
                self.error(where, f"advance {read}: ReadAdvance has no Unsupported field")
                continue
            valid_writes = []
            for w in adv_props.get("writes", []):
                if w not in writes:
                    self.error(where, f"advance {read}: unknown write {w!r}")
                valid_writes.append(w)
            args = [read, str(adv_props["cycles"])]
            tunables = adv_props.get("tunables", [])
            if valid_writes or tunables:
                args.append(join_list(valid_writes))
            if tunables:
                args.append(join_list([str(t) for t in tunables]))
            out.append(f"def RA_{read} : ReadAdvance<{', '.join(args)}>;")

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
            if idents and regexes:
                self.error(where, "map: cannot mix instruction names and regex(...) in one InstRW")
                continue
            resolved = []
            for entry in rw:
                if entry in RESERVED_RW:
                    resolved.append(RESERVED_RW[entry])
                elif entry in writes or entry in reads:
                    resolved.append(entry)
                else:
                    self.error(where, f"map: unknown SchedReadWrite {entry!r}")
            pieces: list[str] = []
            if regexes:
                patterns = []
                for _, pattern in regexes:
                    patterns.append(td_str(pattern))
                    pieces.append("REGEX_" + re.sub(r"[^A-Za-z0-9_]", "", pattern))
                dag = "(instregex " + ", ".join(patterns) + ")"
            else:
                names = []
                for _, name in idents:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        self.error(where, f"map: {name!r} is not a TableGen instruction name")
                    names.append(name)
                    pieces.append(name)
                dag = "(instrs " + ", ".join(names) + ")"
            def_name = "IRW_" + "_".join(pieces)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", def_name):
                def_name = f"IRW_map_{index}"
            if def_name in map_names:
                self.error(where, f"map: duplicate emitted record name {def_name!r}")
                continue
            map_names.add(def_name)
            body = ""
            if item.get("unsupported") is not None:
                body = f" {{ let Unsupported = {'true' if item['unsupported'] else 'false'}; }}"
            out.append(
                f"def {def_name} : InstRW<{join_list(resolved)}, {dag}>{body};"
            )

        model_lets = []
        for key, field in SCHED_MODEL_PROPS.items():
            if key in props:
                value = props[key]
                if isinstance(value, bool):
                    value = "true" if value else "false"
                model_lets.append(f"  let {field} = {value};")
        if "unsupported_features" in props:
            preds = [f"Has{name}" for name in props["unsupported_features"]]
            model_lets.append(f"  let UnsupportedFeatures = {join_list(preds)};")
        if model_lets:
            out.append(
                f"def {info.def_name}Model : SchedMachineModel {{\n"
                + "\n".join(model_lets)
                + "\n}"
            )
        else:
            out.append(f"def {info.def_name}Model : SchedMachineModel;")

        features = props.get("features", [])
        tune = props.get("tune_features", [])
        march = props.get("default_march")
        for name in features:
            if name in self.features and self.features[name].kind == "tune":
                self.error(where, f"tune feature {name!r} belongs in tune_features, not features")
        for name in tune:
            if name in self.features and self.features[name].kind != "tune":
                self.error(where, f"feature {name!r} is not a tune feature")
        fargs = self._resolve_features(where, features)
        targs = self._resolve_features(where, tune)
        if features:
            args = [td_str(decl.name), f"{info.def_name}Model", join_list(fargs)]
            if tune or march:
                args.append(join_list(targs))
            if march is not None:
                args.append(td_str(march))
            out.append(f"def {info.def_name} : RISCVProcessorModel<{', '.join(args)}>;")
        else:
            if march is not None:
                self.error(where, "default_march requires at least one feature")
            args = [td_str(decl.name), f"{info.def_name}Model"]
            if tune:
                args.append(join_list(targs))
            out.append(f"def {info.def_name} : RISCVTuneProcessorModel<{', '.join(args)}>;")

        return "\n\n".join(out)


def emit(
    program: Program,
    strict: bool = False,
    includes: tuple[str, ...] = (),
    header_comment: str | None = None,
) -> tuple[str, list[Diagnostic]]:
    """Lower ``program`` and prepend the includes the output needs to load."""
    emitter = Emitter(program, strict=strict)
    body = emitter.run()
    header = []
    if header_comment:
        header.append(header_comment)
    header.append('include "RISCV.td"')
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
    args = ap.parse_args(argv)

    try:
        program = parse_file(args.input)
    except Exception as exc:  # parse errors carry line/column
        print(f"{args.input}: {exc}", file=sys.stderr)
        return 2

    try:
        text, skips = emit(
            program,
            strict=args.strict,
            includes=tuple(args.includes),
            header_comment=f"// Generated from {args.input} by tools/ssdl/emit_td.py.",
        )
    except SSDLEmitError as exc:
        for diag in exc.diagnostics:
            print(diag.render(), file=sys.stderr)
        return 1

    for diag in skips:
        print(diag.render(), file=sys.stderr)

    output = text
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
