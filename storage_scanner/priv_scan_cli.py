"""Headless entry point for `--priv-scan <path>` (macOS elevated scanning).

Relaunching the *whole* GUI as root via `do shell script ... with
administrator privileges` can never show a window: macOS's authorization
trampoline detaches the elevated child from the window-server connection,
so a Tk window it tries to open just never appears. Instead, only this
headless scan ever runs as root — it walks the tree with elevated
filesystem access and prints the result as JSON on stdout, which the
still-running, still-visible GUI process (running as the normal user)
reads back and displays like any other scan result.
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
