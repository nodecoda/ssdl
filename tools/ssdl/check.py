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
import re
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from ssdl import compare as compare_mod  # noqa: E402
from ssdl import emit_td, parser  # noqa: E402
from ssdl import sets as sets_mod  # noqa: E402
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
    # syntax, so these are the blockers that remain.
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
        "lowering: error: sti_function SiFive7DepBreaking.OrrXzrXzr: "
        "emitted def 'OrrXzrXzr' already exists",
    ]:
        assert_in(expected, text)


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
    assert_in("def ITINRW_IIC_iALU : ItinRW<[W_ALU, ReadDefault], [IIC_iALU]>;", text)

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
        "CheckRegOperand<1, X0>]>>, [W_ALU]>, SchedVar<SchedPred, [W_ALU]>, "
        "SchedVar<NoSchedPred, [W_ALU]>]>",
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
