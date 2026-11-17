#!/usr/bin/env python3
"""Pin the v1.2 expansion of every family source, byte for byte.

The v1.3 elaborator replaces the text expander in ``family_expand.py``; AC2
requires the new path to reproduce v1.2 output exactly.  This module hashes
the *expansion* of each source (not the source itself), so a refactor that
changes only the front end cannot hide behind an unchanged input.

Entries:

    fixture:tests/ssdl/family-mini.ssdl    tests/ssdl + docs/examples
    model:Andes45Model                     $SSDL_CACHE/sweep-final/<model>

Model entries are cache-gated: when the sweep directory is absent they are
skipped, exactly like the other cache-dependent checks in ``check.py``.

Usage::

    python3 tools/ssdl/golden.py --write    # regenerate data/ssdl-golden.sha256
    python3 tools/ssdl/golden.py --check    # verify (default)
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import parser, paths  # noqa: E402

MANIFEST = ROOT / "data/ssdl-golden.sha256"
FIXTURE_DIRS = (ROOT / "tests/ssdl", ROOT / "docs/examples")
HEADER = "# SSDL family expansion golden; regenerate with tools/ssdl/golden.py --write"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class Entry:
    sha: str


def fixture_entries() -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for directory in FIXTURE_DIRS:
        for path in sorted(directory.glob("*.ssdl")):
            text = path.read_text(encoding="utf-8")
            if not parser.FAMILY_SOURCE.search(text):
                continue
            name = f"fixture:{path.relative_to(ROOT).as_posix()}"
            entries[name] = Entry(digest(parser.expand_source(text)))
    return entries


def model_entries() -> dict[str, Entry]:
    sweep = paths.SWEEP
    if not sweep.is_dir():
        return {}
    entries: dict[str, Entry] = {}
    for model_dir in sorted(sweep.iterdir()):
        if not model_dir.is_dir():
            continue
        source = model_dir / "family.ssdl"
        if not source.is_file():
            source = model_dir / "flat.ssdl"
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        entries[f"model:{model_dir.name}"] = Entry(digest(parser.expand_source(text)))
    return entries


def compute(include_models: bool = True) -> dict[str, Entry]:
    entries = fixture_entries()
    if include_models:
        entries.update(model_entries())
    return entries


def read_manifest(path: Path = MANIFEST) -> dict[str, Entry] | None:
    if not path.is_file():
        return None
    entries: dict[str, Entry] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"{path}:{line_no}: bad entry {line!r}")
        entries[parts[1]] = Entry(parts[0])
    return entries


def write_manifest(path: Path = MANIFEST) -> tuple[int, int]:
    entries = compute()
    fixtures = sum(1 for name in entries if name.startswith("fixture:"))
    models = len(entries) - fixtures
    lines = [HEADER]
    lines.extend(f"{entry.sha}  {name}" for name, entry in sorted(entries.items()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fixtures, models


@dataclass
class Report:
    ok: bool
    checked: int
    skipped: int
    problems: list[str]

    def message(self) -> str:
        if self.ok:
            return f"{self.checked} expansions match ({self.skipped} model entries skipped)"
        return "; ".join(self.problems) + (
            f" [{self.checked} checked, {self.skipped} skipped]"
        )


def check(path: Path = MANIFEST) -> Report:
    expected = read_manifest(path)
    if expected is None:
        return Report(False, 0, 0, [f"missing golden manifest {_display(path)}"])
    current = compute(include_models=False)
    problems: list[str] = []
    checked = 0
    skipped = 0

    sweep_available = paths.SWEEP.is_dir()
    if sweep_available:
        current.update(model_entries())

    for name, entry in sorted(expected.items()):
        if name.startswith("model:") and not sweep_available:
            skipped += 1
            continue
        actual = current.pop(name, None)
        if actual is None:
            problems.append(f"{name}: pinned but not found")
            continue
        checked += 1
        if actual.sha != entry.sha:
            problems.append(f"{name}: {entry.sha[:12]} != {actual.sha[:12]}")
    for name in sorted(current):
        problems.append(f"{name}: present but not pinned (run --write)")
    return Report(not problems, checked, skipped, problems)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="regenerate the manifest")
    group.add_argument("--check", action="store_true", help="verify the manifest (default)")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    args = ap.parse_args(argv)

    if args.write:
        fixtures, models = write_manifest(args.manifest)
        print(f"wrote {_display(args.manifest)}: {fixtures} fixtures, {models} models")
        return 0

    report = check(args.manifest)
    print(report.message())
    if not report.ok:
        for problem in report.problems:
            print(f"  {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
