#!/usr/bin/env python3
"""Semantic IR for SSDL 1.3.

The surface source is expanded into these nodes; the elaborator fills identity
and phase.  They are the family-expansion IR, not the emitter input:
``elaborate.render`` turns ``Decl.text`` back into the flat IR image, and
``parser`` + ``emit_td`` consume that AST.  Removing the text seam would mean
making ``family_expand`` produce AST nodes directly; the 2026-11-17 closeout
spike measured that as a separate project, not a bounded change.

Identity is structural: ``Symbol(scope, kind, name, axes)``.  Spelling is a
lowering concern.  Two *model-scoped* identities (resources, resource groups,
and resource-carried payloads such as ``write_seq``) may not share a spelling;
the elaborator checks that.  Target-level vocabulary (``SchedWrite`` /
``SchedRead``) is declared inside machine blocks so the source stays local, but
TableGen hoists it once per target: there, one spelling denotes one vocabulary
entry shared by every model, not two identities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    """When a value exists.

    ``ELAB`` values are known while the source is expanded (an instance
    argument, an axis unit).  ``RUNTIME`` values depend on the instruction or
    the subtarget and therefore cannot enter a structural position.
    """

    ELAB = "elab"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class Dimension:
    """A finite axis: members, their spelling suffix and numeric projection."""

    name: str
    values: tuple[str, ...]
    name_format: str = "_{value}"
    units: tuple[int, ...] | None = None
    projections: tuple[tuple[str, tuple[int, ...]], ...] = ()


@dataclass(frozen=True)
class Symbol:
    """A declaration identity: scope + kind + name (+ axis members)."""

    scope: str
    kind: str
    name: str
    axes: tuple[tuple[str, str], ...] = ()

    def render(self) -> str:
        parts = [self.scope] if self.scope else []
        parts.append(self.kind)
        if self.name:
            parts.append(self.name)
        if self.axes:
            parts.append("[" + ",".join(f"{axis}={value}" for axis, value in self.axes) + "]")
        return "/".join(parts)


@dataclass(frozen=True)
class Value:
    """A value with the phase at which it is known."""

    phase: Phase
    payload: object

    @property
    def is_elab(self) -> bool:
        return self.phase is Phase.ELAB


@dataclass
class Decl:
    """One declaration in the flat program.

    ``text`` is the rendered IR statement (a variant or a nested block may
    span several lines).  ``line`` is the source line when the statement was
    produced by an indexed expansion, else 0.
    """

    symbol: Symbol
    text: str
    line: int = 0
    phase: Phase = Phase.ELAB
    spelling: str = ""


@dataclass
class Machine:
    name: str
    params: tuple[str, ...] = ()
    family: str | None = None
    args: tuple[int, ...] = ()
    decls: list[Decl] = field(default_factory=list)


@dataclass
class SemProgram:
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    machines: list[Machine] = field(default_factory=list)
    globals: list[Decl] = field(default_factory=list)

    def symbols(self) -> list[Symbol]:
        return [decl.symbol for machine in self.machines for decl in machine.decls] + [
            decl.symbol for decl in self.globals
        ]

    def by_symbol(self) -> dict[Symbol, Decl]:
        return {decl.symbol: decl for decl in self.globals} | {
            decl.symbol: decl for machine in self.machines for decl in machine.decls
        }
