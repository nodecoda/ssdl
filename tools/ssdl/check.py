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
from ssdl import diagnose as diagnose_mod  # noqa: E402
from ssdl import effects as effects_mod  # noqa: E402
from ssdl import elaborate as elaborate_mod  # noqa: E402
from ssdl import emit_td, expand_records as expand_mod, parser  # noqa: E402
from ssdl import equiv as equiv_mod  # noqa: E402
from ssdl import paths as paths_mod  # noqa: E402
from ssdl import family_expand as family_mod  # noqa: E402
from ssdl import family_mine as family_mine_mod  # noqa: E402
from ssdl import family_sweep as family_sweep_mod  # noqa: E402
from ssdl import family_tables as family_tables_mod  # noqa: E402
from ssdl import from_records as from_records_mod  # noqa: E402
from ssdl import golden as golden_mod  # noqa: E402
from ssdl import record_ir as record_ir_mod  # noqa: E402
from ssdl import records as records_mod  # noqa: E402
from ssdl import sets as sets_mod  # noqa: E402
from ssdl import semir as semir_mod  # noqa: E402
from ssdl import syntax_v2  # noqa: E402
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


def llvm_schema(*extra: str) -> "td_schema_mod.Schema | None":
    """Parse the TD declarations once; the census and the type checks share it.

    Returns None when ``SSDL_LLVM_SRC`` is unset, so callers can skip.
    """
    src = paths_mod.llvm_src()
    if src is None:
        return None
    key = (str(src),) + extra
    if key not in _SCHEMA_CACHE:
        sources = td_schema_mod.default_sources(str(src))
        sources.extend(Path(path) for path in extra)
        _SCHEMA_CACHE[key] = td_schema_mod.parse_files(sources)
    return _SCHEMA_CACHE[key]


def has_llvm_source() -> bool:
    src = paths_mod.llvm_src()
    return src is not None and (src / "include/llvm/Target/TargetSchedule.td").is_file()


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
    src = paths_mod.llvm_src()
    if src is None or not (src / "utils/TableGen").is_dir():
        return  # no LLVM checkout; the rest of the checks do not need one
    schema = llvm_schema()
    bindings, scanned = td_schema_mod.census(str(src))
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
    src = paths_mod.llvm_src()
    if src is None or not (src / "utils/TableGen").is_dir():
        return
    schema = llvm_schema()
    bindings, scanned = td_schema_mod.census(str(src))
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
def effect_keys_resolve_in_the_schema():
    """Every consumer-semantics key names a field the TD schema declares.

    ``effects.py`` is curated.  If upstream renames a field, the table must be
    re-audited; silently documenting a field that no longer exists is exactly
    the folklore this module is supposed to replace.
    """
    if not has_llvm_source():
        return
    src = paths_mod.llvm_src()
    extra = [
        str(src / "lib/Target/RISCV/RISCVFeatures.td"),
        str(src / "lib/Target/RISCV/RISCVProcessors.td"),
    ]
    schema = llvm_schema(*extra)
    missing = []
    for key in effects_mod.FIELD_EFFECTS:
        cls, fld = effects_mod.key_parts(key)
        if not schema.has_field(cls, fld):
            missing.append(key)
    assert_true(
        missing == [],
        "effects name fields the schema does not declare: " + ", ".join(missing),
    )


@test
def effect_evidence_is_live():
    """Each claim points at a real file and a line that still exists.

    This is deliberately weaker than matching the line's text: comments and
    code move independently, but a missing file or an out-of-range line is a
    definite stale audit.
    """
    if not has_llvm_source():
        return
    root = paths_mod.require_llvm_src().parent
    problems = []
    for key, effect in effects_mod.FIELD_EFFECTS.items():
        if not effect.evidence:
            problems.append(f"{key}: no evidence")
            continue
        for item in effect.evidence:
            path, _, line = item.rpartition(":")
            source = root / path
            if not source.is_file():
                problems.append(f"{key}: missing {path}")
                continue
            if not line.isdigit():
                problems.append(f"{key}: bad line reference {item}")
                continue
            count = sum(1 for _ in source.open("rb"))
            if not 1 <= int(line) <= count:
                problems.append(f"{key}: line {line} > {count} in {path}")
    assert_true(
        problems == [],
        "stale effect evidence:\n  " + "\n  ".join(problems),
    )


@test
def effect_roles_are_consistent():
    """Role, consumers, and emission flags cannot contradict each other."""
    problems = []
    for key, effect in effects_mod.FIELD_EFFECTS.items():
        if effect.role not in effects_mod.ROLES:
            problems.append(f"{key}: unknown role {effect.role!r}")
        if effect.role == effects_mod.ROLE_EFFECT and not effect.consumers:
            problems.append(f"{key}: effect with no consumer")
        if effect.role != effects_mod.ROLE_EFFECT and effect.consumers:
            problems.append(
                f"{key}: {effect.role} claims consumers {sorted(effect.consumers)}"
            )
        if effect.role == effects_mod.ROLE_DEAD and effect.emitted:
            problems.append(f"{key}: dead field is emitted")
        unknown = set(effect.consumers) - effects_mod.RUNTIME_CONSUMERS
        if unknown:
            problems.append(f"{key}: unknown consumers {sorted(unknown)}")
        if not effect.default_expansion:
            problems.append(f"{key}: no default_expansion")
    assert_true(
        problems == [],
        "inconsistent effects:\n  " + "\n  ".join(problems),
    )


@test
def schema_json_carries_consumer_semantics():
    """``--json`` is the machine-readable contract, not just field types.

    Spot-check the three categories the audit established: MCA-only,
    CodeGen-only, and dead.
    """
    if not has_llvm_source():
        return
    data = llvm_schema().to_json()
    fields = data["classes"]["ProcWriteResources"]["fields"]
    assert_true(
        fields["RetireOOO"]["effect"]["consumers"] == ["mca"],
        f"RetireOOO should be MCA-only: {fields['RetireOOO'].get('effect')}",
    )
    assert_true(
        fields["AcquireAtCycles"]["effect"]["consumers"] == ["codegen"],
        "AcquireAtCycles should be CodeGen-only: "
        f"{fields['AcquireAtCycles'].get('effect')}",
    )
    dead = data["classes"]["InstRW"]["fields"]["Unsupported"]["effect"]
    assert_true(dead["role"] == "dead", f"InstRW.Unsupported should be dead: {dead}")
    tti = data["classes"]["SchedMachineModel"]["fields"][
        "LoopMicroOpBufferSize"
    ]["effect"]
    assert_true(tti["consumers"] == ["tti"], f"LoopMicroOpBufferSize: {tti}")


@test
def effect_diagnostics_name_the_real_consumer():
    """A misleading knob produces a warning that names the subsystem.

    ``loop_micro_op_buffer`` sits in the scheduling block but only feeds the
    TTI unroll threshold; ``retire_ooo`` looks like a scheduling property but
    only llvm-mca reads it.
    """
    source = """
machine M {
  loop_micro_op_buffer = 8;
  resource P;
  write W;
  read R;
  bind W -> [P], latency = 3, retire_ooo = true;
  advance R : cycles = 1;
}
"""
    findings = diagnose_mod.diagnose(parser.parse(source))
    warnings = [f for f in findings if f.level == diagnose_mod.WARNING]
    notes = [f for f in findings if f.level == diagnose_mod.NOTE]
    text = "\n".join(f.render() for f in warnings)
    assert_in("TTI unroll threshold", text)
    assert_in("retire_ooo", text)
    assert_true(
        any("ReleaseAtCycles defaults" in f.message for f in notes),
        "latency without release should explain the default: "
        + "\n".join(f.render() for f in notes),
    )
    assert_true(
        not [f for f in findings if f.level == diagnose_mod.ERROR],
        "unexpected errors: " + "\n".join(f.render() for f in findings),
    )


@test
def effect_diagnostics_warn_about_dead_inst_rw_unsupported():
    """``map ... : unsupported = true`` asks for an effect that does not exist.

    The lowering is still exercised (SSDL 1.0 defines it), but the author is
    told that InstRW.Unsupported has no consumer.
    """
    source = """
machine M {
  write W;
  map [ADD] -> [W] : unsupported = true;
}
"""
    findings = diagnose_mod.diagnose(parser.parse(source))
    warnings = [f for f in findings if f.level == diagnose_mod.WARNING]
    assert_true(len(warnings) == 1, f"expected one warning, got {findings}")
    assert_in("dead field", warnings[0].message)
    assert_true(warnings[0].effect == "InstRW.Unsupported", warnings[0])

    text, skips = emit_source(source)
    assert_true(skips == [], skips)
    assert_in("InstRW", text)


@test
def effect_diagnostics_flag_non_portable_sti_predicates():
    """A default ``expand_for_mc`` silently turns pseudo/non_portable into false."""
    source = """
machine M { }
sti_decl X { }
sti_function F : X {
  sched_model = M;
  dep_breaking_class C([ADD], non_portable([{ true }]), [1]);
}
"""
    findings = diagnose_mod.diagnose(parser.parse(source))
    warnings = [f for f in findings if f.level == diagnose_mod.WARNING]
    text = "\n".join(f.render() for f in warnings)
    assert_in("non_portable", text)
    assert_in("MC version expands", text)

    quiet = """
machine M { }
sti_decl X { expand_for_mc = false; }
sti_function F : X {
  sched_model = M;
  dep_breaking_class C([ADD], non_portable([{ true }]), [1]);
}
"""
    quiet_findings = diagnose_mod.diagnose(parser.parse(quiet))
    assert_true(
        not [f for f in quiet_findings if f.level == diagnose_mod.WARNING],
        "expand_for_mc = false should silence the portability warning: "
        + "\n".join(f.render() for f in quiet_findings),
    )


@test
def effect_diagnostics_explain_itineraries_to_mca_users():
    """Itinerary-only CPUs are rejected by llvm-mca; both means MCA ignores them."""
    only_itin = """
machine M {
  itineraries {
    func_units = [FU];
    itin I { stages = [stage(1, [FU])]; }
  }
}
"""
    findings = diagnose_mod.diagnose(parser.parse(only_itin))
    warnings = [f for f in findings if f.level == diagnose_mod.WARNING]
    assert_true(
        any("rejects CPUs that provide only itineraries" in f.message for f in warnings),
        "expected an itinerary-only warning: " + "\n".join(f.render() for f in findings),
    )

    both = """
machine M {
  resource P;
  write W;
  bind W -> [P], latency = 1;
  itineraries {
    func_units = [FU];
    itin I { stages = [stage(1, [FU])]; }
  }
}
"""
    both_findings = diagnose_mod.diagnose(parser.parse(both))
    notes = [f for f in both_findings if f.level == diagnose_mod.NOTE]
    assert_true(
        any("consults them before the sched model" in f.message for f in notes),
        "expected the CodeGen-before-MCA note: "
        + "\n".join(f.render() for f in both_findings),
    )


@test
def effect_diagnostics_are_quiet_on_a_clean_model():
    """The shipped example is clean at warning/error level."""
    program = parser.parse_file(str(ROOT / "tests/ssdl/sifive7-mini.ssdl"))
    findings = diagnose_mod.diagnose(program)
    loud = [f for f in findings if f.level != diagnose_mod.NOTE]
    assert_true(
        loud == [],
        "unexpected diagnostics: " + "\n".join(f.render() for f in loud),
    )
    assert_true(
        any("RISCVISAInfo" in f.message for f in findings),
        "extension metadata should still be explained as a note",
    )


@test
def validate_all_merges_sort_errors_and_effect_findings():
    """The validate entry point exposes both kinds of diagnostic."""
    source = """
predicate P = feature(F);
tii_predicate T = return P;
machine M {
  write W;
  map [ADD] -> [W] : unsupported = true;
}
"""
    errors, findings = validate_mod.validate_all(parser.parse(source))
    assert_true(any("requires an MC predicate" in e for e in errors), errors)
    assert_true(
        any(f.level == diagnose_mod.WARNING and "dead field" in f.message for f in findings),
        findings,
    )


@test
def effect_diagnostics_fold_repeats_into_one_line():
    """A field set on thousands of writes should print once, with a count."""
    source = """
machine M {
  read R;
  write W1;
  write W2;
  write W3;
  bind W1 -> [R], acquire = [0], release = [2];
  bind W2 -> [R], acquire = [0], release = [2];
  bind W3 -> [R], acquire = [0], release = [2];
}
"""
    findings = diagnose_mod.diagnose(parser.parse(source))
    lines = diagnose_mod.render_findings(findings)
    acquire = [line for line in lines if "acquire is read only by CodeGen" in line]
    assert_true(len(acquire) == 1, lines)
    assert_true(acquire[0].endswith("(x3)"), acquire)
    assert_true(acquire[0].startswith("machine M.bind W1.acquire: note:"), acquire)


@test
def records_json_expands_class_defaults():
    """The table carries definitions; expansion fills the class defaults."""
    if not has_llvm_source():
        return
    program = parser.parse(
        """
machine M {
  resource R : units = 2;
  write W;
  bind W -> [R], latency = 5;
}
"""
    )
    records = record_ir_mod.RecordSet()
    emit_td.emit(program, records=records, target_include="SSDLStub.td")
    work = Path(tempfile.mkdtemp(prefix="ssdl-expand-"))
    path = work / "records.json"
    path.write_text(records.dumps())
    schema = expand_mod.schema_for(str(paths_mod.require_llvm_src()))
    table = expand_mod.expand_table(str(path), schema)
    assert_true("NumUnits = 2" in table.body["R"], table.body["R"])
    assert_true("BufferSize = -1" in table.body["R"], table.body["R"])
    binding = next(
        name for name, lines in table.body.items()
        if "WriteType = W" in lines
    )
    lines = table.body[binding]
    assert_true("Latency = 5" in lines, lines)
    assert_true("NumMicroOps = 1" in lines, lines)
    assert_true("Unsupported = 0" in lines, lines)


@test
def riscv_records_json_matches_without_tblgen():
    """Every model matches the in-tree dump straight from --records-json."""
    bases = paths_mod.SWEEP
    dump = paths_mod.RECORDS
    if not bases.is_dir() or not dump.is_file():
        return
    items = family_sweep_mod.discover(dump, None)
    assert_true(len(items) == 14, f"models: {len(items)}")
    for item in items:
        table = bases / family_sweep_mod.sanitise(item["model"]) / "flat.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/ssdl/equiv.py"),
                str(dump),
                str(table),
                "--in-model",
                item["model"],
                "--gen-model",
                item["instance"] + "Model",
                "--in-processor",
                item["processor"],
                "--gen-processor",
                item["instance"],
                "--llvm-src",
                str(paths_mod.require_llvm_src()),
            ],
            capture_output=True,
            text=True,
        )
        assert_true(
            "records match" in proc.stdout,
            f"{item['model']}: " + "\n".join((proc.stdout or proc.stderr).splitlines()[-4:]),
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
    dump = paths_mod.RECORDS
    src = paths_mod.llvm_src()
    table = ROOT / "data/riscv-sched-census.csv"
    if src is None or not dump.is_file() or not (src / "lib/Target/RISCV").is_dir() or not table.is_file():
        return
    recs = records_mod.load(str(dump))
    rows, _files = census_mod.census(recs, riscv_dir=src / "lib/Target/RISCV")
    text = census_mod.render(rows, census_mod.llvm_commit(src))
    assert_true(
        text == table.read_text(),
        "census table is stale; regenerate with tools/ssdl/census_riscv.py "
        f"--records {paths_mod.RECORDS} --llvm $SSDL_LLVM_SRC",
    )


@test
def riscv_model_diagnostics_match_the_committed_table():
    """Effect findings on all 14 models are pinned, and no model has an error.

    The mined sources are a sweep artifact, so this skips when the sweep
    directory is absent, exactly like the census test skips without
    ``$SSDL_CACHE/riscv.rec``.
    """
    bases = paths_mod.SWEEP
    table = ROOT / "data/riscv-sched-diagnostics.csv"
    src = paths_mod.llvm_src()
    if src is None:
        return
    commit = family_sweep_mod.llvm_commit(src)
    if not bases.is_dir() or not table.is_file() or commit == "unknown":
        return
    results = [
        family_sweep_mod.Result(
            model_dir.name,
            "match",
            "",
            [],
            family_sweep_mod.diagnose_source(
                (model_dir / "family.ssdl")
                if (model_dir / "family.ssdl").is_file()
                else (model_dir / "flat.ssdl")
            ),
        )
        for model_dir in sorted(bases.iterdir())
        if model_dir.is_dir() and (model_dir / "flat.ssdl").is_file()
    ]
    assert_true(len(results) == 14, f"models: {len(results)}")
    text = family_sweep_mod.diagnostics_table(results, commit)
    assert_true(
        text == table.read_text(),
        "diagnostics table is stale; regenerate with tools/ssdl/family_sweep.py "
        "--diagnose-only --diagnostics-table data/riscv-sched-diagnostics.csv",
    )
    loud = {
        (result.model, item["effect"])
        for result in results
        for item in result.diagnostics
        if item["level"] in ("error", "warning")
    }
    assert_true(
        loud == {("XiangShanNanHuModel", "SchedMachineModel.LoopMicroOpBufferSize")},
        f"loud findings: {sorted(loud)}",
    )


@test
def family_source_expands_before_parsing():
    """A v1.2 source is normed to v1.0 text before the parser sees it."""
    text = parser.expand_source(
        """
set A = [x, y] : kind = dimension, name_format = "_{value}";
machine M {
  write W[A];
  bind W_x -> [];
  bind W_y -> [];
}
"""
    )
    program = parser.parse(text)
    machine = next(d for d in program.decls if isinstance(d, parser.MachineDecl))
    writes = [item["name"] for item in machine.items if item["kind"] == "write"]
    binds = [item["write"] for item in machine.items if item["kind"] == "bind"]
    assert_true(writes == ["W_x", "W_y"], writes)
    assert_true(binds == ["W_x", "W_y"], binds)


@test
def family_statements_are_layout_independent():
    """AC3: breaking every statement across lines keeps the flat text intact.

    The split points are chosen where the joining rule produces exactly one
    space, so the comparison stays byte-for-byte, not merely semantic.
    """
    multi = parser.expand_source((ROOT / "tests/ssdl/multiline.ssdl").read_text())
    compact = parser.expand_source(
        """
set D = [A, B] : kind = dimension, name_format = "_{value}";
machine fam(vlen: INT) {
    write W[D];
    bind W[D] -> [P], latency = vlen / 128, uops = 1, release = [1, 2];
    variant W[D] {
        case all_of(feature(F), is_zero(1)): [W[D]];
        default: [W[D]];
    }
}
machine m512 : fam(512) {
    resource P : units = 2;
    read R;
    map [ADD] -> [W_A];
    advance R : cycles = 1 : writes = [W_A];
}
"""
    )
    if multi != compact:
        left, right = multi.splitlines(), compact.splitlines()
        index = 1
        for index, (a, b) in enumerate(zip(left, right), 1):
            if a != b:
                break
        raise Failure(
            f"line {index}: {left[index - 1]!r} != {right[index - 1]!r}"
        )


@test
def top_level_block_declarations_keep_their_braces():
    """A block-shaped global is copied as one unit, body and closing brace.

    Before the statement scanner, ``predicate P {`` was collected and the body
    then failed as a top-level declaration; ``};`` lost its semicolon too.
    """
    flat = parser.expand_source(
        """
set D = [A] : kind = dimension;
predicate P
{
    selector = cxx_expr([{ Subtarget->hasX() }]);
    assembler = all_of([Fx]);
}
tii_predicate X = switch opcode
{
    case [ADD]: return true;
    default: return false;
};
machine m : f(1)
{
}
machine f(v: INT)
{
}
"""
    )
    assert_true("predicate P {" in flat, flat)
    assert_true("assembler = all_of([Fx]);" in flat, flat)
    assert_true("tii_predicate X = switch opcode {" in flat, flat)
    assert_true("};" in flat, flat)
    parser.parse(flat)


@test
def mined_family_sources_expand_to_reparsable_v1_0():
    """Expanding an authored family source leaves plain, parseable v1.0 text.

    Older sweep directories also carry the expanded source; when it is there,
    the two must be identical.
    """
    bases = paths_mod.SWEEP
    if not bases.is_dir():
        return
    checked = 0
    for model_dir in sorted(bases.iterdir()):
        family = model_dir / "family.ssdl"
        if not family.is_file():
            continue
        flat = parser.load_source(str(family))
        assert_true(
            parser.FAMILY_SOURCE.search(flat) is None,
            f"{model_dir.name}: expansion still contains a dimension declaration",
        )
        parser.parse(flat)
        flat_path = model_dir / "flat.ssdl"
        if flat_path.is_file():
            assert_true(
                flat == flat_path.read_text(),
                f"{model_dir.name}: expansion differs from flat.ssdl",
            )
        checked += 1
    assert_true(checked == 14, f"models: {checked}")


@test
def semantic_ir_carries_identity_line_and_phase():
    """The elaborator yields structured identities, not just flat text.

    Rendering must still match the legacy text path byte-for-byte, so the IR
    can be the default without a semantic change.
    """
    source = (ROOT / "tests/ssdl/family-mini.ssdl").read_text()
    program = elaborate_mod.elaborate(source)
    symbols = {symbol.render() for symbol in program.symbols()}
    for expected in ("m512/write/W_A", "m512/bind/W_C", "m512/resource/P"):
        assert_true(expected in symbols, f"missing {expected}: {sorted(symbols)}")
    assert_true(program.dimensions["D"].values == ("A", "B", "C"), program.dimensions)
    decls = {decl.symbol.render(): decl for machine in program.machines for decl in machine.decls}
    assert_true(decls["m512/write/W_A"].line == 5, decls["m512/write/W_A"].line)
    assert_true(decls["m512/bind/W_A"].line == 6, decls["m512/bind/W_A"].line)
    assert_true(
        all(decl.phase is semir_mod.Phase.ELAB for decl in decls.values()),
        "phase 1b has no runtime values",
    )
    assert_true(
        elaborate_mod.render(program) == family_mod.expand_legacy(source),
        "IR rendering differs from the legacy expander",
    )


@test
def family_instances_spell_resources_per_scope():
    """AC4: one resource name in two instances gets two spellings.

    The identity stays ``(small, resource, PipeA)``; only the lowering name
    gains the instance prefix, so ``emit_td`` sees distinct ProcResource defs
    instead of reporting a global collision.
    """
    source = (ROOT / "tests/ssdl/two-instances.ssdl").read_text()
    program = elaborate_mod.elaborate(source)
    resources = {
        machine.name: (decl.symbol.name, decl.spelling)
        for machine in program.machines
        for decl in machine.decls
        if decl.symbol.kind == "resource"
    }
    assert_true(
        resources
        == {"small": ("PipeA", "small_PipeA"), "big": ("PipeA", "big_PipeA")},
        resources,
    )
    flat = elaborate_mod.render(program)
    assert_in("resource small_PipeA : units = 1;", flat)
    assert_in("resource big_PipeA : units = 1;", flat)
    assert_in("bind W_A -> [small_PipeA], latency = 1;", flat)
    assert_in("bind W_A -> [big_PipeA], latency = 2;", flat)
    text, _ = emit_source(flat, strict=True)
    assert_in("def small_PipeA", text)
    assert_in("def big_PipeA", text)


@test
def resource_name_template_uses_family_parameters():
    """An explicit ``name = "PipeA_{vlen}"`` is a spelling, not a new identity."""
    source = """
set D = [A] : kind = dimension;
machine fam(vlen: INT) {
    resource P : units = 1, name = "PipeA_{vlen}";
    write W[D];
    bind W[D] -> [P], latency = 1;
}
machine small : fam(128) { }
machine big : fam(256) { }
"""
    flat = parser.expand_source(source)
    assert_in("resource PipeA_128 : units = 1;", flat)
    assert_in("resource PipeA_256 : units = 1;", flat)
    assert_in("bind W_A -> [PipeA_128]", flat)
    assert_true("name = " not in flat, "the name directive is compile-time only")


@test
def colliding_resource_spellings_name_both_identities():
    """AC5: one spelling for two identities is a hard error, not a warning."""
    source = """
set D = [A] : kind = dimension;
machine fam(vlen: INT) {
    resource P : units = 1, name = "Same";
    write W[D];
    bind W[D] -> [P], latency = 1;
}
machine small : fam(1) { }
machine big : fam(2) { }
"""
    try:
        parser.expand_source(source)
    except family_mod.ExpandError as exc:
        message = str(exc)
        assert_in("machine small/resource P", message)
        assert_in("machine big/resource P", message)
        return
    raise Failure("expected a resource spelling collision")


@test
def family_conditional_expression_folds_per_instance():
    """AC6: ``?:`` is ordinary elab-time arithmetic when the condition is a parameter."""
    source = """
set D = [A] : kind = dimension;
machine fam(has_fast: BOOL, vlen: INT) {
    write W;
    bind W -> [P], latency = has_fast ? 4 : 7 + cdiv(vlen, 128),
                    release = [1, has_fast ? 2 : 3];
}
machine fast : fam(true, 512) { resource P : units = 1; }
machine slow : fam(false, 512) { resource P : units = 1; }
"""
    flat = parser.expand_source(source)
    assert_in("latency = 4, release = [1, 2];", flat)
    assert_in("latency = 11, release = [1, 3];", flat)


@test
def malformed_conditional_names_the_expression():
    source = """
set D = [A] : kind = dimension;
machine m {
    write W;
    bind W -> [P], latency = 1 ? 2;
}
"""
    try:
        parser.expand_source(source)
    except family_mod.ExpandError as exc:
        assert_in("missing ':'", str(exc))
        return
    raise Failure("expected a malformed conditional to fail")


@test
def runtime_condition_lowers_to_variant_and_aux_writes():
    """AC7: an instruction predicate in a field becomes aux writes + ``variant``."""
    source = """
set D = [A] : kind = dimension;
machine m {
    resource P : units = 1;
    write W[D];
    bind W[D] -> [P], latency = opcode([ADD]) ? 4 : 7, uops = 1;
}
"""
    flat = parser.expand_source(source)
    assert_in("write W_A__case0;", flat)
    assert_in("write W_A__case1;", flat)
    assert_in("bind W_A__case0 -> [P], latency = 4, uops = 1;", flat)
    assert_in("bind W_A__case1 -> [P], latency = 7, uops = 1;", flat)
    assert_in(
        "variant W_A {\n        case opcode([ADD]): [W_A__case0];\n"
        "        default: [W_A__case1];\n    }",
        flat,
    )
    text, _ = emit_source(flat, strict=True)
    assert_in("SchedWriteVariant<[", text)
    assert_in("CheckOpcode<[ADD]>", text)
    assert_in("[W_A__case0]", text)
    assert_in("[W_A__case1]", text)
    try:
        tblgen = compare_mod.default_tblgen()
    except compare_mod.CompareError:
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime_variant.td"
        path.write_text(text, encoding="utf-8")
        compare_mod.run_tblgen(tblgen, str(path), compare_mod.default_include_dirs())


@test
def runtime_condition_in_structural_position_is_rejected():
    """AC8: names, resources and machine properties cannot depend on the instruction."""
    cases = [
        (
            """
set D = [A] : kind = dimension;
machine m { issue_width = opcode([ADD]) ? 1 : 2; }
""",
            "structural position",
        ),
        (
            """
set D = [A] : kind = dimension;
machine m { resource P : units = opcode([ADD]) ? 1 : 2; }
""",
            "structural position",
        ),
        (
            """
set D = [A] : kind = dimension;
machine m {
    resource P : units = 1;
    write W[D];
    bind W[D] -> [P], release = [1, opcode([ADD]) ? 2 : 3];
}
""",
            "only supported on latency/uops/cycles",
        ),
    ]
    for source, needle in cases:
        try:
            parser.expand_source(source)
        except family_mod.ExpandError as exc:
            assert_in(needle, str(exc))
            assert_in("profile", str(exc)) if needle == "structural position" else None
        else:
            raise Failure(f"expected a runtime-condition error for:\n{source}")


@test
def runtime_condition_lowers_per_instance():
    """AC10: instances share SchedWrite vocabulary, not model-scoped payloads."""
    source = (ROOT / "tests/ssdl/two-instances-runtime.ssdl").read_text()
    first = parser.expand_source(source)
    second = parser.expand_source(source)
    assert_true(first == second, "expansion spelling is not deterministic")
    program_a = elaborate_mod.elaborate(source)
    program_b = elaborate_mod.elaborate(source)
    spellings_a = [
        (decl.symbol.render(), decl.spelling)
        for machine in program_a.machines
        for decl in machine.decls
    ]
    spellings_b = [
        (decl.symbol.render(), decl.spelling)
        for machine in program_b.machines
        for decl in machine.decls
    ]
    assert_true(spellings_a == spellings_b, "declaration spelling is not deterministic")

    assert_in("resource Pipe1 : units = 1;", first)
    assert_in("resource Pipe2 : units = 1;", first)
    assert_true(
        first.count("write W_A__case0;") == 2,
        "aux writes are declared once per machine in flat text",
    )
    assert_in("bind W_A__case0 -> [Pipe1], latency = 4;", first)
    assert_in("bind W_A__case0 -> [Pipe2], latency = 5;", first)

    text, _ = emit_source(first, strict=True)
    assert_true(
        text.count("def W_A__case0 : SchedWrite;") == 1,
        "aux SchedWrite is hoisted once, like any target vocabulary",
    )
    assert_in("def shallow_WR_W_A__case0", text)
    assert_in("def deep_WR_W_A__case0", text)
    assert_in("def shallow_W_AVariant", text)
    assert_in("def deep_W_AVariant", text)
    assert_true("anonymous_" not in text, "aux records must not be numbered by parse order")

    try:
        tblgen = compare_mod.default_tblgen()
    except compare_mod.CompareError:
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "two_instances_runtime.td"
        path.write_text(text, encoding="utf-8")
        compare_mod.run_tblgen(tblgen, str(path), compare_mod.default_include_dirs())


@test
def generated_aux_name_cannot_shadow_a_source_declaration():
    """An aux write gets a deterministic spelling; a source may not take it."""
    source = """
set D = [A] : kind = dimension;
machine m {
    resource P : units = 1;
    write W_A__case0;
    write W[D];
    bind W[D] -> [P], latency = opcode([ADD]) ? 4 : 7;
}
"""
    try:
        parser.expand_source(source)
    except family_mod.ExpandError as exc:
        assert_in("collides with a declaration expanded from", str(exc))
        return
    raise Failure("expected the generated aux spelling to be rejected")


@test
def declaration_order_does_not_change_equivalence():
    """AC10: reordering declarations changes the text, not the records."""
    first = """
machine m {
    resource P : units = 1;
    resource Q : units = 2;
    write W;
    write V;
    bind W -> [P], latency = 3;
    bind V -> [Q], latency = 5;
    map [ADD] -> [W, read_default];
}
"""
    second = """
machine m {
    write V;
    write W;
    resource Q : units = 2;
    resource P : units = 1;
    bind V -> [Q], latency = 5;
    bind W -> [P], latency = 3;
    map [ADD] -> [W, read_default];
}
"""
    text_a, _ = emit_source(parser.expand_source(first), strict=True)
    text_b, _ = emit_source(parser.expand_source(second), strict=True)
    assert_true(text_a != text_b, "the fixtures should differ textually")
    with tempfile.TemporaryDirectory() as tmp:
        include_dirs = compare_mod.default_include_dirs()
        recs_a = dump_records(text_a, tmp, "order_a", include_dirs)
        recs_b = dump_records(text_b, tmp, "order_b", include_dirs)
    code = equiv_mod.compare(
        equiv_mod.build(recs_a, "mModel"), equiv_mod.build(recs_b, "mModel"), 20
    )
    assert_true(code == 0, "declaration order changed the equivalence verdict")


@test
def instance_prefixes_are_normalized():
    """AC10: ``defm``/multi-instance prefixes fold before the comparison."""
    source = """
set D = [A] : kind = dimension;
machine fam(x: INT) {
    resource P : units = 1;
    write W;
    bind W -> [P], latency = 4;
}
machine small : fam(1) { }
machine big : fam(2) { }
"""
    text, _ = emit_source(parser.expand_source(source), strict=True)
    with tempfile.TemporaryDirectory() as tmp:
        recs = dump_records(text, tmp, "prefixed", compare_mod.default_include_dirs())
    small = equiv_mod.build(recs, "smallModel", "small")
    big = equiv_mod.build(recs, "bigModel", "big")
    assert_true("P" in small.resources, small.resources)
    assert_true(small.writes["W"]["resources"] == ("P",), small.writes["W"])
    code = equiv_mod.compare(small, big, 20)
    assert_true(code == 0, "instance prefixes were not folded")
    assert_true(
        equiv_mod.strip_prefix("VLEN512SiFive7PipeA", "VLEN512SiFive7") == "PipeA",
        "defm concatenation is not folded",
    )
    assert_true(
        equiv_mod.strip_prefix("small_PipeA", ("other", "small_")) == "PipeA",
        "an explicit separator prefix is not folded",
    )


@test
def finite_relation_and_profile_projection_evaluate():
    """AC9: a source relation and a host-pinned projection are both ELAB lookups."""
    flat = parser.expand_source(
        (ROOT / "tests/ssdl/projection-contract.ssdl").read_text()
    )
    assert_in("latency = 1, release = [1, 64];", flat)
    assert_in("latency = 15, release = [1, 32];", flat)
    assert_true("extern fn" not in flat, "the signature is compile-time only")
    assert_true("kind = relation" not in flat, "the table is compile-time only")


@test
def relation_miss_lists_the_domain():
    source = """
set F : kind = relation = [(8, 1), (16, 15)];
machine m {
    resource P : units = 1;
    write W;
    bind W -> [P], latency = F(24);
}
"""
    try:
        parser.expand_source(source)
    except family_mod.ExpandError as exc:
        assert_in("no row for (24", str(exc))
        assert_in("domain", str(exc))
        return
    raise Failure("expected a relation miss")


@test
def unknown_projection_and_unpinned_value_are_rejected():
    unknown = """
extern fn f(a: INT) -> INT : missing;
machine m {
    resource P : units = 1;
    write W;
    bind W -> [P], latency = f(1);
}
"""
    try:
        parser.expand_source(unknown)
    except family_mod.ExpandError as exc:
        assert_in("unknown projection 'missing'", str(exc))
    else:
        raise Failure("expected an unknown projection to fail")

    unpinned = """
extern fn cycles(lmul: INT, sew: INT) -> INT : cycles;
machine m {
    resource P : units = 1;
    write W;
    bind W -> [P], latency = cycles(1, 24);
}
"""
    try:
        parser.expand_source(unpinned)
    except family_mod.ExpandError as exc:
        assert_in("no pinned value for cycles", str(exc))
    else:
        raise Failure("expected an unpinned projection value to fail")


@test
def family_expansions_match_the_golden_manifest():
    """The v1.2 expansion of every family source is pinned byte-for-byte (AC2).

    Phase 1 replaces the text expander; this check is the parity gate.  Model
    entries skip when the sweep cache is absent, like the other cache-gated
    checks.
    """
    report = golden_mod.check()
    assert_true(report.ok, report.message())


@test
def v1_2_parameter_expressions_reproduce_upstream_latencies():
    """A machine parameter and an axis projection evaluate during norm.

    The fixture encodes SiFive7's VCompress formula from the in-tree TD:
    ``cycles = VLEN / SEW`` for LMUL = M1, ``latency = 7 + cycles``,
    ``release = [1, 8 + cycles]``.  The three VLEN instances must reproduce the
    values in the pinned records dump.
    """
    program = parser.parse_file(str(ROOT / "tests/ssdl/sifive7-vcompress-family.ssdl"))
    records = record_ir_mod.RecordSet()
    emit_td.emit(program, records=records, target_include="SSDLStub.td")
    if not has_llvm_source():
        return
    table = expand_mod.expand_obj(
        records.to_json(),
        expand_mod.schema_for(str(paths_mod.require_llvm_src())),
    )
    upstream = {}
    dump = paths_mod.RECORDS
    if dump.is_file():
        recs = records_mod.load(str(dump))
        upstream = {
            vlen: equiv_mod.build(recs, model)
            for vlen, model in (
                (128, "SiFive7VLEN128X100Model"),
                (512, "SiFive7VLEN512Model"),
                (1024, "SiFive7VLEN1024X300Model"),
            )
        }
    for vlen in (128, 512, 1024):
        for sew in (8, 16, 32, 64):
            cycles = vlen // sew
            name = f"sifive7_vlen{vlen}_WR_WriteVCompressV_M1_E{sew}"
            lines = table.body[name]
            assert_true(f"Latency = {7 + cycles}" in lines, f"{name}: {lines}")
            assert_true(f"ReleaseAtCycles = [1, {8 + cycles}]" in lines, f"{name}: {lines}")
            assert_true("AcquireAtCycles = [0, 1]" in lines, f"{name}: {lines}")
            if vlen in upstream:
                expected = upstream[vlen].writes[f"WriteVCompressV_M1_E{sew}"]
                assert_true(
                    expected["latency"] == 7 + cycles
                    and expected["release"] == (1, 8 + cycles)
                    and expected["acquire"] == (0, 1),
                    f"{vlen}/{sew}: upstream {expected}",
                )


@test
def validate_reports_family_coverage_errors():
    """An axis-expanded write without a bind is a validate error, not a surprise."""
    work = Path(tempfile.mkdtemp(prefix="ssdl-cov-"))
    path = work / "cov.ssdl"
    path.write_text(
        """
set A = [x, y] : kind = dimension, name_format = "_{value}";
machine M {
  write W[A];
  bind W[A in [x]] -> [];
}
"""
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools/ssdl/validate.py"), str(path)],
        capture_output=True,
        text=True,
    )
    assert_true(proc.returncode == 1, proc.stderr)
    assert_true("write W_y has no bind" in proc.stderr, proc.stderr)


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
    src = paths_mod.llvm_src()
    table = ROOT / "data/riscv-sched-families.json"
    if src is None or not (src / "lib/Target/RISCV").is_dir() or not table.is_file():
        return
    text = family_tables_mod.render(
        family_tables_mod.build(src / "lib/Target/RISCV", family_tables_mod.llvm_commit(src))
    )
    assert_true(
        text == table.read_text(),
        "family table is stale; regenerate with tools/ssdl/family_tables.py",
    )


@test
def family_expansion_preserves_scheduling_records():
    """Expand the compressed mini family and compare its records exactly."""
    source = (ROOT / "tests/ssdl/family-mini.ssdl").read_text(encoding="utf-8")
    flat = family_mod.expand(source)
    assert_in("write W_A;", flat)
    assert_in("write W_C;", flat)
    assert_in("bind W_A -> [P], latency = 4, uops = 1;", flat)
    text, skips = emit_td.emit(
        parser.parse(flat), strict=True, target_include="SSDLStub.td"
    )
    assert_true(skips == [], f"strict emission skipped constructs: {skips}")
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "family.td"
        generated.write_text(text, encoding="utf-8")
        diff = compare_mod.compare(
            str(generated),
            str(ROOT / "tests/ssdl/family-mini.expected.td"),
            compare_mod.default_tblgen(),
            tblgen_include_dirs(),
        )
    assert_true(diff.ok(), "record mismatch:\n" + diff.render())


@test
def family_coverage_rejects_unbound_members():
    """A bind site that covers more members than the write must be diagnosed."""
    source = """set D = [A, B, C] : kind = dimension;

machine f(n: INT) {
    write W[D in [A, B]];
    bind  W[D] -> [P], latency = n;
}

machine m : f(4) {
    resource P : units = 1;
}
"""
    origins: list[tuple[str, str, int]] = []
    flat = family_mod.expand(source, origins=origins)
    errors = family_mod.check_coverage(flat, origins)
    assert_true(
        any("bind W_C has no write" in error for error in errors),
        f"diagnostics: {errors}",
    )
    assert_true(
        any("(line 5)" in error for error in errors),
        f"diagnostics lost the source line: {errors}",
    )


@test
def family_add_axis_value_reports_every_uncovered_site():
    """Adding M16 must name every site that does not cover the new member."""
    source = """set D = [A, B] : kind = dimension;

machine f(n: INT) {
    write W[D];
    bind  W[D in [A]] -> [P], latency = n;
}

machine m : f(4) {
    resource P : units = 1;
}
"""
    origins: list[tuple[str, str, int]] = []
    flat = family_mod.expand(source, add_axis=("D", "C"), origins=origins)
    errors = family_mod.check_coverage(flat, origins)
    assert_true(any("write W_B has no bind" in e for e in errors), f"diagnostics: {errors}")
    assert_true(any("write W_C has no bind" in e for e in errors), f"diagnostics: {errors}")
    assert_true(all("(line 4)" in e for e in errors), f"diagnostics: {errors}")


@test
def sifive7_vlde_family_matches_upstream_values():
    """A real SiFive7 sub-family must expand to the upstream record values.

    The compressed source writes each sparse group once (MF8/MF4/MF2, M1, ...,
    M8/WorstCase); the expanded records are compared field by field against the
    pinned ``-print-records`` dump, so this is not a re-statement of the
    generator's own output.
    """
    upstream_path = paths_mod.RECORDS
    if not upstream_path.is_file():
        return
    source = (ROOT / "tests/ssdl/sifive7-vlde.ssdl").read_text(encoding="utf-8")
    flat = family_mod.expand(source)
    text, skips = emit_td.emit(
        parser.parse(flat), strict=True, target_include="SSDLStub.td"
    )
    assert_true(skips == [], f"strict emission skipped constructs: {skips}")
    with tempfile.TemporaryDirectory() as tmp:
        generated = dump_records(text, tmp, "vlde", tblgen_include_dirs())
    upstream = records_mod.load(str(upstream_path))

    def by_write_type(recs, model: str) -> dict[str, str]:
        found = {}
        for name in recs.scoped_to(model):
            if recs.cls.get(name) != "WriteRes" or not census_mod.explicit(recs, name):
                continue
            write = recs.value(name, "SchedWrite WriteType")
            if write and write.startswith("WriteVLDE"):
                found[write] = name
        return found

    expected = by_write_type(upstream, "SiFive7VLEN512Model")
    got = by_write_type(generated, "vldeModel")
    assert_true(
        set(got) == set(expected),
        f"write sets differ: missing {sorted(set(expected) - set(got))}, "
        f"extra {sorted(set(got) - set(expected))}",
    )
    fields = [
        ("int Latency", "Latency"),
        ("int NumMicroOps", "NumMicroOps"),
        ("list<int> ReleaseAtCycles", "ReleaseAtCycles"),
        ("list<int> AcquireAtCycles", "AcquireAtCycles"),
        ("list<ProcResourceKind> ProcResources", "ProcResources"),
    ]
    for write in sorted(expected):
        for field, label in fields:
            want = upstream.value(expected[write], field)
            have = generated.value(got[write], field)
            assert_true(
                have == want,
                f"{write}.{label}: generated {have!r}, upstream {want!r}",
            )


@test
def family_name_format_makes_multi_axis_names_enumerable():
    """NF/EEW/LMUL must reproduce upstream names without free-form concatenation.

    WriteVLSEG3e16_M1 is the shape SiFive7 uses today; the axis formats carry
    the literal separators so the expansion stays a finite, checkable product.
    """
    source = """set NF = [3, 4] : kind = dimension, name_format = "{value}";
set EEW = [8, 16] : kind = dimension, name_format = "e{value}";
set LMUL = [M1, M2] : kind = dimension, name_format = "_{value}";

machine f {
    resource P : units = 1;
    write W[NF, EEW, LMUL];
    bind  W[NF, EEW, LMUL] -> [P];
}
"""
    flat = family_mod.expand(source)
    for name in ("W3e8_M1", "W3e16_M1", "W4e8_M2", "W4e16_M2"):
        assert_in(f"write {name};", flat)
        assert_in(f"bind {name} -> [P];", flat)
    assert_true(flat.count("write W") == 8, f"expected 8 products:\n{flat}")

    worst_source = """set A = [X, Y] : kind = dimension;

machine f {
    resource P : units = 1;
    write W[A];
    write W[worst(A)];
    bind  W[A] -> [P];
    bind  W[worst(A)] -> [P];
}
"""
    worst_flat = family_mod.expand(worst_source)
    assert_in("write W_WorstCase;", worst_flat)
    assert_in("bind W_WorstCase -> [P];", worst_flat)
    assert_true("W_WorstCase_X" not in worst_flat, "worst expanded into the product")



def expect_expand_error(source: str, needle: str) -> None:
    try:
        family_mod.expand(source)
    except family_mod.ExpandError as exc:
        assert_in(needle, str(exc))
        return
    raise Failure(f"expected ExpandError for:\n{source}")


@test
def family_units_reproduce_the_observed_latency_shapes():
    """`units` turns a member into the integer the upstream formula needs.

    The two vectors are the two most common shapes in the SiFive7 record
    table; here they are written as formulas instead of 16 literals.
    """
    source = """set LMUL = [MF8, MF4, MF2, M1, M2, M4, M8] :
    kind = dimension, name_format = "_{value}", units = [1, 2, 4, 8, 16, 32, 64];

machine m {
    resource P : units = 1;
    write A[LMUL];
    write B[LMUL];
    write C[LMUL];
    bind  A[LMUL] -> [P], latency = 3 + 2 * LMUL;
    bind  B[LMUL] -> [P], latency = max(1, LMUL // 4);
    bind  C[LMUL] -> [P], latency = cdiv(512 * LMUL, 4 * 64), release = [1, 1 + 2 * LMUL];
}
"""
    flat = family_mod.expand(source)
    rows = [
        ("MF8", 5, 1, 2, 3),
        ("MF4", 7, 1, 4, 5),
        ("MF2", 11, 1, 8, 9),
        ("M1", 19, 2, 16, 17),
        ("M2", 35, 4, 32, 33),
        ("M4", 67, 8, 64, 65),
        ("M8", 131, 16, 128, 129),
    ]
    for member, a, b, c, release in rows:
        assert_in(f"bind A_{member} -> [P], latency = {a};", flat)
        assert_in(f"bind B_{member} -> [P], latency = {b};", flat)
        assert_in(
            f"bind C_{member} -> [P], latency = {c}, release = [1, {release}];", flat
        )


@test
def family_worst_member_takes_the_widest_units():
    """`worst(AXIS)` names `_WorstCase` and reuses the last member's number."""
    source = """set LMUL = [MF8, M8] :
    kind = dimension, name_format = "_{value}", units = [1, 64];

machine m {
    resource P : units = 1;
    write W[LMUL];
    write W[worst(LMUL)];
    bind  W[LMUL] -> [P], latency = LMUL;
    bind  W[worst(LMUL)] -> [P], latency = LMUL;
}
"""
    flat = family_mod.expand(source)
    assert_in("bind W_M8 -> [P], latency = 64;", flat)
    assert_in("bind W_WorstCase -> [P], latency = 64;", flat)


@test
def family_numeric_members_become_units_without_declaration():
    """A range axis is already numeric; no `units` list is needed."""
    source = """set NF = 2..8 : kind = dimension, name_format = "{value}";

machine m {
    resource P : units = 1;
    write V[NF];
    bind  V[NF] -> [P], latency = NF * 2, release = [1, max(1, NF - 1)];
}
"""
    flat = family_mod.expand(source)
    assert_in("bind V2 -> [P], latency = 4, release = [1, 1];", flat)
    assert_in("bind V8 -> [P], latency = 16, release = [1, 7];", flat)


@test
def family_numeric_axis_without_units_is_rejected():
    """A naming-only axis cannot silently become a number."""
    source = """set LMUL = [MF8, M1] : kind = dimension, name_format = "_{value}";

machine m {
    resource P : units = 1;
    write W[LMUL];
    bind  W[LMUL] -> [P], latency = LMUL;
}
"""
    expect_expand_error(source, "unknown parameter 'LMUL'")


@test
def family_axis_units_must_match_the_member_count():
    source = """set LMUL = [MF8, M1] :
    kind = dimension, units = [1, 2, 4];

machine m { }
"""
    expect_expand_error(source, "2 members but 3 units")


@test
def family_repeated_axis_in_one_index_is_rejected():
    source = """set A = [X, Y] : kind = dimension;

machine m {
    resource P : units = 1;
    write W[A, A];
    bind  W[A, A] -> [P];
}
"""
    expect_expand_error(source, "axis repeated")


@test
def family_colliding_expansions_are_rejected():
    """Two statements must not mint the same record name."""
    source = """set A = [X] : kind = dimension;

machine m {
    resource P : units = 1;
    write W[A];
    write W[A];
    bind  W[A] -> [P];
}
"""
    expect_expand_error(source, "expanded twice")


@test
def family_expression_kernel_stays_bounded():
    source = """set A = [X] : kind = dimension;

machine m {
    resource P : units = 1;
    write W[A];
    bind  W[A] -> [P], latency = 2 ** 8;
}
"""
    expect_expand_error(source, "unsupported syntax")


@test
def family_catalogue_explains_every_segmented_family():
    """The catalogued SEG formulas must cover all nine templates exactly.

    Nine segmented templates, 224 members each (NF x EEW x LMUL), are 2016 of
    the 3772 explicit records.  If a formula and the records disagree, this
    test names the member instead of leaving a silent residue.
    """
    upstream_path = paths_mod.RECORDS
    if not upstream_path.is_file():
        return
    recs = records_mod.load(str(upstream_path))
    bases, _total = family_mine_mod.collect(recs, "SiFive7VLEN512Model", family_mine_mod.DEFAULT_LMUL)
    catalogue = json.loads((ROOT / "data/riscv-sched-formulas.json").read_text())["formulas"]
    seg = {name: base for name, base in bases.items() if "SEG" in name}
    assert_true(len(seg) == 9, f"expected 9 segmented templates, got {sorted(seg)}")
    explained = 0
    for name, base in seg.items():
        covered: set = set()
        for entry in catalogue:
            got, _failures = family_mine_mod.match_formula(base, entry, 512)
            covered.update(got)
        assert_true(
            covered == set(base.members),
            f"{name}: {len(base.members) - len(covered)} members unexplained",
        )
        explained += len(base.members)
    assert_true(explained == 2016, f"segmented records: {explained}")


@test
def family_catalogue_leaves_no_record_unexplained():
    """AC8: every explicit SiFive7VLEN512 record is flat, constant or a formula.

    A residue here means the language is missing a construct, so this test is
    the stop condition from the plan rather than a coverage metric.
    """
    upstream_path = paths_mod.RECORDS
    if not upstream_path.is_file():
        return
    recs = records_mod.load(str(upstream_path))
    catalogue = json.loads((ROOT / "data/riscv-sched-formulas.json").read_text())
    bases, total = family_mine_mod.collect(
        recs, "SiFive7VLEN512Model", family_mine_mod.DEFAULT_LMUL, catalogue.get("glued")
    )
    assert_true(total == 3772, f"explicit records: {total}")
    unexplained = []
    explained_records = 0
    for name, base in bases.items():
        covered: set = set()
        for entry in catalogue["formulas"]:
            got, _failures = family_mine_mod.match_formula(base, entry, 512)
            covered.update(got)
        constant = len({payload.as_tuple() for payload in base.members.values()}) == 1
        if not covered and not constant and base.axes:
            unexplained.append(name)
        explained_records += len(base.members) if (covered or constant or not base.axes) else 0
    assert_true(unexplained == [], f"unexplained bases: {unexplained}")
    assert_true(explained_records == 3772, f"explained records: {explained_records}")

@test
def family_variant_is_one_statement_per_member():
    """``variant W[D]`` expands to one subject per member, not a template."""
    flat = family_mod.expand(
        """
set D = [A, B] : kind = dimension, name_format = "_{value}", units = [1, 2];
machine m {
    write W[D];
    bind W[D] -> [P], latency = 1;
    variant W[D] {
        case opcode([X]): [W];
        default: [W];
    }
}
"""
    )
    assert_in("variant W_A {", flat)
    assert_in("variant W_B {", flat)
    assert_true("variant W[" not in flat, flat)


@test
def family_read_keeps_members_and_worst_case():
    """A read axis with a worst case expands to members plus ``_WorstCase``."""
    flat = family_mod.expand(
        """
set LMUL = [MF8, M1] : kind = dimension, name_format = "_{value}", units = [1, 8];
machine m {
    read R[LMUL];
    advance R[LMUL] : cycles = 1;
    read R[worst(LMUL)];
    advance R[worst(LMUL)] : cycles = 2;
}
"""
    )
    for name in ("read R_MF8;", "read R_M1;", "read R_WorstCase;"):
        assert_in(name, flat)
    assert_in("advance R_WorstCase : cycles = 2;", flat)


@test
def family_sweep_covers_every_riscv_model_with_a_processor():
    """The sweep must see all 14 scheduling models, not just the four pilots."""
    upstream_path = paths_mod.RECORDS
    if not upstream_path.is_file():
        return
    models = family_sweep_mod.discover(upstream_path, None)
    expected = {
        "Andes45Model",
        "GenericOOOModel",
        "MIPSP8700Model",
        "RocketModel",
        "SiFiveP400Model",
        "SiFiveP500Model",
        "SiFiveP600Model",
        "SiFiveP800Model",
        "SpacemitX100Model",
        "SpacemitX60Model",
        "SyntacoreSCR1Model",
        "SyntacoreSCR7Model",
        "XiangShanKunMingHuModel",
        "XiangShanNanHuModel",
    }
    assert_true({item["model"] for item in models} == expected, f"models: {len(models)}")
    assert_true(all(item["instance"] and item["processor"] for item in models), models)


@test
def family_read_axis_keeps_the_real_sifive7_members():
    """The shape Step A fixed: ReadVWSLLV is 7 real members plus WorstCase."""
    upstream_path = paths_mod.RECORDS
    if not upstream_path.is_file():
        return
    recs = records_mod.load(str(upstream_path))
    bases = family_mine_mod.collect_reads(recs, "SiFive7VLEN512Model", None)
    base = bases["ReadVWSLLV_{{LMUL}}"]
    assert_true(len(base["members"]) == 8, f"members: {len(base['members'])}")
    assert_true(
        set(base["axes"]["LMUL"]) == {"MF8", "MF4", "MF2", "M1", "M2", "M4", "M8", "WorstCase"},
        f"LMUL: {base['axes']['LMUL']}",
    )


@test
def family_map_survives_expansion_unchanged():
    """``map`` carries instruction names, which are not axis-indexed."""
    flat = family_mod.expand(
        """
machine m {
    write W_A;
    read R_A;
    map [ADD, SUB] -> [W_A, R_A] : unsupported = true;
}
"""
    )
    assert_in("map [ADD, SUB] -> [W_A, R_A] : unsupported = true;", flat)


@test
def profiles_name_every_instance_the_miner_generates():
    """The four verified models each have a profile with all launch fields."""
    table = ROOT / "data/riscv-sched-profiles.json"
    data = json.loads(table.read_text())
    profiles = data["profiles"]
    expected = {
        "sifive7-512": ("SiFive7VLEN512Model", 512, "sifive_7_series", "SIFIVE_7"),
        "sifive7-128x100": (
            "SiFive7VLEN128X100Model",
            128,
            "sifive_x160",
            "SIFIVE_X160",
        ),
        "sifive7-1024x300": (
            "SiFive7VLEN1024X300Model",
            1024,
            "sifive_x390",
            "SIFIVE_X390",
        ),
        "andes45": ("Andes45Model", 128, "andes_a45", "ANDES_A45"),
    }
    for name, (model, vlen, instance, processor) in expected.items():
        profile = profiles[name]
        assert_true(profile["model"] == model, f"{name}: {profile}")
        assert_true(profile["vlen"] == vlen, f"{name}: {profile}")
        assert_true(profile["instance"] == instance, f"{name}: {profile}")
        assert_true(profile["processor"] == processor, f"{name}: {profile}")
        assert_true(profile["lmul"][0] == "MF8" and profile["lmul"][-1] == "M8", name)


V2_FIXTURES = [
    ("family-mini", "tests/ssdl/family-mini.ssdl", "tests/ssdl/v2/family-mini-v2.ssdl"),
    ("multiline", "tests/ssdl/multiline.ssdl", "tests/ssdl/v2/multiline-v2.ssdl"),
    ("two-instances", "tests/ssdl/two-instances.ssdl", "tests/ssdl/v2/two-instances-v2.ssdl"),
    (
        "two-instances-runtime",
        "tests/ssdl/two-instances-runtime.ssdl",
        "tests/ssdl/v2/two-instances-runtime-v2.ssdl",
    ),
    ("itineraries", "tests/ssdl/itineraries.ssdl", "tests/ssdl/v2/itineraries-v2.ssdl"),
    ("entities", "tests/ssdl/entities.ssdl", "tests/ssdl/v2/entities-v2.ssdl"),
    (
        "machine-roles",
        "tests/ssdl/machine-roles.ssdl",
        "tests/ssdl/v2/machine-roles-v2.ssdl",
    ),
    ("variants", "tests/ssdl/variants.ssdl", "tests/ssdl/v2/variants-v2.ssdl"),
    ("predicates", "tests/ssdl/predicates.ssdl", "tests/ssdl/v2/predicates-v2.ssdl"),
    ("map", "tests/ssdl/map.ssdl", "tests/ssdl/v2/map-v2.ssdl"),
    (
        "projection-contract",
        "tests/ssdl/projection-contract.ssdl",
        "tests/ssdl/v2/projection-contract-v2.ssdl",
    ),
]


def v2_error(source: str) -> str:
    try:
        syntax_v2.to_v1(source)
    except syntax_v2.V2Error as exc:
        return str(exc)
    raise Failure(f"expected a V2Error for:\n{source}")


@test
def v2_and_v1_frontends_build_the_same_ir():
    """AC4: the surface changes, the semantic program does not."""
    for label, v1_path, v2_path in V2_FIXTURES:
        v1_text = (ROOT / v1_path).read_text(encoding="utf-8")
        v2_text = (ROOT / v2_path).read_text(encoding="utf-8")
        v1 = elaborate_mod.elaborate(v1_text)
        v2 = elaborate_mod.elaborate(v2_text, syntax="v2")
        assert_true(v1.dimensions == v2.dimensions, f"{label}: dimensions differ")
        symbols = v1.symbols()
        assert_true(symbols == v2.symbols(), f"{label}: symbol lists differ")
        by1, by2 = v1.by_symbol(), v2.by_symbol()
        for symbol in symbols:
            assert_true(
                by1[symbol].spelling == by2[symbol].spelling,
                f"{label}: spelling differs for {symbol.render()}",
            )
            assert_true(
                by1[symbol].phase is by2[symbol].phase,
                f"{label}: phase differs for {symbol.render()}",
            )
        # Global declarations (itineraries) are pass-through text in the IR,
        # so their source spacing is not part of the contract.  Machine
        # statements are, and must be byte-identical.
        assert_true(
            [decl.text for machine in v1.machines for decl in machine.decls]
            == [decl.text for machine in v2.machines for decl in machine.decls],
            f"{label}: rendered machine statements differ",
        )


@test
def v2_rejects_v1_field_separators_and_kind_tails():
    """AC5/AC3: every v1 separator gets a v2 error, not a silent accept."""
    cases = [
        (
            "machine family f { resource P : units = 2; }",
            "v1 field separator",
        ),
        (
            "machine family f { advance R : cycles = 1; }",
            "v1 field separator",
        ),
        (
            "machine family f { bind W -> [P], latency = 1; }",
            "expected '{' to open a field body",
        ),
        (
            "set D = [A] : kind = dimension;",
            "v2 requires a set kind",
        ),
        (
            "set dimension D = [A] : kind = dimension;",
            "v1 set property separator",
        ),
    ]
    for source, needle in cases:
        message = v2_error(source)
        assert_in(needle, message)


@test
def v2_map_operand_positions_are_explicit():
    """AC7: the operand position is written, not implied by list order."""
    flat = syntax_v2.to_v1(
        "machine family f { write W; read R; map [ADD] { 1 = R; 0 = W; } }"
    )
    assert_in("map [ADD] -> [W, R];", flat)

    v1_source = """
machine m {
    write W;
    read R;
    map [ADD, SUB] -> [W, R];
    map [MUL] -> [W] : unsupported = true;
}
"""
    v2_source = """
machine family m {
    write W;
    read R;
    map [ADD, SUB] { 0 = W; 1 = R; }
    map [MUL] { 0 = W; unsupported = true; }
}
"""
    v1 = elaborate_mod.elaborate(v1_source)
    v2 = elaborate_mod.elaborate(v2_source, syntax="v2")
    assert_true(v1.symbols() == v2.symbols(), "map: symbol lists differ")
    assert_true(elaborate_mod.render(v1) == elaborate_mod.render(v2), "map: render differs")

    for source, needle in [
        ("machine family f { map [ADD] { 0 = W; 0 = R; } }", "duplicate operand index 0"),
        ("machine family f { map [ADD] { 0 = W; 2 = R; } }", "contiguous from 0"),
        ("machine family f { map [ADD] { x = W; } }", "operand index must be an integer"),
    ]:
        assert_in(needle, v2_error(source))


@test
def v2_machine_roles_split_model_and_processor():
    """AC6: ``machine model`` and ``machine cpu`` emit one record each."""
    source = (ROOT / "tests/ssdl/v2/machine-roles-v2.ssdl").read_text(
        encoding="utf-8"
    )
    flat = elaborate_mod.render(elaborate_mod.elaborate(source, syntax="v2"))
    text, skips = emit_source(flat, strict=True)
    assert_true(skips == [], f"skips: {skips}")
    assert_in("def roles_modelModel : SchedMachineModel", text)
    assert_in('def roles_cpu : RISCVProcessorModel<"roles-cpu", roles_modelModel', text)
    assert_true(
        "def roles_cpuModel : SchedMachineModel" not in text,
        "a cpu must not emit a SchedMachineModel",
    )
    assert_true(
        "def roles_model : RISCV" not in text,
        "a model must not emit a ProcessorModel",
    )

    negatives = [
        (
            "machine model M { features = [F]; }",
            "features belongs on a cpu, not a model",
        ),
        ("machine cpu C { }", "a cpu must name its model"),
        ("machine cpu C { model = Missing; }", "'Missing' is not a declared machine"),
        ("machine model M { model = N; }", "'model =' is only allowed on a cpu"),
        (
            "machine model M { resource P { units = 1; } }\n"
            "machine cpu C { model = M; resource Q { units = 1; } }",
            "a cpu cannot declare resource",
        ),
        (
            "machine model M { resource P { units = 1; } }\n"
            "machine cpu C { model = M; }\n"
            "itineraries { sched_model = C; func_units = [FU]; }",
            "is a cpu, not a model",
        ),
    ]
    for src, needle in negatives:
        try:
            emit_source(syntax_v2.to_v1(src), strict=True)
        except SSDLEmitError as exc:
            assert_in(needle, str(exc))
        else:
            raise Failure(f"expected an emit error for:\n{src}")


@test
def v2_entity_fields_are_checked():
    """Each entity accepts exactly its field table, with no duplicates."""
    for source, needle in [
        (
            "machine family m { resource P { unit = 2; } }",
            "resource P: unknown field 'unit'",
        ),
        (
            "machine family m { write W; bind W { resources = [P]; latency = 1; latency = 2; } }",
            "bind W: repeated field 'latency'",
        ),
        (
            "machine family m { reg_file G { num_phys = 8; cost = [1]; } }",
            "reg_file G: unknown field 'cost'",
        ),
        (
            "machine family m { retire_unit { buffer = 8; max_per_cycle = 2; cycles = 1; } }",
            "retire_unit: unknown field 'cycles'",
        ),
    ]:
        assert_in(needle, v2_error(source))


@test
def v2_stage_fields_are_named_and_checked():
    """A stage is a field body; index/units are required, typos are rejected."""
    for source, needle in [
        (
            "machine family m { itineraries { itin I { stages = ["
            "stage { index = 1; index = 2; units = [F]; }]; } } }",
            "repeated field 'index'",
        ),
        (
            "machine family m { itineraries { itin I { stages = ["
            "stage { index = 1; units = [F]; foo = 2; }]; } } }",
            "unknown field 'foo'",
        ),
        (
            "machine family m { itineraries { itin I { stages = ["
            "stage { index = 1; units = [F]; kind = reserved; }]; } } }",
            "kind requires latency",
        ),
        (
            "machine family m { itineraries { itin I { stages = ["
            "stage { index = 1; }]; } } }",
            "index and units are required",
        ),
    ]:
        assert_in(needle, v2_error(source))


@test
def v2_floor_division_is_not_a_comment():
    """v2 comments are '#' and '/* */'; '//' is the floor-division operator."""
    source = """
machine family f(vlen: INT) {
    write W;
    bind W { resources = [P]; latency = vlen // 128; }
}
machine instance m128 : f(128) {
    resource P { units = 1; }
}
"""
    text = elaborate_mod.render(elaborate_mod.elaborate(source, syntax="v2"))
    assert_in("bind W -> [P], latency = 1;", text)


def main() -> int:
    if not has_llvm_source():
        print(
            f"check: set {paths_mod.LLVM_SRC_ENV} to an LLVM checkout; the suite "
            "reads the TD declarations and the RISC-V target sources",
            file=sys.stderr,
        )
        return 2
    if paths_mod.tblgen() is None:
        print(
            "check: llvm-tblgen not found; set "
            f"{paths_mod.LLVM_BUILD_ENV} to an LLVM build directory or put "
            "llvm-tblgen on PATH",
            file=sys.stderr,
        )
        return 2
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
