# Storage Scanner — working notes for agents

Read this before changing anything here. omp's task agents read it
explicitly; a session started inside this repo gets it automatically.

## Code

- Python 3.9-compatible source (Ruff's `target-version` is the contract: no
  newer syntax, whatever the local interpreter accepts). No third-party
  *runtime* dependencies by design — `matplotlib`, `pyarrow`, `openpyxl` are
  optional and lazily imported. Frozen with PyInstaller; Windows-first with
  real macOS and Linux branches.
- **Hot loops are hot.** In scanning/parsing/hashing paths avoid per-item
  allocation, copying and attribute lookups. `os.scandir`/`os.path` are used
  over `pathlib` on purpose — `PTH` rules are off in `pyproject.toml`. Do not
  "modernise" those paths.
- **Platform code is branched, not assumed.** Windows-only APIs
  (`ctypes.windll`, raw volume handles, `SHFileOperationW`) live behind the
  existing platform flags and need defined behaviour on the other two OSes.
- **Persistence changes need a schema version and a migration.** The SQLite
  databases live in the user's app-data dir and already hold real data.

## UI (Tkinter, `storage_scanner/ui/`)

- 31 modules; 21 export a `*Mixin` composed into one `StorageScannerApp` in
  `storage_scanner/app.py` (the composition root is `app.py`, not
  `ui/app.py`). Every attribute or method shared across mixins is declared
  once in `ui/app_state.py` (`AppState`, aliased to `AppMixin` under
  `TYPE_CHECKING`, `object` at run time). Skip it and mypy fails —
  `check_untyped_defs` is on for `storage_scanner.app` and
  `storage_scanner.ui.*` to catch exactly that.
- **No worker thread touches Tk.** Workers push tuples onto a `queue.Queue`;
  the Tk thread drains it on a `root.after(...)` tick
  (`ui/scan_lifecycle.py::_poll_progress`). For a long job from a window use
  `ui/busy_task.py::run_busy(...)`; never hand-roll another worker/dialog
  pair. Batched UI updates use a fixed tick, a per-tick work cap and a time
  budget (`ui/live_tree.py`, `ui/owner_column.py`) — unbatched inserts froze
  the window.
- Colours and fonts come from `settings.py` (`COLORS`, `FONT*`); never
  hardcode a hex value or font name.
- Verify by launching the app, or through `tests/conftest.py`'s `tk_root` /
  `scanner_app` / `run_tk_until` fixtures.

## Tests (`tests/`)

- `tests/conftest.py` redirects `LOCALAPPDATA`/`XDG_DATA_HOME`/`HOME` to a
  tempdir **at import time**, before any `storage_scanner` import, and raises
  `pytest.UsageError` if the real app-data dir is reached. Never bypass it.
- One process-wide Tk interpreter lives in `tk_root`; `scanner_app`,
  `dialogs`, `recycle_bin`, `run_tk`/`run_tk_until` and `scan_in_app` build
  on it. Never create a second `Tk()`.
- Windows-only tests carry `@pytest.mark.windows` (module-level `pytestmark`
  is common); `pytest_collection_modifyitems` skips them elsewhere. Anything
  testable with fakes must run everywhere — put it in a separate module if
  the existing one is wholly Windows-gated.
- Coverage is reported locally; the floor is enforced only in the Windows CI
  job. Never write a test to move the number.
- Some gates are Windows-only. Off Windows, say which you could not run.

## Changelog and releases

- `CHANGELOG.md`: `## vX.Y.Z — YYYY-MM-DD`, newest first, `###` subheadings
  (`Main window`, `Fixes`, `Security`, `Development`, …). Work in progress
  goes under `## Unreleased`. `release_notes.py` extracts the section
  verbatim as the GitHub release body and **fails the release if it is
  missing or empty** — it is published text.
- A release is two commits, in order:
  1. **Release `vX.Y.Z`** — rename `## Unreleased` to
     `## vX.Y.Z — YYYY-MM-DD`; nothing else. Check with
     `python release_notes.py vX.Y.Z` before tagging.
  2. **Scoop and winget manifests: `vX.Y.Z`** — `bucket/storage-scanner.json`
     (`version`, download `url`, and the portable ZIP's SHA-256 in `hash`), a
     new `packaging/winget/manifests/c/cwilson-111/StorageScanner/X.Y.Z/`
     with all three YAML files copied forward, and the `README.md` lines
     naming the manifest path.
- **Never hand-edit `storage_scanner/version.py`**: every build job
  overwrites it from the tag.
- The Scoop `hash` comes from the published `SHA256SUMS.txt`, never a local
  build (PyInstaller output is not reproducible).

## Security surface

Threat model: a local non-admin user and malicious filesystem content.
Nothing listens on a socket.

- **Privilege elevation**, three ways: `ShellExecuteExW` `runas` to a
  headless `--mft-scan` helper (Windows), `osascript … with administrator
  privileges` running `--priv-scan` (macOS), `pkexec --priv-scan` (Linux).
  Check what is interpolated into each command, that the elevated child's
  output is validated before the parent trusts it
  (`turbo_scan._first_path_outside` exists for this — nothing may bypass
  it), and that the child cannot be pointed outside the user's request.
- **Raw volume I/O**: `mft_volume.py` opens the volume;
  `mft_parser.py`/`mft_records.py` parse attacker-influenceable records byte
  by byte. Every offset/length needs bounds validation before use, every
  loop a termination condition an adversarial record cannot defeat.
- **Filesystem redirection**: junctions, symlinks, reparse points, hard
  links. `RedirectionGuard` and `_NAME_SURROGATE_BIT` handling must cover
  every path that deletes, writes or follows a link, with TOCTOU windows
  closed.
- **Destructive operations**: deletion goes through `SHFileOperationW` with
  `FOF_ALLOWUNDO` (and platform trash equivalents). Nothing may turn a
  recycle into a permanent delete; `FOF_NORECURSEREPARSE` stays set; the
  duplicate-cleanup keeper rules cannot be bypassed by select-all.
- **Local state**: `storage_history.db`, `turbo_scan_cache.db`,
  `cleanup_cache.db`. No SQL by string concatenation; nothing read back from
  a cache trusted as a path.
- **Supply chain**: dependencies hash-locked (`requirements-*.lock`,
  `--require-hashes`), Actions pinned to commit SHAs. PyInstaller bundles
  whatever is installed at build time, so a build-time dependency ships.
