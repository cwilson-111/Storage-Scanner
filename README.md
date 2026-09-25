<img src="docs/Designer.png" alt="Storage Scanner icon" width="96" align="left" />

# Storage Scanner

A fast, free disk-usage analyzer for **Windows, macOS, and Linux**. Pick a
drive or folder and Storage Scanner scans it concurrently, then shows every
folder and file in a tree **sorted by size**, with a percentage bar so the
space hogs jump right out — plus duplicate detection, review-first cleanup
recommendations, a treemap explorer, growth history with capacity
forecasting, and an audit log of everything it's ever deleted.

<br clear="left" />

### Download

[![Download for Windows](https://img.shields.io/badge/Windows-StorageScanner.exe-2563eb?style=for-the-badge&logo=windows)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.exe)
[![Download for macOS](https://img.shields.io/badge/macOS-StorageScanner.dmg-2563eb?style=for-the-badge&logo=apple)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.dmg)
[![Download for Linux](https://img.shields.io/badge/Linux-StorageScanner.tar.gz-2563eb?style=for-the-badge&logo=linux)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner-linux-x86_64.tar.gz)

[![Latest release](https://img.shields.io/github/v/release/cwilson-111/Storage-Scanner?style=for-the-badge)](https://github.com/cwilson-111/Storage-Scanner/releases/latest)

Or grab any of these straight from the [Releases](https://github.com/cwilson-111/Storage-Scanner/releases/latest) page.

## Download & run

**Windows** — double-click `StorageScanner.exe`. No installer, no dependencies.

> Windows SmartScreen may warn about an unsigned app the first time. Click
> **More info → Run anyway**. (The app is open source — you can read every line here.)

> Some browsers (Opera, Chrome, etc.) may block the `.exe` download itself,
> flagging it before SmartScreen ever gets a chance to — a reputation check
> against an unsigned, freshly-released, PyInstaller-built binary, not a
> real detection. If that happens, download **`StorageScanner-portable.zip`**
> instead (same binary, zipped — usually avoids the same trigger), or try a
> different browser. Verify against `SHA256SUMS.txt` either way.

**macOS** — open `StorageScanner.dmg` and drag Storage Scanner into
Applications.

> The app isn't notarized (that needs a paid Apple Developer account — same
> class of cost blocker as Windows code-signing), so Gatekeeper will refuse
> to open it the first time with an "unidentified developer" warning.
> Right-click (or Control-click) the app in Applications → **Open** → confirm
> **Open** in the dialog. You only need to do this once.

**Linux** — extract `StorageScanner-linux-x86_64.tar.gz`, then run
`./StorageScanner` (mark it executable first if needed: `chmod +x StorageScanner`).
Built and tested against `ubuntu-latest`; needs glibc, so distros noticeably
older than that release may not run it — build from source there instead
(see below).

### Verifying a release

Every release includes, per platform:
- **`StorageScanner.exe`** / **`StorageScanner-portable.zip`** (Windows) —
  the same executable, zipped as an alternative if you'd rather not have
  anything auto-registered by an installer-style download.
- **`StorageScanner.dmg`** (macOS).
- **`StorageScanner-linux-x86_64.tar.gz`** (Linux).
- **`sbom*.json`** — a software bill of materials (CycloneDX format, one
  per platform) listing exactly what's bundled into that executable: the
  frozen Python interpreter, the Tcl/Tk library the GUI depends on, and
  whichever optional packages (see **Features**) were installed at build
  time. Core scanning, duplicate detection, and cleanup need no third-party
  runtime package at all.
- **`SHA256SUMS*.txt`** — checksums for that platform's files, so you can
  confirm what you downloaded matches what was actually built
  (`sha256sum -c SHA256SUMS*.txt` on macOS/Linux, `Get-FileHash` on Windows).

No release is code-signed or notarized yet — checksums let you verify
integrity, but don't establish who built it, which is what signing/notarization
is for. That's tracked as future work. See [BUILD_PROVENANCE.md](BUILD_PROVENANCE.md)
for exactly how a release is built and what these guarantees do and don't cover,
and [PRIVACY.md](PRIVACY.md) for what the app does (and doesn't) do with your data.

### Which Windows download do I want? (standard vs. Data build)

| | `StorageScanner.exe` (standard) | `StorageScanner-Data.exe` |
|---|---|---|
| Where | The [latest release](https://github.com/cwilson-111/Storage-Scanner/releases/latest) | A **pre-release** on the [Releases](https://github.com/cwilson-111/Storage-Scanner/releases) page, tagged `data-v…` |
| Scanning, duplicates, cleanup, history | Yes | Yes |
| Tools ▸ Data Tools (CSV → Parquet / Excel) | Not included | Included |
| Size / startup | Smaller, faster to start | Much larger (bundles `pyarrow`), slower to start |
| In-app update notice | Yes | No — a Data build never prompts you to update, so check the Releases page yourself |

If you don't need the CSV conversion tools, use the standard build. The Data
build is Windows-only and otherwise identical; it ships with its own
`sbom-data.json` and `SHA256SUMS-data.txt`, which list exactly which optional
packages were bundled.

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
- **Turbo Scan (Experimental, Windows only)** — an opt-in toggle (Tools ▸
  Settings) that reads the NTFS Master File Table directly instead of
  walking directories one at a time, with a persistent cache and NTFS USN
  Journal incremental refresh: a repeat scan of an unchanged volume is much
  faster than the first one, and a repeat scan of one folder loads only
  that folder from the cache, not the whole drive. Falls back to the normal
  scan engine automatically on anything it can't handle (non-NTFS volumes,
  network shares, any failure). Off by default while it gets more real-world
  mileage.

### Admin/elevated scanning
- **Windows** — relaunches the whole app elevated via the standard UAC
  prompt, so folders your account can't open get scanned too.
- **macOS** — relaunching the entire GUI as root doesn't work on macOS
  (losing the window-server connection kills any window it tries to show),
  so instead only the *scan itself* runs elevated via the normal
  admin-password prompt — your window stays open and gets the results back
  directly, without restarting.
- **Linux** — same only-the-scan-runs-elevated approach as macOS, via a
  PolicyKit (`pkexec`) prompt instead: a full relaunch-as-root can show a
  window on a traditional X11 session, but Wayland compositors generally
  refuse a root process a connection to your session outright, so the
  headless approach works the same regardless of which one you're running.
  Needs a PolicyKit authentication agent running for your desktop (ships by
  default with GNOME/KDE and most desktop distros).

### Finding things
- **Search & Filter** — filter the current scan by name, extension, size
  range, and modified-date range, with sortable results.
- **Treemap explorer** — a drill-down, click-to-zoom treemap where
  rectangle area represents size and color represents relative heat, with
  breadcrumb navigation and hover details.
- **Largest Files** and **File Types Breakdown** views.

### Cleaning up safely
- **Duplicate file finder** — a staged pipeline (group by size → hash the
  first and last 1 MB → hash the middle 1 MB) that never reads a whole large
  file. Files up to 3 MB are fully covered by those windows, so matches are
  byte-exact. Above 3 MB a match is *sampled*: two files with the same size
  and identical first, middle and last 1 MB are grouped even if they differ
  somewhere in between — the window and Cleanup Recommendations label those
  groups as sampled (medium risk) instead of exact, so review before
  deleting. Each group gets an automatic **keeper recommendation** (prefers
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
- **Archive instead of delete** — for Review candidates, compress to a
  `.zip` right next to the original instead of removing it outright.
  Nothing is lost, just shrunk; the original is only removed after the
  archive is written and verified. Works best on text/logs/uncompressed
  documents — already-compressed formats (video, photos, PDFs) won't
  shrink much, and the UI tells you that before you commit.
- **Everything goes through the Recycle Bin/Trash.** Nothing in this app
  permanently deletes a file.

### History and trust
- **Growth History** — compare any two saved snapshots of a path (not just
  the two most recent), with per-folder growth/shrink breakdowns. Available
  as soon as the app opens, from scans saved by earlier runs and earlier
  versions; no rescan needed.
- **Confidence-aware capacity forecasting** — fits a regression across your
  full scan history and reports a range and an explicit confidence level
  ("low"/"medium"/"high"), instead of a single number presented as certain.
- **Anomaly detection** — flags scan-to-scan size changes that are
  statistical outliers for that specific path (a sudden spike or a
  mass-deletion-shaped drop), based on that path's own history.
- **Audit Log** — every delete/recycle action the app has ever performed,
  from any window, with date, source, path, size, and result — a durable
  record of what to go look for in the Recycle Bin/Trash if you need it back.
- **Storage Budgets** — right-click any folder to set a size threshold, and
  get a dismissible alert when it's exceeded — checked right after you scan
  it, and again at launch using the last saved scan, so you can see a
  breach before you've rescanned anything. A scheduled scan of a budgeted
  folder that finds it over budget also shows a desktop notification (see
  Scheduled scans). No background service: alerts only fire when a scan
  runs or you open the app, never continuously. Budgets alert; they don't
  stop files from being written.

### Automation
- **CLI mode** — `Storage-Scanner.py --cli <path> [--format json|csv|none]
  [--output FILE] [--save-history] [--notify]` runs a headless scan and
  prints structured output with proper exit codes, for scripts, cron, or
  Task Scheduler. `--save-history` records the scan in scan history exactly
  like a scan run from the app, so Growth History, forecasts, anomaly
  detection and budgets all include it. `--notify` (with `--save-history`)
  shows a desktop notification if the folder is over its budget; if it
  can't (e.g. Windows notifications are turned off), it says why on stderr
  and in the app log, and the exit code is unchanged.
- **Scheduled scans** — Tools ▸ History & Trust ▸ Schedule Scans… scans a
  folder daily or weekly and saves each run to scan history, so growth
  tracking keeps working without you remembering to rescan. On Windows it
  creates a Task Scheduler task for you (as your own user, no admin rights
  needed; a missed run happens as soon as the PC is back on). On macOS and
  Linux it gives you the line to add with `crontab -e`. Scheduled scans run
  without admin rights, so folders only an administrator can read are
  skipped, and the app doesn't need to be open. If a scanned folder is over
  its budget, you get a desktop notification: a Windows toast (shown as
  coming from "Windows PowerShell", which delivers it), a macOS
  notification, or `notify-send` on Linux (which usually needs extra setup
  under cron). Schedules created before this existed need to be saved again
  to get notifications. On Windows the same window lists every scheduled
  scan with its last run, result and next run, and flags any that need
  attention (saved before notifications, the app moved since, or disabled
  in Task Scheduler). Select one to load it into the form, then save it
  again or remove it.
- **Export Results** — Tools ▸ Export Results… saves the current scan as
  CSV (one row per file and folder, for Excel) or JSON (the nested folder
  tree), the same formats the CLI writes.
- **Data Tools (Data build only — see "Which download do I want?" above)** —
  Tools ▸ Data Tools lets you compress any CSV file (not just this app's own
  exports) to Parquet, or convert it to an Excel `.xlsx` workbook. Both use
  optional third-party packages (`pyarrow`, `openpyxl` respectively) that
  aren't required for anything else in the app.

### Staying current
- **Update notice** — on launch, a quiet check (at most once a day) for a
  newer release, shown as a small dismissible banner with a link — never
  an auto-download or auto-run of anything. See [PRIVACY.md](PRIVACY.md)
  for exactly what this does and doesn't send.

Pure Python standard library for everything above — **no required
third-party runtime dependencies**. Three features are the exceptions,
each independently optional and only imported when actually used:
`matplotlib` (growth-history charts), `pyarrow` (Compress CSV to Parquet),
and `openpyxl` (Convert CSV to Excel). The app runs fully without any of
them installed; those specific menu items just report that the package is
missing instead.

## Run from source

Requires Python 3.9+ (Tkinter ships with the standard Windows and macOS
Python installers; on Linux install your distro's Tk package first, e.g.
`sudo apt install python3-tk` on Debian/Ubuntu).

```bash
python Storage-Scanner.py
```

## Build it yourself

```bash
pip install -r requirements-dev.txt
build.bat          # Windows: StorageScanner.exe + portable ZIP
```

On macOS or Linux, run the same PyInstaller commands `build.yml` uses
(there's no `build.bat`-equivalent script for those yet — see
`.github/workflows/build.yml`'s `build-macos`/`build-linux` jobs for the
exact commands). Either way, the executable, an SBOM, and a checksums file
land in `dist/`.

Releases are also built automatically by GitHub Actions — push a tag like
`v1.0.0` and `StorageScanner.exe`, `StorageScanner.dmg`, and
`StorageScanner-linux-x86_64.tar.gz` are all attached to the release.

## Running the test suite

```bash
pip install -r requirements-dev.txt
pytest tests/          # also enforces the coverage floor
ruff check .           # lint + import order
black --check .        # formatting (drop --check to apply)
mypy storage_scanner/  # type checking
```

All four read their settings from `pyproject.toml`, and CI runs the same
four commands as the `test` job every release build depends on, plus
`python benchmarks/scale.py --check`, which fails the build if memory per
file, history size per scan, Turbo cache size per record, or the records a
folder rescan loads get more than 15% worse than `benchmarks/baseline.json`.

### Benchmarking the scanner

`benchmarks/scale.py` (above) gates how memory and database sizes grow on
synthetic volumes; `benchmark_scan.py` is the on-disk counterpart, checking
scan correctness on edge cases and timing real scans across versions:

```bash
python benchmark_scan.py --profile medium --output bench-before.json
# ...change the scanner...
python benchmark_scan.py --profile medium --baseline bench-before.json
```

Generates a folder tree from a fixed seed (`small` ≈ 2k files, `medium` ≈
20k, `large` ≈ 100k and about 1 GB), scans it, checks the result against
what was generated (totals, per-folder rollups, hard links counted once,
symlinks/junctions not followed), then times several scans and measures
peak memory. It exits 2 if the scan doesn't match the tree, and 3 if the
median is more than `--max-slowdown` (default 25%) slower than the baseline.
Compare only runs from the same machine; `--dir` picks the drive to test.

## Privacy & trust

- [PRIVACY.md](PRIVACY.md) — what the app reads, stores, and (doesn't) send anywhere.
- [BUILD_PROVENANCE.md](BUILD_PROVENANCE.md) — exactly how a release binary is built, and what that does/doesn't guarantee.

## Roadmap

The full plan, with what's done and what's next, is in
[NEURAL_STORAGE_MATRIX_PROJECT_ROADMAP.md](NEURAL_STORAGE_MATRIX_PROJECT_ROADMAP.md).

**Next: scale for very large drives and long histories.** History that
doesn't grow forever (retention plus a compact layout), then a smaller
in-memory tree, measured by `benchmarks/scale.py`.

**Then: enterprise monitoring for computers and databases** (Phase 5 in the
roadmap). The desktop app stays free and local-first; the fleet pieces are
separate and reuse the same scan engine.

| Step | What it adds |
|---|---|
| **Agent** | Runs as a Windows service, macOS launchd daemon or Linux systemd service. Takes a central policy, scans incrementally, and sends only small summaries: folder totals, top files, and changes since last time. Works offline and deploys through Intune, GPO, SCCM or Jamf. |
| **Central store** | An HTTPS ingest API with per-device certificates, backed by PostgreSQL + TimescaleDB. Keeps full detail for 30 days, then daily and weekly summaries, so 10,000 machines stay affordable. |
| **Database monitoring** | Read-only connectors for SQL Server, PostgreSQL, MySQL/MariaDB, Oracle and MongoDB. Track data, index, log and free space per database and table, bloat, and log or WAL growth. Uses monitoring roles only, never table data. |
| **Console and alerts** | A fleet dashboard, fill-date forecasts with confidence levels, and alert rules that extend today's budgets. Sends to email, Teams/Slack, PagerDuty or ServiceNow/Jira, with scheduled reports. |
| **Security** | SSO (Entra ID, Okta), role-based access, an exportable audit trail, and an option to hash user folder names. Signed builds are required. |
| **Remote cleanup** (opt-in) | Cleanup plans built from the existing recommendations. Each is dry-run first, approved by a second person, sent to the Recycle Bin or quarantine only (never deleted outright), and fully audited. Databases stay alert-only. |

## License

[MIT](LICENSE) — free to use, modify, and distribute.
