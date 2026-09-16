# Turbo Scan (NTFS MFT fast path) — validation status

Working doc for resuming the Phase F validation gate (see
`NEURAL_STORAGE_MATRIX_PROJECT_ROADMAP.md` and the original plan). Delete
this file once Turbo Scan is fully validated and shipped — it's a session
handoff note, not permanent project documentation.

## Where things stand

Phases A–E of Turbo Scan are implemented and unit-tested (parser, tree
builder, engine selection/fallback, real volume I/O, GUI wiring — all
merged, off by default behind the "Turbo Scan (Experimental)" toggle).
Phase F (real-hardware validation) is in progress, run against the user's
actual C: drive from an elevated terminal.

### Bugs found and fixed so far (all confirmed via `compare_scan_engines.py`
against the real machine, not just unit tests)

1. **Fragmented `$MFT` — FIXED.** `mft_volume.py` assumed the `$MFT` was
   one contiguous span. This machine's `$MFT` has 11 real extents; the old
   code only ever read the first one (~198K of 1.39M real records), so
   entire foundational directories (`C:\Windows`, `C:\Users`) were
   silently missing from every scan. Fixed by parsing record #0's own
   `$DATA` data runs (`mft_parser.decode_data_runs` /
   `get_nonresident_data_runs_bytes`) to get the MFT's true multi-extent
   layout. `RecordSource` in `mft_volume.py` is now extent-aware.

2. **Whole-volume hard-link dedup scope mismatch — FIXED.** Turbo Scan
   reads the whole volume internally and used to decide "which hard-link
   occurrence is primary" globally, across the entire disk. A file
   hard-linked between `C:\Windows\Fonts` and (most likely) WinSxS could
   get zeroed out within a scan that only asked about Fonts, because its
   *other* occurrence — completely invisible to that scan — got picked as
   primary instead. `scanner.py`'s Compatible engine can never make this
   mistake, since it only ever sees what it actually walks.
   Fixed by splitting `mft_scan.build_tree()` (leaves every node at its
   full, undeduped size, returns a new `frn_by_node_id` side channel) from
   a new `mft_scan.finalize_subtree()` (does the actual dedup + rollup,
   called *after* `find_subtree_node()` slices out just the requested
   folder — never on the whole volume).

3. **`alloc_size` under-reporting for ordinary files — FIXED.**
   `scanner.py`'s `_windows_alloc_size` used `GetCompressedFileSizeW`,
   which for a normal (non-compressed, non-sparse) file just echoes the
   logical size back — not rounded to the volume's actual cluster size,
   even though NTFS can only ever allocate whole clusters. Turbo Scan read
   real cluster-rounded allocation straight from the MFT, so the two
   engines disagreed on `alloc_size` for nearly every file. Fixed by
   rounding `GetCompressedFileSizeW`'s result up to the volume's real
   cluster size (`GetDiskFreeSpaceW`, cached per drive so it's one extra
   syscall per volume, not per file).

### Validation results so far

- `C:\Windows\Fonts`: went from 738 → 333 → **2** discrepancies as the
  three bugs above were fixed. The 2 remaining are `alloc_size` only, on
  one tiny (65-byte) `desktop.ini` file almost certainly stored *resident*
  in its own MFT record (Turbo Scan correctly reports its unrounded size;
  the Compatible-engine fix has no cheap way to know a file is resident,
  so it rounds up regardless). Explained, negligible (0.001% of total
  bytes), **accepted as a known limitation, not being chased further.**

- `C:\Windows` (whole tree, ~207K files): **NEW, unresolved, much bigger
  gap** — 277,019 discrepancies, Turbo Scan under-reporting by ~26% of
  total bytes (39.4B vs 52.6B) and ~3,900 files. The vast majority are
  "MISSING FROM TURBO" entirely (not just zeroed/mis-sized), concentrated
  in `SysWOW64`, `.NET`/GAC assemblies, and other heavily-hard-linked
  system areas (WinSxS-adjacent). Root cause not yet diagnosed.

## Next step (not yet run)

Ask the user to run, from an elevated terminal:

```
python diagnose_hardlinks.py C:\ AddressParser.dll CheckNetIsolation.exe winsipolicy.p7b GlobalMonospace.CompositeFont
```

This reuses the existing `diagnose_hardlinks.py` tool (already built) to
dump the full raw parsed record for a few of the specific "MISSING FROM
TURBO" files — every `$FILE_NAME` entry, each one's `parent_frn`/
`namespace`. That tells us whether these records:
(a) never get parsed/found at all,
(b) get parsed fine but their parent_frn doesn't match the real parent's
    FRN (an `$ATTRIBUTE_LIST` resolution gap is the leading suspect, since
    these are exactly the kind of heavily-hard-linked-across-many-folders
    files that would need extension records to hold all their
    `$FILE_NAME` attributes), or
(c) something else entirely.

Each full-volume diagnostic run takes ~6–7 minutes (reading all ~1.5M MFT
records twice). Takes ~400s for `compare_scan_engines.py` too.

## Tooling built for this investigation (all temporary, delete when done)

- `compare_scan_engines.py` — the actual Phase F validation gate script
  (has real unit tests in `tests/test_compare_scan_engines.py` — that
  part is *not* temporary, keep it).
- `diagnose_turbo_scan.py` — dumps extent list, parse/orphan counts, and
  an orphan-classification breakdown (missing parent / parent is a
  reparse point / parent not a directory / cascading).
- `diagnose_hardlinks.py` — dumps full raw parsed-record detail (all
  `$FILE_NAME` entries) for one or more named files.

## Also still deferred (separate from the above, lower priority)

`C:\Users\danet\Documents` is very likely itself a reparse point (OneDrive
Known Folder redirection — this whole project already lives under
`OneDrive\Desktop\Projects`, and Documents/Desktop are commonly redirected
together). `scanner.py`'s Compatible engine has an asymmetry: it excludes
a reparse point only when encountered as a *child* during traversal, but
follows it transparently when it's the scan *root* itself (root handling
just uses `os.path.isdir`). Turbo Scan's `find_subtree_node()` doesn't
currently make that same distinction — it would return whatever node
matches the final path component as-is (a leaf, `is_dir=False`, no
children) if that node happens to be a reparse point. Confirm with
`fsutil reparsepoint query "C:\Users\danet\Documents"` and decide whether
`find_subtree_node` needs a fix so the *final* target node is never
treated as a leaf-only reparse point, even though intermediate/descendant
reparse points still correctly are.
