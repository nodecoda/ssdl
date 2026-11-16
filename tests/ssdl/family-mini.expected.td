include "SSDLStub.td"

def W_A : SchedWrite;
def W_B : SchedWrite;
def W_C : SchedWrite;
def m512Model : SchedMachineModel;

let SchedModel = m512Model in {
  def P : ProcResource<2>;

  def m512_WR_W_A : WriteRes<W_A, [P]> {
    let Latency = 4;
    let NumMicroOps = 1;
  }

  def m512_WR_W_B : WriteRes<W_B, [P]> {
    let Latency = 4;
    let NumMicroOps = 1;
  }

  def m512_WR_W_C : WriteRes<W_C, [P]> {
    let Latency = 4;
    let NumMicroOps = 1;
  }
}

def m512 : RISCVTuneProcessorModel<"m512", m512Model>;
