<img src="docs/icon_preview.png" alt="Storage Scanner icon" width="96" align="left" />

# Storage Scanner

A fast, free disk-usage analyzer for Windows. Pick a drive or folder and Storage
Scanner scans it recursively, then shows every folder and file in a tree
**sorted by size**, with a percentage bar so the space hogs jump right out.

<br clear="left" />

### [⬇ Download StorageScanner.exe](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.exe)

[![Download](https://img.shields.io/badge/Download-StorageScanner.exe-2563eb?style=for-the-badge&logo=windows)](https://github.com/cwilson-111/Storage-Scanner/releases/latest/download/StorageScanner.exe)
[![Latest release](https://img.shields.io/github/v/release/cwilson-111/Storage-Scanner?style=for-the-badge)](https://github.com/cwilson-111/Storage-Scanner/releases/latest)

## Download & run (no Python needed)

1. Click the **Download** button above (or grab it from the
   [Releases](https://github.com/cwilson-111/Storage-Scanner/releases/latest) page).
2. Double-click `StorageScanner.exe`. That's it — no installer, no dependencies.

> Windows SmartScreen may warn about an unsigned app the first time. Click
> **More info → Run anyway**. (The app is open source — you can read every line here.)

## Features

- **Concurrent scanning** — walks directories in parallel across many threads,
  so even large drives finish quickly.
- **Sorted tree** — folders and files are ordered largest-first at every level.
- **Largest files view** — list the top **25 / 50 / 100** biggest files across the
  whole scan (pick the count from the dropdown), with double-click to reveal each
  in Explorer.
- **Percentage bars** — see at a glance what's eating your space.
- **Responsive UI** — scanning runs in the background with a live file counter
  and a **Cancel** button; the window never freezes.
- **Lazy loading** — sub-folders populate only when expanded, so huge trees stay snappy.
- **Right-click** any item to **Open in Explorer** or **Copy path**
  (double-click a file to reveal it).
- Pure Python standard library — **no third-party runtime dependencies**.

## Run from source

Requires Python 3.8+ (Tkinter ships with the standard Windows installer).

```bash
python treesize.py
```

## Build the .exe yourself

```bash
pip install -r requirements-dev.txt
build.bat
```

The standalone executable lands in `dist/StorageScanner.exe`.

Releases are also built automatically by GitHub Actions — push a tag like
`v1.0.0` and the `.exe` is attached to the release (see `.github/workflows/build.yml`).

## License

[MIT](LICENSE) — free to use, modify, and distribute.
