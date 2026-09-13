#!/usr/bin/env python3
"""
Storage Scanner - a disk usage analyzer for Windows and macOS.

Pick a drive or folder and it scans recursively, then shows every folder and
file in a tree sorted by size, with a percentage bar so the space hogs jump out.

This file is intentionally a thin entry point (kept at this path/name for
build.bat and .github/workflows/build.yml, which pass it straight to
PyInstaller) — the actual implementation lives in the storage_scanner/
package alongside it.

Run:  python Storage-Scanner.py
"""

from storage_scanner.app import main

if __name__ == "__main__":
    main()
