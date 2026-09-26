"""Test-session setup that has to happen before any app module is imported.

storage_scanner.logging_setup attaches its log file handler at import time,
so a fixture would be too late: by then every test run has already written
into the real app log in %LOCALAPPDATA%. Pytest imports this file before
collecting any test module, so pointing the log directory at a throwaway
folder here keeps test runs out of the real log entirely.

Additionally, history.DB_NAME is computed at module import time based on
LOCALAPPDATA (or XDG_DATA_HOME on Linux), so we redirect those env vars
before any storage_scanner module is imported, fail loudly if DB_NAME ends
up in the real app directory, and clean up afterward.
"""

import os
import sys
import tempfile
from pathlib import Path

# Store original environment before any changes
_ORIGINAL_ENV = {
    "LOCALAPPDATA": os.environ.get("LOCALAPPDATA"),
    "APPDATA": os.environ.get("APPDATA"),
    "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
    "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
    "HOME": os.environ.get("HOME"),
    "USERPROFILE": os.environ.get("USERPROFILE"),
}

# Create a session-wide temp directory for app data
_TEST_APP_DATA_DIR = tempfile.mkdtemp(prefix="storage-scanner-test-")
_TEST_LOG_DIR = tempfile.mkdtemp(prefix="storage-scanner-test-logs-")

# Redirect all environment variables that control app data and log paths
# before any storage_scanner module is imported
os.environ["LOCALAPPDATA"] = _TEST_APP_DATA_DIR
os.environ["APPDATA"] = _TEST_APP_DATA_DIR
os.environ["XDG_DATA_HOME"] = _TEST_APP_DATA_DIR
os.environ["XDG_STATE_HOME"] = _TEST_APP_DATA_DIR
os.environ["HOME"] = _TEST_APP_DATA_DIR
os.environ["USERPROFILE"] = _TEST_APP_DATA_DIR
os.environ["STORAGE_SCANNER_LOG_DIR"] = _TEST_LOG_DIR


def pytest_configure(config):
    """Validate that history.DB_NAME resolves inside the test directory."""
    # Import history after redirecting env vars, so DB_NAME is computed correctly
    import history

    db_path = Path(history.DB_NAME).resolve()
    test_base = Path(_TEST_APP_DATA_DIR).resolve()

    # Check that DB_NAME doesn't end up in the real user's app data directories.
    # The real database would be at <REALAPPDATA>\NeuralStorageMatrix\storage_history.db
    # We check by seeing if db_path contains "NeuralStorageMatrix" and if it's NOT
    # inside our test directory
    if "NeuralStorageMatrix" in str(db_path):
        # The app uses the "NeuralStorageMatrix" folder naming convention
        try:
            db_path.relative_to(test_base)
            # db_path is inside test_base: this is good, database is isolated
        except ValueError:
            # db_path is NOT inside test_base, but it has NeuralStorageMatrix in it
            # This means it's probably the real database location
            for original_appdata_path in [_ORIGINAL_ENV.get("LOCALAPPDATA"),
                                           _ORIGINAL_ENV.get("APPDATA"),
                                           _ORIGINAL_ENV.get("XDG_DATA_HOME"),
                                           _ORIGINAL_ENV.get("HOME"),
                                           _ORIGINAL_ENV.get("USERPROFILE")]:
                if original_appdata_path:
                    real_app_folder = Path(original_appdata_path).resolve() / "NeuralStorageMatrix"
                    if real_app_folder in db_path.parents or str(db_path).startswith(str(real_app_folder)):
                        raise RuntimeError(
                            f"CRITICAL: history.DB_NAME ({db_path}) resolves inside "
                            f"real user app data ({real_app_folder}). This would corrupt the user's "
                            f"actual database during testing. Aborting tests."
                        )
            # If we get here, it's in some other NeuralStorageMatrix folder, which is weird
            raise RuntimeError(
                f"CRITICAL: history.DB_NAME ({db_path}) is not inside test app "
                f"data directory ({test_base}). Database isolation failed."
            )


def pytest_unconfigure(config):
    """Restore original environment variables."""
    for key, original_value in _ORIGINAL_ENV.items():
        if original_value is not None:
            os.environ[key] = original_value
        else:
            os.environ.pop(key, None)
    # Note: We intentionally leave the temp directories on disk for post-mortem
    # verification. The test runner can clean them up or the OS can delete them.
