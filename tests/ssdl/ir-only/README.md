# IR-only fixtures

These sources pin IR spellings that have **no v2 surface form yet**.  They are
deliberately *not* migrated; every other fixture in `tests/ssdl/` has a surface
twin under `tests/ssdl/surface/`, and `to_v2` round trips it.

This directory is IR, not a second grammar: read it with ``--syntax ir``.

| fixture | why it has no surface form |
|---|---|
| `errata.ssdl` | `tii_predicate X = return P;` — v2 spells a TII hook as `switch opcode`, so a bare `return` has no equivalent (see the migration table, gap 1) |

When v2 grows a spelling for one of these constructs, migrate the fixture and
delete the row.  The file is still parsed by the IR front end
(`errata_syntax_parses`), so the construct cannot silently rot.
