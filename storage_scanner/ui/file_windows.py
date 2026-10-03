"""Largest Files and File Types Breakdown windows.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Largest
Files, and the files of one extension opened from File Types, are both a
FileListWindow (ui/file_list_view.py).
"""

import heapq
import os
from collections import defaultdict
from operator import attrgetter
from tkinter import BOTH, END, TOP, E, Toplevel, W, X, ttk
from typing import NamedTuple

from storage_scanner.formatting import bar, human_size
from storage_scanner.logging_setup import logger
from storage_scanner.models import FileNode, iter_file_rows
from storage_scanner.platform_support import resource_path
from storage_scanner.settings import COLORS, heat_color, px
from storage_scanner.ui.file_list_view import FileListWindow

NO_EXTENSION = "(no extension)"

# How many of one extension's files its list shows: enough to find the big
# ones, few enough that a 200,000-file extension doesn't stall Tk.
EXTENSION_FILE_LIMIT = 1000


class FileList(NamedTuple):
    nodes: list  # the files listed, largest first
    matched: int  # how many files matched, listed or not
    size: int  # their bytes


def extension_of(name):
    """A file's type as File Types groups it: its lowercased extension, or
    NO_EXTENSION. Takes a name or a whole path."""
    return os.path.splitext(name)[1].lower() or NO_EXTENSION


def extension_totals(root):
    """({extension: bytes}, {extension: file count}) across `root`'s tree."""
    sizes = defaultdict(int)
    counts = defaultdict(int)
    for folder, indexes in iter_file_rows(root):
        names, file_sizes = folder.file_names, folder.file_sizes
        for i in indexes:
            ext = extension_of(names[i])
            sizes[ext] += file_sizes[i]
            counts[ext] += 1
    return sizes, counts


def largest_files(root, count):
    """The `count` largest files in `root`'s tree, largest first."""
    top = heapq.nlargest(
        count,
        (FileNode(folder, i) for folder, rows in iter_file_rows(root) for i in rows),
        key=attrgetter("size"),
    )
    return FileList(top, len(top), sum(node.size for node in top))


def files_with_extension(root, ext, limit):
    """The files in `root`'s tree whose extension_of() is `ext`: the `limit`
    largest, largest first, with the count and bytes of all of them."""
    matches = [
        (folder, i)
        for folder, indexes in iter_file_rows(root)
        for i in indexes
        if extension_of(folder.file_names[i]) == ext
    ]
    size = sum(folder.file_ints[2 * i] for folder, i in matches)
    top = heapq.nlargest(limit, matches, key=lambda row: row[0].file_ints[2 * row[1]])
    return FileList([FileNode(folder, i) for folder, i in top], len(matches), size)


class FileWindowsMixin:
    def show_top_files(self, count=None):
        if not self.root_node:
            return
        if count is None:
            try:
                count = int(self.top_count_var.get())
            except (ValueError, AttributeError):
                count = 25

        scan_tree = self.root_node
        if not scan_tree.file_count:
            self.status_var.set("No files found.")
            return

        # Reuses one window (_top_win) so changing the dropdown doesn't
        # stack windows.
        FileListWindow(
            self,
            "_top_win",
            source="Largest Files",
            scan_tree=scan_tree,
            collect=lambda: largest_files(scan_tree, count),
            title=lambda listed, _size: f"Top {listed} Largest Files",
            heading=f"Largest files under {scan_tree.path}",
        )

    def show_extension_files(self, scan_tree, ext):
        """The files of one File Types row, largest first, in their own
        window (_type_files_win, replaced by the next one opened)."""
        FileListWindow(
            self,
            "_type_files_win",
            source="File Types",
            scan_tree=scan_tree,
            collect=lambda: files_with_extension(scan_tree, ext, EXTENSION_FILE_LIMIT),
            title=lambda count, size: (
                f"{ext} Files — {human_size(size)} in {count:,} file{'' if count == 1 else 's'}"
            ),
            heading=f"{ext} files under {scan_tree.path}, largest first",
            # A deleted folder may have held some; a file of this type that
            # wasn't listed changes the count and the files past the limit.
            recount_for=lambda node: node.is_dir or extension_of(node.path) == ext,
        )

    # -- File-type breakdown ----------------------------------------------- #
    def show_file_types(self):
        if not self.root_node:
            return

        # Aggregate bytes + counts by lowercased extension across the tree.
        scan_tree = self.root_node
        sizes, counts = extension_totals(scan_tree)
        rows = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
        total = scan_tree.size or 1

        # Reuse one window so re-opening doesn't stack them.
        existing = getattr(self, "_types_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._types_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"File Types — {len(rows)} extensions")
        win.geometry(f"{px(760)}x{px(520)}")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("File Types window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            text=f"Space by file type under {scan_tree.path}"
            "   (double-click a type to list its files)",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("ext", "size", "percent", "files")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("ext", text="Type")
        tv.heading("size", text="Size")
        tv.heading("percent", text="% of Total")
        tv.heading("files", text="Files")
        tv.column("ext", width=px(150), anchor=W, stretch=False)
        tv.column("size", width=px(110), anchor=E, stretch=False)
        tv.column("percent", width=px(260), anchor=W, stretch=True)
        tv.column("files", width=px(90), anchor=E, stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        heat_seen = set()

        def heat_tag(fraction):
            bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
            name = f"heat{bucket}"
            if name not in heat_seen:
                tv.tag_configure(name, foreground=heat_color(bucket / 24))
                heat_seen.add(name)
            return name

        iid_to_ext = {}
        for index, (ext, size) in enumerate(rows):
            fraction = size / total
            percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
            iid = tv.insert(
                "",
                END,
                values=(ext, human_size(size), percent, f"{counts[ext]:,}"),
                tags=(heat_tag(fraction), "odd" if index % 2 else "even"),
            )
            iid_to_ext[iid] = ext

        def open_focused(_event=None):
            ext = iid_to_ext.get(tv.focus())
            if ext is not None:
                self.show_extension_files(scan_tree, ext)
            return "break"

        tv.bind("<Double-1>", open_focused)
        tv.bind("<Return>", open_focused)
