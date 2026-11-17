#!/usr/bin/env python3
"""Group a scheduling model's records into canonical bases and try formulas.

This is a *reporting* tool, not a generator.  It answers one question the
plan makes a hard gate (AC8): after folding the declared axes, is every
explicit record explained by an authored formula, or is some residue left
over that nobody can name?

Pipeline
--------
1. Read ``llvm-tblgen -print-records`` and keep ``WriteRes`` records whose
   ``SchedModel`` matches and whose ``Unsupported`` is 0 (TableGen fills the
   rest with ``Unsupported = 1``; those are not authored).
2. Canonicalise each record name against the axis vocabulary: ``LMUL`` in any
   ``_``-separated position, uppercase ``E<n>``, and the glued ``{nf}e{eew}``
   spelling used by segmented loads.  ``_WorstCase`` is the ``worst`` member.
3. Group by template; a template plus its axis members is one ``bind``.
4. Evaluate every formula in the catalogue over each base's members and
   report the bases that no formula reproduces.

The catalogue (``data/riscv-sched-formulas.json``) is hand-authored and every
entry cites the TableGen class it mirrors.  The tool never invents a formula
from the data; it only checks the authored ones.  That keeps the oracle
upstream records and keeps the compressed source explainable line by line.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssdl import family_expand  # noqa: E402
from ssdl import paths  # noqa: E402
from ssdl.records import load as load_records  # noqa: E402

# Defaults come from ``data/riscv-sched-profiles.json``; ``--profile`` swaps
# them at startup so the miner itself names no target.
LMUL_UNITS = [1, 2, 4, 8, 16, 32, 64]
DEFAULT_LMUL = ["MF8", "MF4", "MF2", "M1", "M2", "M4", "M8"]
LMUL_NAMES = list(DEFAULT_LMUL)
LMUL_UNIT_LIST = list(LMUL_UNITS)
SEG_RE = re.compile(r"^(?P<stem>.*?)(?P<nf>\d+)e(?P<eew>8|16|32|64)$")
SEW_RE = re.compile(r"^E(8|16|32|64)$")
RESOURCE_PREFIX_RE = re.compile(r"^.*?SiFive7")


@dataclass
class Payload:
    latency: int
    uops: int
    resources: tuple[str, ...]
    release: tuple[int, ...]
    acquire: tuple[int, ...]

    def as_tuple(self):
        return (self.latency, self.uops, self.resources, self.release, self.acquire)


@dataclass
class Base:
    template: str
    axes: dict[str, list[str]] = field(default_factory=dict)
    shape: "Shape | None" = None
    members: dict[tuple[str, ...], Payload] = field(default_factory=dict)

    @property
    def axis_names(self) -> list[str]:
        return sorted(self.axes)


def _fields(recs, name: str) -> dict[str, str]:
    out = {}
    for line in recs.body[name]:
        text = line.strip().rstrip(";")
        if "=" in text:
            key, value = text.split("=", 1)
            out[key.split()[-1]] = value.strip()
    return out


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"-?\d+", text or ""))


def _resources(text: str) -> tuple[str, ...]:
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or "")
    return tuple(RESOURCE_PREFIX_RE.sub("", name) for name in names)


@dataclass
class Shape:
    template: str
    axes: dict[str, str]
    base: str
    order: list[str]
    formats: dict[str, str]
    tails: dict[str, str]


def canonicalise(
    name: str, lmul: list[str], glued: dict[str, list[str]] | None = None
) -> Shape:
    """Split a record name into template, axes, the literal base and formats."""
    axes: dict[str, str] = {}
    segments = []
    literals: list[str] = []
    order: list[str] = []
    formats: dict[str, str] = {}
    glued = glued or {}

    def add(axis: str, member: str, fmt: str, literal: str) -> None:
        axes[axis] = member
        order.append(axis)
        formats[axis] = fmt
        segments.append(f"{literal}{{{{ {axis} }}}}".replace(" ", ""))
        if literal:
            literals.append(literal)

    for token in name.split("_"):
        attached = None
        for axis, stems in glued.items():
            for stem in stems:
                suffix = token[len(stem):] if token.startswith(stem) else None
                if suffix in ("8", "16", "32", "64"):
                    attached = (axis, suffix, stem)
                    break
            if attached:
                break
        if attached:
            axis, member, stem = attached
            add(axis, member, "{value}", stem)
            continue
        if token in lmul or token == "WorstCase":
            add("LMUL", token, "_{value}", "")
            continue
        match = SEW_RE.match(token)
        if match:
            add("SEW", match.group(1), "_E{value}", "")
            continue
        match = SEG_RE.match(token)
        if match:
            stem = match.group("stem")
            add("NF", match.group("nf"), "{value}", stem)
            add("EEW", match.group("eew"), "e{value}", "")
            continue
        literals.append(token)
        segments.append(token)
    template_text = "_".join(segments)
    first = template_text.find("{{")
    base = template_text[:first].rstrip("_") if first >= 0 else template_text
    tails: dict[str, str] = {}
    for index, segment in enumerate(segments):
        match = re.fullmatch(r"\{\{(\w+)\}\}", segment)
        if not match:
            continue
        tail = []
        for later in segments[index + 1:]:
            if re.fullmatch(r"\{\{(\w+)\}\}", later):
                break
            tail.append(later)
        tails[match.group(1)] = "_" + "_".join(tail) if tail else ""
    return Shape(template_text, axes, base, order, formats, tails)


def collect(
    recs, model: str, lmul: list[str], glued: dict[str, list[str]] | None = None
) -> tuple[dict[str, Base], int]:
    bases: dict[str, Base] = {}
    total = 0
    for name in recs.body:
        f = _fields(recs, name)
        if "WriteType" not in f or "Latency" not in f:
            continue
        if f.get("SchedModel") != model or f.get("Unsupported") != "0":
            continue
        total += 1
        wt = f["WriteType"]
        shape = canonicalise(wt, lmul, glued)
        template, axes = shape.template, shape.axes
        base = bases.setdefault(template, Base(template))
        base.shape = shape
        key = tuple(f"{axis}={axes[axis]}" for axis in sorted(axes))
        base.members[key] = Payload(
            latency=int(f["Latency"]),
            uops=int(f["NumMicroOps"]),
            resources=_resources(f.get("ProcResources", "")),
            release=_ints(f.get("ReleaseAtCycles", "")),
            acquire=_ints(f.get("AcquireAtCycles", "")),
        )
        for axis, member in axes.items():
            values = base.axes.setdefault(axis, [])
            if member not in values:
                values.append(member)
    return bases, total


def _member_env(key: tuple[str, ...], base: Base, vlen: int) -> dict[str, int]:
    """Numeric environment for one member.

    ``worst`` means "the widest member this base actually covers", which is how
    ``SiFive7IsWorstCaseMX<mx, SchedMxList>`` behaves: a family over MF8..M4
    emits a ``_WorstCase`` record that duplicates M4, not M8.
    """
    present = [name for name in base.axes.get("LMUL", []) if name != "WorstCase"]
    widest = max((LMUL_UNIT_LIST[LMUL_NAMES.index(name)] for name in present), default=LMUL_UNIT_LIST[-1])
    env: dict[str, int] = {"vlen": vlen}
    for item in key:
        axis, _, member = item.partition("=")
        if axis == "LMUL":
            env["LMUL"] = widest if member == "WorstCase" else LMUL_UNIT_LIST[LMUL_NAMES.index(member)]
        else:
            env[axis] = int(member)
    return env


def _eval(
    expr: str,
    env: dict[str, int],
    base: Base,
    key: tuple[str, ...],
    projections: dict[str, dict[str, int]] | None = None,
) -> int:
    merged = dict(env)
    for name, table in (projections or {}).items():
        axis, _, prop = name.partition(".")
        chosen = dict(item.partition("=")[::2] for item in key).get(axis)
        if chosen is not None and chosen in table:
            merged[f"{axis}.{prop}"] = table[chosen]
    return family_expand._eval_expr(expr, merged)


def _table_or_expr(
    entry: dict,
    field: str,
    key: tuple[str, ...],
    env: dict[str, int],
    base: Base,
    projections: dict[str, dict[str, int]] | None,
) -> int | None:
    """A field is either an expression or an explicit per-member table."""
    table = entry.get(f"{field}_table")
    if table is not None:
        chosen = dict(item.partition("=")[::2] for item in key)
        for axis in ("LMUL", "SEW", "EEW", "NF"):
            if axis in chosen and chosen[axis] in table:
                return int(table[chosen[axis]])
        raise family_expand.ExpandError(f"{field}_table has no row for {key}")
    expr = entry.get(field)
    if expr is None:
        return None
    return _eval(expr, env, base, key, projections)


def _entry_scope(base: Base, entry: dict) -> list[tuple[str, ...]] | None:
    """Members an entry claims: template glob and axis subset, if declared."""
    patterns = entry.get("templates") or ([entry["template"]] if entry.get("template") else [])
    if patterns and not any(fnmatch.fnmatch(base.template, pattern) for pattern in patterns):
        return None
    subset = entry.get("subset") or {}
    keys = []
    for key in base.members:
        chosen = dict(item.partition("=")[::2] for item in key)
        if all(chosen.get(axis) in allowed for axis, allowed in subset.items()):
            keys.append(key)
    return keys or None


def match_formula(base: Base, entry: dict, vlen: int) -> tuple[list[tuple[str, ...]], list[str]]:
    """Return (members explained, failures) for one catalogue entry."""
    scope = _entry_scope(base, entry)
    if scope is None:
        return [], []
    failures = []
    matched = []
    for key in scope:
        payload = base.members[key]
        env = _member_env(key, base, vlen)
        for axis in ("LMUL", "NF", "EEW", "SEW"):
            if axis in entry.get("requires", []) and axis not in env:
                return ["missing axis " + axis]
        try:
            projections = entry.get("projections")
            latency = _table_or_expr(entry, "latency", key, env, base, projections)
            release_hi = _table_or_expr(entry, "release_hi", key, env, base, projections)
            release = (1, release_hi) if release_hi is not None else ()
        except family_expand.ExpandError as exc:
            return [], [f"{key}: {exc}"]
        resources = tuple(entry.get("resources", ()))
        acquire = (0, 1) if entry.get("acquire", True) else ()
        if (
            payload.latency != latency
            or payload.release != release
            or payload.resources != resources
            or payload.acquire != acquire
            or payload.uops != entry.get("uops", 1)
        ):
            failures.append(
                f"{key}: got lat={payload.latency} rel={payload.release} "
                f"res={payload.resources}, want lat={latency} rel={release} res={resources}"
            )
            continue
        matched.append(key)
    if failures:
        # An entry explains a base completely or not at all; a partial fit is a
        # different formula, not a partial explanation.
        return [], failures
    return matched, []


def report(records: str, model: str, catalogue: str | None, vlen: int, out: Path) -> int:
    recs = load_records(records)
    payload = json.loads(Path(catalogue).read_text()) if catalogue else {}
    entries = payload.get("formulas", [])
    glued = payload.get("glued")
    bases, total = collect(recs, model, LMUL_NAMES, glued)
    out.mkdir(parents=True, exist_ok=True)

    unexplained = []
    matched = 0
    kinds = {"flat": 0, "constant": 0, "formula": 0}
    with (out / "bases.jsonl").open("w") as handle:
        for template in sorted(bases):
            base = bases[template]
            covered: set[tuple[str, ...]] = set()
            fits = []
            for entry in entries:
                explained, failures = match_formula(base, entry, vlen)
                if explained and not failures:
                    fits.append(entry["name"])
                    covered.update(explained)
            constant = len({payload.as_tuple() for payload in base.members.values()}) == 1
            flat = not base.axes
            complete = flat or constant or len(covered) == len(base.members)
            handle.write(
                json.dumps(
                    {
                        "template": template,
                        "axes": {axis: sorted(values) for axis, values in base.axes.items()},
                        "members": len(base.members),
                        "formulas": fits,
                        "kind": "flat" if flat else ("constant" if constant else "formula"),
                        "records": [
                            {
                                "key": list(key),
                                "latency": payload.latency,
                                "uops": payload.uops,
                                "resources": list(payload.resources),
                                "release": list(payload.release),
                                "acquire": list(payload.acquire),
                            }
                            for key, payload in sorted(base.members.items())
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if complete:
                matched += 1
                if flat:
                    kinds["flat"] += 1
                elif constant:
                    kinds["constant"] += 1
                else:
                    kinds["formula"] += 1
            else:
                unexplained.append(
                    {
                        "template": template,
                        "axes": {axis: sorted(values) for axis, values in base.axes.items()},
                        "members": len(base.members),
                        "sample": {
                            "latency": sorted({p.latency for p in base.members.values()}),
                            "resources": sorted({p.resources for p in base.members.values()}),
                        },
                    }
                )

    (out / "unexplained.json").write_text(json.dumps(unexplained, ensure_ascii=False, indent=1))
    shapes: dict[tuple, list[str]] = {}
    for template, base in bases.items():
        signature = tuple(p.latency for _, p in sorted(base.members.items()))
        shapes.setdefault(signature, []).append(template)
    with (out / "shapes.md").open("w") as handle:
        handle.write(f"# {model}: latency shapes\n\n")
        for signature, templates in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
            handle.write(f"- {len(templates)} bases, {len(signature)} members: {signature}\n")
            handle.write(f"  - e.g. {', '.join(templates[:4])}\n")

    print(f"explicit records: {total}")
    print(f"bases: {len(bases)}  explained: {matched}  unexplained: {len(unexplained)}")
    print(f"  flat: {kinds['flat']}  constant: {kinds['constant']}  formula: {kinds['formula']}")
    print(f"latency shapes: {len(shapes)}")
    for item in unexplained[:10]:
        print(f"  unexplained: {item['template']} {item['axes']}")
    return 0 if not unexplained else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", default=str(paths.RECORDS))
    parser.add_argument("--profile", help="entry in data/riscv-sched-families.json")
    parser.add_argument("--profiles", default=str(paths.ROOT / "data/riscv-sched-profiles.json"))
    parser.add_argument("--model")
    parser.add_argument("--catalogue", default=str(paths.ROOT / "data/riscv-sched-formulas.json"))
    parser.add_argument("--vlen", type=int)
    parser.add_argument("--out", default=str(paths.CACHE / "mine"))
    parser.add_argument("--emit", help="write compressed SSDL for the model here")
    parser.add_argument("--prefix", help="ProcResource name prefix")
    parser.add_argument("--family")
    parser.add_argument("--instance")
    parser.add_argument("--processor", help="host ProcessorModel def to copy features/tunes from")
    args = parser.parse_args(argv)

    global LMUL_NAMES, LMUL_UNIT_LIST, LMUL_ORDER, RESOURCE_PREFIX_RE
    profile: dict = {}
    if args.profile:
        profiles = json.loads(Path(args.profiles).read_text())["profiles"]
        if args.profile not in profiles:
            parser.error(f"unknown profile {args.profile!r}")
        profile = profiles[args.profile]
        LMUL_NAMES = list(profile.get("lmul", DEFAULT_LMUL))
        LMUL_UNIT_LIST = list(profile.get("lmul_units", LMUL_UNITS))
        LMUL_ORDER = {name: index for index, name in enumerate(LMUL_NAMES)}
        RESOURCE_PREFIX_RE = re.compile(profile.get("resource_pattern", "^.*?SiFive7"))
    args.model = args.model or profile.get("model") or "SiFive7VLEN512Model"
    args.vlen = args.vlen if args.vlen is not None else profile.get("vlen", 512)
    args.prefix = args.prefix if args.prefix is not None else profile.get("prefix", "VLEN512SiFive7")
    args.family = args.family or profile.get("family") or "siFive7"
    args.instance = args.instance or profile.get("instance") or "sifive-7-series"
    args.processor = args.processor or profile.get("processor")
    catalogue = args.catalogue if Path(args.catalogue).exists() else None
    if catalogue is None:
        print("note: no catalogue yet; every base will be unexplained", file=sys.stderr)
    if args.emit:
        payload = json.loads(Path(args.catalogue).read_text())
        recs = load_records(args.records)
        text = emit_ssdl(
            recs, args.model, payload, args.vlen, args.prefix,
            args.family, args.instance, args.model, args.processor,
        )
        Path(args.emit).write_text(text)
        print(f"wrote {args.emit}: {len(text.splitlines())} lines")
        return 0
    return report(args.records, args.model, catalogue, args.vlen, Path(args.out))




# --------------------------------------------------------------------------
# Emission: turn the mined bases back into compressed SSDL.
# --------------------------------------------------------------------------

LMUL_ORDER = {name: index for index, name in enumerate(LMUL_NAMES)}


def _order_members(axis: str, members) -> tuple[str, ...]:
    if axis == "LMUL":
        return tuple(sorted(set(members), key=lambda m: LMUL_ORDER[m]))
    return tuple(sorted(set(members), key=int))


def resource_table(recs, model: str) -> dict[str, dict]:
    """ProcResources of ``model``, plus unscoped ones its writes reference.

    SiFive7 defines its units inside a model-scoped multiclass, so every
    record carries ``SchedModel``.  Rocket and several other models define
    them at target level (``SchedModel = ?``) and rely on the writes to name
    them.  Both spellings must reach the emitted machine.
    """
    used: set[str] = set()
    for name in recs.scoped_to(model):
        if recs.cls.get(name) not in ("WriteRes", "SchedWriteRes"):
            continue
        used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _fields(recs, name).get("ProcResources", "")))

    defs: dict[str, tuple[str, dict[str, str]]] = {}
    for name in recs.body:
        cls = recs.cls.get(name, "")
        if cls not in ("ProcResource", "ProcResGroup"):
            continue
        fields = _fields(recs, name)
        if fields.get("SchedModel") not in (model, "?"):
            continue
        defs[fields.get("Kind", name)] = (cls, fields)

    selected = {
        kind for kind, (_cls, fields) in defs.items() if fields.get("SchedModel") == model
    }
    stack = list(selected | used)
    while stack:
        kind = stack.pop()
        if kind in selected or kind not in defs:
            continue
        selected.add(kind)
        cls, fields = defs[kind]
        if cls == "ProcResGroup":
            stack.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fields.get("Resources", "")))
        super_ = fields.get("Super", "?")
        if super_ != "?":
            stack.append(super_)

    table = {}
    for kind in sorted(selected):
        cls, fields = defs[kind]
        if cls == "ProcResGroup":
            members = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fields.get("Resources", ""))
            table[kind] = {"group": members, "buffer": int(fields.get("BufferSize", "0") or 0)}
        else:
            table[kind] = {
                "units": int(fields.get("NumUnits", "1")),
                "buffer": int(fields.get("BufferSize", "0") or 0),
                "super": None if fields.get("Super", "?") == "?" else fields["Super"],
            }
    return table


def decompose(keys: list[tuple[str, ...]], axes: list[str]) -> list[dict[str, tuple[str, ...]]]:
    """Split a member set into rectangular blocks (product == member count).

    ``WriteVFALUV_{LMUL}_E{SEW}`` is not a product: SEW=64 only exists for
    M1..M8.  Splitting by the axis with the fewest values recovers rectangles,
    which is exactly what ``[LMUL in [...], SEW in [...]]`` can express.
    """
    if not keys:
        return []
    table = [dict(item.partition("=")[::2] for item in key) for key in keys]
    values = {axis: _order_members(axis, [row[axis] for row in table]) for axis in axes}
    size = 1
    for axis in axes:
        size *= len(values[axis])
    if size == len(keys):
        return [values]
    axis = min(axes, key=lambda name: len(values[name]))
    blocks = []
    for value in values[axis]:
        subset = [key for key, row in zip(keys, table) if row[axis] == value]
        rest = [name for name in axes if name != axis]
        for block in decompose(subset, rest) if rest else [{}]:
            blocks.append({axis: (value,), **block})
    return blocks


class AxisViews:
    def __init__(self):
        self.views: dict[tuple, str] = {}
        self.definitions: list[str] = []

    def get(self, axis: str, members: tuple[str, ...], fmt: str) -> str:
        key = (axis, members, fmt)
        if key not in self.views:
            name = (
                axis
                if axis == "LMUL" and members == tuple(LMUL_NAMES) and fmt == "_{value}"
                else f"{axis}_{len(self.views)}"
            )
            self.views[key] = name
            units = (
                [LMUL_UNIT_LIST[LMUL_NAMES.index(member)] for member in members]
                if axis == "LMUL"
                else [int(member) for member in members]
            )
            if axis in ("EEW", "NF", "SEW"):
                units = [int(member) for member in members]
            extra = ""
            if axis == "SEW" and all(member in ("16", "32", "64") for member in members):
                factors = {"16": 15, "32": 28, "64": 57}
                extra = ", div_factor = [" + ", ".join(str(factors[m]) for m in members) + "]"
            self.definitions.append(
                f"set {name} = [{', '.join(members)}] : kind = dimension, "
                f'name_format = "{fmt}", units = [{", ".join(str(u) for u in units)}]{extra};'
            )
        return self.views[key]

    def render(self, axis: str, members: tuple[str, ...], fmt: str) -> str:
        name = self.get(axis, members, fmt)
        declared = next(ms for (ax, ms, f), n in self.views.items() if n == name)
        if tuple(members) == declared:
            return name
        return f"{name} in [{', '.join(members)}]"


def _rename(expr: str, mapping: dict[str, str]) -> str:
    """Formulas name the axis (`LMUL`); the emitted statement uses the view."""
    out = expr
    for axis, view in mapping.items():
        out = re.sub(rf"\b{axis}\b", view, out)
    return out


def _release(entry: dict, mapping: dict[str, str]) -> str:
    if not entry.get("release_hi"):
        return "[]"
    return f"[1, {_rename(entry['release_hi'], mapping)}]"


def render_bind(prefix, target, latency, uops, resources, release, acquire) -> str:
    res = ", ".join(prefix + name for name in resources)
    rel = "[" + ", ".join(str(value) for value in release) + "]" if release else "[]"
    acq = "[" + ", ".join(str(value) for value in acquire) + "]" if acquire else "[]"
    return (
        f"    bind {target} -> [{res}], latency = {latency}, uops = {uops}, "
        f"release = {rel}, acquire = {acq};"
    )


def collect_reads(recs, model: str, glued) -> dict[str, dict]:
    """Group ``SchedRead``/``ReadAdvance`` records by canonical template."""
    bases: dict[str, dict] = {}
    for name in recs.body:
        fields = _fields(recs, name)
        if fields.get("SchedModel") != model:
            continue
        read = fields.get("ReadType")
        if not read:
            continue
        shape = canonicalise(read, LMUL_NAMES, glued)
        base = bases.setdefault(shape.template, {"shape": shape, "axes": {}, "members": {}})
        key = tuple(f"{axis}={shape.axes[axis]}" for axis in shape.order)
        base["members"][key] = (
            int(fields.get("Cycles", "0")),
            fields.get("ValidWrites", "[]"),
            fields.get("CycleTunables", "[]"),
            fields.get("Unsupported", "0"),
        )
        for axis, member in shape.axes.items():
            values = base["axes"].setdefault(axis, [])
            if member not in values:
                values.append(member)
    return bases


def emit_reads(bases: dict[str, dict], views: "AxisViews") -> list[str]:
    """Emit the read half, mirroring the write emitter's axis handling.

    An axis is only dropped when it has nothing but its ``_WorstCase``
    member; a family that carries both real members and a worst case emits
    the members through a view and the worst case through ``worst(AXIS)``.
    Dropping the whole axis collapses 8 records into 1 (see ReadVWSLLV).
    """
    lines: list[str] = []

    def advance(target: str, payload: tuple) -> str:
        cycles, writes, tun, unsupported = payload
        text = f"    advance {target} : cycles = {cycles}"
        if writes and writes != "[]":
            text += f" : writes = {writes}"
        if tun and tun != "[]":
            text += f" : tunables = {tun}"
        if unsupported == "1":
            text += " : unsupported = true"
        return text + ";"

    for template in sorted(bases):
        base = bases[template]
        shape = base["shape"]
        if not base["axes"]:
            (key,) = base["members"]
            lines.append(f"    read {shape.base};")
            lines.append(advance(shape.base, base["members"][key]))
            continue
        all_axes = list(shape.order)
        members = {
            axis: _order_members(axis, [m for m in base["axes"][axis] if m != "WorstCase"])
            for axis in all_axes
        }
        if any(not members[axis] for axis in all_axes):
            payload = next(iter(base["members"].values()))
            query = ", ".join(f"worst({axis})" for axis in all_axes)
            target = f"{shape.base}[{query}]"
            lines.append(f"    read {target};")
            lines.append(advance(target, payload))
            continue
        query = ", ".join(
            views.render(axis, members[axis], shape.formats[axis]) for axis in all_axes
        )
        lines.append(f"    read {shape.base}[{query}];")
        groups: dict[tuple, list[tuple[str, ...]]] = {}
        for key, payload in base["members"].items():
            if any("=WorstCase" in part for part in key):
                continue
            groups.setdefault(payload, []).append(key)
        for payload, keys in groups.items():
            for block in decompose(keys, all_axes):
                sub = ", ".join(
                    views.render(axis, _order_members(axis, block[axis]), shape.formats[axis])
                    for axis in all_axes
                )
                target = f"{shape.base}[{sub}]"
                lines.append(advance(target, payload))
        if "WorstCase" in base["axes"].get("LMUL", ()):
            key = next(k for k in sorted(base["members"]) if "LMUL=WorstCase" in k)
            sub = ", ".join(
                f"worst({views.render(axis, members[axis], shape.formats[axis])})"
                if axis == "LMUL"
                else views.render(axis, members[axis], shape.formats[axis])
                for axis in all_axes
            )
            target = f"{shape.base}[{sub}]"
            lines.append(f"    read {target};")
            lines.append(advance(target, base["members"][key]))
    return lines


def _strip_prefix(name: str, prefix: str) -> str:
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def _strip_anon(name: str) -> str:
    """``VLEN512SiFive7anonymous_89809WriteVLDS8_M1_Pred`` -> ``WriteVLDS8_M1_Pred``."""
    return re.sub(r"^.*?anonymous_\d+", "", name)


def _fmt(shape: "Shape", axis: str) -> str:
    return shape.formats[axis] + shape.tails.get(axis, "")


def _concrete(shape: "Shape", members: dict[str, str]) -> str:
    """Rebuild one concrete name from base, formats and tails."""
    out = shape.base
    for axis in shape.order:
        out += shape.formats[axis].replace("{value}", members[axis])
        out += shape.tails.get(axis, "")
    return out


def _list(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip() for part in text.strip().strip("[]").split(",") if part.strip()]


def collect_unsupported(recs, model: str, lmul, glued) -> dict[str, dict]:
    """``WriteRes`` records TableGen filled in with ``Unsupported = 1``."""
    bases: dict[str, dict] = {}
    for name in recs.body:
        fields = _fields(recs, name)
        if "WriteType" not in fields or "Latency" not in fields:
            continue
        if fields.get("SchedModel") != model or fields.get("Unsupported") != "1":
            continue
        shape = canonicalise(fields["WriteType"], lmul, glued)
        base = bases.setdefault(shape.template, {"shape": shape, "axes": {}, "members": set()})
        base["members"].add(tuple(f"{axis}={shape.axes[axis]}" for axis in shape.order))
        for axis, member in shape.axes.items():
            values = base["axes"].setdefault(axis, [])
            if member not in values:
                values.append(member)
    return bases


def emit_unsupported(bases: dict[str, dict], views: "AxisViews") -> list[str]:
    lines: list[str] = []
    for template in sorted(bases):
        base = bases[template]
        shape = base["shape"]
        if not base["axes"]:
            lines.append(f"    write {shape.base};")
            lines.append(f"    bind {shape.base} -> [], unsupported = true;")
            continue
        axes = list(shape.order)
        if any("WorstCase" in base["axes"][axis] for axis in axes):
            for key in sorted(base["members"]):
                name = _concrete(shape, dict(part.split("=") for part in key))
                lines.append(f"    write {name};")
                lines.append(f"    bind {name} -> [], unsupported = true;")
            continue
        for block in decompose(sorted(base["members"]), axes):
            query = ", ".join(
                views.render(axis, _order_members(axis, block[axis]), _fmt(shape, axis))
                for axis in axes
            )
            target = f"{shape.base}[{query}]"
            lines.append(f"    write {target};")
            lines.append(f"    bind {target} -> [], unsupported = true;")
    return lines


def collect_branch_writes(recs, model: str, prefix: str, lmul, glued) -> dict[str, dict]:
    """``SchedWriteRes`` defs: the concrete writes a variant case selects."""
    bases: dict[str, dict] = {}
    for name in recs.body:
        fields = _fields(recs, name)
        if "Latency" not in fields or "WriteType" in fields:
            continue
        if fields.get("SchedModel") != model:
            continue
        short = _strip_anon(_strip_prefix(name, prefix))
        shape = canonicalise(short, lmul, glued)
        base = bases.setdefault(shape.template, {"shape": shape, "axes": {}, "members": {}})
        key = tuple(f"{axis}={shape.axes[axis]}" for axis in shape.order)
        base["members"][key] = Payload(
            latency=int(fields.get("Latency", "0")),
            uops=int(fields.get("NumMicroOps", "1")),
            resources=_resources(fields.get("ProcResources", "")),
            release=_ints(fields.get("ReleaseAtCycles", "")),
            acquire=_ints(fields.get("AcquireAtCycles", "")),
        )
        for axis, member in shape.axes.items():
            values = base["axes"].setdefault(axis, [])
            if member not in values:
                values.append(member)
    return bases


def emit_branch_writes(bases: dict[str, dict], prefix: str, views: "AxisViews") -> list[str]:
    lines: list[str] = []
    for template in sorted(bases):
        base = bases[template]
        shape = base["shape"]
        if not base["axes"]:
            (key,) = base["members"]
            payload = base["members"][key]
            lines.append(f"    write {shape.base};")
            lines.append(render_bind(prefix, shape.base, payload.latency, payload.uops,
                                     payload.resources, payload.release, payload.acquire))
            continue
        axes = list(shape.order)
        if any("WorstCase" in base["axes"][axis] for axis in axes):
            for key, payload in sorted(base["members"].items()):
                name = _concrete(shape, dict(part.split("=") for part in key))
                lines.append(f"    write {name};")
                lines.append(render_bind(prefix, name, payload.latency, payload.uops,
                                         payload.resources, payload.release, payload.acquire))
            continue
        groups: dict[tuple, list[tuple[str, ...]]] = {}
        for key, payload in base["members"].items():
            signature = (payload.latency, payload.uops, payload.resources,
                         payload.release, payload.acquire)
            groups.setdefault(signature, []).append(key)
        for keys in groups.values():
            payload = base["members"][keys[0]]
            for block in decompose(keys, axes):
                query = ", ".join(
                    views.render(axis, _order_members(axis, block[axis]), _fmt(shape, axis))
                    for axis in axes
                )
                target = f"{shape.base}[{query}]"
                lines.append(f"    write {target};")
                lines.append(render_bind(prefix, target, payload.latency, payload.uops,
                                         payload.resources, payload.release, payload.acquire))
    return lines


def _mc_text(recs, name: str | None) -> str:
    if not name:
        raise SystemExit("family_mine: variant predicate has no MCInstPredicate")
    cls = recs.cls.get(name, "")
    fields = _fields(recs, name)
    if cls == "CheckRegOperand":
        return f"reg_is({fields['OpIndex']}, {fields['Reg']})"
    if cls == "CheckOpcode":
        ops = _list(fields.get("ValidOpcodes") or fields.get("Opcodes"))
        return f"opcode([{', '.join(ops)}])"
    if cls == "MCTrue":
        return "true"
    if cls == "MCFalse":
        return "false"
    if cls in ("CheckAll", "AllOf", "CheckAny", "AnyOf", "CheckNot", "Not"):
        kids = [_mc_text(recs, kid) for kid in _list(fields.get("Predicates") or fields.get("Pred"))]
        fn = {"CheckAll": "all_of", "AllOf": "all_of", "CheckAny": "any_of",
              "AnyOf": "any_of"}.get(cls)
        if fn is None:
            return f"not({kids[0]})"
        return f"{fn}({', '.join(kids)})"
    raise SystemExit(f"family_mine: unsupported MCInstPredicate class {cls!r} ({name})")


def _pred_text(recs, name: str | None) -> str | None:
    """Translate a ``SchedPredicateBase`` record into SSDL, or None for default."""
    if not name or name == "NoSchedPred":
        return None
    cls = recs.cls.get(name, "")
    fields = _fields(recs, name)
    if cls == "FeatureSchedPredicate":
        return f"feature({fields['Feature']})"
    if cls in ("AllOfSchedPreds", "AnyOfSchedPreds", "NoneOfSchedPreds"):
        kids = [_pred_text(recs, kid) for kid in _list(fields.get("Predicates"))]
        if None in kids:
            raise SystemExit(f"family_mine: {name} mixes a default predicate into a combiner")
        if cls == "AllOfSchedPreds":
            return f"all_of({', '.join(kids)})"
        if cls == "AnyOfSchedPreds":
            return f"any_of({', '.join(kids)})"
        return f"not(any_of({', '.join(kids)}))"
    if cls in ("NotSchedPred", "NotSchedPreds"):
        inner = _pred_text(recs, _list(fields.get("Predicates"))[0])
        return f"not({inner})"
    if cls == "MCSchedPredicate":
        return _mc_text(recs, fields.get("Pred"))
    if cls == "SchedPredicate":
        return f"sched_code({fields['Predicate']})"
    raise SystemExit(f"family_mine: unsupported SchedPredicateBase class {cls!r} ({name})")


def _variant_cases(recs, variant: str, prefix: str, lmul, glued) -> list[dict]:
    cases = []
    for var in _list(_fields(recs, variant).get("Variants")):
        var_fields = _fields(recs, var)
        selected = [
            canonicalise(_strip_anon(_strip_prefix(sel, prefix)), lmul, glued)
            for sel in _list(var_fields.get("Selected"))
        ]
        cases.append({"pred": _pred_text(recs, var_fields.get("Predicate")), "selected": selected})
    return cases


def collect_variants(recs, model: str, prefix: str, lmul, glued) -> list[dict]:
    """Alias subjects plus standalone variants, with expanded selections."""
    out: list[dict] = []
    alias_targets: set[str] = set()
    for name in recs.body:
        if recs.cls.get(name) != "SchedAlias":
            continue
        fields = _fields(recs, name)
        if fields.get("SchedModel") != model:
            continue
        alias_targets.add(fields["AliasRW"])
        subject = canonicalise(_strip_anon(_strip_prefix(fields["MatchRW"], prefix)), lmul, glued)
        out.append(
            {
                "subject": subject,
                "cases": _variant_cases(recs, fields["AliasRW"], prefix, lmul, glued),
                "kind": "variant",
            }
        )
    # Some models point InstRW straight at a SchedWriteVariant; there is no
    # alias record to carry the subject.
    for name in recs.body:
        cls = recs.cls.get(name)
        if cls not in ("SchedWriteVariant", "SchedReadVariant") or name in alias_targets:
            continue
        fields = _fields(recs, name)
        if fields.get("SchedModel") != model:
            continue
        subject = canonicalise(_strip_anon(_strip_prefix(name, prefix)), lmul, glued)
        out.append(
            {
                "subject": subject,
                "cases": _variant_cases(recs, name, prefix, lmul, glued),
                "kind": "read_variant" if cls == "SchedReadVariant" else "variant",
            }
        )
    return out


def emit_variants(variants: list[dict], views: "AxisViews") -> list[str]:
    """One ``variant`` per subject template, over the members that exist.

    The records contain one alias per member, all with the same case
    structure, so they collapse to a single dimensioned statement.
    Worst-case subjects get their own ``worst(AXIS)`` statement.
    """
    groups: dict[tuple, list[dict]] = {}
    for item in variants:
        shape = item["subject"]
        key = (
            shape.template,
            item.get("kind", "variant"),
            tuple(shape.axes.get(axis) == "WorstCase" for axis in shape.order),
        )
        groups.setdefault(key, []).append(item)

    lines: list[str] = []
    for key in sorted(groups):
        items = groups[key]
        subject = items[0]["subject"]
        axes = list(subject.order)
        members: dict[str, list[str]] = {}
        for axis in axes:
            seen: list[str] = []
            for item in items:
                member = item["subject"].axes.get(axis)
                if member and member != "WorstCase" and member not in seen:
                    seen.append(member)
            members[axis] = _order_members(axis, seen) if seen else ()

        signature = [
            [(case["pred"], tuple(sel.template for sel in case["selected"])) for case in item["cases"]]
            for item in items
        ]
        if any(sig != signature[0] for sig in signature):
            raise SystemExit(
                f"family_mine: members of {subject.template} disagree on their variant cases"
            )

        worst = any(subject.axes.get(axis) == "WorstCase" for axis in axes)

        def name_of(shape) -> str:
            if worst:
                return _concrete(shape, shape.axes)
            if not subject.order:
                # Concrete variant subject: each case selects one record, so
                # the selection is spelled out member by member.
                return _concrete(shape, shape.axes) if shape.order else shape.base
            if not shape.order:
                return shape.base
            parts = []
            for axis in shape.order:
                if axis not in members or not members[axis]:
                    raise SystemExit(
                        f"family_mine: variant selection {shape.template} uses {axis!r} "
                        f"with no members on subject {subject.template}"
                    )
                view = views.render(axis, members[axis], _fmt(shape, axis))
                parts.append(view)
            return f"{shape.base}[{', '.join(parts)}]"

        if worst:
            target = _concrete(subject, subject.axes)
        elif not axes:
            target = subject.base
        else:
            target = f"{subject.base}[{', '.join(views.render(axis, members[axis], _fmt(subject, axis)) for axis in axes)}]"
        keyword = items[0].get("kind", "variant")
        lines.append(f"    {keyword} {target} {{")
        for case in items[0]["cases"]:
            head = f"case {case['pred']}:" if case["pred"] else "default:"
            sels = ", ".join(name_of(sel) for sel in case["selected"])
            lines.append(f"        {head} [{sels}];")
        lines.append("    }")
    return lines


def declared_names(tables: list[dict]) -> set[str]:
    """Concrete write names the other emitters already declare."""
    names: set[str] = set()
    for table in tables:
        for base in table.values():
            if isinstance(base, dict):
                shape, keys = base["shape"], base["members"]
            else:
                shape, keys = base.shape, base.members
            if not keys:
                names.add(shape.base)
                continue
            for key in keys:
                names.add(
                    _concrete(shape, dict(part.split("=") for part in key))
                    if key
                    else shape.base
                )
    return names


def emit_subject_writes(variants: list[dict], declared: set[str], views: "AxisViews") -> list[str]:
    """Declare variant subjects that no bind introduced.

    A subject is aliased to its variant, so it carries no WriteRes; it still
    has to exist as a SchedWrite before the variant can name it.
    """
    by_template: dict[str, list[dict]] = {}
    for item in variants:
        by_template.setdefault(item["subject"].template, []).append(item)
    lines: list[str] = []
    for template in sorted(by_template):
        items = by_template[template]
        subject = items[0]["subject"]
        axes = list(subject.order)
        missing = []
        for item in items:
            shape = item["subject"]
            name = _concrete(shape, shape.axes) if shape.order else shape.base
            if name in declared:
                continue
            declared.add(name)
            missing.append(tuple(f"{axis}={shape.axes[axis]}" for axis in axes))
        if not missing:
            continue
        keyword = "read" if items[0].get("kind") == "read_variant" else "write"
        if not axes or any("WorstCase" in part for key in missing for part in key):
            for key in missing:
                lines.append(f"    {keyword} {_concrete(subject, dict(part.split('=') for part in key))};")
            continue
        for block in decompose(missing, axes):
            parts = []
            for axis in axes:
                members = _order_members(axis, block[axis])
                parts.append(views.render(axis, members, _fmt(subject, axis)))
            lines.append(f"    {keyword} {subject.base}[{', '.join(parts)}];")
    return lines


MODEL_PROP_FIELDS = (
    ("int IssueWidth", "issue_width", -1, "${value}"),
    ("int MicroOpBufferSize", "micro_op_buffer_size", -1, "${value}"),
    ("int LoopMicroOpBufferSize", "loop_micro_op_buffer", -1, "${value}"),
    ("int LoadLatency", "load_latency", -1, "${value}"),
    ("int HighLatency", "high_latency", -1, "${value}"),
    ("int MispredictPenalty", "mispredict_penalty", -1, "${value}"),
    ("bit PostRAScheduler", "post_ra_scheduler", False, "${value}"),
    ("bit CompleteModel", "complete_model", True, "${value}"),
    ("bit FullInstRWOverlapCheck", "full_inst_rw_overlap_check", True, "${value}"),
    ("bit EnableIntervals", "enable_intervals", False, "${value}"),
)


def collect_model_props(recs, model: str) -> list[str]:
    """Machine properties that differ from the ``SchedMachineModel`` defaults."""
    lines: list[str] = []
    for field_name, key, default, _ in MODEL_PROP_FIELDS:
        raw = recs.value(model, field_name)
        if field_name.startswith("int"):
            value = int(raw) if raw is not None else default
        else:
            value = raw not in (None, "0") if raw is not None else default
        if value == default:
            continue
        rendered = "true" if value is True else ("false" if value is False else value)
        lines.append(f"    {key} = {rendered};")
    return lines


def _feature_decl(recs, name: str) -> str | None:
    """One ``feature`` declaration copied from a host feature record."""
    fields = _fields(recs, name)
    if "Name" not in fields:
        return None
    mattr = fields["Name"]
    field = fields.get("FieldName", '""')
    value = fields.get("Value", '"true"')
    desc = fields.get("Desc", '""')
    cls = recs.cls.get(name, "")
    positive = fields.get("PositiveDirectiveName")
    if positive is not None:
        negative = fields.get("NegativeDirectiveName")
        if negative is None:
            negative = '"no-' + positive.strip('"') + '"'
        return (
            f"feature {name} : kind = tune, field = {field}, "
            f"value = {value}, positive_directive = {positive}, "
            f"negative_directive = {negative}, desc = {desc};"
        )
    return (
        f"feature {name} : kind = plain, mattr = {mattr}, field = {field}, "
        f"value = {value}, desc = {desc};"
    )


def collect_processor(recs, processor: str | None) -> tuple[list[str], list[str], list[str]]:
    """Feature decls and the two processor lists, straight from the host record."""
    if not processor or processor not in recs.body:
        return [], [], []
    features = _list(recs.value(processor, "list<SubtargetFeature> Features"))
    tune = _list(recs.value(processor, "list<SubtargetFeature> TuneFeatures"))
    decls: list[str] = []
    seen: set[str] = set()
    for name in features + tune:
        line = _feature_decl(recs, name)
        if line is not None and name not in seen:
            seen.add(name)
            decls.append(line)
    return decls, features, tune


def _canon_rw(name: str, prefix: str) -> str:
    """Scheduling identity of a referenced write: defm/anon prefixes dropped."""
    name = _strip_anon(_strip_prefix(name, prefix))
    return re.sub(r"^.*?_WRes_", "", name)


def collect_inst_rw(recs, model: str, prefix: str) -> list[str]:
    """``InstRW`` records: one ``map`` per record.

    Two spellings appear upstream: explicit ``(instrs ...)`` lists and a
    single ``(instregex "...")``.  The latter must stay a regex; splitting
    its text into identifiers would invent instructions that do not exist.
    """
    lines: list[str] = []
    for name in recs.scoped_to(model):
        instrs = recs.value(name, "dag Instrs")
        if instrs is None:
            continue
        if instrs.startswith("(instregex"):
            inst_names = [
                f"regex({json.dumps(literal)})"
                for literal in re.findall(r'"((?:[^"\\]|\\.)*)"', instrs)
            ]
        else:
            body = instrs[len("(instrs"):].rstrip(")")
            inst_names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)
        rws = [
            _canon_rw(n, prefix)
            for n in _list(recs.value(name, "list<SchedReadWrite> OperandReadWrites"))
        ]
        if not inst_names or not rws:
            continue
        unsupported = recs.value(name, "bit Unsupported")
        suffix = " : unsupported = true" if unsupported == "1" else ""
        lines.append(f"    map [{', '.join(inst_names)}] -> [{', '.join(rws)}]{suffix};")
    return lines


def collect_features(recs, variants: list[dict]) -> list[str]:
    """Feature decls for the predicates the variants use."""
    needed: list[str] = []

    def walk(text: str | None) -> None:
        if not text:
            return
        for match in re.finditer(r"feature\(([A-Za-z_]\w*)\)", text):
            if match.group(1) not in needed:
                needed.append(match.group(1))

    for item in variants:
        for case in item["cases"]:
            walk(case["pred"])
    lines = []
    for feature in needed:
        fields = _fields(recs, feature)
        lines.append(
            f"feature {feature} : kind = plain, mattr = {fields['Name']}, "
            f"field = {fields['FieldName']}, value = {fields.get('Value', chr(34) + 'true' + chr(34))};"
        )
    return lines


def collect_predicate_decls(variants: list[dict]) -> list[str]:
    """One ``predicate`` per distinct case expression, referenced by name."""
    lines: list[str] = []
    names: dict[str, str] = {}
    for item in variants:
        for case in item["cases"]:
            pred = case["pred"]
            if pred is None or pred in names:
                continue
            name = f"Pred_{len(names)}"
            names[pred] = name
            lines.append(f"predicate {name} = {pred};")
            case["pred"] = name
    return lines


def emit_ssdl(
    recs,
    model: str,
    catalogue: dict,
    vlen: int,
    prefix: str,
    family: str,
    instance: str,
    machine_name: str,
    processor: str | None = None,
) -> str:
    bases, total = collect(recs, model, LMUL_NAMES, catalogue.get("glued"))
    unsupported = collect_unsupported(recs, model, LMUL_NAMES, catalogue.get("glued"))
    branch_writes = collect_branch_writes(
        recs, model, prefix, LMUL_NAMES, catalogue.get("glued")
    )
    variants = collect_variants(recs, model, prefix, LMUL_NAMES, catalogue.get("glued"))
    entries = catalogue["formulas"]
    resources = resource_table(recs, model)
    views = AxisViews()
    lines: list[str] = [
        f"# Generated from {model} by tools/ssdl/family_mine.py --emit.",
        f"# Formula catalogue: data/riscv-sched-formulas.json (each entry cites TableGen).",
        "",
    ]
    body: list[str] = []
    for name in sorted(resources):
        info = resources[name]
        if "group" in info:
            body.append(f"    res_group {name} = [{', '.join(info['group'])}]"
                        + (f" : buffer = {info['buffer']};" if info["buffer"] >= 0 else ";"))
            continue
        extra = f", buffer = {info['buffer']}" if info["buffer"] >= 0 else ""
        super_ = f" : super = {info['super']}" if info["super"] else ""
        body.append(f"    resource {name}{super_} : units = {info['units']}{extra};")
    body.append("")

    for template in sorted(bases):
        base = bases[template]
        shape = base.shape
        if not base.axes:
            (key,) = base.members
            payload = base.members[key]
            body.append(f"    write {shape.base};")
            body.append(render_bind(prefix, shape.base, payload.latency, payload.uops,
                                    payload.resources, payload.release, payload.acquire))
            continue
        all_axes = list(shape.order)
        members = {
            axis: _order_members(axis, [m for m in base.axes[axis] if m != "WorstCase"])
            for axis in all_axes
        }
        if any(not members[axis] for axis in all_axes):
            # Nothing but a worst case exists on some axis: emit it concretely.
            for key in sorted(base.members):
                name = _concrete(shape, dict(part.split("=") for part in key))
                payload = base.members[key]
                body.append(f"    write {name};")
                body.append(render_bind(prefix, name, payload.latency, payload.uops,
                                        payload.resources, payload.release, payload.acquire))
            continue

        def query_for(block: dict[str, tuple[str, ...]]) -> str:
            parts = []
            for axis in all_axes:
                chosen = [member for member in block[axis] if member != "WorstCase"]
                if len(chosen) != len(block[axis]):
                    parts.append(
                        f"worst({views.render(axis, members[axis], _fmt(shape, axis))})"
                    )
                else:
                    parts.append(
                        views.render(axis, _order_members(axis, chosen), _fmt(shape, axis))
                    )
            return f"{shape.base}[{', '.join(parts)}]"

        covered: dict[object, list[tuple[str, ...]]] = {}
        hits: dict[tuple[str, ...], str] = {}
        for candidate in entries:
            matched, _failures = match_formula(base, candidate, vlen)
            for member in matched:
                hits.setdefault(member, candidate["name"])
        for key in sorted(base.members):
            chosen = hits.get(key)
            entry = next((e for e in entries if e["name"] == chosen), None)
            if entry is None or entry.get("latency_table"):
                payload = base.members[key]
                chosen = (chosen, payload.latency, payload.release, payload.resources)
            covered.setdefault(chosen, []).append(key)

        for chosen, group in covered.items():
            entry = next((e for e in entries if e["name"] == chosen), None)
            worst_keys = [key for key in group if any("=WorstCase" in part for part in key)]
            normal_keys = [key for key in group if key not in worst_keys]
            blocks = decompose(normal_keys, all_axes) if normal_keys else []
            blocks += [
                {axis: (dict(part.split("=") for part in key)[axis],) for axis in all_axes}
                for key in worst_keys
            ]
            for block in blocks:
                target = query_for(block)
                body.append(f"    write {target};")
                if entry is None or entry.get("latency_table"):
                    sample = next(
                        (k for k in group if all(
                            dict(part.split("=") for part in k)[axis] == block[axis][0]
                            for axis in all_axes
                        )),
                        group[0],
                    )
                    payload = base.members[sample]
                    body.append(render_bind(prefix, target, payload.latency, payload.uops,
                                            payload.resources, payload.release, payload.acquire))
                else:
                    mapping = {}
                    for axis in all_axes:
                        chosen = [member for member in block[axis] if member != "WorstCase"]
                        view_members = (
                            tuple(_order_members(axis, chosen)) if chosen else members[axis]
                        )
                        mapping[axis] = views.get(axis, view_members, _fmt(shape, axis))
                    body.append(
                        f"    bind {target} -> [{', '.join(prefix + r for r in entry['resources'])}], "
                        f"latency = {_rename(entry['latency'], mapping)}, uops = {entry.get('uops', 1)}, "
                        f"release = {_release(entry, mapping)}, acquire = [0, 1];"
                    )

    declared = declared_names([bases, unsupported, branch_writes])
    subject_lines = emit_subject_writes(variants, declared, views)
    unsupported_lines = emit_unsupported(unsupported, views)
    branch_lines = emit_branch_writes(branch_writes, prefix, views)
    inst_rw_lines = collect_inst_rw(recs, model, prefix)
    reads = collect_reads(recs, model, catalogue.get("glued"))
    # Reads may introduce new axis views, so they must run before the header is
    # snapshotted from ``views.definitions``.
    body = (
        body
        + subject_lines
        + unsupported_lines
        + branch_lines
        + ([""] if branch_lines or unsupported_lines else [])
        + emit_reads(reads, views)
        + [""]
        + emit_variants(variants, views)
        + inst_rw_lines
    )
    proc_decls, proc_features, proc_tunes = collect_processor(recs, processor)
    features = collect_features(recs, variants)
    seen = {line.split()[1] for line in features}
    for line in proc_decls:
        name = line.split()[1]
        if name not in seen:
            seen.add(name)
            features.append(line)
    predicates = collect_predicate_decls(variants)
    header = features + ([""] if features else []) + predicates + ([""] if predicates else [])
    props = collect_model_props(recs, model)
    header += views.definitions + ["", f"machine {family}(vlen: INT) {{"]
    header += props
    inst_props: list[str] = []
    if proc_features:
        inst_props.append(f"    features = [{', '.join(proc_features)}];")
    if proc_tunes:
        inst_props.append(f"    tune_features = [{', '.join(proc_tunes)}];")
    footer = ["}", "", f"machine {instance} : {family}({vlen}) {{"]
    footer += inst_props + ["}"]
    return "\n".join(lines + header + body + footer) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
