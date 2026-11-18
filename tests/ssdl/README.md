# SSDL fixtures: three buckets, two layers

SSDL has **one surface syntax** (`v2`) and **one IR text form**.  The fixtures
are split so the directory tells you which layer a file belongs to.

| bucket | layer | how it is read |
|---|---|---|
| `surface/*.ssdl` | v2 surface | default (`--syntax v2`) |
| `*.ssdl` (this directory) | IR text | `--syntax ir` |
| `ir-only/*.ssdl` | IR text with no surface spelling yet | `--syntax ir` |

- **`surface/`** is what a person writes.  Each file has an IR twin in this
  directory with the same base name; `check.py` asserts both produce the same
  `SemProgram` and the same records.
- **Root `*.ssdl`** are the IR twins: the expansion `elaborate.render` prints.
  They are the byte-level comparison side, not an alternative syntax.
- **`ir-only/`** pins constructs that have no v2 spelling (for example a bare
  `return` in a TII hook).  When the surface grows a spelling, the fixture
  moves to `surface/` and the row in `ir-only/README.md` is deleted.

`surface/dim-uses.ssdl` pins the dimension rules that are easiest to regress:
one declaration per axis, subsets at the use site (`SEW in [64]`), and
`worst(<narrow view>)` for a base whose widest member is below the axis
maximum.  A `variant` case selects a record through a view name, never
through `in [...]` — TableGen has no per-case membership condition.

Shared inputs (`attrs.json`, `stub-target/`, `*.expected.td`) live here because
they serve both layers.
