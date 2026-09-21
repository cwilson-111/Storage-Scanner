# Neural Storage Matrix: Current Build and Product Roadmap

## Status update

Phases 1-3 below are complete, including NTFS MFT fast scan (Turbo Scan),
except a handful of items explicitly scoped out along the way —
ransomware-style extension tracking, duplicate-count history, an in-app
auto-undo. Phase 4 is partially done: everything buildable without a
purchased certificate is in place; actual code-signing is still blocked on
you obtaining one.

Turbo Scan (item 1 below) went from "not started" to fully built, unit- and
integration-tested, and validated across several real-hardware sessions —
correctness bugs (fragmented `$MFT`, hard-link dedup scope, `alloc_size`
rounding, non-resident `$ATTRIBUTE_LIST`, a chunk-cache perf bug, cloud-
placeholder false positives, a reparse-point-as-root gap), a persistent
cache with NTFS USN Journal incremental refresh, and a progress-reporting
bug caught by a real user report after initial release. It ships off by
default behind a "Turbo Scan (Experimental)" toggle — see the item's own
section below for current status and what's still open.

The app also gained a Linux port during this work (scan, Trash-based
delete, and its own elevated-scan flow — same three-platform pattern as
Windows/macOS), and a "Data Tools" menu (Compress CSV to Parquet, Convert
CSV to Excel), shipped only in a separate Windows "Data build" — see the
CSV-to-Parquet section near the end of this doc.

Two gaps flagged in an earlier pass here are now closed:
- **Naming consistency.** The in-app window title now reads "Storage
  Scanner" (matching the repo, README, and executable name) instead of
  the old "Neural Storage Matrix" — the P1 naming-consistency item below
  is fully resolved.
- **CI gates on tests.** `build.yml` now has a `test` job (`pytest` +
  `pyflakes`, on `windows-latest` since several tests exercise Windows-only
  code paths) that the `build`/release job depends on via `needs: test` —
  a broken test or lint failure now blocks the release, closing out the
  automated-quality-gates item below (P4/#10) as far as CI wiring goes.

See the phase checklists further down for what's done vs. not, item by item.

## Executive assessment

The project is already beyond a basic disk-usage viewer. It combines concurrent scanning, sortable storage analysis, duplicate detection, safe deletion, historical snapshots, growth comparison, capacity forecasting, a custom Tkinter interface, standalone Windows packaging, and automated GitHub releases.

The uncomfortable truth is that feature count alone will not beat mature tools. The winning direction is **faster scans, safer cleanup decisions, clearer visual exploration, and IT-grade automation**. The product should help users decide what is safe and valuable to remove, not merely show large files.

## What has been built

### Core storage engine

- Recursive scanning of drives, folders, and individual files.
- A shared work queue with multiple background worker threads for directory enumeration.
- Cancellation support through thread events.
- Iterative post-order rollup of directory sizes and file counts, avoiding recursion-depth failures on deep folder trees.
- Error tracking for unreadable or protected paths.
- Automatic drive discovery for available Windows drive letters.
- Human-readable file size formatting from bytes through terabytes.

### Main interface

- Dark cyber-terminal visual theme.
- Animated header with scan-line grid, neon text treatment, and radar animation.
- Drive and folder selection.
- Scan and cancel controls.
- Sortable columns for name, size, parent percentage, and file count.
- Lazy population of child rows so large result trees are not rendered all at once.
- Percentage bars and heat coloring to make large consumers stand out.
- Alternating row colors and distinct directory, file, warning, and placeholder treatments.
- Status messages and indeterminate/determinate progress modes.
- Keyboard shortcuts including F5 to rescan and Delete to recycle a selected item.

### File operations

- Open directories and reveal files in Windows Explorer.
- Copy a selected path to the clipboard.
- Send selected items to the Recycle Bin rather than permanently deleting them.
- Update in-memory size totals after deletion.
- Confirmation and error dialogs around destructive actions.

### Analysis tools

- Largest-files view with configurable top 25, 50, or 100 results.
- File-type aggregation by extension with total size, percentage, and file count.
- Duplicate detection using a staged pipeline:
  1. Group candidates by exact file size.
  2. Hash the first and last portions with BLAKE2b.
  3. Fully hash only candidates that survive the first two filters.
  4. Group confirmed matches and rank groups by potential recoverable space.
- Parallel partial and full hashing.
- Default exclusions for sensitive or low-value Windows/system paths.
- Duplicate-scan statistics for checked, skipped, partially hashed, and fully hashed files.
- Multi-selection deletion from duplicate results.

### Historical intelligence

- SQLite-backed scan history.
- Scan records containing path, total size, drive capacity, file count, folder count, and timestamp.
- Folder snapshots with indexes for scan and path lookup.
- Comparison with the previous scan of the same normalized path.
- Folder growth in bytes and percentage.
- Growing, shrinking, unchanged, and newly observed folder classification.
- Summary metrics for current and previous sizes and file counts.
- Largest growth and shrink identification.
- Estimated days until full based on historical growth.
- Optional history chart generation through Matplotlib.
- Background persistence so the interface remains responsive while history is saved.

### Packaging and delivery

- Custom PNG-to-ICO generation with multiple embedded Windows icon sizes.
- PyInstaller one-file, windowed executable packaging.
- Bundled application icon for executable and Tkinter windows.
- GitHub Actions workflow that builds on pushes and manual runs.
- Build artifact upload for testing.
- Version-tag-triggered GitHub Release creation and EXE attachment.
- Semantic-style version tags such as `v1.1.0`.

## Important defects and technical debt to fix first

### P0: Correct duplicate root insertion

`_finish_scan()` currently inserts and populates the root node twice. Remove the repeated block. This can create duplicate rows, unnecessary UI work, and confusing state.

### P0: Fix persistent database location

`history.py` places `storage_history.db` beside `__file__`. In a PyInstaller one-file build, application resources are extracted to a temporary directory. Store writable user data under `%LOCALAPPDATA%\\NeuralStorageMatrix\\` instead. Add a schema version and migrations.

### P0: Fix `print_growth_report()`

`percent_text` is calculated before `growth_percent` exists and is then reused for every row. Calculate it inside the loop and handle `None` for new folders.

### P0: Stop swallowing icon and runtime errors silently

Several broad `except Exception: pass` blocks hide packaging and UI failures. Log diagnostic details to a rotating file in `%LOCALAPPDATA%` while keeping user-facing messages concise.

### P1: Resolve naming consistency — ✅ done (as "Storage Scanner", not "Neural Storage Matrix")

Use one canonical entry point and product name everywhere. Recommended:

- Source entry point: `storage_scanner.py`
- Executable: `NeuralStorageMatrix.exe` or `StorageScanner.exe`
- Display name: `Neural Storage Matrix`

Update module docstrings, build scripts, workflow commands, README instructions, icon metadata, and release names together.

The project settled on **"Storage Scanner"** as the canonical name instead —
repo, README, executable (`StorageScanner.exe`), and now the in-app window
title all agree. "Neural Storage Matrix" survives only as this document's
own title/filename, a relic of the earlier "cyber terminal" theme this app
no longer has.

### P1: Split the monolith

The main source file is carrying scanning, hashing, Windows shell operations, state, persistence orchestration, and multiple UI windows. Refactor toward:

```text
storage_scanner/
  app.py
  models.py
  scanner.py
  duplicates.py
  history.py
  file_ops.py
  settings.py
  ui/
    main_window.py
    duplicate_window.py
    history_window.py
    visualizations.py
```

This will make testing and performance work much easier.

### P1: Improve scan correctness

Explicitly define handling for:

- Symbolic links, junctions, and mount points.
- Sparse files and compressed files.
- Logical size versus allocated size.
- Hard links, which can otherwise be double-counted.
- Long paths and inaccessible folders.
- Files changing or disappearing during a scan.

## Market-leading product roadmap

### 1. Add an NTFS Master File Table fast path — ✅ done, ships as "Turbo Scan (Experimental)"

Built as originally scoped: two engines — **Turbo Scan** (raw MFT parsing,
NTFS-only, `storage_scanner/mft_*.py`/`turbo_scan.py`) with automatic
fallback to **Compatible Scan** (the existing directory-walking engine) for
network shares, removable media, non-NTFS filesystems, or any Turbo Scan
failure. A persistent on-disk cache plus NTFS USN Journal incremental
refresh (`turbo_cache.py`/`usn_journal.py`) means a repeat scan of an
unchanged volume reaches parity with Compatible's own speed instead of
re-reading the whole MFT every time.

Not built: the "achieved throughput / confidence-completeness indicators"
UI polish from the original scope — the engine and its fallback are surfaced
functionally (status text, automatic fallback with no user action needed)
but there's no dedicated indicator panel.

Off by default behind a "Turbo Scan (Experimental)" toggle (Tools ▸
Settings) pending more real-world mileage before it's recommended broadly —
see the "Status update" section above for what's been validated so far and
what's still open (a small-subtree incremental-scan performance follow-up;
not a correctness issue).

### 2. Build a synchronized treemap and sunburst explorer

Add an interactive treemap where rectangle area represents allocated size and color represents file type, age, growth, or cleanup confidence. Synchronize selection among the treemap, folder tree, and details panel. Add breadcrumb navigation, zoom, hover details, keyboard navigation, and image export.

A second sunburst or radial hierarchy view would differentiate the product visually, but the treemap should come first because it is immediately understandable.

### 3. Create a review-first cleanup system

Do not market automatic deletion as intelligence. Build explainable recommendations with categories such as:

- Safe candidate: old installer already represented by a newer version.
- Review candidate: large, old media file with no recent access.
- Duplicate candidate: exact content match with a clearly identified keeper.
- Protected: operating-system, application, cloud-placeholder, or policy-sensitive content.

Every recommendation should show **why it was flagged**, estimated recoverable space, risk level, dependencies, and proposed action. Default to review queues, Recycle Bin, quarantine, or archive. Never silently delete user content.

**✅ Done: persist recommendations across restarts.** Cleanup Recommendations now shows full cold-start recall: `storage_scanner/cleanup_cache.py` persists the last *computed* set of Protected/Review/Duplicate recommendations to SQLite (`cleanup_cache.db`, same `%LOCALAPPDATA%` convention as `history.py`/`turbo_cache.py`), keyed by scan path, each save fully replacing the previous one. Opening the app fresh and going straight to Tools ▸ Clean Up ▸ Cleanup Recommendations — with zero scans this session — shows the most recently cached run immediately, labeled with when it was computed and for which path, plus a **Rescan** button to refresh it for real. Deleting or archiving a row updates the persisted cache too, so a stale row for an already-removed file doesn't linger into the next cold start. Simpler than originally scoped here: rather than persisting the full per-file metadata needed to recompute recommendations from scratch, it persists the already-computed recommendation rows themselves — smaller, and a more direct match for "show me what I found last time," with Rescan covering the "get a truly fresh answer" case.

### 4. Make duplicate cleanup genuinely safer

Improve duplicate handling with:

- Automatic keeper recommendations based on path, modification time, naming conventions, and protected folders.
- A rule that prevents deletion of every copy in a group.
- Preview support for images and documents where practical.
- Side-by-side metadata comparison.
- Hard-link replacement as an advanced, opt-in space-saving action.
- Ignore rules and saved decisions.
- An undo ledger recording every move, recycle, archive, or hard-link action.

### 5. Turn history into anomaly detection

The existing SQLite foundation can become a major differentiator:

- Multi-point charts instead of only previous-versus-current comparison.
- Folder-level growth velocity and acceleration.
- Confidence bands for capacity forecasts.
- Anomaly alerts for sudden spikes, mass deletions, ransomware-like extension growth, or unexpected duplicate explosions.
- Calendar heatmaps and change timelines.
- Snapshot labels before and after upgrades, migrations, and cleanups.
- Compare any two snapshots, not only the latest two.

Forecasting should require adequate data and display uncertainty. A single straight-line estimate should never be presented as certainty.

### 6. Add enterprise and technician mode

A clear IT-focused edition could support:

- UNC and network-share scanning with credential-safe access.
- Remote inventory through an optional signed agent.
- Scheduled headless scans.
- CLI and PowerShell-friendly JSON output.
- CSV, JSON, HTML, and PDF reports.
- Exit codes for automation.
- Centralized policy files for exclusions and retention rules.
- Machine comparison dashboards.
- Audit logs, role separation, and exportable remediation evidence.

This direction aligns especially well with real help-desk and endpoint-management workflows.

### 7. Improve the everyday experience

Add saved scan profiles, recent locations, global result search, advanced filters, bookmarks, pinned folders, column presets, and session restoration. Support filtering by size, age, extension, owner, path, attributes, and modified/accessed date. Let users save and combine filters.

Add a first-run explanation of safe deletion, permission limitations, cloud placeholders, and Windows-protected locations. The interface should communicate what is happening instead of merely displaying activity.

### 8. Add cloud-awareness without causing hydration

OneDrive and similar placeholders must be distinguished from fully local files. Show logical size, local allocated size, online-only state, and sync state when Windows exposes those attributes. Avoid accidentally downloading online-only content during hashing or preview. Offer safe actions such as “Free up local space” separately from deletion.

### 9a. Ship packaged builds for macOS and Linux, not just Windows

**Not yet built.** The app itself already runs cross-platform — `platform_support.py`'s
`IS_MACOS`/`IS_LINUX` branches cover trash/recycle, elevated scanning, and
drive listing on both — but `build.yml` only ever runs on `windows-latest`,
so the only downloadable artifact anywhere is `StorageScanner.exe`. A macOS
or Linux user who clicks the README's download button gets a Windows PE
binary their OS categorically cannot run: macOS's Gatekeeper refuses it
outright with an explicit "Microsoft Windows applications are not
supported on macOS" dialog; Linux has no equivalent friendly message at
all — depending on the desktop environment, double-clicking it typically
does nothing (no application associated with a foreign PE binary), or
running it from a terminal surfaces a bare kernel `Exec format error`.
Today's README callout (see the "On macOS?" note near the top) papers
over this by warning people before they click, but a warning isn't the
same as a working download.

To actually fix it:

- Add a `macos-latest` job to `build.yml` that runs PyInstaller
  (`--windowed --onedir`, since macOS `.app` bundles don't like
  `--onefile`) to produce `StorageScanner.app`, then wraps it in a `.dmg`
  (`hdiutil create` — stdlib-adjacent, no extra dependency) for release.
  Unsigned/unnotarized, it'll hit Gatekeeper's "unidentified developer"
  prompt (right-click → Open bypasses it) — the same class of trust
  friction Windows SmartScreen already causes today, not a new problem,
  and notarization has the same certificate/Apple Developer Program cost
  blocker as Windows code-signing (see item 9 below).
- Add a `ubuntu-latest` job producing a plain PyInstaller `--onefile`
  binary (or an AppImage for a nicer double-click experience across
  distros without a system Python/Tkinter already present).
- Update the README's download section to link all three artifacts, and
  update the "On macOS?" callout once a real macOS build exists — it
  should point people at a `.dmg`, not just at running from source.
- Extend `make_sbom.py`/checksum generation to cover both new artifacts,
  matching what Windows already gets.

### 9. Treat trust as a product feature

- Code-sign Windows releases.
- Publish checksums for every release.
- Generate an SBOM.
- Pin GitHub Action versions to immutable commit SHAs.
- Run dependency and secret scanning.
- Add reproducible build notes.
- Publish a privacy statement stating that scanning is local unless the user opts into remote features.
- Add crash-report opt-in rather than silent telemetry.

Unsigned executables face reputation warnings. Packaging alone will not solve this. Signing, transparent builds, and a stable release process are the long-term answer.

### 10. Add automated quality gates

Build tests for:

- Rollup accuracy.
- Permission failures.
- Cancellation behavior.
- Symlink, junction, sparse-file, hard-link, and long-path handling.
- Duplicate detection correctness and false-positive prevention.
- Database migration and corruption recovery.
- Deletion safeguards.
- Packaging smoke tests on a clean Windows runner.

Add Ruff, Black, mypy, pytest, coverage thresholds, and a GitHub Actions test job that must pass before release. Create generated test trees so correctness and performance can be benchmarked across versions.

**The GitHub Actions test-job-gating piece is done** — `build.yml` now runs
`pytest`/`pyflakes` in a `test` job that `build` (and therefore the release)
depends on via `needs:`. Ruff/Black/mypy, coverage thresholds, and clean-runner
packaging smoke tests are still not in place.

## Recommended delivery sequence

### Phase 1: Reliability foundation — ✅ done

1. ✅ Remove duplicate root insertion.
2. ✅ Move the database and logs to `%LOCALAPPDATA%` (and macOS's `~/Library/Application Support`).
3. ✅ Fix growth-report bugs and missing-data handling.
4. ✅ Refactor the code into modules (`storage_scanner/` package, 8 mixins under `ui/`).
5. ✅ Add unit tests (335 and counting), and `build.yml` now runs them (plus `pyflakes`) in a `test` job the release build depends on — see Status update above. Packaging smoke tests on a clean runner are still not wired in.
6. ✅ Add structured logging and crash diagnostics (`logging_setup.py`).

### Phase 2: Competitive core — ✅ done

1. ✅ Add treemap visualization.
2. ✅ Add search and advanced filters.
3. ✅ Add allocated-size, hard-link, junction, and cloud-placeholder correctness.
4. ✅ Add MFT fast scan with fallback — built and real-hardware validated as Turbo Scan; see item 1 above.
5. ✅ Add snapshot comparison between arbitrary dates.

### Phase 3: Differentiation — ✅ done (CLI-only for #5)

1. ✅ Build review-first cleanup recommendations (Protected / Review / Duplicate candidates, plus an Archive-to-.zip action added afterward).
2. ✅ Add a protected keeper workflow for duplicate groups (auto keeper pick + reasoning + manual override; the keeper can't be deleted even via select-all).
3. ✅ Add anomaly detection and forecast confidence (regression-based range + confidence level; spike/drop detection).
4. ✅ Add reversible cleanup plans and audit history (every delete/recycle/archive logged; no auto-undo — see Status update above for why).
5. 🚧 Add CLI, scheduling, and export features — CLI mode with JSON/CSV output and exit codes is done; a scheduled-scan helper and GUI export were scoped for this item but not built.

### Phase 4: Distribution and trust — 🚧 partial, blocked on a certificate

1. ❌ Sign the executable and installer — needs a purchased code-signing certificate; not something that can be built without one.
2. 🚧 Produce an installer plus portable ZIP — portable ZIP done; no MSI/installer built.
3. ✅ Publish SHA-256 checksums and an SBOM.
4. ❌ Create polished onboarding, documentation, screenshots, and benchmark results — not started.
5. 🚧 Add an update checker that verifies signatures before installation — the update checker exists (version check + dismissible notice, no auto-download/auto-run), but there's nothing signed yet for it to verify.
6. ❌ Ship packaged macOS (`.dmg`) and Linux builds — not started; see item 9a above. Today, clicking the README's download button on either OS gets you a Windows `.exe` that can't run there at all (Gatekeeper blocks it outright on macOS; Linux has no build or friendly error either).

## Product positioning

Recommended one-line positioning:

> A Windows storage intelligence tool that combines fast analysis, explainable cleanup, historical growth tracking, and reversible remediation for users and IT teams.

The differentiator should not be “another TreeSize clone.” The differentiator should be:

> **See what changed, understand why it matters, and reclaim space without guessing.**

## Success metrics

Track these during development:

- Scan completion time and files processed per second.
- Peak memory during large scans.
- Number and size of unreadable paths.
- Duplicate false-positive rate, which should be zero for confirmed results.
- Percentage of cleanup actions that remain reversible.
- Forecast error over time.
- Crash-free launches and successful release smoke tests.
- Time from scan completion to a confident cleanup decision.

## Definition of a strong next release

A compelling next release would include:

- The critical bug fixes above.
- Persistent app data under `%LOCALAPPDATA%`.
- Interactive treemap synchronized with the tree.
- Search and compound filters.
- Safer duplicate keeper recommendations.
- Arbitrary snapshot comparison and improved history charts.
- Automated tests, signed or checksum-verified release assets, and a portable ZIP.

That release would materially improve reliability, usability, visual clarity, and trust instead of merely adding more menu items.


## Compressing chosen csv files to parquet — ✅ done

Shipped as a "Data Tools" menu (Tools ▸ Data Tools) with two actions:
**Compress CSV to Parquet…** (`storage_scanner/csv_to_parquet.py`, using
`pyarrow` as sketched below) and, added alongside it, **Convert CSV to
Excel (.xlsx)…** (`storage_scanner/csv_to_xlsx.py`, using `openpyxl`).
Both file pickers work on any CSV on disk, not just this app's own
exports. Both packages are optional, lazily-imported runtime dependencies
(same pattern as the existing optional `matplotlib` growth-history charts)
— which is why they ship only in a separate Windows "Data build"
(`.github/workflows/build-data.yml`, tagged `data-v…`, published as a
pre-release), not in the standard `.exe`, since `pyarrow` alone adds well
over 100 MB.

Original note this was built from, kept for reference:

2. Using PyArrow (Fastest & Memory Efficient)If you are dealing with larger datasets and want to bypass the overhead of creating a Pandas DataFrame, you can use pyarrow directly. It is highly optimized for the Apache Arrow format. [1] (https://www.confessionsofadataguy.com/converting-csvs-to-parquets-with-python-and-scala/)pythonimport pyarrow.csv as pv
import pyarrow.parquet as pq

# Read the CSV file into an Arrow Table
table = pv.read_csv('input.csv')

# Write the Table to a Parquet file with specified compression
pq.write_table(table, 'output.parquet', compression='snappy')


# Fix Miscrosoft Wondpws Bug unsupported on Mac bug when downlaoding executable from git. Might have been changed during linux pathing