// Hand-written equivalent of sifive7-mini.ssdl, using the real LLVM
// TableGen classes.  `tools/ssdl/compare.py` compares `llvm-tblgen
// -print-records` output for the defs declared in both files.
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

def PipeA : ProcResource<1>;
def PipeB : ProcResource<1>;
def LSU : ProcResource<1> { let BufferSize = 4; }

def W_ALU : SchedWrite;
def W_LOAD : SchedWrite;
def R_ALU : SchedRead;

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

def ssdl_generic_rv64Model : SchedMachineModel;

def ssdl_generic_rv64 : RISCVProcessorModel<"ssdl-generic-rv64",
                                            ssdl_generic_rv64Model,
                                            [FeatureSSDL64Bit, FeatureStdExtXssdl],
                                            [], "rv64i2p1">;

def ssdl_sifive_7Model : SchedMachineModel {
  let IssueWidth = 2;
  let MicroOpBufferSize = 0;
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = false;
  let UnsupportedFeatures = [HasStdExtZbkb];
  let EnableIntervals = true;
}

def ssdl_sifive_7 : RISCVProcessorModel<"ssdl-sifive-7", ssdl_sifive_7Model,
                                        [FeatureSSDL64Bit, FeatureStdExtXssdl,
                                         FeatureStdExtZssdlcnt],
                                        [TuneSSDLPostRA]>;

def ssdl_sifive_7_alias : ProcessorAlias<"ssdl-sifive-7-alias", "ssdl-sifive-7">;
