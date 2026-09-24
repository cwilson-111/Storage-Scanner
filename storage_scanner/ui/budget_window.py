"""Storage Budgets window: manage per-folder size alerts.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Budgets
are created from the main tree's right-click "Set Budget…" (see
MainWindowMixin) — this window lists what's defined, shows status against
each one's last known scan, and lets you remove one.
"""

from tkinter import BOTH, BOTTOM, END, LEFT, RIGHT, TOP, Toplevel, X, messagebox, ttk

from history import delete_budget, get_latest_scan_snapshot, list_budgets
from storage_scanner.budgets import check_all_budgets
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import resource_path
from storage_scanner.settings import COLORS


class BudgetMixin:
    def _check_budgets_on_launch(self):
        breaches = check_all_budgets()
        if breaches:
            self._show_budget_banner(breaches)

    def _show_budget_banner(self, breaches):
        existing = getattr(self, "_budget_banner", None)
        if existing is not None:
            existing.destroy()

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._budget_banner = banner

        count = len(breaches)
        noun = "budget" if count == 1 else "budgets"

        def view_details():
            self.show_budgets()

        def dismiss():
            banner.destroy()
            self._budget_banner = None

        ttk.Label(
            banner,
            style="Accent.TLabel",
            text=f"⚠ {count} storage {noun} exceeded.",
        ).pack(side=LEFT)
        ttk.Button(banner, text="View Details", command=view_details).pack(side=LEFT, padx=(10, 0))
        ttk.Button(banner, text="✕", width=3, command=dismiss).pack(side=RIGHT)

        banner.pack(side=TOP, fill=X, before=self.toolbar_frame)

    def show_budgets(self):
        existing = getattr(self, "_budgets_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._budgets_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Storage Budgets")
        win.geometry("780x420")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Storage Budgets window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            style="Accent.TLabel",
            text=(
                "Alerts fire when you scan a budgeted folder or launch the "
                "app — not continuously in the background."
            ),
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("path", "threshold", "current", "status", "as_of")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("path", text="Folder")
        tv.heading("threshold", text="Budget")
        tv.heading("current", text="Last Known Size")
        tv.heading("status", text="Status")
        tv.heading("as_of", text="As Of")
        tv.column("path", width=300, anchor="w", stretch=True)
        tv.column("threshold", width=90, anchor="e", stretch=False)
        tv.column("current", width=110, anchor="e", stretch=False)
        tv.column("status", width=80, anchor="w", stretch=False)
        tv.column("as_of", width=150, anchor="w", stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("over", foreground=COLORS["error"])
        tv.tag_configure("ok", foreground=COLORS["good"])
        tv.tag_configure("unknown", foreground=COLORS["muted"])

        id_by_iid = {}

        def populate():
            tv.delete(*tv.get_children())
            id_by_iid.clear()
            rows = list_budgets()
            if not rows:
                tv.insert(
                    "",
                    END,
                    values=(
                        "No budgets set yet — right-click a folder in the main tree.",
                        "",
                        "",
                        "",
                        "",
                    ),
                )
                return
            for index, (budget_id, path, threshold_bytes, _created_at) in enumerate(rows):
                snapshot = get_latest_scan_snapshot(path)
                if snapshot is None:
                    current_text, status, as_of_text, tag = (
                        "—",
                        "Unknown",
                        "never scanned",
                        "unknown",
                    )
                else:
                    scanned_at, total_size, _file_count, _folder_count = snapshot
                    current_text = human_size(total_size)
                    as_of_text = scanned_at.replace("T", " ")
                    if total_size > threshold_bytes:
                        status, tag = "Over", "over"
                    else:
                        status, tag = "OK", "ok"
                iid = tv.insert(
                    "",
                    END,
                    values=(path, human_size(threshold_bytes), current_text, status, as_of_text),
                    tags=(tag, "odd" if index % 2 else "even"),
                )
                id_by_iid[iid] = budget_id

        populate()

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def remove_selected():
            sel = tv.focus()
            budget_id = id_by_iid.get(sel)
            if budget_id is None:
                return
            path = tv.item(sel)["values"][0]
            if not messagebox.askyesno(
                "Remove Budget",
                f"Remove the budget for:\n{path}?",
                parent=win,
            ):
                return
            delete_budget(budget_id)
            populate()

        ttk.Button(button_bar, text="Remove Selected", command=remove_selected).pack(side=RIGHT)
