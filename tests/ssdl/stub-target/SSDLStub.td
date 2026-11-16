//===- SSDLStub.td - minimal target for validating SSDL output -*- tblgen -*-==//
//
// This is not a real target.  It exists so `check.py` can feed the generated
// TableGen through `-gen-subtarget` / `-gen-instr-info` without pulling in the
// whole RISC-V instruction set, which no partial machine model can cover.
//
// Two rules make that work:
//   * instructions are declared BEFORE the generated scheduling records, and
//     carry no `SchedRW`; the SSDL `map` statements supply `InstRW` classes;
//   * the class names the emitter uses (`RISCVProcessorModel`,
//     `RISCVTuneProcessorModel`) are shimmed to their target-independent
//     `ProcessorModel` equivalents.
//
//===----------------------------------------------------------------------===//

include "llvm/Target/Target.td"

def SSDLStubInstrInfo : InstrInfo;
def SSDLStub : Target { let InstructionSet = SSDLStubInstrInfo; }

class RISCVProcessorModel<string n, SchedMachineModel m,
                          list<SubtargetFeature> f,
                          list<SubtargetFeature> tunef = []>
    : ProcessorModel<n, m, f, tunef>;

class RISCVTuneProcessorModel<string n, SchedMachineModel m,
                              list<SubtargetFeature> tunef = [],
                              list<SubtargetFeature> f = []>
    : ProcessorModel<n, m, f, tunef>;

// The feature classes below mirror RISCVFeatures.td.  SSDL `kind = extension`
// and `kind = tune` lower to these classes, and the stub must be able to
// compile them just like the real target does.
class RISCVExtension<int major, int minor, string desc,
                     list<SubtargetFeature> implies = [],
                     string fieldname = !subst("Feature", "Has", NAME),
                     string value = "true", bit IsExperimental = false>
    : SubtargetFeature<"", fieldname, value, "", implies> {
  defvar extName = !subst("FeatureVendor", "",
                          !subst("FeatureStdExt", "", NAME));
  let Name = !if(IsExperimental, "experimental-", "") # !tolower(extName);
  let Desc = "'" # extName # "' (" # desc # ")";
  int MajorVersion = major;
  int MinorVersion = minor;
  bit Experimental = false;
}

class RISCVExtensionBitmask<bits<3> groupID, int bitPos> {
  int GroupID = groupID;
  int BitPos = bitPos;
}

class RISCVExperimentalExtension<int major, int minor, string desc,
                                list<RISCVExtension> implies = [],
                                string fieldname = !subst("Feature", "Has", NAME),
                                string value = "true">
    : RISCVExtension<major, minor, desc, implies, fieldname, value, true> {
  let Experimental = true;
}

class RISCVTuneFeature<string name, string pos_directive, string neg_directive,
                       string field_name, string value, string description,
                       list<RISCVTuneFeature> implied_features = [],
                       InlineBehavior ib = InlineIgnore>
    : SubtargetFeature<name, field_name, value, description> {
  let Implies = implied_features;
  let InlineBehavior = ib;
  string PositiveDirectiveName = pos_directive;
  string NegativeDirectiveName = neg_directive;
}

def X0 : Register<"x0"> { let Namespace = "SSDLStub"; }
def GPR : RegisterClass<"SSDLStub", [i64], 64, (add X0)>;
defm : RemapAllTargetPseudoPointerOperands<GPR>;

/// No SchedRW on purpose: only InstRW (SSDL `map`) gives these a sched class.
multiclass StubBinOp<string mnemonic> {
  def NAME : Instruction {
    let Namespace = "SSDLStub";
    let Size = 4;
    let OutOperandList = (outs GPR:$rd);
    let InOperandList = (ins GPR:$rs1, GPR:$rs2);
    let AsmString = mnemonic # " $rd, $rs1, $rs2";
  }
}

defm ADD : StubBinOp<"add">;
defm SUB : StubBinOp<"sub">;
defm MUL : StubBinOp<"mul">;
defm XOR : StubBinOp<"xor">;
