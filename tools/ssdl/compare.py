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
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ssdl import paths  # noqa: E402

DEF_RE = re.compile(r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\{]", re.MULTILINE)
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


class DumpIndex:
    """Nameless records (``anonymous_N``) normalized by content.

    ``llvm-tblgen -print-records`` numbers anonymous records in parse order.
    Two semantically identical files can therefore differ in those numbers, and
    nested instantiations such as ``CheckAll<[CheckOpcode<[ADD]>]>`` or
    ``SchedVar<...>`` would never compare equal.  Each anonymous record is
    replaced by ``anon#<hash of its canonical body>``, so only the structure and
    field values matter.
    """

    def __init__(self, dump: str):
        self.lines = dump.splitlines()
        self.blocks: dict[str, tuple[int, int]] = {}
        self.memo: dict[str, str] = {}
        self.stack: list[str] = []
        i = 0
        n = len(self.lines)
        while i < n:
            match = RECORD_RE.match(self.lines[i])
            if not match:
                i += 1
                continue
            start = i
            depth = self.lines[i].count("{") - self.lines[i].count("}")
            i += 1
            while depth > 0 and i < n:
                depth += self.lines[i].count("{") - self.lines[i].count("}")
                i += 1
            self.blocks[match.group(1)] = (start, i)

    def _block_lines(self, name: str) -> list[str]:
        start, end = self.blocks[name]
        return self.lines[start:end]

    def _body(self, name: str) -> str:
        lines = self._block_lines(name)
        text = "\n".join(lines)
        text = re.sub(r"^def\s+[A-Za-z_][A-Za-z0-9_]*\s*\{", "", text)
        text = text.rstrip()
        if text.endswith("}"):
            text = text[:-1]
        return text

    def _replace_anonymous(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            return self.canonical(match.group(0))

        return re.sub(r"\banonymous_\d+\b", repl, text)

    def canonical(self, name: str) -> str:
        if name in self.memo:
            return self.memo[name]
        if name not in self.blocks:
            raise CompareError(f"record {name!r} not found in llvm-tblgen output")
        if name in self.stack:
            return "anon#cycle"
        self.stack.append(name)
        if name.startswith("anonymous_"):
            body = self._replace_anonymous(self._body(name))
            digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
            result = f"anon#{digest}"
        else:
            block = normalize(self._block_lines(name))
            result = self._replace_anonymous(block)
        self.stack.pop()
        self.memo[name] = result
        return result

    def records(self, names: list[str]) -> dict[str, str]:
        return {name: self.canonical(name) for name in names if name in self.blocks}


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

    gen_index = DumpIndex(run_tblgen(tblgen, generated_path, include_dirs))
    exp_index = DumpIndex(run_tblgen(tblgen, expected_path, include_dirs))
    gen_records = gen_index.records(gen_names)
    exp_records = exp_index.records(exp_names)

    missing_in_expected = [n for n in gen_names if n not in exp_records]
    missing_in_generated = [n for n in exp_names if n not in gen_records]
    changed = {
        name: (gen_records[name], exp_records[name])
        for name in gen_names
        if name in gen_records and name in exp_records and gen_records[name] != exp_records[name]
    }
    return Diff(missing_in_expected, missing_in_generated, changed)


def default_tblgen() -> str:
    candidate = paths.tblgen()
    if candidate:
        return candidate
    raise CompareError(
        "llvm-tblgen not found; set SSDL_LLVM_BUILD to an LLVM build directory "
        "or put llvm-tblgen on PATH"
    )


def default_include_dirs() -> list[str]:
    src = paths.require_llvm_src()
    return [str(src / "include"), str(src / "lib/Target/RISCV")]


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
