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

**New work as of 2026-09-16: a persistent cache + USN Journal incremental
refresh — COMPLETE AND FULLY VALIDATED ON REAL HARDWARE.** So a repeat
Turbo Scan doesn't re-read the whole volume's MFT every time. Motivated by
bug #5 below (a cold full-volume scan is still ~64s even after all known
bugs are fixed — inherent to reading the whole MFT, not a bug). Built,
wired into both scan-engine call sites, unit/integration-tested (269
tests), and every item in the Phase 4 real-hardware checklist below has
now passed, including catching and fixing one real AV/EDR incompatibility
(FortiClient blocking a write-access volume handle) before it could ship.
The only thing left is a cosmetic/perf follow-up (small-subtree incremental
scans take longer than expected, likely the cache's own writes generating
USN journal noise on the same volume — not a correctness issue), tracked
in its own section near the end of this doc ("Cache + USN Journal
incremental refresh").

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

4. **Non-resident `$ATTRIBUTE_LIST` silently dropped — FIXED.**
   `diagnose_hardlinks.py` confirmed hypothesis (b) below: `mft_parser.
   _parse_attribute_list` gave up entirely (`return []`) whenever a
   record's own `$ATTRIBUTE_LIST` attribute was itself non-resident —
   exactly the case a heavily hard-linked file hits once it has too many
   `$FILE_NAME`/`$DATA` entries to fit inline. Before the fix,
   `GlobalMonospace.CompositeFont`'s 4 most-linked physical copies showed
   `logical_size: 0, alloc_size: 0` with only 2–3 names each; after, all
   report the correct `26,040`/`28,672` with 8–9 names each. Fixed by
   adding `RecordSource.read_clusters()` (`mft_volume.py`) and having
   `_parse_attribute_list` decode the non-resident list's own data runs
   (`decode_data_runs`, already existed for the `$MFT` itself) and read
   the real bytes off the volume instead of bailing out. See
   `tests/test_mft_parser.py::test_nonresident_attribute_list_merges_a_file_name_from_an_extension_record`
   and `tests/test_mft_volume.py::test_read_clusters_reads_the_right_offset_and_length`.

   Some `parent_frn` values on these records are still very large (e.g.
   `139048638495631558`) — not corruption, just a high sequence number,
   meaning that particular hard-link's parent directory slot has since
   been deleted and reused many times. Each affected record also carries
   at least one normal-looking (sequence 1–2) `parent_frn`, so the file
   should still attach to the tree at its live location with the correct
   size; the stale entries should land in `diagnose_turbo_scan.py`'s
   existing orphan classification rather than cause another miss.

   Rerunning `compare_scan_engines.py C:\Windows` after this fix did
   shrink the gap (277,019 → 243,599 discrepancies), but also surfaced a
   second, much bigger problem — see bug #5.

5. **Out-of-order `record_at()` calls evicting the sequential chunk cache
   — FIXED.** The same `compare_scan_engines.py C:\Windows` rerun showed
   `speedup: 0.0x` — Turbo Scan took 678.0s vs. the Compatible engine's
   28.4s, i.e. Turbo was ~24x *slower*, and most of the remaining 243,599
   discrepancies turned out to be scan-duration artifacts (SRU logs,
   `WinSxS\Temp\PendingDeletes`, ETW trace files — things that
   legitimately changed on a live system during an 11-minute scan),
   not real parsing bugs.

   Root cause, found with a temporary instrumented run
   (`diagnose_attribute_list_perf.py`, deleted after use — see below):
   `read_clusters` (bug #4's new I/O path) was a red herring, only 0.1%
   of total time. The real cost was `RecordSource._load_chunk_containing`:
   93.8% of total time (632.6s), 95,598 chunk loads for what a clean
   sequential pass over ~1.15M records needs only ~282 of. Cause:
   `RecordSource` had one shared chunk cache. Bug #4's fix means a
   non-resident `$ATTRIBUTE_LIST` now actually resolves real entries
   (instead of always `[]`), so `parse_base_record` now calls
   `record_source.record_at(target_record_number)` for extension records
   it never used to fetch at all — and each of those out-of-order lookups
   evicted the *sequential* walk's chunk, so the very next sequential
   `record_at()` call had to reload it right back. One interruption, two
   full 4MB reads. This weakness pre-dated bug #4's fix (the class's own
   docstring called out-of-order access "rare" and treated a fresh reload
   as harmless) but was never actually exercised at scale until non-resident
   attribute lists started resolving to real entries.

   First attempt: gave `RecordSource` a second, independent chunk slot
   (`_aux_chunk_*`) for out-of-order lookups. Made **no real difference**
   on a real rerun (630.9s, still `speedup: 0.0x`) -- classification
   checked "is this already cached in aux?" before deciding sequential-
   vs-out-of-order, so whenever an out-of-order lookup happened to land in
   the region the sequential walk was about to reach next, aux silently
   absorbed it, primary never advanced again, and the walk itself then
   permanently looked "out of order" against a frozen primary chunk (only
   13 primary loads total; aux did everything -- 88,481 loads, 94.3% of
   scan time, measured with a second instrumented run since the first was
   deleted too early).

   Real fix: track the walk's logical position explicitly
   (`_sequential_position`), decoupled from which cache happens to hold
   what, with an unconditional primary-hit fast path checked first so
   nearby out-of-order lookups still get served for free — see the class
   docstring in `mft_volume.py` and
   `tests/test_mft_volume.py::test_walk_advancing_past_an_out_of_order_detour_does_not_reload_primary`.
   Dropped scan time 678.0s → 180.4s (primary loads back to the expected
   ~282; aux 28,610 loads, 89.4% of time).

   Then shrank aux's own chunk size from the primary's 4MB down to a
   dedicated 64KB (`_AUX_READ_CHUNK_BYTES`) -- measured reuse of an
   already-loaded aux chunk was only ~5x on average, so a 4MB window per
   out-of-order lookup was reading ~4096x more data than needed. Dropped
   scan time further to 44.2s (aux time 161.3s → 13.9s) — see
   `tests/test_mft_volume.py::test_aux_chunk_uses_its_own_smaller_size_independent_of_the_primary_chunk`.

   **Confirmed on a real `compare_scan_engines.py C:\Windows` rerun:**
   678.0s → 64.4s (10.6x faster), `speedup: 0.0x` → `0.4x`. Turbo Scan
   still doesn't beat Compatible outright on this specific target -- by
   design it always reads the *whole* volume's MFT, not just the
   requested subtree, an accepted architectural tradeoff, not a bug.

6. **`is_cloud_placeholder` false positives for CompactOS/WIMBoot files —
   FIXED.** After bugs #4/#5, discrepancies on `C:\Windows` plateaued at
   ~243,550 regardless of scan duration (678s/630.9s/64.4s all showed
   basically the same count) — ruling out live-churn-during-a-slow-scan
   as the main cause, contrary to the bug #5 writeup above. Breaking the
   discrepancies down (`diagnose_discrepancy_categories.py`) showed only
   ~42 paths were actually missing/extra; 243,504 of 243,546 (99.98%)
   were field mismatches on paths both engines agreed exist:
   `alloc_size` 87,809, `size` 62,950, `hardlink_dup` 53,841,
   `is_cloud_placeholder` 38,885, `file_count` 19.

   `hardlink_dup`/`size`/`alloc_size` mismatches are mostly
   `compare_scan_engines.py`'s own documented limitation (the two engines
   can legitimately pick a different hard-link occurrence as "primary" on
   a hard-link-heavy target like `C:\Windows`) -- not chased further.

   `is_cloud_placeholder`, 38,885 mismatches, was not explained by that
   and turned out to be a real, previously-undiscovered bug.
   `diagnose_cloud_placeholder_mismatch.py` dumped raw `$STANDARD_
   INFORMATION.FileAttributes` for actual mismatching paths: every one
   showed `0x040020 [ARCHIVE, RECALL_ON_OPEN]` on the raw MFT read vs.
   `0x000020 [ARCHIVE]` live (Compatible's `os.stat()`), on completely
   ordinary files like `C:\Windows\Boot\EFI\kd_02_10df.dll` -- not
   OneDrive/cloud-sync files at all, and critically, **no `REPARSE_POINT`
   bit**. A genuine cloud placeholder (OneDrive Files On-Demand etc.) is
   always implemented as an `IO_REPARSE_TAG_CLOUD` reparse point; these
   are almost certainly CompactOS/WIMBoot-compressed system files
   (`RECALL_ON_OPEN` = "decompress from the WIM on open", unrelated to
   cloud sync), likely explaining why this machine shows it so widely if
   it's running Windows on an OS install that uses Compact OS (e.g. an
   ARM64 device, matching `directml_arm64.dll` seen earlier).

   Fixed by adding `storage_scanner.scanner.is_cloud_placeholder_attrs()`,
   requiring the reparse-point bit alongside the existing recall/offline
   bits, and using it in both engines (`scanner.py`'s two call sites,
   `mft_parser.py`'s `parse_base_record`) instead of the raw bitmask
   check — see `tests/test_alloc_size.py::test_is_cloud_placeholder_attrs_requires_reparse_point`
   and `tests/test_mft_parser.py::test_recall_on_open_without_reparse_point_is_not_a_cloud_placeholder`.
   Not yet reconfirmed against a real `compare_scan_engines.py C:\Windows`
   run — see next step.

## Next step

~~Ask the user to rerun `compare_scan_engines.py C:\Windows` to confirm
bug #6 collapses the `is_cloud_placeholder` mismatch count~~ —
**superseded, effectively confirmed.** The later Phase 4 cache-validation
runs (below) happened after bug #6 landed and explicitly note their
204,65x discrepancy count "matches the post-bug-6 baseline" — no separate
rerun needed.

7. **Reparse-point-as-scan-root gap — FIXED, 2026-09-17.** The theory
   behind this fix (see "Also still deferred" below) turned out to be
   wrong on this machine — `fsutil reparsepoint query
   "C:\Users\danet\Documents"` returned `Error 4390: The file or
   directory is not a reparse point`, so Documents was never actually
   the trigger case (the earlier failure investigated under that theory
   was bug #1's fragmentation bug, unrelated). Fixed anyway, since the
   underlying architectural gap is real and reachable by any junction/
   symlink a user might scan directly (OneDrive-redirected folders,
   `mklink /J`, WSL distro mount points, etc.), not just Documents.

   `mft_scan.build_tree()` always left a reparse point as an unexpanded
   leaf, correct for one encountered as a *child* but not for the node
   `find_subtree_node()` actually resolved to — `scanner.py`'s Compatible
   engine follows a reparse point transparently when it's the scan root
   (`os.path.isdir()`), only excluding it as a child during traversal.
   Fixed by adding `mft_scan.reroot_if_reparse_point(node, target_path,
   records, frn_by_node_id)`: if the resolved node is a reparse point
   pointing at a real directory, it re-runs `build_tree()` rooted at that
   node's own record number (reusing `build_tree`'s existing
   `root_record_number` parameter, already general enough for this) and
   returns the new expanded root instead. Any reparse point *inside* the
   newly rebuilt subtree is still correctly left as a leaf — only the one
   outermost node gets root treatment. Wired into both call sites between
   `find_subtree_node()` and `finalize_subtree()`: `turbo_scan.
   _run_turbo_in_process()` and `mft_scan_cli.run_mft_scan()`.

   Tested at the unit level (`tests/test_mft_scan.py`, 4 new tests,
   mutation-verified against a reverted fix) and the integration level
   (`tests/test_turbo_scan_integration.py::
   test_scanning_a_reparse_point_directly_still_reveals_its_contents`,
   real hand-built MFT bytes through the full `scan_with_best_engine`
   pipeline, cache included). Full suite: 274 passed (up from 269), same
   8 pre-existing unrelated failures throughout this whole session.

What's actually left, in priority order:

1. **Not urgent, not correctness:** the small-subtree incremental-scan
   performance follow-up (see "Known follow-up" below).
2. Otherwise, Turbo Scan's correctness + the cache/incremental-refresh
   feature are both confirmed on real hardware. Worth a product decision
   on whether/when it graduates from "Experimental, opt-in, off by
   default" toward being recommended more broadly.

Each full-volume diagnostic run takes ~6–7 minutes (reading all ~1.5M MFT
records twice). `compare_scan_engines.py` on `C:\Windows` now takes
~60–90s (was ~400–700s before bug #5's fix).

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

~~`C:\Users\danet\Documents` is very likely itself a reparse point~~ —
**wrong, and fixed anyway; see item 7 in "Next step" above.**

## Cache + USN Journal incremental refresh (new, started 2026-09-16)

Even fully fixed, a cold Turbo Scan of `C:\Windows` takes ~64s, because it
reads and parses the *entire* volume's MFT (1.15M records) every single
time, regardless of what subtree was requested or whether anything changed
since the last scan — `find_subtree_node`'s own docstring already explains
why (MFT records aren't grouped by directory, so there's no way to know
which ones matter without reading them all first). Not a bug — an
architectural limitation worth fixing with a persistent cache.

Design (see `C:\Users\cwilson\.claude\plans\noble-jingling-honey.md` for
the full plan this was built from): cache the *flat, whole-volume*
`ParsedRecord` list on disk, keyed by `(volume_serial, record_number)` —
not by packed FRN, since a USN journal entry's FRN goes stale the instant
a record slot is freed and reused. Refresh incrementally via the NTFS USN
Change Journal: treat every touched record number as "dirty," re-run
`mft_parser.parse_base_record` on it, and replace the cached row wholesale
— no hand-interpreting CREATE/DELETE/RENAME_OLD_NAME/RENAME_NEW_NAME
`Reason` flags needed, a fresh parse already re-derives the current
parent+name for every hard link. `build_tree`/`finalize_subtree` keep
running unmodified, in-memory, per request, on whatever the cache says the
whole volume currently looks like.

**Built and fully unit-tested this session:**

- `storage_scanner/turbo_cache.py` — SQLite storage for cached records
  (`turbo_scan_cache.db`, same `%LOCALAPPDATA%\NeuralStorageMatrix\`
  directory and per-call-connection/WAL conventions as `history.py`, but a
  separate DB file). `tests/test_turbo_cache.py`, 9 tests, all passing.
- `storage_scanner/usn_journal.py` — Win32 USN Change Journal reader
  (`FSCTL_QUERY_USN_JOURNAL`/`FSCTL_CREATE_USN_JOURNAL`/
  `FSCTL_READ_USN_JOURNAL`), mirroring `mft_volume.py`'s thin-ctypes-
  wrapper style. `tests/test_usn_journal.py`, 10 tests, all passing,
  including real hand-packed `USN_RECORD` V2 buffers (not pre-parsed
  mocks) — same discipline that caught this project's real parsing bugs.
- `storage_scanner/mft_volume.py`: small additive changes so
  `usn_journal.py` can share `RecordSource`'s already-open volume handle —
  `volume_serial`/`record_size` public attributes, a `raw_handle`
  property, and opening with `GENERIC_READ | GENERIC_WRITE` instead of
  read-only (`FSCTL_CREATE_USN_JOURNAL` needs write access). 2 new tests
  in `tests/test_mft_volume.py` cover the new attributes and open flags.

**Phase 3 (wiring) — also done.** `turbo_scan.get_records_using_cache()`
is the new shared entry point both `_run_turbo_in_process` (in-process,
already-elevated) and `mft_scan_cli.run_mft_scan` (the headless spawned
elevated-helper subprocess) now call instead of an unconditional full MFT
walk — see its docstring in `turbo_scan.py` for the exact decision logic
(`_full_scan_and_cache`/`_try_incremental_refresh`). Every cache/journal
failure mode falls back to a full scan, never raises out, never falls all
the way back to the Compatible engine just because caching had a problem.

Caught and fixed one real bug during wiring, before any live testing:
nothing ever called `turbo_cache.init_cache_db()`, so the very first real
scan would have hit `sqlite3.OperationalError: no such table:
cached_volumes` and silently defeated the whole feature on every single
run. Fixed by calling it (cheap, idempotent) at the top of
`get_records_using_cache()` itself, since that function is the one thing
both call sites share — `mft_scan_cli.py`'s subprocess never goes through
`app.py`'s own `init_history_db()`-style startup at all.

Test coverage added: `tests/test_turbo_scan.py` (8 new unit tests on the
cache/incremental decision logic itself, `turbo_cache`/`usn_journal` fully
mocked), `tests/test_mft_scan_cli.py` (5 existing tests updated to mock
`get_records_using_cache` instead of the now-deleted `_read_all_records`/
`parse_base_record` seam), and — most valuable —
`tests/test_turbo_scan_integration.py` gained a real, unmocked, hand-faked
USN journal (`_FakeKernel32` now dispatches on FSCTL code instead of
always answering as `FSCTL_GET_NTFS_VOLUME_DATA`, which would have silently
fed USN journal calls garbage volume-data-shaped bytes) and two new tests:
a same-volume rescan taking the incremental path (fewer `ReadFile` calls
than the first full scan), and a rescan that picks up a real simulated new
file via a hand-packed `USN_RECORD` without re-reading the rest of the
volume. Full suite: 268 passed (up from 259), same 8 pre-existing
unrelated failures throughout this whole session.

**Next step: real elevated-hardware validation (Phase 4)** — this is the
one thing that genuinely cannot be shortened or simulated away, same as
every other real bug this session needed a live machine to find. At
minimum:

1. First run against a real volume with no prior cache: confirm a full
   scan still produces the same tree Turbo Scan produced before caching
   existed, and that `turbo_scan_cache.db` gets created and populated
   under `%LOCALAPPDATA%\NeuralStorageMatrix\`.
2. A second run against the same, unmodified volume: confirm it's
   noticeably faster and produces an identical tree.
3. ~~Make real filesystem changes between two scans~~ — **DONE, CONFIRMED
   CORRECT.** Used a scratch `C:\TurboScanTest` folder (deliberately
   separate from `C:\Windows`) rather than risk anything real. First pass
   used tiny `Out-File`-written files (~22 bytes each) and surfaced 5
   `alloc_size`-only mismatches — not a new bug, the same already-
   documented resident-file limitation from the `C:\Windows\Fonts`
   `desktop.ini` case earlier (files that small store *resident* inside
   their own MFT record with zero cluster allocation; Turbo Scan reports
   the true unrounded size, Compatible always rounds up to a cluster
   since it has no cheap way to detect residency). Redone with larger
   (non-resident) files: clean `MATCH`, 0 discrepancies. Then, in one
   pass: deleted a file, created a new one, renamed another, and changed
   a third's content/size — rescan was another clean `MATCH`, 0
   discrepancies. **The incremental-refresh correctness loop is fully
   confirmed on real hardware**, not just in fake-volume integration
   tests. (Small-subtree incremental scans still took ~10s even for a
   3-file folder — see "Known follow-up" below, not a correctness
   concern.)
4. ~~Confirm `GENERIC_WRITE` doesn't break opening the volume for real~~
   — **it did, on the very first real attempt**, then **confirmed fixed**
   on retry. `RecordSource` opening with `GENERIC_READ | GENERIC_WRITE`
   (added so the shared handle could also serve
   `FSCTL_CREATE_USN_JOURNAL`) got the entire volume-open blocked by
   FortiClient (this user's AV/EDR): every Turbo Scan failed with "Could
   not open '\\\\.\\C:' for raw access," not just journal creation.
   **FIXED:** `RecordSource` is read-only again;
   `usn_journal.ensure_journal()` tries `query_journal()` (read-only)
   first, only falling through to `create_journal()` (same read-only
   handle, fails cleanly with an OS access-denied instead of an AV block)
   if no journal exists yet.

   **Retry, real `C:\Windows`, three consecutive `compare_scan_engines.py`
   runs:** 49.3s (cold, first-ever cache build, `speedup: 0.2x`) → 15.6s
   (incremental, `0.8x`) → 12.7s (incremental, `speedup: 1.0x` — parity
   with the Compatible engine's own 12.8s). Discrepancy count held steady
   at 204,657 / 204,652 / 204,654 across all three (matches the post-bug-6
   baseline; the few-per-run variance is ordinary live churn, not
   incremental-refresh drift) — **the cache + incremental refresh loop is
   confirmed correct and fast on real hardware.**
5. ~~Force `fsutil usn deletejournal` between two scans~~ — **DONE,
   CONFIRMED CLEAN.** On `C:\TurboScanTest`: ran `fsutil usn deletejournal
   /D C:`, then two scans. First scan after deletion: 49.0s — matches the
   original cold-scan baseline exactly, confirming it correctly detected
   the invalidated journal, fell back to a full rescan, and rebuilt the
   cache; still a clean `MATCH`, 0 discrepancies. Second scan: back down
   to 10.6s (this folder's normal incremental time), confirming a new
   journal cursor was re-established and incremental refresh resumed.
   No crash, no wrong tree, exactly the designed degrade-and-recover path.
6. ~~Run once via the in-process path and once via the spawned
   `--mft-scan` elevated-helper subprocess~~ — **DONE, CONFIRMED SHARED.**
   Rather than driving the full GUI through a UAC prompt, invoked the
   exact same entry point directly from the already-elevated terminal:
   `python Storage-Scanner.py --mft-scan C:\ --subtree C:\TurboScanTest
   --output out.json` — the identical command `file_ops.
   run_elevated_scan_windows` builds. Output matched the known-correct
   state (122,758 bytes, 3 files). Confirmed via `cached_volumes`: exactly
   one row for `C:`'s `volume_serial` (no duplicate/separate cache
   created), and `last_refreshed_at` updated to match this run's
   timestamp, later than the prior `full_scan_completed_at` — the
   subprocess path reads and writes the exact same on-disk cache the
   in-process path uses.
7. ~~Time `turbo_cache.load_all_records()` against the full ~1.15M-record
   volume~~ — **implicitly answered.** `turbo_scan_cache.db` is real and
   ~337MB after caching all of `C:`. The fastest observed incremental
   `C:\Windows` run (12.7s total, including `load_all_records()`) already
   matched the Compatible engine's own time, so JSON-deserializing the
   whole cache is not a bottleneck in practice.

## Known follow-up (not urgent, not a correctness issue) — root cause found, partially fixed 2026-09-17

Small-subtree incremental scans (e.g. a 3-file test folder) still took
~10s on real hardware, not the near-instant result you'd expect for "a
handful of dirty records." The doc's earlier leading theory (USN-journal
noise from the cache DB's own writes to itself) was **never confirmed and
turned out to be wrong** — root-caused instead with a synthetic benchmark
at realistic ~1.15M-record scale (no elevated session needed:
`turbo_cache.py` is pure SQLite, zero ctypes/Win32 access, so this is
fully reproducible without real hardware).

Real cause: `turbo_scan.get_records_using_cache()`'s incremental-refresh
path calls `turbo_cache.load_all_records()` *unconditionally* after
applying the dirty records — and that function deserializes the **entire
cached volume**, every time, regardless of how small the actual change or
requested subtree was (`apply_incremental_changes` itself, the part that
actually processes the dirty records, took 4ms for 3 records in the
benchmark; `load_all_records()` took 8.34s for the full ~1.15M-record
volume alongside it). This is inherent to the architecture --
`build_tree()`/`find_subtree_node()` need the whole volume's record set
to walk down to an arbitrary subtree, caching was only ever meant to skip
re-reading/re-parsing raw MFT bytes, not to make a small-subtree request
complete in time proportional to the subtree's own size -- but the ~10s
floor this imposes on every request, however small, was undocumented and
worse than expected.

Breaking down where `load_all_records()`'s ~8.3s (1.15M records) went:
SQLite `SELECT`+`fetchall` transport was only ~0.65s (not the
bottleneck); `json.loads()` + manual dict-to-dataclass reconstruction was
~5.6s. **Fixed the easy part:** switched `cached_records`'s stored row
format from JSON text (`json.dumps(dataclasses.asdict(record))`) to
Python's stdlib `pickle` (`_record_to_blob`/`_record_from_blob` in
`turbo_cache.py`) — no new dependency, small diff, measured ~34% faster
deserialize (5.6s → 3.7s at 1.15M records in the same benchmark). Also
added a one-time migration in `init_cache_db()`: an old JSON-format
`cached_records` table (detected via `PRAGMA table_info` for a stale
`record_json` column) is wiped along with its `cached_volumes` row rather
than left to raise `UnpicklingError` on every future refresh or, worse,
silently leave a `cached_volumes` row pointing at an empty new-format
table — forces exactly one clean full rescan for anyone with a real
pre-existing cache (this user's own ~337MB real `C:` cache included),
same as any other cache miss. New tests:
`tests/test_turbo_cache.py::test_init_cache_db_wipes_a_pre_pickle_json_format_cache`
(mutation-verified), plus the existing round-trip tests continue to pass
against the new pickle format unchanged. Full suite: 275 passed (up from
274), same 8 pre-existing unrelated failures.

**Not fixed, and not attempted:** constructing ~1.15M Python dataclass
instances is itself an unavoidable floor in pure Python regardless of
serialization format (confirmed: a single-blob-pickle-for-the-whole-list
variant was tried in the same benchmark and was no faster than per-row
pickle — object construction, not serialization format, dominates once
JSON is off the table). So a small-subtree incremental scan should now
take roughly ~5-6s instead of ~10s on this machine's real `C:` volume
size, a real but partial win — not yet reconfirmed on real hardware (no
elevated session available in this environment; needs the same real
`C:\TurboScanTest` before/after comparison the original Phase 4 checklist
used). Getting a small-subtree request closer to actually-near-instant
would need a real architectural change: an indexed, targeted load that
only deserializes records relevant to the requested subtree (via
`parent_frn` chain lookup) instead of always materializing the whole
volume — considered, explicitly deferred as a bigger, riskier change than
this session's mandate ("not urgent, not correctness") justified.
