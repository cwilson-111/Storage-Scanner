"""Test-session setup that has to happen before any app module is imported.

history.py works out its app-data folder (the history database, and the
default log folder) from LOCALAPPDATA, XDG_DATA_HOME or HOME when it's
imported, and storage_scanner.logging_setup attaches its log file handler
at import time, so a fixture would be too late: by then a test run has
already written into the real app data. Pytest imports this file before
collecting any test module, so every one of those is pointed at a throwaway
folder here, and the session refuses to start if the database or the log
still resolves inside the real app-data folder.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every place history.py can put its app-data folder, from the environment
# as it was before the redirect below: Windows, Linux (XDG) and macOS.
_REAL_HOME = Path.home()
_REAL_APP_DATA_BASES = [
    Path(os.environ.get("LOCALAPPDATA") or _REAL_HOME),
    Path(os.environ.get("XDG_DATA_HOME") or _REAL_HOME / ".local" / "share"),
    _REAL_HOME / "Library" / "Application Support",
]

_sandbox = Path(tempfile.mkdtemp(prefix="storage-scanner-test-"))
for _name in ("LOCALAPPDATA", "XDG_DATA_HOME", "HOME"):
    os.environ[_name] = str(_sandbox / "appdata")
os.environ["STORAGE_SCANNER_LOG_DIR"] = str(_sandbox / "logs")


def pytest_configure(config):
    import history
    import storage_scanner.logging_setup  # noqa: F401 - attaches the log handler

    real_dirs = [(base / history.APP_NAME).resolve() for base in _REAL_APP_DATA_BASES]
    log_files = [
        Path(handler.baseFilename)
        for handler in logging.getLogger("storage_scanner").handlers
        if isinstance(handler, logging.FileHandler)
    ]
    for path in [Path(history.DB_NAME), *log_files]:
        resolved = path.resolve()
        for real_dir in real_dirs:
            if resolved.is_relative_to(real_dir):
                raise pytest.UsageError(
                    f"{resolved} is inside the real app-data folder {real_dir}; "
                    "refusing to run tests against real user data"
                )
