// Hand-written equivalent of sifive7-mini.ssdl, using the real LLVM
// TableGen classes.  `tools/ssdl/compare.py` compares `llvm-tblgen
// -print-records` output for the defs declared in both files.
//
// This file also pins the 1.1 lowering:
//   set            -> compile-time only (no record)
//   predicate      -> CheckOpcode / FeatureSchedPredicate / Predicate +
//                     AssemblerPredicate
//   sti_decl       -> STIPredicateDecl
//   sti_function   -> DepBreakingClass + STIPredicate (with SchedModel)
//   tii_predicate  -> TIIPredicate<MCOpcodeSwitchStatement<...>>
//   variant        -> SchedWriteVariant / SchedReadVariant + SchedAlias
//   reg_file       -> RegisterFile
//   queues/retire  -> LoadQueue / StoreQueue / RetireControlUnit
//   itineraries    -> FuncUnit / Bypass / InstrItinClass / InstrItinData /
//                     ProcessorItineraries, attached through `let Itineraries`.
include "RISCV.td"

def FeatureSSDL64Bit : SubtargetFeature<"ssdl-64bit", "IsRV64", "true",
                                        "Implements RV64">;

def FeatureStdExtXssdl : RISCVExtension<2, 1, "SSDL base extension">,
                         RISCVExtensionBitmask<0, 8>;

def FeatureStdExtZssdlcsr : RISCVExtension<2, 0, "SSDL CSR instructions">;

def FeatureStdExtZssdlcnt : RISCVExtension<2, 0, "SSDL counters",
                                           [FeatureStdExtZssdlcsr]>,
                            RISCVExtensionBitmask<1, 15>;

def FeatureStdExtZssdlexp : RISCVExperimentalExtension<0, 1,
                                                       "SSDL experimental extension">,
                            RISCVExtensionBitmask<1, 13>;

def TuneSSDLPostRA : RISCVTuneFeature<"use-ssdl-postra-scheduler",
                                      "use-ssdl-postra-scheduler",
                                      "no-use-ssdl-postra-scheduler",
                                      "UsePostRAScheduler", "true",
                                      "Schedule again after register allocation">;

// Top-level itineraries name a machine through `sched_model`; the functional
// unit and bypass lists define their own records.
def FU_TOP : FuncUnit;
def no_bypass_top : Bypass;
def IIC_iTop : InstrItinClass;

def ssdl_itin_rv64Itineraries
    : ProcessorItineraries<
        [FU_TOP], [no_bypass_top],
        [InstrItinData<IIC_iTop,
                       [InstrStage<1, [FU_TOP]>,
                        InstrStage<2, [FU_TOP], 1, Reserved>],
                       [1, 3], [no_bypass_top], 2>]> {
  let PacketizerNamespace = "SSDLTop";
}

// `set ALUInsts = inst(category == "alu")` evaluated to this explicit list.
def IsALU : CheckOpcode<[ADD, SUB, XOR, AND, MUL, DIV]>;

// SCHED-sorted predicates are SchedPredicateBase subclasses.
def HasXssdl : FeatureSchedPredicate<FeatureStdExtXssdl>;

def SelNoXssdl : Predicate<"!Subtarget->hasStdExtXssdl()">,
                 AssemblerPredicate<(none_of FeatureStdExtXssdl),
                                    "SSDL base extension is absent"> {
  let RecomputePerFunction = true;
}

def SSDLPredicateProlog : PredicateProlog<[{
    [[maybe_unused]] const RISCVInstrInfo *TII =
        static_cast<const RISCVInstrInfo*>(SchedModel->getInstrInfo());
}]>;

def isZeroIdiomDecl : STIPredicateDecl<"isZeroIdiom", FalsePred,
                                       true, true, true, []>;
def isDependencyBreakingDecl : STIPredicateDecl<"isDependencyBreaking",
                                                FalsePred, true, true, true,
                                                [isZeroIdiomDecl]>;

def XorX0X0 : DepBreakingClass<
    [XOR],
    CheckAll<[CheckOpcode<[XOR]>,
              CheckRegOperand<1, X0>,
              CheckRegOperand<2, X0>]>,
    [1, 2]>;

def XorSelf : DepBreakingClass<
    [XOR],
    CheckAll<[CheckOpcode<[XOR]>, CheckSameRegOperand<1, 2>]>,
    [1, 2]>;

def ssdl_generic_rv64Model : SchedMachineModel;

def ssdl_generic_rv64 : RISCVProcessorModel<"ssdl-generic-rv64",
                                            ssdl_generic_rv64Model,
                                            [FeatureSSDL64Bit, FeatureStdExtXssdl],
                                            [], "rv64i2p1">;

def W_ALU : SchedWrite;
def W_LOAD : SchedWrite;
def W_MULDIV : SchedWrite;
def R_ALU : SchedRead;

def FU_ALU : FuncUnit;
def no_bypass : Bypass;
def IIC_iALU : InstrItinClass;

def ssdl_sifive_7Itineraries
    : ProcessorItineraries<[FU_ALU], [no_bypass],
        [InstrItinData<IIC_iALU, [InstrStage<1, [FU_ALU]>], [1, 1]>]> {
  let PacketizerNamespace = "SSDL";
}

def ssdl_sifive_7Model : SchedMachineModel {
  let Itineraries = ssdl_sifive_7Itineraries;
  let IssueWidth = 2;
  let MicroOpBufferSize = 0;
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = false;
  let UnsupportedFeatures = [HasStdExtZbkb];
  let EnableIntervals = true;
}

// Everything that carries a SchedModel is scoped to the machine model.
let SchedModel = ssdl_sifive_7Model in {
  def PipeA : ProcResource<1>;
  def PipeB : ProcResource<1>;
  def LSU : ProcResource<1> { let BufferSize = 4; }

  // WriteSequence is itself a SchedWrite; `repeat = 2` becomes the second
  // template argument.
  def W_MULDIV_SEQ : WriteSequence<[W_ALU, W_ALU], 2>;
  def SchedAlias_W_MULDIV_W_MULDIV_SEQ : SchedAlias<W_MULDIV, W_MULDIV_SEQ>;

  def WR_W_ALU : WriteRes<W_ALU, [PipeA]> {
    let Latency = 1;
  }

  def WR_W_LOAD : WriteRes<W_LOAD, [LSU]> {
    let Latency = 3;
    let ReleaseAtCycles = [3];
  }

  def RA_R_ALU : ReadAdvance<R_ALU, 1, [W_ALU]>;

  def IRW_ADD_SUB_XOR : InstRW<[W_ALU, ReadDefault, ReadDefault],
                               (instrs ADD, SUB, XOR)>;
  def IRW_LD : InstRW<[W_LOAD, ReadDefault], (instrs LD)>;
  def IRW_REGEX_MUL : InstRW<[W_ALU, ReadDefault, ReadDefault],
                             (instregex "MUL.*")>;
  def IRW_AND_instrs : InstRW<[W_ALU, ReadDefault], (instrs AND)>;
  def IRW_REGEX_DIV_regex : InstRW<[W_ALU, ReadDefault], (instregex "DIV.*")>;

  // A variant is a new SchedWriteVariant record aliased onto the write it
  // specialises.  MC predicates are promoted through MCSchedPredicate.
  def W_ALUVariant : SchedWriteVariant<[
    SchedVar<MCSchedPredicate<CheckAll<[
      CheckOpcode<[ADD, SUB, XOR, AND, MUL, DIV]>,
      CheckSameRegOperand<1, 2>]>>, [W_ALU]>,
    SchedVar<NoSchedPred, [W_ALU]>]>;
  def SSDLVariantAlias_W_ALU : SchedAlias<W_ALU, W_ALUVariant>;

  def R_ALUVariant : SchedReadVariant<[
    SchedVar<MCSchedPredicate<CheckAll<[
      CheckOpcode<[AND, XOR, ADD, SUB, MUL, DIV]>,
      CheckRegOperand<1, X0>]>>, [R_ALU]>,
    SchedVar<NoSchedPred, [R_ALU]>]>;
  def SSDLVariantAlias_R_ALU : SchedAlias<R_ALU, R_ALUVariant>;

  def GPRFile : RegisterFile<180, [GPR, GPRNoX0], [1, 1], [true, true], 4>;

  def ssdl_sifive_7RetireUnit : RetireControlUnit<320, 6>;
  def LoadQueue_LSU : LoadQueue<LSU>;
  def StoreQueue_LSU : StoreQueue<LSU>;

  // `map_itin [IIC_iALU] -> [...]` is an ItinRW record.
  def ITINRW_IIC_iALU : ItinRW<[W_ALU, ReadDefault], [IIC_iALU]>;
}

def ssdl_sifive_7 : RISCVProcessorModel<"ssdl-sifive-7", ssdl_sifive_7Model,
                                        [FeatureSSDL64Bit, FeatureStdExtXssdl,
                                         FeatureStdExtZssdlcnt],
                                        [TuneSSDLPostRA]>;

def ssdl_sifive_7_alias : ProcessorAlias<"ssdl-sifive-7-alias", "ssdl-sifive-7">;

def ssdl_itin_rv64Model : SchedMachineModel {
  let Itineraries = ssdl_itin_rv64Itineraries;
}

def ssdl_itin_rv64 : RISCVProcessorModel<"ssdl-itin-rv64",
                                         ssdl_itin_rv64Model,
                                         [FeatureSSDL64Bit]>;

// `sched_model` binds the STIPredicate to the machine model, so these records
// follow the model defs.
let SchedModel = ssdl_sifive_7Model in
def SSDLZeroIdiom : STIPredicate<isZeroIdiomDecl, [XorX0X0]>;

let SchedModel = ssdl_sifive_7Model in
def SSDLDepBreaking : STIPredicate<isDependencyBreakingDecl, [XorSelf]>;

def isXorZeroIdiom
    : TIIPredicate<"isXorZeroIdiom",
                   MCOpcodeSwitchStatement<
                     [MCOpcodeSwitchCase<[XOR],
                        MCReturnStatement<CheckAll<[
                          CheckOpcode<[XOR]>,
                          CheckRegOperand<1, X0>]>>>],
                     MCReturnStatement<FalsePred>>>;
