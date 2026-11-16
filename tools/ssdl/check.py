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

import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import census_riscv as census_mod  # noqa: E402
from ssdl import compare as compare_mod  # noqa: E402
from ssdl import emit_td, parser  # noqa: E402
from ssdl import equiv as equiv_mod  # noqa: E402
from ssdl import family_tables as family_mod  # noqa: E402
from ssdl import from_records as from_records_mod  # noqa: E402
from ssdl import record_ir as record_ir_mod  # noqa: E402
from ssdl import records as records_mod  # noqa: E402
from ssdl import sets as sets_mod  # noqa: E402
from ssdl import td_schema as td_schema_mod  # noqa: E402
from ssdl import td_types as td_types_mod  # noqa: E402
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


def attrs_schema() -> "sets_mod.AttributeSchema":
    return sets_mod.AttributeSchema.load(str(ROOT / "tests/ssdl/attrs.json"))


def emit_source(source: str, strict: bool = False, attrs=None):
    return emit_td.emit(parser.parse(source), strict=strict, attrs=attrs)


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
    # the -mattr name the grammar cannot express for a plain feature, and uses
    # sets whose host attribute schema is not part of the example.  The 1.1
    # lowering now accepts the predicate/STI/TII/variant/reg_file/itineraries
    # syntax, and sharing one equivalence class between two STI functions is
    # accepted; these are the blockers that remain.
    for expected in [
        "feature Feature64Bit: error: kind = plain requires field 'mattr'",
        "set ZeroRegs: error: requires the host attribute schema",
        "machine generic-rv64: error: unknown feature 'StdExtZicsr'",
        "machine generic-rv64: error: unknown feature 'StdExtZifencei'",
        "machine siFive7: error: unknown feature 'StdExtM'",
        "machine siFive7.itineraries: error: itin IIC_iLoad: unknown bypass "
        "'Bypass_ALU_LD'",
        "lowering: error: tii_predicate isDependencyBreaking.IsZeroReg: "
        "set 'ZeroRegs' requires the attribute schema",
    ]:
        assert_in(expected, text)
    # `OrrXzrXzr` is declared in both STI functions; that is one shared def,
    # not a duplicate (section 16.4 of parser-notes).
    assert_true("emitted def 'OrrXzrXzr'" not in text, text)


@test
def fixture_lowers_to_the_handwritten_records():
    source = (ROOT / "tests/ssdl/sifive7-mini.ssdl").read_text(encoding="utf-8")
    attrs = attrs_schema()
    text, skips = emit_source(source, strict=True, attrs=attrs)
    assert_true(skips == [], f"strict emission skipped constructs: {skips}")
    text_again, _ = emit_source(source, strict=True, attrs=attrs)
    assert_true(text == text_again, "emitter is not deterministic")

    # Records that carry a SchedModel must actually be scoped to it.
    assert_in("let SchedModel = ssdl_sifive_7Model in {", text)
    assert_in("def W_MULDIV_SEQ : WriteSequence<[W_ALU, W_ALU], 2>;", text)
    assert_in("def ssdl_sifive_7_ITINRW_IIC_iALU : ItinRW<[W_ALU, ReadDefault], [IIC_iALU]>;", text)

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
    text, _ = emit_source(source, strict=True, attrs=attrs_schema())
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
set S = inst(category == "alu");
machine Empty { }
"""
    text, skips = emit_source(source)
    assert_in("def EmptyModel : SchedMachineModel;", text)
    assert_true(
        any("requires the host attribute schema" in d.message for d in skips),
        f"skips: {skips}",
    )
    # strict mode turns the same skip into an error
    try:
        emit_source(source, strict=True)
    except SSDLEmitError as exc:
        assert_in(
            "requires the host attribute schema",
            "\n".join(d.render() for d in exc.diagnostics),
        )
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
            ["duplicate emitted record name 'M_IRW_ADD'"],
        ),
        (
            'feature T : kind = tune, positive_directive = "p", field = "F", value = "true";\n'
            "machine M { features = [T]; }",
            ["tune feature 'T' belongs in tune_features"],
        ),
        (
            'feature E : kind = extension, version = "1.0";\n'
            "machine M { tune_features = [E]; }",
            ["belongs in features, not tune_features"],
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
    assert_in("def M_IRW_ADD_instrs : InstRW<[W, ReadDefault], (instrs ADD)>;", text)
    assert_in(
        'def M_IRW_REGEX_MUL_regex : InstRW<[W, ReadDefault], (instregex "MUL.*")>;',
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


@test
def set_evaluation_matches_the_host_order():
    program = parser.parse(
        """
set ZeroRegs = reg(is_zero);
set ALUInsts = inst(category == "alu");
set Logical  = [AND, XOR, ...ALUInsts];
set LowRegs  = [X0, X1];
"""
    )
    ev = sets_mod.SetEvaluator(program, attrs_schema())
    assert_true(ev.expand_set("ZeroRegs", "test") == ("reg", ["X0"]), "ZeroRegs")
    assert_true(
        ev.expand_set("ALUInsts", "test")
        == ("inst", ["ADD", "SUB", "XOR", "AND", "MUL", "DIV"]),
        "ALUInsts",
    )
    # Stable host order, stable deduplication: AND/XOR come first because they
    # are written first, and the repeated ADD/XOR/MUL/DIV members disappear.
    assert_true(
        ev.expand_set("Logical", "test")
        == ("inst", ["AND", "XOR", "ADD", "SUB", "MUL", "DIV"]),
        "Logical",
    )
    assert_true(ev.expand_set("LowRegs", "test") == ("reg", ["X0", "X1"]), "LowRegs")
    ev.finish()


@test
def set_evaluation_negative_controls():
    cases = [
        ('set A = inst(nope == true);', "attribute 'nope' is not declared"),
        ('set A = inst(category < 3);', "operator < requires int operands"),
        ('set A = [ADD]; set B = [A];', "is a set; write '...A'"),
        ("set A = [X0, ADD];", "cannot mix inst and reg"),
        ("set A = [NOPE];", "unknown instruction or register 'NOPE'"),
        ("set A = [...B]; set B = [...A];", "recursive set reference"),
    ]
    for source, expected in cases:
        program = parser.parse(source)
        ev = sets_mod.SetEvaluator(program, attrs_schema())
        declared = [d.name for d in program.decls if isinstance(d, parser.SetDecl)]
        ev.expand_set(declared[-1], "test")
        try:
            ev.finish()
        except sets_mod.SetError as exc:
            text = "\n".join(exc.errors)
            assert_in(expected, text)
        else:
            raise Failure(f"expected a set error for:\n{source}")


@test
def unknown_attribute_is_not_false():
    """JSON null means unknown, and unknown must never be folded to false."""

    def schema() -> "sets_mod.AttributeSchema":
        return sets_mod.AttributeSchema(
            {
                "order": {"inst": ["ADD", "SUB"], "reg": ["X0"]},
                "inst": {
                    "ADD": {"may_load": None, "category": "alu"},
                    "SUB": {"may_load": False, "category": "alu"},
                },
                "reg": {"X0": {"is_zero": True}},
            }
        )

    program = parser.parse("set L = inst(may_load == false);")
    ev = sets_mod.SetEvaluator(program, schema())
    ev.expand_set("L", "test")
    try:
        ev.finish()
    except sets_mod.SetError as exc:
        text = "\n".join(exc.errors)
        assert_in("attribute 'may_load' is unknown for inst 'ADD'", text)
        assert_in("comparison against an unknown value is an error", text)
    else:
        raise Failure("a comparison against an unknown attribute must be an error")

    program = parser.parse("set L = inst(!may_load);")
    ev = sets_mod.SetEvaluator(program, schema())
    kind, names = ev.expand_set("L", "test")
    assert_true(kind == "inst", kind)
    assert_true(
        names == ["SUB"],
        f"unknown must not behave like false (ADD must be excluded): {names!r}",
    )
    try:
        ev.finish()
    except sets_mod.SetError:
        pass
    else:
        raise Failure("negating an unknown attribute must still report it")


@test
def predicate_and_hook_lowering_shapes():
    source = (ROOT / "tests/ssdl/v1.1.ssdl").read_text(encoding="utf-8")
    text, skips = emit_source(source, strict=True, attrs=attrs_schema())
    assert_true(skips == [], skips)
    # MC / SCHED / SEL forms from 1.1 §4.
    assert_in("def IsZeroIdiom : CheckAll<[CheckOpcode<[ADD]>, CheckRegOperand<1, X0>]>;", text)
    assert_in("def McPred : CheckOpcode<[ADD]>;", text)
    assert_in(
        "def SchedPred : AllOfSchedPreds<[FeatureSchedPredicate<FeatureStdExtXssdl>, "
        "MCSchedPredicate<CheckOpcode<[ADD]>>, "
        "SchedPredicate<[{ MI->getOpcode() == 1 }]>]>;",
        text,
    )
    assert_in("def AllEmpty : CheckAll<[TruePred]>;", text)
    assert_in("def AnyEmpty : CheckAll<[FalsePred]>;", text)
    # MC is promoted to SCHED in a variant case; a SCHED case is used directly.
    assert_in(
        "SchedWriteVariant<[SchedVar<MCSchedPredicate<CheckAll<[CheckOpcode<[ADD]>, "
        "CheckRegOperand<1, X0>]>>, [v11_machine_WRes_W_ALU]>, "
        "SchedVar<SchedPred, [v11_machine_WRes_W_ALU]>, "
        "SchedVar<NoSchedPred, [v11_machine_WRes_W_ALU]>]>",
        text,
    )
    # Top-level itineraries are attached to the machine model.
    assert_in("def v11_machineItineraries : ProcessorItineraries<", text)
    assert_in("let Itineraries = v11_machineItineraries;", text)
    assert_in('def V11TII : TIIPredicate<"V11TII", MCOpcodeSwitchStatement<', text)
    # ... and the whole file is valid TableGen.
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "v11.td"
        generated.write_text(text, encoding="utf-8")
        compare_mod.run_tblgen(
            compare_mod.default_tblgen(),
            str(generated),
            compare_mod.default_include_dirs(),
        )


@test
def predicate_macros_accept_set_and_feature_parameters():
    source = """
set ZeroRegs = reg(is_zero);
predicate IsALUAndZero(ops, regs, i) =
    all_of(opcode(ops), reg_is(i, regs));
predicate P = IsALUAndZero([ADD, SUB], ZeroRegs, 1);

feature Xssdl : kind = extension, version = "1.0";
predicate HasFeat(f) = feature(f);
predicate S = HasFeat(Xssdl);
"""
    text, skips = emit_source(source, strict=True, attrs=attrs_schema())
    assert_true(skips == [], skips)
    assert_in(
        "def P : CheckAll<[CheckOpcode<[ADD, SUB]>, CheckRegOperand<1, X0>]>;",
        text,
    )
    assert_in("def S : FeatureSchedPredicate<FeatureXssdl>;", text)


@test
def hook_lowering_negative_controls():
    cases = [
        (
            "sti_decl D { }\nsti_function F : D { delegates = [D]; }",
            "sti_function.delegates has no landing spot on STIPredicate (M7)",
        ),
        (
            "machine M { write W; variant W { case true: [W]; } }",
            "variant requires an explicit default case",
        ),
        (
            "machine M { write W; variant Nope { default: [W]; } }",
            "variant must name a declared write",
        ),
        (
            "machine M { read R; read_variant R { default: [R]; } }\n"
            "machine N { write W; read_variant W { default: [W]; } }",
            "read_variant must name a declared read",
        ),
        (
            "machine M { resource P; retire_unit { buffer = 8; } }",
            "retire_unit: max_per_cycle is required",
        ),
        (
            "feature F : kind = tune, positive_directive = \"t\", field = \"F\", "
            "value = \"true\";\n"
            "predicate S = feature(F);\n"
            "tii_predicate T = return S;",
            "an MC context requires an MC predicate",
        ),
        (
            "machine M { write W; write_seq S = [Nope]; }",
            "write_seq S: unknown write 'Nope'",
        ),
        (
            "machine M { write W; read R; alias W -> Nope; }",
            "unknown SchedReadWrite 'Nope'",
        ),
        (
            "machine M { write W; map_itin [IIC_Nope] -> [W]; }",
            "map_itin: unknown itinerary class 'IIC_Nope'",
        ),
        (
            "machine M { write W; map [ADD] -> [W]; }\n"
            "predicate P = reg_is(1, [X0], map = f);",
            "map= is not lowered yet",
        ),
    ]
    for source, expected in cases:
        expect_emit_errors(source, [expected])


@test
def scheduling_vocabulary_is_target_global():
    source = """
machine A {
    resource P : units = 1;
    write W_ALU;
    read  R_ALU;
    bind W_ALU -> [P], latency = 1;
    advance R_ALU : cycles = 1;
}
machine B {
    resource Q : units = 2;
    write W_ALU;
    read  R_ALU;
    bind W_ALU -> [Q], latency = 2;
    advance R_ALU : cycles = 2;
}
"""
    text, skips = emit_source(source, strict=True)
    assert_true(text.count("def W_ALU : SchedWrite;") == 1, "write is emitted once")
    assert_true(text.count("def R_ALU : SchedRead;") == 1, "read is emitted once")
    assert_in("def A_WR_W_ALU", text)
    assert_in("def B_WR_W_ALU", text)
    assert_in("def A_RA_R_ALU", text)
    assert_in("def B_RA_R_ALU", text)
    assert_true(text.count("let SchedModel = ") == 2, "each model keeps its own scope")


@test
def scheduling_vocabulary_conflict_is_reported():
    source = """
machine A {
    resource P : units = 1;
    write W;
    bind W -> [P], latency = 1;
}
machine B {
    resource Q : units = 1;
    read W;
}
"""
    expect_emit_errors(source, ["'W' is already declared as a write by machine A"])


@test
def resource_collision_names_the_rule():
    source = """
machine A { resource P : units = 1; }
machine B { resource P : units = 1; }
"""
    expect_emit_errors(source, ["resource 'P' is already defined by 'machine A'", "distinct names"])


@test
def machine_scoped_records_are_namespaced():
    source = """
machine A {
    resource P : units = 1;
    write W;
    read  R;
    bind W -> [P], latency = 1;
    advance R : cycles = 1;
    load_queue : resource = P;
}
machine B {
    resource Q : units = 1;
    write W;
    read  R;
    bind W -> [Q], latency = 2;
    advance R : cycles = 2;
    load_queue : resource = Q;
}
"""
    text, _ = emit_source(source, strict=True)
    for name in ("A_LoadQueue_P", "B_LoadQueue_Q"):
        assert_in(f"def {name}", text)
    assert_true("def WR_W" not in text, "WriteRes must be namespaced")


@test
def generated_td_passes_gen_subtarget():
    """The generated records must satisfy the real scheduling-model checks.

    The stub target exists because no *partial* model can satisfy
    ``-gen-subtarget`` against a full instruction set: once a model defines any
    ``WriteRes``, every reachable ``SchedWrite`` needs one.  The stub keeps the
    instruction set small enough that the generated model is complete for it.
    """
    try:
        tblgen = compare_mod.default_tblgen()
    except compare_mod.CompareError:
        return  # no llvm-tblgen in this environment; the compare test gates that
    include_dirs = compare_mod.default_include_dirs()
    include_dirs.append(str(ROOT / "tests/ssdl/stub-target"))

    def run(text: str, tmp: str, name: str):
        path = Path(tmp) / f"{name}.td"
        path.write_text(text, encoding="utf-8")
        results = {}
        for gen in ("-gen-subtarget", "-gen-instr-info"):
            out = Path(tmp) / f"{name}{gen}.inc"
            cmd = [tblgen, gen]
            for inc in include_dirs:
                cmd.extend(["-I", inc])
            cmd.extend(["-o", str(out), str(path)])
            proc = subprocess.run(cmd, capture_output=True, text=True)
            results[gen] = (proc, out)
        return results

    text, skips = emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
    )
    assert_true(skips == [], skips)

    broken_source = """
machine stub-cpu {
    complete_model = false;
    resource P : units = 1;
    write W_ALU;
    map [ADD] -> [W_ALU, read_default, read_default];
}
"""
    broken, _ = emit_td.emit(
        parser.parse(broken_source), strict=True, target_include="SSDLStub.td"
    )

    with tempfile.TemporaryDirectory() as tmp:
        ok = run(text, tmp, "ok")
        for gen, (proc, out) in ok.items():
            assert_true(proc.returncode == 0, f"{gen} failed:\n{proc.stderr}")
            assert_true(
                out.exists() and out.stat().st_size > 0, f"{gen} produced no output"
            )
        subtarget = ok["-gen-subtarget"][1].read_text(encoding="utf-8")
        assert_in("ADD_SUB", subtarget)
        assert_in("MUL", subtarget)
        # The variant really dispatches through the generated sched class: the
        # case selects W_ALU while the default selects the materialised
        # SchedWriteRes, so both names show up in the transition table.
        assert_in("stub_cpu_WRes_W_XOR", subtarget)
        assert_in("W_ALU_ReadDefault", subtarget)

        # Negative control: ProcResources without any WriteRes is the exact
        # failure a partial model produces, and it must stay fatal.
        bad = run(broken, tmp, "broken")
        proc = bad["-gen-subtarget"][0]
        assert_true(proc.returncode != 0, "a model without WriteRes must be rejected")
        assert_in("without defining WriteRes", proc.stderr)


@test
def builtin_sti_hooks_are_bound_not_redeclared():
    """`isZeroIdiom` and friends already exist in TargetInstrPredicate.td."""
    text, skips = emit_source(
        """
sti_decl isZeroIdiom { opcode_mask = true; }
sti_decl isDependencyBreaking { opcode_mask = true; delegates = [isZeroIdiom]; }
sti_decl isOptimizableRegisterMove { }
machine m {
    write W;
}
sti_function F : isOptimizableRegisterMove {
    sched_model = m;
    equiv_class C([ADD], true);
}
"""
    )
    assert_true(skips == [], skips)
    # Binding, not redeclaration: the only declaration records are upstream's.
    assert_true("isZeroIdiomDecl : STIPredicateDecl" not in text, text)
    assert_true("IsDepBreakingDecl : STIPredicateDecl" not in text, text)
    assert_in("def F : STIPredicate<IsOptimizableRegisterMoveDecl, [C]> {", text)
    assert_in("let SchedModel = mModel;", text)

    # TargetInstrPredicate.td scopes UpdatesOpcodeMask to the first two hooks.
    expect_emit_errors(
        "sti_decl isOptimizableRegisterMove { opcode_mask = true; }\n",
        ["'isOptimizableRegisterMove' is a fixed TargetSubtargetInfo hook",
         "opcode_mask must be False"],
    )
    expect_emit_errors(
        "sti_decl isDependencyBreaking { opcode_mask = true; }\n",
        ["has a fixed delegation ['IsZeroIdiom']; got []"],
    )


@test
def sti_functions_can_share_an_equivalence_class():
    """Section 5 declares OrrXzrXzr in both isZeroIdiom and isDependencyBreaking."""
    text, skips = emit_source(
        """
sti_decl isZeroIdiom { }
sti_decl isDependencyBreaking { delegates = [isZeroIdiom]; }
machine m { write W; }
sti_function Z : isZeroIdiom {
    sched_model = m;
    dep_breaking_class Shared([ADD], same_reg(1, 2), [1, 2]);
}
sti_function D : isDependencyBreaking {
    sched_model = m;
    dep_breaking_class Shared([ADD], same_reg(1, 2), [1, 2]);
}
"""
    )
    assert_true(skips == [], skips)
    assert_true(text.count("def Shared : DepBreakingClass<") == 1, text)
    assert_in("STIPredicate<IsZeroIdiomDecl, [Shared]>", text)
    assert_in("STIPredicate<IsDepBreakingDecl, [Shared]>", text)

    expect_emit_errors(
        """
sti_decl isZeroIdiom { }
machine m { write W; }
sti_function Z : isZeroIdiom {
    sched_model = m;
    dep_breaking_class Shared([ADD], same_reg(1, 2), [1, 2]);
}
sti_function D : isZeroIdiom {
    sched_model = m;
    dep_breaking_class Shared([SUB], same_reg(1, 2), [1, 2]);
}
""",
        ["equivalence class 'Shared' is already declared with different contents"],
    )


@test
def variant_self_reference_needs_a_local_binding():
    """A branch that names the subject itself selects this model's binding."""
    expect_emit_errors(
        """
machine m {
    write W;
    variant W {
        case opcode([ADD]): [W];
        default: [W];
    }
}
""",
        ["'W' selects itself", "add a `bind` or name a distinct SchedReadWrite"],
    )


@test
def variant_cycles_are_rejected_before_tblgen():
    """llvm-tblgen loops forever on a variant cycle; diagnose it in SSDL."""
    expect_emit_errors(
        """
machine m {
    resource P : units = 1;
    write A; write B;
    bind A -> [P];
    bind B -> [P];
    variant A {
        case opcode([ADD]): [B];
        default: [A];
    }
    variant B {
        case opcode([ADD]): [A];
        default: [B];
    }
}
""",
        ["variant cycle: A -> B -> A"],
    )


@test
def variant_subject_cannot_be_an_alias_endpoint():
    """A subject gets its own SchedAlias, so `alias` cannot also use it."""
    expect_emit_errors(
        """
machine m {
    resource P : units = 1;
    write A; write B;
    bind A -> [P];
    bind B -> [P];
    alias A -> B;
    variant A {
        case opcode([ADD]): [B];
        default: [A];
    }
}
""",
        ["'A' is both an alias source and a variant subject"],
    )


@test
def attribute_schema_exports_tablegen_records():
    from ssdl import schema as schema_mod

    records = """def ADD {  // Instruction
  bit isPseudo = 0;
  bit mayLoad = 0;
  bit mayStore = 1;
  int Size = 4;
}
def BROKEN {  // Instruction
  bit mayLoad = ?;
  bit hasSideEffects = ?;
  dag InOperandList = (ins i32imm:$imm);
}
def X0 {  // Register
  bit isConstant = 1;
  bit isArtificial = 0;
}
def GPR64 {  // RegisterClass
  bit isAllocatable = 1;
}
"""
    merged = schema_mod.build_schema(
        records,
        {"inst": {"ADD": {"category": "alu"}}, "reg": {"X0": {"is_zero": True}}},
    )
    assert_true(merged["order"]["inst"] == ["ADD", "BROKEN"], "inst order")
    assert_true(merged["order"]["reg"] == ["X0"], "reg order")
    assert_true(
        merged["inst"]["ADD"]
        == {"is_pseudo": False, "may_load": False, "may_store": True, "category": "alu"},
        f"ADD attrs: {merged['inst']['ADD']!r}",
    )
    assert_true(
        merged["inst"]["BROKEN"] == {"may_load": None, "has_side_effects": None},
        "unset bit must stay tri-state (null), not be dropped or coerced to false",
    )
    assert_true(
        merged["reg"]["X0"] == {"is_constant": True, "is_artificial": False, "is_zero": True},
        f"X0 attrs: {merged['reg']['X0']!r}",
    )
    assert_true("GPR64" not in merged["reg"], "register classes are not registers")


@test
def anonymous_records_are_canonicalized():
    a = 'include "RISCV.td"\ndef X : CheckAll<[CheckOpcode<[ADD]>]>;\n'
    b = 'include "RISCV.td"\ndef X : CheckAll<[CheckOpcode<[ADD]>]>;\n'
    c = 'include "RISCV.td"\ndef X : CheckAll<[CheckOpcode<[SUB]>]>;\n'
    with tempfile.TemporaryDirectory() as tmp:
        pa = Path(tmp) / "a.td"
        pb = Path(tmp) / "b.td"
        pc = Path(tmp) / "c.td"
        for path, text in ((pa, a), (pb, b), (pc, c)):
            path.write_text(text, encoding="utf-8")
        same = compare_mod.compare(
            str(pa), str(pb), compare_mod.default_tblgen(), compare_mod.default_include_dirs()
        )
        assert_true(same.ok(), "identical nested predicates did not compare equal")
        diff = compare_mod.compare(
            str(pa), str(pc), compare_mod.default_tblgen(), compare_mod.default_include_dirs()
        )
    assert_true(not diff.ok(), "nested predicate mutation was not detected")
    rendered = diff.render()
    hashes = set(re.findall(r"anon#[0-9a-f]{12}", rendered))
    assert_true(len(hashes) == 2, f"expected two distinct anon hashes:\n{rendered}")


@test
def advance_unsupported_is_lowered():
    """ProcReadAdvance has an Unsupported bit; SSDL must be able to set it."""
    text, skips = emit_source(
        """
machine M {
    resource P : units = 1;
    write W;
    read R;
    bind W -> [P];
    advance R : cycles = 0 : unsupported = true;
}
"""
    )
    assert_true(skips == [], skips)
    assert_in("def M_RA_R : ReadAdvance<R, 0> { let Unsupported = true; }", text)

    # The read-variant materialisation path carries the same field.
    text, _ = emit_source(
        """
machine M {
    resource P : units = 1;
    write W;
    read R;
    bind W -> [P];
    advance R : cycles = 1 : unsupported = true;
    read_variant R { case true: [R]; default: [R]; }
}
"""
    )
    assert_in("def M_RAdv_R : SchedReadAdvance<1> { let Unsupported = true; }", text)

    expect_emit_errors(
        "machine M { resource P : units = 1; write W; read R; bind W -> [P]; "
        "advance R : cycles = 0 : nope = true; }",
        ["unknown property 'nope'"],
    )


@test
def machine_name_is_the_mcpu_string():
    """RISC-V `-mcpu` names contain '-' and must survive as the processor Name."""
    text, skips = emit_source("machine sifive-7-series { issue_width = 2; }")
    assert_true(skips == [], skips)
    assert_in(
        'def sifive_7_series : RISCVTuneProcessorModel<"sifive-7-series", '
        "sifive_7_seriesModel>;",
        text,
    )


@test
def from_records_round_trips_the_stub_model():
    """records -> SSDL -> records must reproduce the stub model field by field."""
    try:
        tblgen = compare_mod.default_tblgen()
    except compare_mod.CompareError:
        return
    include_dirs = compare_mod.default_include_dirs()
    include_dirs.append(str(ROOT / "tests/ssdl/stub-target"))

    text, skips = emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
    )
    assert_true(skips == [], skips)

    def dump(td_text: str, tmp: str, name: str) -> str:
        td_path = Path(tmp) / f"{name}.td"
        td_path.write_text(td_text, encoding="utf-8")
        rec_path = Path(tmp) / f"{name}.rec"
        cmd = [tblgen, "-print-records"]
        for inc in include_dirs:
            cmd.extend(["-I", inc])
        cmd.extend(["-o", str(rec_path), str(td_path)])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert_true(proc.returncode == 0, f"-print-records failed:\n{proc.stderr}")
        return str(rec_path)

    with tempfile.TemporaryDirectory() as tmp:
        original = records_mod.load(dump(text, tmp, "original"))
        ssdl = from_records_mod.generate(
            original, "stub_cpuModel", "stub-cpu", "stub_cpu", False
        )
        generated = records_mod.load(
            dump(
                emit_td.emit(
                    parser.parse(ssdl), strict=True, target_include="SSDLStub.td"
                )[0],
                tmp,
                "roundtrip",
            )
        )
    intree_model = equiv_mod.build(original, "stub_cpuModel")
    generated_model = equiv_mod.build(generated, "stub_cpuModel")
    code = equiv_mod.compare(
        intree_model,
        generated_model,
        20,
        equiv_mod.build_processor(original, "stub_cpu"),
        equiv_mod.build_processor(generated, "stub_cpu"),
    )
    assert_true(code == 0, "records -> SSDL -> records did not round trip")


# ---------------------------------------------------------------- records
def dump_records(td_text: str, tmp: str, name: str, include_dirs: list[str]) -> "records_mod.RecordFile":
    """Run llvm-tblgen -print-records on ``td_text`` and parse the dump."""
    try:
        tblgen = compare_mod.default_tblgen()
    except compare_mod.CompareError as exc:  # pragma: no cover - no tblgen here
        raise Failure(str(exc)) from exc
    td_path = Path(tmp) / f"{name}.td"
    td_path.write_text(td_text, encoding="utf-8")
    rec_path = Path(tmp) / f"{name}.rec"
    cmd = [tblgen, "-print-records"]
    for inc in include_dirs:
        cmd.extend(["-I", inc])
    cmd.extend(["-o", str(rec_path), str(td_path)])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert_true(proc.returncode == 0, f"-print-records failed:\n{proc.stderr}")
    return records_mod.load(str(rec_path))


def dump_field(recs: "records_mod.RecordFile", name: str, field: str) -> str | None:
    """Value of ``field`` in a -print-records body line, or None."""
    for line in recs.body.get(name, ()):
        text = line.strip()
        if not text.endswith(";"):
            continue
        lhs, sep, value = text[:-1].partition(" = ")
        if not sep:
            continue
        if lhs.split()[-1] == field:
            return value.strip()
    return None


def normalize_value(value: str) -> str:
    """Compare TableGen values as printed: bits are 1/0, lists drop padding."""
    value = value.strip()
    if value == "true":
        return "1"
    if value == "false":
        return "0"
    return re.sub(r",\s+", ", ", value)


_SCHEMA_CACHE: dict[tuple[str, ...], "td_schema_mod.Schema"] = {}


def llvm_schema(*extra: str) -> "td_schema_mod.Schema":
    """Parse the TD declarations once; the census and the type checks share it.

    Returns None when there is no LLVM checkout, so callers can skip.
    """
    src = os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm")
    key = (src,) + extra
    if key not in _SCHEMA_CACHE:
        sources = td_schema_mod.default_sources(src)
        sources.extend(Path(path) for path in extra)
        _SCHEMA_CACHE[key] = td_schema_mod.parse_files(sources)
    return _SCHEMA_CACHE[key]


def has_llvm_source() -> bool:
    src = os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm")
    return Path(src, "include/llvm/Target/TargetSchedule.td").is_file()


def tblgen_include_dirs() -> list[str]:
    include_dirs = compare_mod.default_include_dirs()
    include_dirs.append(str(ROOT / "tests/ssdl/stub-target"))
    return include_dirs


@test
def backend_bindings_are_declared_in_the_schema():
    """The strings the emitters bind to must exist in the TD declarations.

    This is the executable form of "the keywords have no public spec": the
    schema is parsed from the same headers the backend includes, so a binding
    that drifts from its declaration fails here instead of silently reading a
    default.
    """
    src = os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm")
    if not Path(src, "utils/TableGen").is_dir():
        return  # no LLVM checkout; the rest of the checks do not need one
    schema = llvm_schema()
    bindings, scanned = td_schema_mod.census(src)
    assert_true(len(scanned) >= 6, f"only scanned {len(scanned)} emitter files")
    undeclared = [
        binding for binding in bindings if not schema.resolve(binding)
    ]
    assert_true(
        undeclared == [],
        "undeclared backend bindings: "
        + ", ".join(
            f"{b.file}:{b.line}:{b.kind} {b.name}" for b in undeclared
        ),
    )
    # The count is a regression guard: a pattern that stops matching (layout
    # change in the emitter) must not look like a clean census.
    assert_true(
        len({(b.kind, b.name) for b in bindings}) >= 240,
        f"census found only {len(bindings)} bindings; patterns may have drifted",
    )


@test
def inst_rw_unsupported_is_lowered():
    """`map ... : unsupported = true` reaches InstRW.Unsupported and compiles.

    Inline bodies carry no trailing semicolon: llvm-tblgen rejects
    ``def ... { ... };`` outright, so the previous shape never reached a test.
    """
    text, skips = emit_td.emit(
        parser.parse(
            """
machine m {
    write W_ALU;
    map [ADD] -> [W_ALU] : unsupported = true;
    map [SUB] -> [W_ALU] : unsupported = false;
}
"""
        ),
        target_include="SSDLStub.td",
    )
    assert_true(skips == [], skips)
    assert_in(
        "def m_IRW_ADD : InstRW<[W_ALU], (instrs ADD)> { let Unsupported = true; }",
        text,
    )
    assert_in(
        "def m_IRW_SUB : InstRW<[W_ALU], (instrs SUB)> { let Unsupported = false; }",
        text,
    )
    with tempfile.TemporaryDirectory() as tmp:
        recs = dump_records(text, tmp, "inst_rw_unsupported", tblgen_include_dirs())
    assert_true(dump_field(recs, "m_IRW_ADD", "Unsupported") == "1", "Unsupported bit")
    assert_true(dump_field(recs, "m_IRW_SUB", "Unsupported") == "0", "Unsupported bit")


@test
def record_table_fields_are_declared_by_their_classes():
    """Every field binding the emitter writes exists on the class it names.

    ``emit_td.py`` holds the SSDL-to-TD field mapping as Python constants
    (``latency`` -> ``Latency`` ...).  The record table makes that mapping
    checkable: a renamed or misspelled field would land on a class that does
    not declare it, and llvm-tblgen would silently use the default.
    """
    if not has_llvm_source():
        return
    schema = llvm_schema()
    records = record_ir_mod.RecordSet()
    emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
        records=records,
    )
    unknown = []
    for record in records:
        for field_name, _ in record.fields:
            declared = set()
            for base in record.bases:
                declared.update(schema.fields_of(base.name))
            if field_name not in declared:
                unknown.append(f"{record.name}.{field_name}")
    assert_true(unknown == [], f"fields not declared by their class: {unknown}")


@test
def keyword_reference_is_up_to_date():
    """The generated keyword reference must match the backend it documents.

    This is the public spec of "which TD names the emitters understand": if a
    binding is added, renamed, or read somewhere else, regenerating changes the
    document and this check fails until the committed copy is refreshed.
    """
    src = os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm")
    if not Path(src, "utils/TableGen").is_dir():
        return
    schema = llvm_schema()
    bindings, scanned = td_schema_mod.census(src)
    expected = td_schema_mod.render_reference(schema, bindings, scanned)
    path = ROOT / "docs/dsl/2026-11-16-ssdl-keyword-reference.md"
    committed = path.read_text(encoding="utf-8")
    assert_true(
        committed == expected,
        f"{path.name} is stale; regenerate with "
        "`python3 tools/ssdl/td_schema.py --reference > "
        f"docs/dsl/{path.name}`",
    )


@test
def record_values_match_the_declared_types():
    """Values are typed against the TD declarations, not just carried as text.

    Positive: every value the stub model emits checks out.  Negative: the two
    mistakes this project has actually made (`"1"` where an int belongs, a
    string pushed into `RISCVExtension`'s implies slot) are rejected.
    """
    if not has_llvm_source():
        return
    schema = llvm_schema(str(ROOT / "tests/ssdl/stub-target/SSDLStub.td"))

    records = record_ir_mod.RecordSet()
    emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
        records=records,
    )
    checker = td_types_mod.check(records, schema)
    assert_true(
        checker.errors == [],
        "type errors: " + "; ".join(d.render() for d in checker.errors),
    )
    assert_true(checker.checked >= 80, f"only {checker.checked} values checked")

    bad = record_ir_mod.RecordSet()
    bad.add("BadLatency", ["ReadAdvance<R_ALU, \"1\">"])
    # The 17.3 bug: field/value shifted into the `list<SubtargetFeature>`
    # implies slot because the emitter forgot the empty list.
    bad.add("BadSlot", ['RISCVExtension<2, 1, "d", "field", "value">'])
    bad.add("BadField", ["WriteRes<W_ALU, [P]>"], [("Latency", '"1"')])
    errors = td_types_mod.check(bad, schema).errors
    messages = "\n".join(d.render() for d in errors)
    assert_in("expected int", messages)
    assert_in("expected list", messages)
    assert_in("BadField.Latency: expected int", messages)


@test
def every_def_in_the_output_belongs_to_the_record_table():
    """The printed TableGen is a rendering of the table, not a second source."""
    records = record_ir_mod.RecordSet()
    text, skips = emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
        records=records,
    )
    assert_true(skips == [], skips)
    printed = set(re.findall(r"^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M))
    registered = {record.name for record in records}
    assert_true(
        printed == registered,
        f"text and record table disagree: only in text {printed - registered}, "
        f"only in table {registered - printed}",
    )


@test
def record_fields_survive_the_json_table_and_llvm_tblgen():
    """Records -> JSON -> TableGen reaches the same RecordKeeper.

    Every record's declared fields are first checked against the dump of the
    text the emitter printed, then the table alone is replayed through
    ``record_ir.py`` and the two dumps are compared record by record.
    """
    records = record_ir_mod.RecordSet()
    text, _ = emit_td.emit(
        parser.parse_file(str(ROOT / "tests/ssdl/stub-mini.ssdl")),
        strict=True,
        target_include="SSDLStub.td",
        records=records,
    )
    with tempfile.TemporaryDirectory() as tmp:
        original = dump_records(text, tmp, "table_original", tblgen_include_dirs())

        # 1. The table agrees with what llvm-tblgen read from the printed text.
        for record in records:
            assert_true(record.name in original.cls, f"{record.name} missing from dump")
            for base in record.bases:
                assert_true(
                    base.name in original.cls[record.name],
                    f"{record.name}: class {base.name} not in "
                    f"{original.cls[record.name]!r}",
                )
            for field, value in record.fields:
                printed = dump_field(original, record.name, field)
                assert_true(
                    printed is not None,
                    f"{record.name}.{field} missing from the dump",
                )
                assert_true(
                    normalize_value(printed) == normalize_value(value),
                    f"{record.name}.{field}: table says {value!r}, "
                    f"llvm-tblgen printed {printed!r}",
                )

        # 2. The table is typed: a consumer must not have to parse TableGen
        #    expressions, so every value is a node with a `kind`.
        table = json.loads(records.dumps())
        for entry in table["records"]:
            for base in entry["bases"]:
                assert_true(
                    all(isinstance(arg, dict) and "kind" in arg for arg in base["args"]),
                    f"{entry['name']}: untyped class argument",
                )
            for _, value in entry["fields"]:
                assert_true(
                    isinstance(value, dict) and "kind" in value,
                    f"{entry['name']}: untyped field value",
                )

        # 3. The JSON table alone reproduces the same records.
        replay = record_ir_mod.RecordSet.from_json(table)
        replay_text = 'include "SSDLStub.td"\n\n' + replay.render()
        replayed = dump_records(replay_text, tmp, "table_replay", tblgen_include_dirs())
    assert_true(
        set(replayed.cls) >= {record.name for record in records},
        "the replay lost records",
    )
    for record in records:
        assert_true(
            replayed.cls[record.name] == original.cls[record.name],
            f"{record.name}: base chain changed on replay "
            f"({original.cls[record.name]!r} -> {replayed.cls[record.name]!r})",
        )
        for field, value in record.fields:
            got = dump_field(replayed, record.name, field)
            assert_true(
                got is not None and normalize_value(got) == normalize_value(value),
                f"{record.name}.{field}: replay says {got!r}, table says {value!r}",
            )

@test
def census_separates_explicit_bindings_from_tblgen_fill_ins():
    """A model with one authored WriteRes must not be credited with the defaults.

    ``-print-records`` emits a ``WriteRes`` for every SchedWrite, so W_B shows
    up with ``Unsupported = 1`` even though the model never bound it.  The
    census exists to stop that fill-in from being counted as authored work.
    The record table is built in memory so the test pins the counting rule
    without depending on a tblgen checkout.
    """
    recs = records_mod.RecordFile()
    recs.cls = {
        "TestModel": "SchedMachineModel",
        "W_A": "SchedWrite",
        "W_B": "SchedWrite",
        "R_A": "SchedRead",
        "R_B": "SchedRead",
        "anon_wr_a": "WriteRes",
        "anon_wr_b": "WriteRes",
        "anon_ra_a": "ReadAdvance",
        "anon_ra_b": "ReadAdvance",
        "P": "ProcResource",
    }
    scope = "  SchedMachineModel SchedModel = TestModel;"
    recs.body = {
        "TestModel": ["  int IssueWidth = 2;"],
        "anon_wr_a": ["  int Latency = 1;", "  bit Unsupported = 0;", scope],
        "anon_wr_b": ["  int Latency = 1;", "  bit Unsupported = 1;", scope],
        "anon_ra_a": ["  int Cycles = 1;", "  bit Unsupported = 0;", scope],
        "anon_ra_b": ["  int Cycles = 1;", "  bit Unsupported = 1;", scope],
        "P": ["  int NumUnits = 1;", scope],
    }
    rows, _files = census_mod.census(recs, riscv_dir=None)
    assert_true([row["model"] for row in rows] == ["TestModel"], f"models: {rows}")
    row = rows[0]
    assert_true(row["write_res_explicit"] == 1, f"explicit writes: {row}")
    assert_true(row["read_advance_explicit"] == 1, f"explicit reads: {row}")
    assert_true(row["proc_resources"] == 1, f"resources: {row}")
    defs = census_mod.index_by_model(recs)["TestModel"]
    total = sum(1 for name in defs if recs.cls.get(name) == "WriteRes")
    assert_true(total == 2, f"expected one default fill-in, got {total} WriteRes")


@test
def riscv_census_matches_the_committed_table():
    """The Step 1 table must be reproducible from the pinned records dump.

    Skipped when the dump or the LLVM checkout is absent, exactly like the
    schema test; the committed CSV carries the LLVM commit it was taken from.
    """
    dump = Path("/tmp/riscv.rec")
    src = Path(os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm"))
    table = ROOT / "data/riscv-sched-census.csv"
    if not dump.is_file() or not (src / "lib/Target/RISCV").is_dir() or not table.is_file():
        return
    recs = records_mod.load(str(dump))
    rows, _files = census_mod.census(recs, riscv_dir=src / "lib/Target/RISCV")
    text = census_mod.render(rows, census_mod.llvm_commit(src))
    assert_true(
        text == table.read_text(),
        "census table is stale; regenerate with tools/ssdl/census_riscv.py "
        "--records /tmp/riscv.rec --llvm <llvm>",
    )


@test
def sifive7_family_table_records_the_sparse_shape():
    """The Step 2 table must pin the axes, not just the three model names.

    SiFive7 is a sparse family: four parameters, three members, and the two
    boolean axes are correlated with VLEN.  A table that only lists names
    would let a wrong parameter tuple pass, so the exact tuple is asserted.
    """
    table = ROOT / "data/riscv-sched-families.json"
    assert_true(table.is_file(), f"missing {table}")
    data = json.loads(table.read_text())
    family = data["families"]["siFive7"]
    assert_true(
        family["params"] == ["vlen", "has_dual_valu", "fp_latencies", "has_fast_gather"],
        f"params: {family['params']}",
    )
    assert_true(family["member_count"] == 3, f"members: {family['member_count']}")
    expected = {
        ("SiFive7VLEN128X100Model", 128, False, "Low", True),
        ("SiFive7VLEN512Model", 512, False, "Default", False),
        ("SiFive7VLEN1024X300Model", 1024, True, "Low", True),
    }
    got = {
        (
            member["name"],
            member["vlen"],
            member["has_dual_valu"],
            member["fp_latencies"],
            member["has_fast_gather"],
        )
        for member in family["members"]
    }
    assert_true(got == expected, f"members: {sorted(got)}")
    assert_true(
        family["observed_domain_product"] == 24,
        f"domain product: {family['observed_domain_product']}",
    )
    # The two boolean axes are not free: dual-VALU implies VLEN 1024, and fast
    # gather is absent only in the 512 model.
    assert_true(
        {m["vlen"] for m in family["members"] if m["has_dual_valu"]} == {1024},
        "has_dual_valu is not correlated with vlen",
    )
    assert_true(
        {m["vlen"] for m in family["members"] if m["has_fast_gather"]} == {128, 1024},
        "has_fast_gather is not correlated with vlen",
    )

    vocab = data["vocabulary"]
    assert_true(
        vocab["LMUL"]["values"] == ["MF8", "MF4", "MF2", "M1", "M2", "M4", "M8"],
        f"LMUL: {vocab['LMUL']['values']}",
    )
    assert_true(vocab["LMUL"]["subsets"]["SchedMxListW"] == ["MF8", "MF4", "MF2", "M1", "M2", "M4"], "W subset")
    assert_true(vocab["SEW"]["base"]["MF8"] == [8], f"MF8 SEW: {vocab['SEW']['base']['MF8']}")
    assert_true(vocab["SEW"]["base"]["MF4"] == [8, 16], f"MF4 SEW: {vocab['SEW']['base']['MF4']}")
    assert_true(vocab["SEW"]["base"]["MF2"] == [8, 16, 32], f"MF2 SEW: {vocab['SEW']['base']['MF2']}")
    assert_true(vocab["SEW"]["base"]["M1"] == [8, 16, 32, 64], f"M1 SEW: {vocab['SEW']['base']['M1']}")
    assert_true(vocab["EEW"]["values"] == [8, 16, 32, 64], f"EEW: {vocab['EEW']['values']}")
    assert_true((vocab["NF"]["min"], vocab["NF"]["max"]) == (2, 8), f"NF: {vocab['NF']}")
    assert_true(
        data["projections"][0]["value"] == "M8",
        f"worst(LMUL): {data['projections'][0]}",
    )


@test
def riscv_family_table_matches_the_sources():
    """Regenerating the family table from the pinned sources must be a no-op."""
    src = Path(os.environ.get("SSDL_LLVM_SRC", "/home/ubuntu/llvm/trees/explore/llvm"))
    table = ROOT / "data/riscv-sched-families.json"
    if not (src / "lib/Target/RISCV").is_dir() or not table.is_file():
        return
    text = family_mod.render(
        family_mod.build(src / "lib/Target/RISCV", family_mod.llvm_commit(src))
    )
    assert_true(
        text == table.read_text(),
        "family table is stale; regenerate with tools/ssdl/family_tables.py",
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
