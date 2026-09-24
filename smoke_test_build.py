#!/usr/bin/env python3
"""Smoke-test a packaged build: launch the real frozen app, not the source.

    python smoke_test_build.py dist/StorageScanner.exe

Runs the built binary's headless `--cli` mode (exactly what a scheduled scan
runs) against a small folder of known files, and checks that it:

- starts at all (PyInstaller didn't leave out a module, the bundle isn't
  broken),
- exits 0,
- writes a JSON result with the right totals, and
- saves the scan to scan history (`--save-history`), creating the history
  database from nothing.

It runs the binary with subprocess, which waits for it to finish. That
matters on Windows: the .exe is a windowed (GUI-subsystem) program, and a
shell like PowerShell starts one and returns immediately without its exit
code, so calling the .exe straight from a CI step would always "pass".

The app's data folder is pointed at a temporary directory for the run, so a
smoke test never writes into the real scan history of whoever runs it.
Exits 0 when every check passes, 1 otherwise. Standard library only.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DATA_FOLDER = "NeuralStorageMatrix"  # history.py's APP_NAME
FILES = {"a.txt": 1000, os.path.join("sub", "b.bin"): 5000, os.path.join("sub", "c.bin"): 250}
TIMEOUT_SECONDS = 180


def _isolated_env(app_data_root):
    """Environment that sends every platform's app-data folder to app_data_root."""
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(app_data_root)  # Windows
    env["XDG_DATA_HOME"] = str(app_data_root)  # Linux
    env["HOME"] = str(app_data_root)  # macOS: ~/Library/Application Support
    return env


def _history_db(app_data_root):
    matches = list(Path(app_data_root).rglob("storage_history.db"))
    return matches[0] if matches else None


def run_smoke_test(binary):
    failures = []

    def check(condition, message):
        print(("PASS  " if condition else "FAIL  ") + message)
        if not condition:
            failures.append(message)
        return condition

    if not check(Path(binary).is_file(), f"built binary exists: {binary}"):
        return failures

    with tempfile.TemporaryDirectory(prefix="storage-scanner-smoke-") as work:
        target = Path(work) / "target"
        for relative, size in FILES.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)

        app_data_root = Path(work) / "appdata"
        app_data_root.mkdir()
        output = Path(work) / "result.json"

        command = [
            str(binary),
            "--cli",
            str(target),
            "--format",
            "json",
            "--output",
            str(output),
            "--save-history",
        ]
        print("RUN   " + subprocess.list2cmdline(command))

        try:
            result = subprocess.run(
                command,
                env=_isolated_env(app_data_root),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            check(False, f"finished within {TIMEOUT_SECONDS}s")
            return failures

        for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            if text and text.strip():
                print(f"      {stream}: {text.strip()}")

        check(result.returncode == 0, f"exit code 0 (got {result.returncode})")

        if check(output.is_file(), "wrote the JSON result"):
            data = json.loads(output.read_text(encoding="utf-8"))
            expected_size = sum(FILES.values())
            check(
                data.get("size") == expected_size,
                f"total size {expected_size} (got {data.get('size')})",
            )
            check(
                data.get("file_count") == len(FILES),
                f"file count {len(FILES)} (got {data.get('file_count')})",
            )

        db = _history_db(app_data_root)
        if check(
            db is not None, "created the scan history database in the isolated app-data folder"
        ):
            conn = sqlite3.connect(db)
            try:
                rows = conn.execute("SELECT total_size, file_count FROM scans").fetchall()
            finally:
                conn.close()
            check(
                rows == [(sum(FILES.values()), len(FILES))],
                f"saved exactly one scan to history (got {rows})",
            )

    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("binary", help="Path to the built StorageScanner executable")
    args = parser.parse_args(argv)

    failures = run_smoke_test(args.binary)

    if failures:
        print(f"\nSmoke test FAILED: {len(failures)} check(s)")
        return 1

    print("\nSmoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
