# Build provenance

How `StorageScanner.exe` is actually produced, and — just as
important — what that does and doesn't guarantee.

## What ships

`StorageScanner.exe` is a PyInstaller `--onefile --windowed` bundle of
exactly the Python source in this repository at the commit a release was
tagged from. Nothing else is added at build time.

## Exact build steps

Every release is built by [`.github/workflows/build.yml`](.github/workflows/build.yml)
on GitHub's hosted `windows-latest` runner, running these commands in order:

```
pip install -r requirements-dev.txt
python make_icon.py
pyinstaller --onefile --windowed --name StorageScanner --icon icon.ico --add-data "icon.ico;." Storage-Scanner.py
python make_sbom.py --app-version <the git tag> --output dist/sbom.json
```

followed by generating the portable ZIP and `SHA256SUMS.txt` (also visible
in that same workflow file). The actions the workflow itself depends on
(`actions/checkout`, `actions/setup-python`, etc.) are pinned to an exact
commit SHA each, not a movable version tag — see `build.yml`'s comments
for what each SHA corresponds to.

## What's actually bundled

PyInstaller's `--onefile` mode embeds a full Python interpreter and the
Tcl/Tk library Tkinter depends on into the executable, alongside this
repo's own source — that's what makes it runnable with no separate Python
install. `sbom.json`, attached to every release, records the exact CPython
and Tcl/Tk versions embedded in that specific build. Storage Scanner's own
code imports no third-party runtime package.

## Where to verify any of this yourself

- The **workflow run logs** for every build are public on the repo's
  Actions tab — you can see precisely what commands ran and what they
  printed, for any release.
- The **git tag** for a release names the exact commit its source came from.
- **`SHA256SUMS.txt`** and **`sbom.json`**, attached to every release, let
  you confirm what you downloaded matches what that specific workflow run
  produced, and what's actually inside it.

## What this is *not*: a reproducible build

Running the exact same source through the exact same PyInstaller version
will **not** produce a byte-for-byte identical `.exe` even on a second run
of the same workflow — PyInstaller's bootloader embeds build timestamps,
and the resulting Windows PE file carries its own timestamp too. So a
checksum only ever proves "this is what that specific CI run produced," not
"you could rebuild this exact file yourself and get an identical hash."

That's a real limitation, not a rounding error — true reproducible builds
(where independent parties can verify a binary bit-for-bit) are a
meaningfully bigger undertaking than PyInstaller's normal build process
supports, and this project doesn't claim to have solved that. What's
provided instead is *documented* provenance: an exact, auditable trail from
source commit → public build log → published checksums, which is enough to
catch tampering between "what the workflow produced" and "what you
downloaded," even though it can't prove "this is the only possible binary
this source could produce."
