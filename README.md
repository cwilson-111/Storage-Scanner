<img src="docs/Designer.png" alt="Storage Scanner icon" width="96" align="left" />

# Storage Scanner

A fast, free disk-usage analyzer for **Windows and macOS**. Pick a drive or
folder and Storage Scanner scans it concurrently, then shows every folder
and file in a tree **sorted by size**, with a percentage bar so the space
hogs jump right out — plus duplicate detection, review-first cleanup
recommendations, a treemap explorer, growth history with capacity
forecasting, and an audit log of everything it's ever deleted.

<br clear="left" />

### [⬇ Download StorageScanner.exe (Windows)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.exe)

[![Download](https://img.shields.io/badge/Download-StorageScanner.exe-2563eb?style=for-the-badge&logo=windows)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.exe)
[![Latest release](https://img.shields.io/github/v/release/cwilson-111/Storage-Scanner?style=for-the-badge)](https://github.com/cwilson-111/Storage-Scanner/releases/latest)

## Download & run (Windows, no Python needed)

1. Click the **Download** button above (or grab it from the
   [Releases](https://github.com/cwilson-111/Storage-Scanner/releases/latest) page).
2. Double-click `StorageScanner.exe`. That's it — no installer, no dependencies.

> Windows SmartScreen may warn about an unsigned app the first time. Click
> **More info → Run anyway**. (The app is open source — you can read every line here.)

## Running on macOS

There's no packaged macOS build yet — run it from source (see below). It's
fully supported: native Finder integration, Trash-based deletion, and its
own elevated-scan flow for folders your account can't fully read (see
**Admin/elevated scanning** below).

## Features

### Scanning
- **Concurrent scanning** — a small worker-thread pool walks directories in
  parallel, tuned by measurement rather than guesswork.
- **Correctness on real filesystems** — symlinks, junctions, and mount
  points are recorded but never traversed (no double-counting, no infinite
  loops); hard links are deduplicated so a file linked into multiple folders
  only counts once.
- **Logical vs. actual disk usage** — tracks allocated size separately from
  logical size, so sparse files, NTFS-compressed files, and OneDrive-style
  online-only placeholders don't inflate what's actually on disk.
- **Sorted, heat-colored tree** — every level sorted largest-first, with a
  percentage bar and heat coloring so big consumers stand out immediately.

### Admin/elevated scanning
- **Windows** — relaunches the whole app elevated via the standard UAC
  prompt, so folders your account can't open get scanned too.
- **macOS** — relaunching the entire GUI as root doesn't work on macOS
  (losing the window-server connection kills any window it tries to show),
  so instead only the *scan itself* runs elevated via the normal
  admin-password prompt — your window stays open and gets the results back
  directly, without restarting.

### Finding things
- **Search & Filter** — filter the current scan by name, extension, size
  range, and modified-date range, with sortable results.
- **Treemap explorer** — a drill-down, click-to-zoom treemap where
  rectangle area represents size and color represents relative heat, with
  breadcrumb navigation and hover details.
- **Largest Files** and **File Types Breakdown** views.

### Cleaning up safely
- **Duplicate file finder** — a staged pipeline (group by size → partial
  hash → full hash) finds exact-content duplicates with zero false
  positives. Each group gets an automatic **keeper recommendation** (prefers
  a copy outside Downloads/Desktop/Temp, then the oldest) with the reasoning
  shown — and the keeper is genuinely protected: it can never be deleted
  from that window, even via select-all, though you can manually override
  which copy is the keeper.
- **Cleanup Recommendations** — a review-first view across the whole scan:
  **Protected** paths (OS/app-managed locations, cloud placeholders — never
  suggested for deletion), **Review candidates** (large files untouched for
  a long time — a lead worth checking, not a verified-safe deletion), and
  **Duplicate candidates** (built from the same keeper logic above). Every
  row shows *why* it was flagged, an estimated recoverable size, a risk
  level, and a proposed action.
- **Everything goes through the Recycle Bin/Trash.** Nothing in this app
  permanently deletes a file.

### History and trust
- **Growth History** — compare any two saved snapshots of a path (not just
  the two most recent), with per-folder growth/shrink breakdowns.
- **Confidence-aware capacity forecasting** — fits a regression across your
  full scan history and reports a range and an explicit confidence level
  ("low"/"medium"/"high"), instead of a single number presented as certain.
- **Anomaly detection** — flags scan-to-scan size changes that are
  statistical outliers for that specific path (a sudden spike or a
  mass-deletion-shaped drop), based on that path's own history.
- **Audit Log** — every delete/recycle action the app has ever performed,
  from any window, with date, source, path, size, and result — a durable
  record of what to go look for in the Recycle Bin/Trash if you need it back.

### Automation
- **CLI mode** — `Storage-Scanner.py --cli <path> [--format json|csv]
  [--output FILE]` runs a headless scan and prints structured output with
  proper exit codes, for scripts, cron, or Task Scheduler.

Pure Python standard library — **no third-party runtime dependencies**.

## Run from source

Requires Python 3.9+ (Tkinter ships with the standard Windows and macOS
Python installers).

```bash
python Storage-Scanner.py
```

## Build the Windows .exe yourself

```bash
pip install -r requirements-dev.txt
build.bat
```

The standalone executable lands in `dist/StorageScanner.exe`.

Releases are also built automatically by GitHub Actions — push a tag like
`v1.0.0` and the `.exe` is attached to the release (see `.github/workflows/build.yml`).

## Running the test suite

```bash
pip install -r requirements-dev.txt
pytest tests/
python -m pyflakes storage_scanner/ tests/
```

## License

[MIT](LICENSE) — free to use, modify, and distribute.
