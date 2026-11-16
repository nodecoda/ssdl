#!/usr/bin/env python3
"""Self-check for the SSDL parser/emitter spike.

Run with:

    python3 tools/ssdl/check.py

The positive test lowers ``tests/ssdl/sifive7-mini.ssdl`` and compares the
resulting records, field by field, against the hand-written
``tests/ssdl/sifive7-mini.expected.td``.  The negative tests pin down every
diagnostic the emitter is supposed to produce, so "unsupported" can never mean
"silently dropped".
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import compare as compare_mod  # noqa: E402
from ssdl import emit_td, parser  # noqa: E402
from ssdl import validate as validate_mod  # noqa: E402
from ssdl.emit_td import SSDLEmitError  # noqa: E402


class Failure(Exception):
    pass


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def assert_true(cond: bool, message: str) -> None:
    if not cond:
        raise Failure(message)


def assert_in(needle: str, haystack: str) -> None:
    if needle not in haystack:
        raise Failure(f"expected {needle!r} in:\n{haystack}")


def emit_source(source: str, strict: bool = False):
    return emit_td.emit(parser.parse(source), strict=strict)


def expect_emit_errors(source: str, expectations: list[str]) -> None:
    try:
        emit_source(source)
    except SSDLEmitError as exc:
        text = "\n".join(d.render() for d in exc.diagnostics)
        for expected in expectations:
            assert_in(expected, text)
        return
    raise Failure(f"expected SSDLEmitError for:\n{source}")


def spec_example_text() -> str:
    doc = (ROOT / "docs/dsl/2026-11-16-ssdl-v1.0-final.md").read_text(encoding="utf-8")
    marker = "## 五、完整例子"
    pos = doc.index(marker)
    start = doc.index("```text\n", pos) + len("```text\n")
    end = doc.index("```", start)
    return doc[start:end]


@test
def spec_example_checked_in_copy_matches_the_document():
    checked_in = (ROOT / "tests/ssdl/spec-example.ssdl").read_text(encoding="utf-8")
    assert_true(
        checked_in == spec_example_text(),
        "tests/ssdl/spec-example.ssdl drifted from section 5 of the spec",
    )


@test
def spec_example_parses():
    program = parser.parse(spec_example_text())
    got = [(type(d).__name__, getattr(d, "name", None)) for d in program.decls]
    expected = [
        ("SetDecl", "ZeroRegs"),
        ("SetDecl", "LogicalInsts"),
        ("PredicateDecl", "IsZeroReg"),
        ("PredicateDecl", "HasStdExtI"),
        ("PredicateDecl", "NoStdExtZbb"),
        ("PredicateProlog", None),
        ("FeatureDecl", "StdExtI"),
        ("FeatureDecl", "StdExtZicntr"),
        ("FeatureDecl", "StdExtZibi"),
        ("FeatureDecl", "Feature64Bit"),
        ("FeatureDecl", "TunePostRA"),
        ("STIDecl", "isZeroIdiom"),
        ("STIDecl", "isDependencyBreaking"),
        ("MachineDecl", "generic-rv64"),
        ("MachineDecl", "siFive7"),
        ("MachineDecl", "siFive7-alias"),
        ("STIFunction", "SiFive7ZeroIdiom"),
        ("STIFunction", "SiFive7DepBreaking"),
        ("TIIPredicate", "isDependencyBreaking"),
    ]
    assert_true(got == expected, f"unexpected declarations:\n{got}")


@test
def spec_example_is_not_lowerable_as_written():
    try:
        emit_source(spec_example_text(), strict=True)
    except SSDLEmitError as exc:
        text = "\n".join(d.render() for d in exc.diagnostics)
    else:
        raise Failure("the section 5 example unexpectedly lowered cleanly")

    # The example is an AArch64 idiom model wearing RISC-V feature names, omits
    # the -mattr name the grammar cannot express, and uses constructs the
    # minimal emitter deliberately does not lower.
    for expected in [
        "feature Feature64Bit: error: kind = plain requires field 'mattr'",
        "feature StdExtZicntr: error: unknown feature 'StdExtZicsr'",
        "machine generic-rv64: error: unknown feature 'StdExtZifencei'",
        "machine siFive7.W_ALU: error: SchedWriteVariant is not lowered",
        "machine siFive7.GPRFile: error: reg_file is not lowered",
        "machine siFive7.itineraries: error: itineraries are not lowered",
        "set ZeroRegs: error: attribute-filtered sets are not lowered",
        "predicate IsZeroReg: error: predicates are not lowered",
        "sti_decl isZeroIdiom: error: STI declarations are not lowered",
        "tii_predicate isDependencyBreaking: error: TII predicates are not lowered",
    ]:
        assert_in(expected, text)


@test
def fixture_lowers_to_the_handwritten_records():
    source = (ROOT / "tests/ssdl/sifive7-mini.ssdl").read_text(encoding="utf-8")
    text, skips = emit_source(source, strict=True)
    assert_true(skips == [], f"strict emission skipped constructs: {skips}")
    text_again, _ = emit_source(source, strict=True)
    assert_true(text == text_again, "emitter is not deterministic")

    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "generated.td"
        generated.write_text(text, encoding="utf-8")
        diff = compare_mod.compare(
            str(generated),
            str(ROOT / "tests/ssdl/sifive7-mini.expected.td"),
            compare_mod.default_tblgen(),
            compare_mod.default_include_dirs(),
        )
    assert_true(diff.ok(), "record mismatch:\n" + diff.render())


@test
def comparator_is_not_vacuous():
    expected = (ROOT / "tests/ssdl/sifive7-mini.expected.td").read_text(encoding="utf-8")
    mutated = expected.replace("let Latency = 3;", "let Latency = 4;", 1)
    assert_true(mutated != expected, "test mutation did not apply")
    source = (ROOT / "tests/ssdl/sifive7-mini.ssdl").read_text(encoding="utf-8")
    text, _ = emit_source(source, strict=True)
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "generated.td"
        generated.write_text(text, encoding="utf-8")
        bad = Path(tmp) / "bad.expected.td"
        bad.write_text(mutated, encoding="utf-8")
        diff = compare_mod.compare(
            str(generated),
            str(bad),
            compare_mod.default_tblgen(),
            compare_mod.default_include_dirs(),
        )
    assert_true(not diff.ok(), "comparator accepted a mutated expected file")
    assert_in("WR_W_LOAD", diff.render())


@test
def errata_syntax_parses():
    program = parser.parse_file(str(ROOT / "tests/ssdl/errata.ssdl"))
    by_name = {getattr(d, "name", None): d for d in program.decls}

    # S1 + S2: parameters and named predicate calls.
    zero = by_name["IsZeroReg"]
    assert_true(zero.params == ["i"], f"S1 params: {zero.params}")
    assert_true(zero.expr[0] == "call" and zero.expr[1] == "reg_is", f"S1 expr: {zero.expr}")
    idiom = by_name["IsZeroIdiom"]
    assert_true(idiom.expr[1] == "all_of", f"S2 expr: {idiom.expr}")
    call = idiom.expr[2][1]
    assert_true(
        call == ("call", "IsZeroReg", [("int", 1)]),
        f"S2 named predicate call: {call}",
    )

    # S3: comma-separated feature fields.
    plain = by_name["ErrataPlain"]
    assert_true(plain.fields["kind"] == "plain", plain.fields)
    assert_true(plain.fields["mattr"] == "errata-plain", plain.fields)

    # S4: ':' and ',' both act as property separators.
    machine = by_name["errata-machine"]
    assert_true(machine.items[0]["props"] == {"units": 1, "buffer": 4}, machine.items[0])

    # S5: hyphenated machine names.
    alias = by_name["errata-machine-alias"]
    assert_true(alias.alias == "errata-machine", alias.alias)

    # S6: sti_function body without a trailing ';'.
    hook = by_name["ErrataHook"]
    assert_true(hook.decl == "SomeDecl", hook.decl)
    assert_true(hook.items[0] == {"kind": "sched_model", "value": "errata-machine"}, hook.items)

    # S7: return-only tii_predicate needs exactly one ';'.
    tii = by_name["ErrataTII"]
    assert_true(
        tii.statement == {"kind": "return", "expr": ("bool", True)},
        tii.statement,
    )


@test
def unlowered_constructs_are_reported_not_dropped():
    source = """
set S = [ADD];
machine Empty { }
"""
    text, skips = emit_source(source)
    assert_in("def EmptyModel : SchedMachineModel;", text)
    assert_true(
        any("attribute-filtered sets are not lowered" in d.message for d in skips),
        f"skips: {skips}",
    )
    # strict mode turns the same skip into an error
    try:
        emit_source(source, strict=True)
    except SSDLEmitError as exc:
        assert_in("attribute-filtered sets are not lowered", "\n".join(d.render() for d in exc.diagnostics))
    else:
        raise Failure("--strict did not turn a skip into an error")


@test
def negative_controls():
    cases = [
        (
            'feature F : kind = plain, field = "F", value = "true";',
            ["requires field 'mattr'"],
        ),
        ('feature E : kind = extension;', ["requires field 'version'"]),
        (
            'feature E : kind = extension, version = "2";',
            ["must be MAJOR.MINOR"],
        ),
        (
            'feature E : kind = extension, version = "1.0", value = "true";',
            ["value requires field"],
        ),
        (
            'feature T : kind = tune, field = "F", value = "true";',
            ["requires field 'positive_directive'"],
        ),
        (
            'feature F : kind = plain, mattr = "f", field = "F", value = "true", version = "1.0";',
            ["field 'version' is not allowed for kind = plain"],
        ),
        (
            'feature T : kind = tune, positive_directive = "p", field = "F", value = "true", '
            "bitmask = { group = 0, bit = 1 };",
            ["field 'bitmask' is not allowed for kind = tune"],
        ),
        (
            'feature F : kind = plain, mattr = "f", field = "F", value = "true";\n'
            'feature F : kind = plain, mattr = "f2", field = "F", value = "true";',
            ["duplicate feature name"],
        ),
        (
            'feature E : kind = extension, version = "1.0", bitmask = { group = 9, bit = 0 };',
            ["does not fit bits<3>"],
        ),
        (
            'feature F : kind = plain, mattr = "f", field = "F", value = "true", '
            "inline_behavior = sometimes;",
            ["unknown inline_behavior"],
        ),
        (
            "machine M { features = [Nope]; }",
            ["unknown feature 'Nope'"],
        ),
        ("machine M = Nope;", ["alias target 'Nope' is not a declared machine"]),
        (
            "machine M { write W; read R; advance R : writes = [W]; }",
            ["cycles is required"],
        ),
        (
            "machine M { write W; bind W -> [Nope], latency = 1; }",
            ["unknown resource 'Nope'"],
        ),
        (
            "machine M { resource P; write W; bind W -> [P]; bind W -> [P]; }",
            ["duplicate binding for write 'W'"],
        ),
        (
            "machine M { map [ADD] -> [W]; }",
            ["unknown SchedReadWrite 'W'"],
        ),
        (
            "machine M { write W; map [] -> [W]; }",
            ["empty instruction list"],
        ),
        (
            "machine M { write W; map [ADD] -> [W]; map [ADD] -> [W]; }",
            ["duplicate emitted record name 'IRW_ADD'"],
        ),
        (
            'feature T : kind = tune, positive_directive = "p", field = "F", value = "true";\n'
            "machine M { features = [T]; }",
            ["tune feature 'T' belongs in tune_features"],
        ),
        (
            'feature F : kind = plain, mattr = "f", field = "F", value = "true";\n'
            "machine M { tune_features = [F]; }",
            ["is not a tune feature"],
        ),
        (
            'feature E1 : kind = extension, version = "1.0";\n'
            'feature E2 : kind = extension, version = "1.0", experimental = true, '
            "implies = [E1];",
            ["experimental extensions with implies"],
        ),
        (
            'feature P : kind = plain, mattr = "p", field = "P", value = "true";\n'
            'feature T : kind = tune, positive_directive = "t", field = "T", value = "true", '
            "implies = [P];",
            ["can only imply kind = tune"],
        ),
    ]
    for source, expected in cases:
        expect_emit_errors(source, expected)


@test
def v1_1_predicate_sorts():
    program = parser.parse_file(str(ROOT / "tests/ssdl/v1.1.ssdl"))
    checker = validate_mod.SortChecker(program)
    errors = checker.run()
    assert_true(errors == [], f"unexpected sort errors: {errors}")
    expected = {
        "IsZeroReg": "mc",
        "IsZeroIdiom": "mc",
        "McPred": "mc",
        "SchedPred": "sched",
        "AllEmpty": "mc",
        "AnyEmpty": "mc",
    }
    assert_true(checker.sorts == expected, f"unexpected sorts: {checker.sorts}")


@test
def v1_1_sort_negative_controls():
    cases = [
        (
            "predicate P = feature(F);\ntii_predicate T = return P;",
            "requires an MC predicate",
        ),
        (
            "predicate Q = mc(feature(F));",
            "mc(...) requires an MC predicate",
        ),
        (
            "predicate A = B;\npredicate B = A;",
            "recursive predicate reference",
        ),
        (
            "predicate R(i) = true;\npredicate S = R();",
            "expects 1 argument",
        ),
        (
            "predicate U = Nope;",
            "unknown predicate",
        ),
        (
            "predicate Sel { selector = cxx_expr([{ true }]); }\n"
            "predicate V = Sel;",
            "selection predicate",
        ),
        (
            "predicate Sel { selector = cxx_expr([{ true }]); }\n"
            "machine M { variant W { case Sel: [W]; default: [W]; } }",
            "selection predicate",
        ),
        (
            'itineraries : packetizer_namespace = "X" { func_units = [FU]; }',
            "must set 'sched_model = IDENT'",
        ),
        (
            "itineraries : sched_model = nope { func_units = [FU]; }",
            "not a machine definition",
        ),
        (
            "machine M { itineraries : sched_model = M { func_units = [FU]; } }",
            "must not set sched_model",
        ),
        (
            "machine D { }\nmachine A = D;\n"
            "itineraries : sched_model = A { func_units = [FU]; }",
            "not a machine definition",
        ),
        (
            "predicate opcode = true;",
            "reserved predicate name",
        ),
        (
            "machine M { itin_class IIC; itineraries { itin IIC { } } }",
            "collides with an itin_class declaration",
        ),
        (
            "machine M { }\n"
            "itineraries : sched_model = M { itin IIC { } itin IIC { } }",
            "duplicate itin",
        ),
        (
            "machine M { itin_class IIC; itin_class IIC; }",
            "duplicate itinerary class",
        ),
    ]
    for source, expected in cases:
        errors = validate_mod.validate(parser.parse(source))
        text = "\n".join(errors)
        assert_in(expected, text)


@test
def v1_1_itineraries_binding_parses():
    program = parser.parse_file(str(ROOT / "tests/ssdl/v1.1.ssdl"))
    blocks = [d for d in program.decls if isinstance(d, parser.ItinerariesDecl)]
    assert_true(len(blocks) == 1, f"expected one itinerary block, got {len(blocks)}")
    itin = blocks[0]
    assert_true(itin.sched_model == "v11-machine", itin.sched_model)
    assert_true(itin.namespace == "V11", itin.namespace)
    assert_true(itin.items[0] == {"kind": "func_units", "value": ["FU_ALU"]}, itin.items)


@test
def v1_1_mixed_map_is_split():
    source = """
machine M {
    write W;
    map [ADD, regex("MUL.*")] -> [W, read_default];
}
"""
    text, skips = emit_source(source, strict=True)
    assert_true(skips == [], skips)
    assert_in("def IRW_ADD_instrs : InstRW<[W, ReadDefault], (instrs ADD)>;", text)
    assert_in(
        'def IRW_REGEX_MUL_regex : InstRW<[W, ReadDefault], (instregex "MUL.*")>;',
        text,
    )


@test
def v1_1_tii_return_semicolon_forms():
    program = parser.parse(
        "tii_predicate A = return true;\n"
        "tii_predicate B = return false;;\n"
    )
    got = [
        d.statement for d in program.decls if isinstance(d, parser.TIIPredicate)
    ]
    assert_true(
        got
        == [
            {"kind": "return", "expr": ("bool", True)},
            {"kind": "return", "expr": ("bool", False)},
        ],
        got,
    )


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except Failure as exc:
            failures += 1
            print(f"FAIL {name}\n  {exc}")
        except Exception:
            failures += 1
            print(f"FAIL {name}\n" + traceback.format_exc())
        else:
            print(f"ok   {name}")
    total = len(TESTS)
    print(f"\n{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
