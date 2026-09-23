"""Test-session setup that has to happen before any app module is imported.

storage_scanner.logging_setup attaches its log file handler at import time,
so a fixture would be too late: by then every test run has already written
into the real app log in %LOCALAPPDATA%. Pytest imports this file before
collecting any test module, so pointing the log directory at a throwaway
folder here keeps test runs out of the real log entirely.
"""

import os
import tempfile

os.environ["STORAGE_SCANNER_LOG_DIR"] = tempfile.mkdtemp(prefix="storage-scanner-test-logs-")
