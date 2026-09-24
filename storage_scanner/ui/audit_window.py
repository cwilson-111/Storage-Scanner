"""Audit Log window: a durable, read-only record of every delete/recycle
action the app has performed, across every window that can delete something.

A mixin composed into StorageScannerApp (storage_scanner/app.py). There is
deliberately no "Undo" button here — see storage_scanner/audit.py's
docstring for why a one-click restore-from-Trash isn't offered. Recovery
goes through the OS's own Recycle Bin/Trash (which every deletion in this
app already routes through); this window tells you what to look for there.
"""

from tkinter import BOTH, BOTTOM, END, LEFT, RIGHT, TOP, Toplevel, X, ttk

from history import get_audit_log
from storage_scanner.file_ops import open_trash
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import TRASH_NAME, resource_path
from storage_scanner.settings import COLORS


class AuditMixin:
    def show_audit_log(self):
        existing = getattr(self, "_audit_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._audit_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Audit Log")
        win.geometry("1000x560")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Audit Log window iconbitmap failed", exc_info=True)

        entries = get_audit_log(limit=500)
        reclaimed = sum(size for _c, _s, _a, _p, _d, size, success, _e in entries if success)
        failed_count = sum(1 for row in entries if not row[6])

        ttk.Label(
            win,
            padding=(10, 8),
            style="Accent.TLabel",
            text=(
                f"{len(entries):,} logged action(s) — "
                f"{human_size(reclaimed)} sent to {TRASH_NAME}"
                + (f"  |  {failed_count:,} failed" if failed_count else "")
            ),
        ).pack(side=TOP, fill=X)

        ttk.Label(
            win,
            padding=(10, 0, 10, 8),
            foreground=COLORS["muted"],
            text=(
                f"This is a record of what was deleted, not an undo button — "
                f"everything here went to {TRASH_NAME}, which is where to "
                f"restore something from."
            ),
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("date", "source", "kind", "path", "size", "result")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("date", text="Date")
        tv.heading("source", text="From")
        tv.heading("kind", text="Kind")
        tv.heading("path", text="Path")
        tv.heading("size", text="Size")
        tv.heading("result", text="Result")
        tv.column("date", width=140, anchor="w", stretch=False)
        tv.column("source", width=150, anchor="w", stretch=False)
        tv.column("kind", width=60, anchor="w", stretch=False)
        tv.column("path", width=440, anchor="w", stretch=True)
        tv.column("size", width=90, anchor="e", stretch=False)
        tv.column("result", width=90, anchor="w", stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("failed", foreground=COLORS["error"])

        path_by_iid = {}

        if not entries:
            tv.insert("", END, values=("—", "—", "—", "Nothing has been deleted yet.", "", ""))
        else:
            for index, row in enumerate(entries):
                created_at, source, _action, path, is_dir, size_bytes, success, error_message = row
                date_text = created_at.replace("T", " ")
                result_text = "Recycled" if success else f"Failed: {error_message or 'unknown'}"
                iid = tv.insert(
                    "",
                    END,
                    values=(
                        date_text,
                        source,
                        "Folder" if is_dir else "File",
                        path,
                        human_size(size_bytes),
                        result_text,
                    ),
                    tags=("failed" if not success else "", "odd" if index % 2 else "even"),
                )
                path_by_iid[iid] = path

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def copy_selected_path():
            sel = tv.focus()
            path = path_by_iid.get(sel)
            if path:
                self.root.clipboard_clear()
                self.root.clipboard_append(path)

        def open_trash_clicked():
            if not open_trash():
                self.status_var.set(f"Could not open {TRASH_NAME}.")

        ttk.Button(button_bar, text="Copy Path", command=copy_selected_path).pack(side=LEFT)
        ttk.Button(
            button_bar,
            text=f"Open {TRASH_NAME}",
            command=open_trash_clicked,
        ).pack(side=RIGHT)
