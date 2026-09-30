#!/usr/bin/env python3
"""How long the main tree freezes on a column click or a delete in one big
folder, measured on a real Tk Treeview.

Builds a finished scan's tree the way main_window._finish_scan does (the
root row, opened, with one row per file of a single folder), then times,
through the app's own methods:

  sort_name    a click on the Name heading (the rows were biggest-first)
  sort_size    a click on the Size heading (the rows were by name)
  delete_first taking the folder's first row out after a delete: every
               row below it moves up one, so this is the most rows a
               single delete can touch (main_window._remove_main_tree_row)

Each timing includes redrawing the window (the app's default size, on
screen), as the app would before taking the next click. Files get random
names and sizes (a fixed seed), so a sort moves nearly every row.

Usage:
  python benchmarks/main_tree.py                      # 10,000 and 35,000 files
  python benchmarks/main_tree.py --files 100000
  python benchmarks/main_tree.py --check              # exit 1 over --limit

Timings depend on the machine, so this isn't part of the CI gate
(benchmarks/scale.py): run it by hand after changing how the main tree
orders or refreshes rows. The app-data folder and log point into a
temporary folder, never the real ones. Needs a display (Tk).

Exit codes: 0 ok, 1 with --check, a timing over --limit seconds.
"""

import argparse
import logging
import os
import random
import string
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SEED = 7
DEFAULT_FILES = (10_000, 35_000)
DEFAULT_LIMIT = 1.0  # seconds: roadmap P1-7's target at 35,000 files


def _folder(n_files):
    from storage_scanner.models import Node

    rng = random.Random(SEED)
    folder = Node(os.path.join(tempfile.gettempdir(), "big"), "big")
    for _ in range(n_files):
        name = "".join(rng.choices(string.ascii_letters + string.digits, k=12)) + ".bin"
        size = rng.randrange(1, 1 << 30)
        folder.add_file(name, size, size)
    folder.size = folder.alloc_size = sum(folder.file_sizes)
    folder.file_count = n_files
    return folder


def _app(tk_root):
    """StorageScannerApp with only what the main tree needs (its theme and
    tree): no history database, no update check, no other windows."""
    from tkinter import StringVar

    from storage_scanner.app import StorageScannerApp
    from storage_scanner.settings import apply_theme

    class TreeOnly(StorageScannerApp):
        def __init__(self, root):
            self.root = root
            self.root_node = None
            self.scan_thread = None
            self._previous_folder_sizes = {}
            self.node_by_iid = {}
            self._heat_tags = set()
            self._sort_key = "size"
            self._sort_reverse = True
            self.status_var = StringVar(master=root)
            apply_theme(root)
            self._build_tree()

    return TreeOnly(tk_root)


def measure(n_files):
    """{scenario: seconds} for one folder of n_files files."""
    from tkinter import Tk

    tk_root = Tk()
    tk_root.geometry("960x640")
    try:
        app = _app(tk_root)
        folder = _folder(n_files)
        app.root_node = folder
        root_iid = app._insert_node("", folder, parent_size=folder.size or 1)
        app.tree.item(root_iid, open=True)
        app._populate_children(root_iid, folder)
        tk_root.update()

        def timed(action):
            start = time.perf_counter()
            action()
            tk_root.update_idletasks()
            return time.perf_counter() - start

        return {
            "sort_name": timed(lambda: app._sort_by("name")),
            "sort_size": timed(lambda: app._sort_by("size")),
            "delete_first": timed(
                lambda: app._remove_main_tree_row(app.tree.get_children(root_iid)[0])
            ),
        }
    finally:
        tk_root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--files", type=int, nargs="+", default=list(DEFAULT_FILES), help="rows in the folder"
    )
    parser.add_argument("--check", action="store_true", help="fail when a timing is over --limit")
    parser.add_argument("--limit", type=float, default=DEFAULT_LIMIT, help="seconds (--check)")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="storage-scanner-bench-") as sandbox:
        for name in ("LOCALAPPDATA", "XDG_DATA_HOME", "HOME"):
            os.environ[name] = os.path.join(sandbox, "appdata")
        os.environ["STORAGE_SCANNER_LOG_DIR"] = os.path.join(sandbox, "logs")

        over = []
        try:
            for n_files in args.files:
                for scenario, seconds in measure(n_files).items():
                    print(f"  {n_files:>9,} files  {scenario:<13} {seconds:8.3f} s")
                    if seconds > args.limit:
                        over.append((n_files, scenario, seconds))
        finally:
            logging.shutdown()  # the app's log file is in `sandbox`

    if args.check and over:
        for n_files, scenario, seconds in over:
            print(
                f"TOO SLOW: {scenario} at {n_files:,} files took {seconds:.2f} s "
                f"(limit {args.limit:.2f} s)",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
