"""Headless entry point for `--mft-scan <drive> --subtree <path>
--output <file>` (Windows elevated Turbo Scan helper).

Windows' ShellExecuteExW "runas" elevation has no equivalent to macOS's
`do shell script ... with administrator privileges`, which conveniently
hands the elevated child's stdout back as its own return value (see
storage_scanner/priv_scan_cli.py and file_ops.run_elevated_scan_macos) --
the OS elevation broker calls CreateProcess for the new process, not us,
so there's no pipe we can attach as its stdout. Instead this helper writes
its JSON result to the `--output` file the caller told it to use and
exits; the caller (storage_scanner.file_ops.run_elevated_scan_windows)
reads that file back once the elevated process exits.

Distinct from priv_scan_cli.py's `--priv-scan` and cli.py's `--cli`: this
is Windows-only, Turbo-Scan-specific, and never meant to be run directly
by a user.

`--progress-file` is the same idea applied to *live* progress instead of
the final result: a real queue.Queue can't cross a process boundary, so
this process can't just post to the GUI's progress_q the way the
in-process Turbo Scan path does (see turbo_scan._run_turbo_in_process).
Before this existed, a scan running through this elevated-helper path
posted zero progress of any kind for its entire duration -- often 15s to
a minute-plus on a cold scan (real, measured) -- indistinguishable from a
hang. See _ProgressFileWriter below.

A failed scan still writes the `--output` file: {"error": ..., "traceback":
...} instead of the result, so the GUI can log why the helper failed. Its
stderr goes nowhere -- ShellExecuteExW gives the caller no pipe to read it
from -- and before this, a real helper failure left only "Turbo Scan helper
exited with code 1" in the log.
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback

from storage_scanner.logging_setup import logger
from storage_scanner.mft_volume import open_record_source
from storage_scanner.serialization import node_to_dict
from storage_scanner.turbo_read import scan_subtree_using_cache

EXIT_OK = 0
EXIT_SCAN_ERROR = 1

# The GUI (file_ops._relay_progress_file) opens the progress file to read it
# five times a second, and Windows refuses to replace a file while any
# handle is open on it -- even one opened with FILE_SHARE_DELETE. Each
# collision lasts only as long as one small read, so a short retry clears
# it; one that doesn't clear drops that update, and the next one follows
# within turbo_read's reporting interval anyway.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.01


class _ProgressFileWriter:
    """Duck-types just enough of queue.Queue's `.put()` interface for
    turbo_read.scan_subtree_using_cache() to use unmodified: each
    ("phase", Phase) it posts replaces the file's entire contents with that
    phase as JSON (there's no reader here that needs a history of every
    value, only the most recent one) via a temp-file-plus-os.replace swap,
    so a concurrent reader (run_elevated_scan_windows, polling from a
    completely separate process) can never observe a half-written value.
    turbo_read already limits how often it posts (see its Throttle use),
    so this doesn't need to.

    Only "phase" messages are relayed -- that's all Turbo Scan's reading
    steps post; "root"/"walk" are the Compatible engine's.

    `put` never raises: progress is a courtesy, and a write that fails
    (the GUI's reader holding the file open for longer than the retries
    allow, a full disk) must not fail the scan it reports on. Once, a
    replace colliding with the reader did exactly that -- the PermissionError
    ended the helper with exit code 1 and sent the scan to the Compatible
    engine, in 26 of 556 writes (4.7%) at the real cadence.
    """

    def __init__(self, path):
        self.path = path
        self.dropped = 0

    def put(self, item):
        kind, payload = item
        if kind != "phase":
            return
        try:
            self._write(json.dumps(payload.to_dict()))
        except OSError as exc:
            if not self.dropped:
                logger.warning("Turbo Scan helper could not update its progress file: %s", exc)
            self.dropped += 1

    def _write(self, text):
        tmp_path = self.path + ".tmp"
        with open_without_following_links(tmp_path) as f:
            f.write(text)
        for attempt in range(1, _REPLACE_ATTEMPTS + 1):
            try:
                os.replace(tmp_path, self.path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS:
                    raise
                time.sleep(_REPLACE_RETRY_SECONDS)


def open_without_following_links(path):
    """`path` opened for writing (created if missing, emptied if not), as a
    text stream -- but never through a symbolic link, junction or extra hard
    link. The unelevated app picks these paths in the user's temp folder and
    this helper runs elevated, so without this another process running as
    the same user could swap one for a link and have the helper overwrite a
    file the user can't write. Raises OSError instead."""
    if sys.platform != "win32":
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    # Its own instance, so these prototypes don't change the shared
    # ctypes.windll.kernel32 other modules call.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_write, open_always = 0x40000000, 4
    open_reparse_point = 0x00200000  # open a link itself, never its target
    reparse_attribute = 0x400

    handle = kernel32.CreateFileW(
        str(path), generic_write, 0, None, open_always, open_reparse_point, None
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _FileInfo()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        raise error
    if info.attributes & reparse_attribute or info.links != 1:
        kernel32.CloseHandle(handle)
        raise OSError(f"Refusing to write {path}: it's a link to somewhere else")
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY)  # type: ignore[attr-defined]
    stream = os.fdopen(fd, "w", encoding="utf-8")
    stream.truncate(0)
    return stream


def build_arg_parser():
    parser = argparse.ArgumentParser(prog="Storage-Scanner.py --mft-scan")
    parser.add_argument("drive", help='Volume root to read, e.g. "C:\\\\"')
    parser.add_argument(
        "--subtree",
        required=True,
        help="Path within the volume whose Node to write out",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="FILE",
        help="Write the resulting Node as JSON to FILE",
    )
    parser.add_argument(
        "--progress-file",
        default=None,
        metavar="FILE",
        help="Continuously overwrite FILE with the current scan step and its count "
        "so far, for run_elevated_scan_windows to relay back to the "
        "GUI's progress_q -- optional, omitted entirely when this is "
        "invoked outside that path (e.g. directly from a terminal).",
    )
    return parser


def _write_error(output_path, exc):
    """Best effort: the error, and its traceback for the log, where the GUI
    looks for the result (see file_ops.run_elevated_scan_windows)."""
    envelope = {
        "error": f"{exc.__class__.__name__}: {exc}",
        "traceback": traceback.format_exc(),
    }
    try:
        with open_without_following_links(output_path) as f:
            json.dump(envelope, f)
    except OSError:
        pass  # stderr (below) is all that's left


def run_mft_scan(argv):
    """Parse arguments and run one Turbo Scan. Returns a process exit code.

    Every failure -- an unopenable volume, a record with no root, a
    subtree path that isn't in the tree, a JSON/file-write error -- writes
    its error to the output file (see _write_error), prints it to stderr
    and returns EXIT_SCAN_ERROR. The elevated process is meant to fail
    closed: a caller that sees a non-zero exit or a missing output file
    treats it identically, as one more reason to fall back to the
    Compatible engine, never something that should crash or hang.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)  # exits(2) itself on --help / bad usage

    try:
        cancel_event = threading.Event()  # no external cancellation in this
        # process -- the caller cancels
        # by terminating it outright
        progress_q = _ProgressFileWriter(args.progress_file) if args.progress_file else None
        record_source = open_record_source(args.drive)
        try:
            subtree_node, mft_read = scan_subtree_using_cache(
                record_source,
                args.drive,
                args.subtree,
                progress_q=progress_q,
                cancel_event=cancel_event,
            )
        finally:
            record_source.close()

        with open_without_following_links(args.output) as f:
            # The GUI launching this helper is always the same build, so the
            # envelope's shape never has to be negotiated.
            json.dump({"node": node_to_dict(subtree_node), "mft_read": mft_read.to_dict()}, f)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        _write_error(args.output, exc)
        print(f"Turbo Scan failed: {exc}", file=sys.stderr)
        return EXIT_SCAN_ERROR

    return EXIT_OK
