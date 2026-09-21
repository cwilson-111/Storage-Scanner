# Build provenance

How `StorageScanner.exe`, `StorageScanner.dmg`, and
`StorageScanner-linux-x86_64.tar.gz` are actually produced, and — just as
important — what that does and doesn't guarantee. All three come from the
same source commit, built in parallel by three independent CI jobs.

## What ships

- **Windows** — `StorageScanner.exe` is a PyInstaller `--onefile --windowed`
  bundle.
- **macOS** — `StorageScanner.dmg` contains `StorageScanner.app`, a
  PyInstaller `--windowed` (`--onedir`) app bundle, packaged as a disk image
  alongside an `/Applications` symlink.
- **Linux** — `StorageScanner-linux-x86_64.tar.gz` contains a PyInstaller
  `--onefile` ELF binary.

All three are built from exactly the Python source in this repository at
the commit a release was tagged from. Nothing else is added at build time.

## Exact build steps

Every release is built by [`.github/workflows/build.yml`](.github/workflows/build.yml),
as three separate jobs (`build`, `build-macos`, `build-linux`) on GitHub's
hosted `windows-latest`, `macos-latest`, and `ubuntu-latest` runners
respectively, each gated on a `test` job (`pytest` + `pyflakes`) passing
first. The core commands, in order:

**Windows:**
```
pip install -r requirements-dev.txt
python make_icon.py
pyinstaller --onefile --windowed --name StorageScanner --icon icon.ico --add-data "icon.ico;." Storage-Scanner.py
python make_sbom.py --app-version <the git tag> --output dist/sbom.json
```
followed by generating the portable ZIP and `SHA256SUMS.txt`.

**macOS:**
```
pip install -r requirements-dev.txt
python make_icon.py
pyinstaller --windowed --name StorageScanner --icon icon.icns --add-data "icon.ico:." --add-data "icon.icns:." Storage-Scanner.py
hdiutil create -volname "Storage Scanner" -srcfolder dist/dmg -ov -format UDZO dist/StorageScanner.dmg
python make_sbom.py --app-version <the git tag> --output dist/sbom-macos.json
```
followed by generating `SHA256SUMS-macos.txt` with `shasum`.

**Linux:**
```
pip install -r requirements-dev.txt
pyinstaller --onefile --name StorageScanner --add-data "icon.ico:." Storage-Scanner.py
tar -czf dist/StorageScanner-linux-x86_64.tar.gz -C dist StorageScanner
python make_sbom.py --app-version <the git tag> --output dist/sbom-linux.json
```
followed by generating `SHA256SUMS-linux.txt` with `sha256sum`.

The full command list for each (also visible in `build.yml` itself) is the
source of truth if this ever drifts out of sync with that file. The actions
the workflow itself depends on (`actions/checkout`, `actions/setup-python`,
etc.) are pinned to an exact commit SHA each, not a movable version tag —
see `build.yml`'s comments for what each SHA corresponds to.

## What's actually bundled

PyInstaller embeds a full Python interpreter and the Tcl/Tk library
Tkinter depends on into each platform's executable, alongside this repo's
own source — that's what makes each one runnable with no separate Python
install. Each platform's own `sbom*.json`, attached to every release,
records the exact CPython and Tcl/Tk versions embedded in that specific
build. Core scanning, duplicate detection, and cleanup import no third-party
runtime package at all.

Three features use an optional third-party package instead, imported only
when that specific feature runs: `matplotlib` (growth-history charts),
`pyarrow` (Compress CSV to Parquet), and `openpyxl` (Convert CSV to
Excel). PyInstaller can only bundle a package that's actually installed in
the build environment when it runs, so the standard release doesn't include
`pyarrow`/`openpyxl`; the separate Windows-only **Data build**
(`.github/workflows/build-data.yml`, published as a `data-v…` pre-release)
installs them explicitly and bundles them. Each release's own `sbom*.json`
lists exactly which optional packages made it in — check that rather than
assuming from this doc.

## Where to verify any of this yourself

- The **workflow run logs** for every build are public on the repo's
  Actions tab — you can see precisely what commands ran and what they
  printed, for any release.
- The **git tag** for a release names the exact commit its source came from.
- **`SHA256SUMS*.txt`** and **`sbom*.json`** (one pair per platform),
  attached to every release, let you confirm what you downloaded matches
  what that specific workflow run produced, and what's actually inside it.

## What this is *not*: a reproducible build

Running the exact same source through the exact same PyInstaller version
will **not** produce a byte-for-byte identical executable even on a second
run of the same workflow — PyInstaller's bootloader embeds build
timestamps, and the resulting binary carries its own timestamp too
(a Windows PE file's own header field; similarly for the macOS/Linux
binaries). So a checksum only ever proves "this is what that specific CI
run produced," not "you could rebuild this exact file yourself and get an
identical hash."

That's a real limitation, not a rounding error — true reproducible builds
(where independent parties can verify a binary bit-for-bit) are a
meaningfully bigger undertaking than PyInstaller's normal build process
supports, and this project doesn't claim to have solved that. What's
provided instead is *documented* provenance: an exact, auditable trail from
source commit → public build log → published checksums, which is enough to
catch tampering between "what the workflow produced" and "what you
downloaded," even though it can't prove "this is the only possible binary
this source could produce."
