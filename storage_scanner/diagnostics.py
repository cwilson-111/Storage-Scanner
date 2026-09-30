"""Help ▸ Copy Diagnostic Info: what a bug report needs about this install,
as plain text to paste into a GitHub issue.

It names no file or folder (not the scanned paths, not the user's name in
the app-data path), so it's safe to paste in public; the log, which does
name paths, is something the user attaches only if they choose to.
"""

import os
import platform
import sqlite3
import sys

import history
from storage_scanner import logging_setup, turbo_cache, update_check
from storage_scanner.formatting import human_size
from storage_scanner.platform_support import IS_ROOT
from storage_scanner.version import __version__

ISSUES_URL = "https://github.com/cwilson-111/Storage-Scanner/issues/new/choose"


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _history_summary():
    try:
        conn = sqlite3.connect(f"file:{history.DB_NAME}?mode=ro", uri=True)
        try:
            scans = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            paths = conn.execute("SELECT COUNT(DISTINCT scan_path) FROM scans").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"unreadable ({exc})"
    schema = history.get_app_metadata("schema_version", "?")
    size = human_size(_file_size(history.DB_NAME))
    return f"schema {schema}, {scans:,} scans of {paths:,} paths, {size}"


def diagnostic_text():
    """The lines Copy Diagnostic Info puts on the clipboard."""
    turbo = history.get_app_metadata("turbo_scan_enabled", "0") == "1"
    cache = human_size(turbo_cache.cache_size_bytes())
    level = os.environ.get(logging_setup.LOG_LEVEL_ENV_VAR, "").strip().upper() or "INFO"
    lines = [
        f"Storage Scanner {__version__}",
        f"OS: {platform.platform()} ({platform.machine()})",
        "Python: {} ({})".format(
            platform.python_version(),
            "packaged build" if getattr(sys, "frozen", False) else "run from source",
        ),
        f"Elevated: {'yes' if IS_ROOT else 'no'}",
        f"Turbo Scan: {'on' if turbo else 'off'}, cache {cache}",
        f"Update check: {'on' if update_check.update_check_enabled() else 'off'}",
        f"History: {_history_summary()}",
        f"Log level: {level}",
    ]
    return "\n".join(lines)
