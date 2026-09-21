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
"""

import argparse
import json
import os
import sys
import threading

from storage_scanner.mft_scan import build_tree, finalize_subtree, reroot_if_reparse_point
from storage_scanner.mft_volume import open_record_source
from storage_scanner.serialization import node_to_dict
from storage_scanner.turbo_scan import find_subtree_node, get_records_using_cache

EXIT_OK = 0
EXIT_SCAN_ERROR = 1


class _ProgressFileWriter:
    """Duck-types just enough of queue.Queue's `.put()` interface for
    get_records_using_cache()/_full_scan_and_cache() to use unmodified --
    they already call `progress_q.put(("progress", count))` periodically
    during a cold full scan, previously discarded outright by passing
    `progress_q=None`. Every write replaces the file's entire contents
    with just the latest count (there's no reader here that needs a
    history of every value, only the most recent one) via a temp-file-plus-
    os.replace swap, so a concurrent reader (run_elevated_scan_windows,
    polling from a completely separate process) can never observe a
    half-written value.

    Only "progress" messages are relayed -- "root"/"progress_bytes" are
    scanner.py's own directory-walk-engine message kinds, never posted by
    Turbo Scan's record-parsing loop, so there's nothing else to handle.
    """

    def __init__(self, path):
        self.path = path

    def put(self, item):
        kind, payload = item
        if kind != "progress":
            return
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(payload))
        os.replace(tmp_path, self.path)


def build_arg_parser():
    parser = argparse.ArgumentParser(prog="Storage-Scanner.py --mft-scan")
    parser.add_argument("drive", help='Volume root to read, e.g. "C:\\\\"')
    parser.add_argument(
        "--subtree", required=True,
        help="Path within the volume whose Node to write out",
    )
    parser.add_argument(
        "--output", required=True, metavar="FILE",
        help="Write the resulting Node as JSON to FILE",
    )
    parser.add_argument(
        "--progress-file", default=None, metavar="FILE",
        help="Continuously overwrite FILE with the latest record count read "
             "so far, for run_elevated_scan_windows to relay back to the "
             "GUI's progress_q -- optional, omitted entirely when this is "
             "invoked outside that path (e.g. directly from a terminal).",
    )
    return parser


def run_mft_scan(argv):
    """Parse arguments and run one Turbo Scan. Returns a process exit code.

    Every failure -- an unopenable volume, a record with no root, a
    subtree path that isn't in the tree, a JSON/file-write error -- prints
    to stderr and returns EXIT_SCAN_ERROR. The elevated process is meant
    to fail closed: a caller that sees a non-zero exit or a missing output
    file treats it identically, as one more reason to fall back to the
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
            records = get_records_using_cache(
                record_source, args.drive, progress_q=progress_q, cancel_event=cancel_event,
            )
        finally:
            record_source.close()

        root_node, _orphan_count, frn_by_node_id = build_tree(records, root_path=args.drive)
        if root_node is None:
            raise RuntimeError(
                f"Turbo Scan could not locate a root directory record on {args.drive!r}"
            )
        subtree_node = find_subtree_node(root_node, args.subtree)
        # A requested folder that's itself a reparse point must still be
        # followed -- see mft_scan.reroot_if_reparse_point's docstring.
        subtree_node = reroot_if_reparse_point(subtree_node, args.subtree, records, frn_by_node_id)
        # Hard-link dedup is deliberately scoped to just this subtree, not
        # the whole volume -- see mft_scan.finalize_subtree's docstring.
        finalize_subtree(subtree_node, frn_by_node_id)

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(node_to_dict(subtree_node), f)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        print(f"Turbo Scan failed: {exc}", file=sys.stderr)
        return EXIT_SCAN_ERROR

    return EXIT_OK
