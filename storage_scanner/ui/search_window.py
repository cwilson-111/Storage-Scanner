"""Search & Filter window: find files/folders across the scanned tree by
name, extension, size range, and modified-date range.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

from datetime import datetime
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    E,
    StringVar,
    Toplevel,
    W,
    X,
    messagebox,
    ttk,
)
from typing import Union

from storage_scanner.delete_service import DeleteRequest
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.models import FileNode, Node
from storage_scanner.platform_support import FILE_MANAGER_NAME, TRASH_NAME, resource_path
from storage_scanner.search import largest_matches, parse_size
from storage_scanner.settings import COLORS, px
from storage_scanner.ui.app_state import AppMixin

# Rows a search lists: the largest matches. Listing every match took the
# window about 3.5 s for C:\Windows's 311,000 items, more for a whole drive.
SEARCH_ROW_LIMIT = 5000


class SearchMixin(AppMixin):
    def show_search_window(self):
        if not self.root_node:
            return

        existing = getattr(self, "_search_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._search_win = win
        scan_tree = self.root_node  # what every row here is from
        win.configure(bg=COLORS["bg"])
        win.title("Search & Filter")
        win.geometry(f"{px(920)}x{px(600)}")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Search window iconbitmap failed", exc_info=True)

        name_var = StringVar()
        ext_var = StringVar()
        min_size_var = StringVar()
        max_size_var = StringVar()
        after_var = StringVar()
        before_var = StringVar()
        summary_var = StringVar(
            value=f"Searching under {self.root_node.path} — enter filters and click Search."
        )

        row1 = ttk.Frame(win, padding=(10, 8, 10, 4))
        row1.pack(side=TOP, fill=X)
        ttk.Label(row1, text="Name contains:").grid(row=0, column=0, sticky=W, padx=(0, 4))
        ttk.Entry(row1, textvariable=name_var, width=20).grid(
            row=0, column=1, sticky=W, padx=(0, 14)
        )
        ttk.Label(row1, text="Extensions (csv):").grid(row=0, column=2, sticky=W, padx=(0, 4))
        ttk.Entry(row1, textvariable=ext_var, width=16).grid(
            row=0, column=3, sticky=W, padx=(0, 14)
        )
        ttk.Label(row1, text="Min size:").grid(row=0, column=4, sticky=W, padx=(0, 4))
        ttk.Entry(row1, textvariable=min_size_var, width=10).grid(
            row=0, column=5, sticky=W, padx=(0, 14)
        )
        ttk.Label(row1, text="Max size:").grid(row=0, column=6, sticky=W, padx=(0, 4))
        ttk.Entry(row1, textvariable=max_size_var, width=10).grid(row=0, column=7, sticky=W)

        row2 = ttk.Frame(win, padding=(10, 0, 10, 8))
        row2.pack(side=TOP, fill=X)
        ttk.Label(row2, text="Modified after (YYYY-MM-DD):").grid(
            row=0, column=0, sticky=W, padx=(0, 4)
        )
        ttk.Entry(row2, textvariable=after_var, width=12).grid(
            row=0, column=1, sticky=W, padx=(0, 14)
        )
        ttk.Label(row2, text="Modified before (YYYY-MM-DD):").grid(
            row=0, column=2, sticky=W, padx=(0, 4)
        )
        ttk.Entry(row2, textvariable=before_var, width=12).grid(
            row=0, column=3, sticky=W, padx=(0, 14)
        )
        ttk.Button(row2, text="Search", command=lambda: run_search()).grid(
            row=0, column=4, padx=(14, 4)
        )
        ttk.Button(row2, text="Clear", command=lambda: clear_filters()).grid(row=0, column=5)

        ttk.Label(
            win, textvariable=summary_var, style="Accent.TLabel", padding=(10, 0, 10, 8)
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("kind", "name", "size", "modified", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tv.heading("kind", text="Kind")
        tv.heading("name", text="Name")
        tv.heading("size", text="Size")
        tv.heading("modified", text="Modified")
        tv.heading("path", text="Path")
        tv.column("kind", width=px(55), anchor=W, stretch=False)
        tv.column("name", width=px(190), anchor=W, stretch=False)
        tv.column("size", width=px(90), anchor=E, stretch=False)
        tv.column("modified", width=px(100), anchor=W, stretch=False)
        tv.column("path", width=px(380), anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        iid_to_node: dict[str, Union[Node, FileNode]] = {}
        # Every match's count and bytes, listed or not (only the largest
        # SEARCH_ROW_LIMIT get a row), less the listed ones deleted since.
        totals = {"matched": 0, "size": 0}

        def summarize():
            if totals["matched"]:
                text = f"{totals['matched']:,} result(s)  —  {human_size(totals['size'])} total"
                if len(iid_to_node) < totals["matched"]:
                    text += f"  —  showing the {len(iid_to_node):,} largest"
                summary_var.set(text)
            else:
                summary_var.set("No matches.")

        def forget_deleted(deleted):
            gone = [iid for iid, node in iid_to_node.items() if deleted.covers(node)]
            for iid in gone:
                node = iid_to_node.pop(iid)
                totals["matched"] -= 1
                totals["size"] -= node.size
                tv.delete(iid)
            if gone:
                summarize()

        self._watch_deletions(win, forget_deleted)

        def clear_filters():
            for var in (name_var, ext_var, min_size_var, max_size_var, after_var, before_var):
                var.set("")

        def run_search():
            try:
                min_size = parse_size(min_size_var.get())
                max_size = parse_size(max_size_var.get())
            except ValueError as exc:
                messagebox.showerror("Search & Filter", str(exc), parent=win)
                return

            mtime_after = mtime_before = None
            try:
                if after_var.get().strip():
                    mtime_after = datetime.strptime(after_var.get().strip(), "%Y-%m-%d").timestamp()
                if before_var.get().strip():
                    # End-of-day so "before 2024-01-05" still includes that day.
                    mtime_before = (
                        datetime.strptime(before_var.get().strip(), "%Y-%m-%d").timestamp() + 86399
                    )
            except ValueError:
                messagebox.showerror(
                    "Search & Filter", "Dates must be in YYYY-MM-DD format.", parent=win
                )
                return

            extensions = ext_var.get().split(",") if ext_var.get().strip() else None
            name_query = name_var.get().strip() or None

            result = largest_matches(
                scan_tree,
                SEARCH_ROW_LIMIT,
                name_query=name_query,
                extensions=extensions,
                min_size=min_size,
                max_size=max_size,
                mtime_after=mtime_after,
                mtime_before=mtime_before,
            )
            totals.update(matched=result.matched, size=result.size)

            tv.delete(*tv.get_children())
            iid_to_node.clear()
            for index, node in enumerate(result.nodes):
                modified = (
                    datetime.fromtimestamp(node.mtime).strftime("%Y-%m-%d") if node.mtime else "—"
                )
                iid = tv.insert(
                    "",
                    END,
                    values=(
                        "Folder" if node.is_dir else "File",
                        node.name,
                        human_size(node.size),
                        modified,
                        node.path,
                    ),
                    tags=("odd" if index % 2 else "even",),
                )
                iid_to_node[iid] = node

            summarize()

        win.bind("<Return>", lambda _event: run_search())

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def reveal_selected():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self._reveal(node.path, is_dir=node.is_dir)

        def copy_selected_path():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self.root.clipboard_clear()
                self.root.clipboard_append(node.path)

        def delete_selected():
            selected = list(tv.selection())
            nodes = [iid_to_node[iid] for iid in selected if iid in iid_to_node]
            if not nodes or self._refuse_delete_during_scan(parent=win):
                return

            kind = "item" if len(nodes) == 1 else "items"
            if not messagebox.askyesno(
                f"Delete to {TRASH_NAME}",
                f"Send {len(nodes)} selected {kind} to the {TRASH_NAME}?",
                icon="warning",
                parent=win,
            ):
                return
            # Deleted rows leave this list through forget_deleted.
            self._delete_nodes(
                [DeleteRequest(node, "Search & Filter", tree=scan_tree) for node in nodes], win
            )

        ttk.Button(button_bar, text=f"Reveal in {FILE_MANAGER_NAME}", command=reveal_selected).pack(
            side=LEFT
        )
        ttk.Button(button_bar, text="Copy Path", command=copy_selected_path).pack(side=LEFT, padx=6)
        ttk.Button(button_bar, text="Delete Selected", command=delete_selected).pack(side=RIGHT)

        tv.bind("<Double-1>", lambda _e: reveal_selected())
