"""Headless CLI scan mode (`--cli <path>`).

Usable from scripts, cron, Task Scheduler, or any other automation: runs
one scan and prints structured output (JSON or CSV) with exit codes a
caller can branch on. This is a supported, documented interface — distinct
from priv_scan_cli.py's `--priv-scan`, which is an internal implementation
detail of macOS elevated scanning, never meant to be run directly.

argparse handles `--help` and malformed arguments itself (exiting 2, the
Unix convention for usage errors), so this only needs to handle the scan
itself failing.
"""

import argparse
import os
import queue
import sys
import threading

from storage_scanner.export import FORMATS, export_to_file, write_csv, write_json
from storage_scanner.scanner import scan

EXIT_OK = 0
EXIT_SCAN_ERROR = 1


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="Storage-Scanner.py --cli",
        description="Run a headless disk-usage scan and print structured output.",
    )
    parser.add_argument("path", help="Folder or file to scan")
    parser.add_argument(
        "--format", choices=FORMATS + ("none",), default="json",
        help="Output format (default: json). 'none' writes no data, for a "
             "scheduled scan that only needs --save-history",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="Write output to FILE instead of stdout",
    )
    parser.add_argument(
        "--save-history", action="store_true",
        help="Also record this scan in scan history, exactly like a scan run "
             "from the app, so growth, forecasts, anomalies and budgets see it",
    )
    return parser


def run_cli(argv):
    """Parse CLI arguments and run the scan. Returns a process exit code."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)  # exits(2) itself on --help / bad usage

    if not os.path.exists(args.path):
        print(f"Path does not exist: {args.path}", file=sys.stderr)
        return EXIT_SCAN_ERROR

    progress_q = queue.Queue()
    cancel_event = threading.Event()
    try:
        node = scan(args.path, progress_q, cancel_event)
    except Exception as exc:  # noqa: BLE001 - report any scan failure to the caller
        print(f"Scan failed: {exc}", file=sys.stderr)
        return EXIT_SCAN_ERROR

    # Diagnostics go to stderr so stdout stays clean for the actual data —
    # the same lesson learned from --priv-scan's stdout-pollution bug.
    print(
        f"Scanned {args.path}: {node.size:,} bytes, {node.file_count:,} files",
        file=sys.stderr,
    )

    if args.save_history:
        # Imported here: history.py creates its app-data folder at import
        # time, which a plain `--cli` export has no reason to do.
        from storage_scanner.scan_history import record_scan

        try:
            recorded = record_scan(node)
        except Exception as exc:  # noqa: BLE001 - report to the caller
            print(f"Could not save scan history: {exc}", file=sys.stderr)
            return EXIT_SCAN_ERROR

        print(f"Saved to scan history (scan #{recorded.scan_id})", file=sys.stderr)

        if recorded.budget_breach:
            print(
                f"Over budget: {recorded.budget_breach.current_size_bytes:,} bytes "
                f"(budget {recorded.budget_breach.threshold_bytes:,})",
                file=sys.stderr,
            )

    if args.format == "none":
        return EXIT_OK

    if args.output:
        try:
            export_to_file(node, args.output, args.format)
        except OSError as exc:
            print(f"Could not write output file: {exc}", file=sys.stderr)
            return EXIT_SCAN_ERROR
    else:
        write = write_json if args.format == "json" else write_csv
        write(node, sys.stdout)

    return EXIT_OK
