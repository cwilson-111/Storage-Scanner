"""Headless entry point for `--priv-scan <path>` (macOS and Linux elevated
scanning -- see storage_scanner.file_ops.run_elevated_scan_macos/
run_elevated_scan_linux for why each platform needs this rather than
just relaunching the whole GUI as root).

Nothing about this module itself is platform-specific: it walks the tree
with whatever filesystem access the process it's run under has, and
prints the result as JSON on stdout, which the caller (`do shell script`
on macOS, `pkexec` on Linux) hands back to the still-running,
still-visible GUI process (running as the normal user), which reads it
back and displays it like any other scan result.
"""

import json
import queue
import sys
import threading

from storage_scanner.scanner import scan
from storage_scanner.serialization import node_to_dict


def run_priv_scan(path):
    """Scan `path` and print the resulting tree as JSON on stdout.

    Exits with status 1 and an error message on stderr if the scan itself
    raises (e.g. the path vanished between authorization and the scan).
    """
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    try:
        node = scan(path, progress_q, cancel_event)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
        sys.stderr.write(str(exc))
        sys.exit(1)
    sys.stdout.write(json.dumps(node_to_dict(node)))
