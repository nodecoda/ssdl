# v1 compatibility fixtures

These sources pin v1 spellings that have **no v2 surface form yet**.  They are
deliberately *not* migrated; every other fixture in `tests/ssdl/` has a v2 twin
under `tests/ssdl/v2/`, and `to_v2` round trips it.

| fixture | why it stays v1 |
|---|---|
| `errata.ssdl` | `tii_predicate X = return P;` — v2 spells a TII hook as `switch opcode`, so a bare `return` has no equivalent (see the migration table, gap 1) |

When v2 grows a spelling for one of these constructs, migrate the fixture and
delete the row.  The file is still parsed by the v1 front end
(`errata_syntax_parses`), so the old syntax cannot silently rot.
