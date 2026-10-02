# Changelog

What changed in each release of Storage Scanner, newest first.

Each section is also the release's notes on GitHub: when a `v*` tag is
pushed, the release job in `.github/workflows/build.yml` publishes the
`## <tag>` section (picked out by `release_notes.py`) as the release
description, and fails if there isn't one. To release, rename
`## Unreleased` to `## vX.Y.Z — date`, commit, and tag that commit.

The sections for v1.04 to v1.11.0 were written afterwards from each tag's
message, copied as written. Where the tag is a lightweight one (no message
of its own), the text is the message of the commit it points at. Tags
before v1.04 (v1.0.0 to v1.0.3) aren't listed.

## Unreleased

### Main tree

- A **Change** column shows how much each folder grew or shrank since the
  previous saved scan of the same path (history keeps folders of 50 MB or
  more), and sorts by it. **Changed folders only**, above the tree, lists
  just the folders that changed (files and unchanged folders hide).
- F5 no longer starts a second scan while the last one's result is still
  arriving.
- Closing while a scan's history is being saved waits for the save, and
  deleting waits for it too.
- When something fails inside the window, a dialog says so and can open
  the log folder (it used to do nothing visible).
- Keys: Enter opens a folder, Backspace/Alt+Up go up, Ctrl+C copies the
  path, Ctrl+F opens Search, Shift+F10 / the Menu key open the context
  menu, which now also opens on macOS. "On Disk" sorts by on-disk size.
- CSV exports add readable `modified` and `accessed` columns, and names
  starting with `=`, `+`, `-` or `@` can no longer run as spreadsheet
  formulas.
- A folder with hundreds of thousands of files opens at once: the first
  1,000 rows show, with a row to show more (250,000 files: 5.4 s → 0.3 s).
- `StorageScanner-portable.zip` is now a folder build that starts about
  2 seconds faster than the single `.exe` (extract it, then run
  `StorageScanner\StorageScanner.exe`); the app also loads less at startup.
- `StorageScanner.exe <folder>` scans that folder straight away, and
  Settings can add "Scan with Storage Scanner" to folders' right-click
  menu (Windows, this user only).
- Ctrl/Shift+click selects several rows: Delete, Add to Cart and Copy
  Path act on all of them (a file inside a selected folder goes with it).
- **Modified** and **Accessed** columns, sortable.
- Largest Files has a right-click menu (reveal, copy path, add to cart,
  delete) and takes several rows; in File Types, double-click a type to
  list its files, largest first, with the same menu. Both close when a new
  scan starts.

### Appearance

- A dark theme: Settings ▸ Appearance follows the system's light or dark
  mode by default, or stays Light or Dark (applies at the next start).
- Sharp on high-DPI Windows displays: the app declares DPI awareness, and
  rows, columns and windows grow with the display scale instead of
  Windows blurring a stretched 100% picture.

### Growth History

- The summary counts every tracked folder (not just the top 50), and
  folders that are gone show up, with their shrink.
- A folder only one scan has is labelled "New / was <50 MB" or
  "Gone / now <50 MB" rather than just "New".
- One save made with the clock years ahead no longer thins the whole
  history.
- A few kilobytes' change to a folder that never changes is no longer an
  "anomaly"; a forecast beyond a century says "more than 100 years".

### Cleanup

- Archiving runs in the background with progress and Cancel instead of
  freezing the window.
- Orphaned-install detection ignores a registry read that comes back
  incomplete, and never flags a folder that holds an installed app.
- Sampled-duplicate warnings show the real sample size.

### Turbo Scan

- The cache compacts itself, forgets drives not seen for 90 days, starts
  over if the file is damaged, and can be cleared from Settings.
- The "USN journal wrapped" check reads the right field.
- The elevated helper never writes its result through a link, and the app
  checks that the result stays inside the folder it asked for.

### Scheduled scans

- Refuses to schedule a copy of the app running from a temporary folder or
  a ZIP, flags tasks that run another copy of the app, escapes `%` for
  cron, and refuses Windows paths Task Scheduler would expand (`%NAME%`).

### Privacy and support

- The update check can be turned off (Settings, or the
  `STORAGE_SCANNER_NO_UPDATE_CHECK` environment variable) and never runs
  from a build that isn't a release.
- The Data build (Tools ▸ Data Tools) now comes from the same tag and the
  same release as the standard build, and gets the same update notice. A
  `data-v1.12.0` copy can't hear about it: download `StorageScanner-Data.exe`
  from this release once. From source, the menu appears once
  `requirements-data.txt` is installed.
- Help ▸ Copy Diagnostic Info (no file or folder names) and Report a
  Problem; issue forms and SECURITY.md.
- The log no longer loses lines when the app and a scheduled scan write at
  once, and records INFO and above by default
  (`STORAGE_SCANNER_LOG_LEVEL=DEBUG` for more).
- The unused growth-chart code and every mention of matplotlib are gone;
  the macOS install steps cover macOS 15.

### Development

- The tests run on Linux and macOS (Python 3.12 and 3.13) as well as
  Windows; Windows-only tests are marked and skipped elsewhere. Running
  from source needs Python 3.11 or later.
- Build and test tools are pinned to exact versions.
- Every build carries a GitHub build provenance attestation
  (`gh attestation verify <file> --repo cwilson-111/Storage-Scanner`), and
  each build's smoke test now also opens and closes the real main window
  (`--selftest-gui`), so a broken Tcl/Tk bundle fails the build.
- The main window's code is split by job (toolbar, scan lifecycle, scan
  banners, tree rows), each file under 500 lines.

## v1.12.0 — 2026-09-29

### Safer deletes

- Every window that deletes now goes through one delete service.
- Windows: before anything is touched, the app checks whether the Recycle
  Bin can take it: `subst`, network and removable drives, a volume whose
  bin is missing or turned off, paths too long for the bin, and items
  bigger than the bin's capacity. If it can't, nothing is deleted unless you
  confirm a permanent delete. Before, Windows deleted such items
  permanently and the app reported them as recycled.
- The Audit Log records what actually happened to each item (recycled,
  deleted permanently, refused, or failed) and shows it. Entries from
  earlier versions are marked "not verified".
- Files and folders whose names end in a dot or a space are refused, with
  a hint to rename them, and left out of duplicate matching. Before,
  deleting one could send a different file, the same name without the dot,
  to the Recycle Bin.
- Drive roots, the scanned folder itself, Windows, Program Files,
  ProgramData, your user profile and its known folders (including ones
  moved to OneDrive), and anything containing them can't be deleted.
  Folders of 10 GB or 50,000 files ask you to type their name first.
- A delete in any window updates the Cleanup Cart, the duplicate results
  and every other open window. A copy is deleted as a duplicate only if
  another copy of its group is still on disk, and an item inside a folder
  that's also in the Cart is counted once.
- A file changed since it was listed (a different size or modified time) is
  refused. A folder from Cleanup Recommendations saved in an earlier
  session needs a rescan before it can be deleted.
- Starting a scan closes the Search, Duplicate Files and Cleanup
  Recommendations windows, and nothing listed from a replaced scan can be
  deleted.

### Turbo Scan

- NTFS-compressed, sparse and Compact OS files show their real On Disk
  size (a compressed file read 16× too big, a 512 GiB sparse disk image
  512 GiB instead of about 3 GiB).
- OneDrive folders are scanned like any folder instead of showing as empty
  links.
- A scan of a junction, symbolic link or mount point uses Compatible Scan,
  which follows it, instead of showing an empty folder.
- Changes made while Turbo Scan reads the whole volume show up in the next
  scan.
- The elevated helper no longer fails at random when the app reads its
  progress (about 1 in 100 progress updates ended the scan and fell back to
  Compatible Scan), and when it does fail, the app log says why.

### Main tree

- Sorting a folder of 35,000 files takes about 0.1 s instead of 13 s or
  more, and deleting a row from it no longer redraws every row.

### Growth History

- A noisy history no longer breaks the forecast: with no upper bound it
  shows "at least N days" (it raised a TypeError before), and the window
  opens only after the forecast and anomalies are worked out.
- The forecast counts down the drive's free space at the path's growth
  rate. It used to compare the path's size with the drive's capacity, so a
  drive with sparse files, like `C:\`, read "already full" with hundreds of
  gigabytes free. Each scan now also saves its on-disk size and the drive's
  used and free space.
- "Remove this scan" (Scans tab) deletes a scan that doesn't belong in the
  history.
- A damaged history file no longer stops the app from starting: it's moved
  aside and a new history started, and you're told where it went. A history
  from a newer version is read but never changed, and the history is copied
  before an upgrade changes its layout.

### Command line

- `--cli` in the Windows download works when typed into cmd.exe or
  PowerShell: it writes to that console instead of failing with nothing
  shown. With no console and no `--output FILE` (Task Scheduler, a
  shortcut), JSON or CSV output exits 1 and says why in the app log, where
  its other messages now go too.
- With only stdout redirected (`--cli X > out.json`), the "Scanned …"
  summary no longer ends up in the file ahead of the JSON.
- CSV written to stdout on Windows no longer ends each row with `\r\r\n`,
  which CSV readers took as an extra empty row.

### Development

- The test suite and the scale benchmarks use a throwaway app-data folder
  and log, and the test run refuses to start if the history database or
  the log would land in the real one.
- Each release page gets its version's section of `CHANGELOG.md` as its
  notes.
- Pull requests are built and tested; only the release job can write to
  the repository; `history.py` is type-checked and counted in coverage; a
  pre-commit config runs ruff and black.

## v1.11.0 — 2026-09-26

- Live per-folder scan numbers in the main tree, TreeSize-style (queued ◌ /
  scanning ⏳ / done, subfolders fill in too; Turbo and elevated scans show
  their current step on the root row)
- Bottom per-folder table replaced by two compact progress lines
- Fixes On Disk for files ≥4 GiB (dropped high DWORD of
  GetCompressedFileSizeW), leaked scan worker threads, the inflated
  'Expecting … 2.1 TB' estimate caused by sparse files (now a file count;
  drive-used fallback uses on-disk bytes), deletes now wait until the scan
  ends, and a _refresh_row column mismatch

Also tagged: `data-v1.11.0`, v1.11.0 plus Data Tools (CSV to Parquet/Excel).

## v1.10.0 — 2026-09-26

- Compact in-memory tree, each folder's files held as packed columns (tree
  memory per file 393.9→117.0 B on the 20k benchmark, 404.0→115.5 B at 1M
  files; peak tree memory 1.11 GB→0.30 GB; Turbo rescan peak 1.19→0.83 GB)
- Exports list a folder's subfolders before its files
- Sampled-duplicate warnings on Delete Selected, Add to Cart and cart
  execution, with sampled items tracked in the Cleanup Cart

Also tagged: `data-v1.10.0`, v1.10.0 plus Data Tools (CSV to Parquet/Excel).

## v1.9.0 — 2026-09-25

- Live scan progress UI, history retention with compact schema, onboarding,
  and scale benchmarking

Also tagged: `data-v1.9.0`, v1.9.0 plus Data Tools (CSV to Parquet/Excel).

## v1.8.0 — 2026-09-24

- Saved history usable at launch without a rescan
- Turbo folder rescans load only that folder (columnar cache, no pickle)
- Scale benchmark suite gated in CI

Also tagged: `data-v1.8.0`, v1.8.0 plus Data Tools (CSV to Parquet/Excel).

## v1.7.1 — 2026-09-24

- Scan details strip shows whether Turbo Scan refreshed its cache or read
  the whole MFT
- Strip no longer clips at the default window width

Also tagged: `data-v1.7.1`, v1.7.1 plus Data Tools (CSV to Parquet/Excel).

## v1.7.0 — 2026-09-24

- Cleanup Cart, orphaned-install recommendations, scan details strip,
  over-budget notifications for scheduled scans, scheduled-scan list

Also tagged: `data-v1.7.0`, v1.7.0 plus Data Tools (CSV to Parquet/Excel).

## v1.6.1 — 2026-09-23

- Ruff/Black/mypy/coverage CI gates, CodeQL + pip-audit + gitleaks
  scanning, Dependabot

Also tagged: `data-v1.6.1`, v1.6.1 plus Data Tools (CSV to Parquet/Excel).

## v1.6.0 — 2026-09-23

- Scheduled scans, GUI export, build smoke tests, Turbo Scan
  restart-as-admin option

Also tagged: `data-v1.6.0`, v1.6.0 plus Data Tools (CSV to Parquet/Excel).

## v1.5.0 — 2026-09-21

- Persist Cleanup Recommendations across restarts, unreadable-paths banner,
  faster duplicate hashing, correctness fixes

## data-v1.4.6 — 2026-09-21

- Data build: CSV to Parquet/Excel

## v1.4.5 — 2026-09-19

No notes: the tag is on a merge commit ("Merge remote-tracking branch
'origin/main'").

## v1.4.4 — 2026-09-19

- Fix hard-link dedup and a compressed-size fallback bug, both Windows-only
  and silently broken

## v1.4.3 — 2026-09-19

No notes: the tag's message is just "v1.4.3".

## v1.4.2 — 2026-09-18

- Speed up duplicate detection, correlate growth anomalies to folders, and
  persist duplicate results across windows

## v1.4.1 — 2026-09-17

- Fix Turbo Scan progress reporting: cross-process relay for the elevated
  helper, and incremental-refresh status/progress that was never wired up
  at all

## v1.4.0 — 2026-09-17

- Fix reparse-point-as-root handling, cache format/perf, live-tree scan UI,
  and Linux port (scan/trash/elevation)

## v1.3.0 — 2026-09-16

Fix three more Turbo Scan bugs and add a persistent cache with USN Journal
incremental refresh

- Fix non-resident  parsing gap that dropped extra
  hard-link names and sometimes zeroed size on heavily hard-linked
  WinSxS/SysWOW64/GAC files
- Fix chunk-cache thrashing in RecordSource that made a full-volume
  scan 24x slower than the directory-walking engine
- Fix is_cloud_placeholder false positives on CompactOS/WIMBoot-
  compressed system files
- Add storage_scanner/turbo_cache.py and usn_journal.py: a persistent
  on-disk cache of parsed MFT records plus NTFS USN Change Journal
  incremental refresh, so repeat scans only reprocess what changed
  instead of re-reading the whole MFT every time
- Wire the cache into both the in-process and elevated-helper-
  subprocess scan paths via a shared get_records_using_cache()
- Validate end-to-end on real hardware: cold scan reaches incremental-
  scan parity with the directory-walking engine; real file create/
  delete/rename/content changes are correctly detected; USN journal
  deletion falls back to a full rescan and recovers cleanly; both scan
  entry points share the same on-disk cache
- Fix a real AV/EDR incompatibility found during that validation:
  opening the volume with write access (needed for USN journal
  creation) was blocked outright by FortiClient; RecordSource now
  stays read-only and only attempts journal creation as a fallback
  when no journal already exists

## v1.2.1 — 2026-09-15

- Fix Turbo Scan: multi-extent  reading, subtree-scoped hard-link dedup, and
  cluster-rounded alloc_size (found via real-hardware validation)

## v1.2.0 — 2026-09-14

- Add Turbo Scan: NTFS MFT-based fast scan engine with fallback to the
  existing directory-walking scanner

## v1.1.6 — 2026-09-13

- Add storage budgets with proactive alerts, and update roadmap doc with
  current status

## v1.1.5 — 2026-09-13

- Add Structural Light theme, archive feature, menu reorganization, and
  release-trust infrastructure

## v1.1.4 — 2026-09-13

- Add search/treemap, duplicate keeper safety, cleanup recommendations,
  growth forecasting, audit log, and CLI mode

## v1.1.3 — 2026-09-13

- Add macOS support, admin elevation, structured logging, and split
  monolith into storage_scanner package

## v1.1.2 — 2026-09-11

- Updated scan method by using less threads, still need to implement NTFS

## v1.1.1 — 2026-09-11

No notes: the tag is on a merge commit ("Merge branch 'main' of
https://github.com/cwilson-111/Storage-Scanner").

## v1.1.0 — 2026-09-11

- Fix automated build

## v1.04 — 2026-07-07

No notes: the tag is on a merge commit ("Merge branch 'main' of
https://github.com/cwilson-111/Storage-Scanner").
