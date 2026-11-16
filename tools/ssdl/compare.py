#!/usr/bin/env python3
"""Compare the TableGen records of two .td files.

The comparison is intentionally name-scoped: only the ``def`` names declared
textually in the two input files are compared, so the huge dumps produced by
including a complete target (``RISCV.td``) do not matter.  This is what makes
``generated.td`` versus ``handwritten.expected.td`` a meaningful check: both
files declare the records the emitter claims to produce, and every field of
those records must match exactly.

``llvm-tblgen -print-records`` output is used so that class defaults, template
argument evaluation and multiple inheritance are all resolved by TableGen
itself rather than by re-implementing them here.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\{]", re.MULTILINE)
RECORD_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.MULTILINE)


class CompareError(Exception):
    pass


def declared_defs(source: str) -> list[str]:
    """Names declared with a top-level ``def`` in a .td source file."""
    # Strip // comments so that a commented-out def is not mistaken for one.
    lines = []
    for line in source.splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(line)
    return DEF_RE.findall("\n".join(lines))


def extract_records(dump: str, names: list[str]) -> dict[str, str]:
    """Extract the textual record block for each requested name."""
    wanted = set(names)
    out: dict[str, str] = {}
    lines = dump.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = RECORD_RE.match(line)
        if not m or m.group(1) not in wanted:
            i += 1
            continue
        name = m.group(1)
        block = [line]
        depth = line.count("{") - line.count("}")
        i += 1
        while i < len(lines) and depth > 0:
            block.append(lines[i])
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        if depth != 0:
            raise CompareError(f"unbalanced braces while reading def {name!r}")
        out[name] = normalize(block)
    return out


def normalize(block: list[str]) -> str:
    cleaned = []
    for line in block:
        # Drop the "\t// ClassName" annotation llvm-tblgen adds to def headers.
        if line.startswith("def ") and "\t//" in line:
            line = line.split("\t//", 1)[0]
        cleaned.append(line.rstrip())
    return "\n".join(cleaned)


def run_tblgen(tblgen: str, path: str, include_dirs: list[str]) -> str:
    cmd = [tblgen, "-print-records"]
    for inc in include_dirs:
        cmd.extend(["-I", inc])
    cmd.append(path)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CompareError(
            f"{tblgen} failed on {path} (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return proc.stdout


@dataclass
class Diff:
    missing_in_expected: list[str]
    missing_in_generated: list[str]
    changed: dict[str, tuple[str, str]]

    def ok(self) -> bool:
        return not (self.missing_in_expected or self.missing_in_generated or self.changed)

    def render(self) -> str:
        parts = []
        if self.missing_in_expected:
            parts.append(
                "declared by generated but missing from expected: "
                + ", ".join(self.missing_in_expected)
            )
        if self.missing_in_generated:
            parts.append(
                "declared by expected but missing from generated: "
                + ", ".join(self.missing_in_generated)
            )
        for name, (gen, exp) in sorted(self.changed.items()):
            diff = difflib.unified_diff(
                exp.splitlines(),
                gen.splitlines(),
                fromfile=f"expected/{name}",
                tofile=f"generated/{name}",
                lineterm="",
            )
            parts.append("\n".join(diff))
        return "\n".join(parts)


def compare(
    generated_path: str,
    expected_path: str,
    tblgen: str,
    include_dirs: list[str],
) -> Diff:
    gen_names = declared_defs(open(generated_path, encoding="utf-8").read())
    exp_names = declared_defs(open(expected_path, encoding="utf-8").read())
    if len(set(gen_names)) != len(gen_names):
        raise CompareError(f"{generated_path}: duplicate def names")
    if len(set(exp_names)) != len(exp_names):
        raise CompareError(f"{expected_path}: duplicate def names")

    gen_records = extract_records(run_tblgen(tblgen, generated_path, include_dirs), gen_names)
    exp_records = extract_records(run_tblgen(tblgen, expected_path, include_dirs), exp_names)

    missing_in_expected = [n for n in gen_names if n not in exp_records]
    missing_in_generated = [n for n in exp_names if n not in gen_records]
    changed = {
        name: (gen_records[name], exp_records[name])
        for name in gen_names
        if name in gen_records and name in exp_records and gen_records[name] != exp_records[name]
    }
    return Diff(missing_in_expected, missing_in_generated, changed)


def default_tblgen() -> str:
    env = os.environ.get("LLVM_TBLGEN")
    if env:
        return env
    for candidate in (
        "/home/ubuntu/llvm/builds/explore/bin/llvm-tblgen",
        shutil.which("llvm-tblgen"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    raise CompareError("llvm-tblgen not found; set LLVM_TBLGEN")


def default_include_dirs() -> list[str]:
    src = os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm")
    return [os.path.join(src, "include"), os.path.join(src, "lib/Target/RISCV")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("generated")
    ap.add_argument("expected")
    ap.add_argument("--tblgen", default=None)
    ap.add_argument("-I", "--include", action="append", default=[])
    args = ap.parse_args(argv)

    include_dirs = args.include or default_include_dirs()
    try:
        diff = compare(args.generated, args.expected, args.tblgen or default_tblgen(), include_dirs)
    except CompareError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    if diff.ok():
        print(f"records match: {args.generated} == {args.expected}")
        return 0
    print(diff.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
