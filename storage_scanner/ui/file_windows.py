"""Largest Files and File Types Breakdown windows.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import os
from collections import defaultdict
from tkinter import BOTH, END, TOP, E, Toplevel, W, X, ttk

from storage_scanner.formatting import bar, human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, resource_path
from storage_scanner.settings import COLORS, heat_color


class FileWindowsMixin:
    def show_top_files(self, count=None):
        if not self.root_node:
            return
        if count is None:
            try:
                count = int(self.top_count_var.get())
            except (ValueError, AttributeError):
                count = 25

        # Collect every file in the scanned tree (iterative; deep-tree safe).
        files = []
        stack = [self.root_node]
        while stack:
            node = stack.pop()
            if node.is_dir:
                stack.extend(node.children)
            else:
                files.append(node)
        files.sort(key=lambda n: n.size, reverse=True)
        top = files[:count]
        if not top:
            self.status_var.set("No files found.")
            return

        # Reuse one window so changing the dropdown doesn't stack windows.
        existing = getattr(self, "_top_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._top_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"Top {len(top)} Largest Files")
        win.geometry("820x520")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Largest Files window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            text=f"Largest files under {self.root_node.path}"
            f"   (double-click to reveal in {FILE_MANAGER_NAME})",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("rank", "size", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("rank", text="#")
        tv.heading("size", text="Size")
        tv.heading("path", text="Path")
        tv.column("rank", width=44, anchor=E, stretch=False)
        tv.column("size", width=100, anchor=E, stretch=False)
        tv.column("path", width=640, anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        # Heat each row by its size relative to the largest file in the list.
        if not top:
            self.status_var.set("No files found.")
            return
        max_size = top[0].size or 1
        heat_seen = set()

        def heat_tag(fraction):
            bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
            name = f"heat{bucket}"
            if name not in heat_seen:
                tv.tag_configure(name, foreground=heat_color(bucket / 24))
                heat_seen.add(name)
            return name

        iid_to_path = {}
        for rank, node in enumerate(top, start=1):
            iid = tv.insert(
                "",
                END,
                values=(rank, human_size(node.size), node.path),
                tags=(heat_tag(node.size / max_size), "odd" if rank % 2 else "even"),
            )
            iid_to_path[iid] = node.path

        def on_double(_e):
            sel = tv.focus()
            if sel in iid_to_path:
                self._reveal(iid_to_path[sel], is_dir=False)

        tv.bind("<Double-1>", on_double)

    # -- File-type breakdown ----------------------------------------------- #
    def show_file_types(self):
        if not self.root_node:
            return

        # Aggregate bytes + counts by lowercased extension across the tree.
        sizes = defaultdict(int)
        counts = defaultdict(int)
        stack = [self.root_node]
        while stack:
            node = stack.pop()
            if node.is_dir:
                stack.extend(node.children)
            else:
                ext = os.path.splitext(node.name)[1].lower() or "(no extension)"
                sizes[ext] += node.size
                counts[ext] += 1
        rows = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
        total = self.root_node.size or 1

        # Reuse one window so re-opening doesn't stack them.
        existing = getattr(self, "_types_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._types_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"File Types — {len(rows)} extensions")
        win.geometry("760x520")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("File Types window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            text=f"Space by file type under {self.root_node.path}",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("ext", "size", "percent", "files")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("ext", text="Type")
        tv.heading("size", text="Size")
        tv.heading("percent", text="% of Total")
        tv.heading("files", text="Files")
        tv.column("ext", width=150, anchor=W, stretch=False)
        tv.column("size", width=110, anchor=E, stretch=False)
        tv.column("percent", width=260, anchor=W, stretch=True)
        tv.column("files", width=90, anchor=E, stretch=False)

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

        for index, (ext, size) in enumerate(rows):
            fraction = size / total
            percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
            tv.insert(
                "",
                END,
                values=(ext, human_size(size), percent, f"{counts[ext]:,}"),
                tags=(heat_tag(fraction), "odd" if index % 2 else "even"),
            )
