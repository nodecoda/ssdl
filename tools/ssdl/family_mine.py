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
from ssdl.records import load as load_records  # noqa: E402

LMUL_UNITS = [1, 2, 4, 8, 16, 32, 64]
DEFAULT_LMUL = ["MF8", "MF4", "MF2", "M1", "M2", "M4", "M8"]
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


def canonicalise(name: str, lmul: list[str]) -> tuple[str, dict[str, str]]:
    """Split a record name into ``(template, {axis: member})``."""
    axes: dict[str, str] = {}
    segments = []
    for token in name.split("_"):
        if token in lmul:
            axes["LMUL"] = token
            segments.append("{{LMUL}}")
            continue
        if token == "WorstCase":
            axes["LMUL"] = "WorstCase"
            segments.append("{{LMUL}}")
            continue
        match = SEW_RE.match(token)
        if match:
            axes["SEW"] = match.group(1)
            segments.append("{{SEW}}")
            continue
        match = SEG_RE.match(token)
        if match:
            axes["NF"] = match.group("nf")
            axes["EEW"] = match.group("eew")
            segments.append(f"{match.group('stem')}{{{{NF}}}}e{{{{EEW}}}}")
            continue
        segments.append(token)
    return "_".join(segments), axes


def collect(recs, model: str, lmul: list[str]) -> tuple[dict[str, Base], int]:
    bases: dict[str, Base] = {}
    total = 0
    for name in recs.body:
        if recs.cls.get(name, "") != "WriteRes":
            continue
        f = _fields(recs, name)
        if f.get("SchedModel") != model or f.get("Unsupported") != "0":
            continue
        total += 1
        wt = f["WriteType"]
        template, axes = canonicalise(wt, lmul)
        base = bases.setdefault(template, Base(template))
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


def _member_env(key: tuple[str, ...], vlen: int) -> dict[str, int]:
    env: dict[str, int] = {"vlen": vlen}
    for item in key:
        axis, _, member = item.partition("=")
        if axis == "LMUL":
            units = LMUL_UNITS[DEFAULT_LMUL.index(member)] if member != "WorstCase" else LMUL_UNITS[-1]
            env["LMUL"] = units
        else:
            env[axis] = int(member)
    return env


def _eval(expr: str, env: dict[str, int], base: Base, key: tuple[str, ...]) -> int:
    return family_expand._eval_expr(expr, dict(env))


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
        env = _member_env(key, vlen)
        for axis in ("LMUL", "NF", "EEW", "SEW"):
            if axis in entry.get("requires", []) and axis not in env:
                return ["missing axis " + axis]
        try:
            latency = _eval(entry["latency"], env, base, key)
            release_hi = entry.get("release_hi")
            release = (
                (1, _eval(release_hi, env, base, key)) if release_hi is not None else ()
            )
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
    return matched, failures


def report(records: str, model: str, catalogue: str | None, vlen: int, out: Path) -> int:
    recs = load_records(records)
    bases, total = collect(recs, model, DEFAULT_LMUL)
    entries = json.loads(Path(catalogue).read_text())["formulas"] if catalogue else []
    out.mkdir(parents=True, exist_ok=True)

    unexplained = []
    matched = 0
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
            complete = len(covered) == len(base.members)
            handle.write(
                json.dumps(
                    {
                        "template": template,
                        "axes": {axis: sorted(values) for axis, values in base.axes.items()},
                        "members": len(base.members),
                        "formulas": fits,
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
    print(f"bases: {len(bases)}  matched: {matched}  unexplained: {len(unexplained)}")
    print(f"latency shapes: {len(shapes)}")
    for item in unexplained[:10]:
        print(f"  unexplained: {item['template']} {item['axes']}")
    return 0 if not unexplained else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", default="/tmp/riscv.rec")
    parser.add_argument("--model", default="SiFive7VLEN512Model")
    parser.add_argument("--catalogue", default="data/riscv-sched-formulas.json")
    parser.add_argument("--vlen", type=int, default=512)
    parser.add_argument("--out", default="/tmp/mine")
    args = parser.parse_args(argv)
    catalogue = args.catalogue if Path(args.catalogue).exists() else None
    if catalogue is None:
        print("note: no catalogue yet; every base will be unexplained", file=sys.stderr)
    return report(args.records, args.model, catalogue, args.vlen, Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
