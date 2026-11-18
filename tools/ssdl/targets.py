#!/usr/bin/env python3
"""Target profiles: the target-specific spellings lowering needs.

SSDL's semantic model is target-independent.  Two things are not:

* the TableGen class that carries a processor (RISC-V has its own
  ``RISCVProcessorModel`` with a ``DefaultMarch`` argument; every other target
  uses the four-argument ``ProcessorModel``), and
* the classes that carry RISC-V extension / tune features.

This module is the single place those names live.  Everything else in the
tool chain must go through ``profile()`` instead of spelling a target.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetProfile:
    name: str
    display: str
    td_dir: str
    td_file: str
    processor_class: str
    tune_processor_class: str | None
    extension_class: str | None
    tune_feature_class: str | None
    #: whether ``ProcessorModel`` accepts a trailing ``DefaultMarch`` string
    has_default_march: bool


RISCV = TargetProfile(
    name="riscv",
    display="RISC-V",
    td_dir="RISCV",
    td_file="RISCV.td",
    processor_class="RISCVProcessorModel",
    tune_processor_class="RISCVTuneProcessorModel",
    extension_class="RISCVExtension",
    tune_feature_class="RISCVTuneFeature",
    has_default_march=True,
)

AARCH64 = TargetProfile(
    name="aarch64",
    display="AArch64",
    td_dir="AArch64",
    td_file="AArch64.td",
    processor_class="ProcessorModel",
    tune_processor_class=None,
    extension_class=None,
    tune_feature_class=None,
    has_default_march=False,
)

PROFILES = {profile.name: profile for profile in (RISCV, AARCH64)}
DEFAULT = RISCV


def profile(name: str | None) -> TargetProfile:
    if name is None:
        return DEFAULT
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown target {name!r}; expected one of: {known}") from None
