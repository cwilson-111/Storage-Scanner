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
import csv
import json
import os
import queue
import sys
import threading

from storage_scanner.scanner import scan
from storage_scanner.serialization import node_to_dict

EXIT_OK = 0
EXIT_SCAN_ERROR = 1

_CSV_FIELDS = (
    "path", "name", "is_dir", "size", "alloc_size", "file_count", "mtime",
    "is_link", "hardlink_dup", "is_cloud_placeholder", "error",
)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="Storage-Scanner.py --cli",
        description="Run a headless disk-usage scan and print structured output.",
    )
    parser.add_argument("path", help="Folder or file to scan")
    parser.add_argument(
        "--format", choices=("json", "csv"), default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="Write output to FILE instead of stdout",
    )
    return parser


def _flatten(node, rows):
    rows.append(node)
    for child in node.children:
        _flatten(child, rows)


def _write_json(node, out):
    json.dump(node_to_dict(node), out)
    out.write("\n")


def _write_csv(node, out):
    rows = []
    _flatten(node, rows)
    writer = csv.writer(out)
    writer.writerow(_CSV_FIELDS)
    for n in rows:
        writer.writerow([getattr(n, field) for field in _CSV_FIELDS])


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

    write = _write_json if args.format == "json" else _write_csv

    if args.output:
        newline = "" if args.format == "csv" else None
        try:
            with open(args.output, "w", newline=newline, encoding="utf-8") as f:
                write(node, f)
        except OSError as exc:
            print(f"Could not write output file: {exc}", file=sys.stderr)
            return EXIT_SCAN_ERROR
    else:
        write(node, sys.stdout)

    return EXIT_OK
