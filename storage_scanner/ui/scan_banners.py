"""What a finished scan says about itself above the tree: the scan
details strip, the Turbo Scan fallback banner, and the unreadable-paths
banner with its list window.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    Toplevel,
    W,
    X,
    ttk,
)
from typing import Optional

from storage_scanner import (
    turbo_scan,
)
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    resource_path,
)
from storage_scanner.settings import COLORS, px
from storage_scanner.ui.app_state import AppMixin

# The scan-details strip's second row: what the scan found, as opposed to
# how it ran. One row of everything outgrew the default window width.
_SCAN_OUTCOME_FIELDS = ("Unreadable paths", "Result")


class ScanBannersMixin(AppMixin):
    def _show_scan_details(self, report, inaccessible_nodes):
        frame = self._scan_details_frame
        for child in frame.winfo_children():
            child.destroy()

        self._last_inaccessible_paths = inaccessible_nodes
        fields, complete = turbo_scan.scan_indicators(report, len(inaccessible_nodes))
        rows = (
            [field for field in fields if field[0] not in _SCAN_OUTCOME_FIELDS],
            [field for field in fields if field[0] in _SCAN_OUTCOME_FIELDS],
        )
        for row_fields in rows:
            row = ttk.Frame(frame)
            row.pack(side=TOP, fill=X)
            for label, value in row_fields:
                ttk.Label(row, text=f"{label}:", foreground=COLORS["muted"]).pack(side=LEFT)
                if label == "Result":
                    color = COLORS["good"] if complete else COLORS["warning"]
                else:
                    color = COLORS["fg"]
                ttk.Label(row, text=value, foreground=color).pack(side=LEFT, padx=(4, 0))
                if label == "Unreadable paths" and inaccessible_nodes:
                    ttk.Button(row, text="View", command=self._show_inaccessible_paths_window).pack(
                        side=LEFT, padx=(6, 0)
                    )
                ttk.Label(row, text="·", foreground=COLORS["muted"]).pack(side=LEFT, padx=8)
            # Drop the trailing separator after the row's last field.
            row.winfo_children()[-1].destroy()

        frame.pack(side=BOTTOM, fill=X, after=self._statusbar_frame)

    def _hide_scan_details(self):
        self._scan_details_frame.pack_forget()

    def _show_turbo_fallback_banner(self, reason):
        """Dismissible banner explaining a scan silently used the
        Compatible engine after Turbo Scan failed -- same pattern as
        app.py's _show_update_banner. Never hidden: a fallback should
        always be visible, not silently swallowed (see storage_scanner.
        turbo_scan.scan_with_best_engine's docstring)."""
        self._dismiss_turbo_fallback_banner()

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._turbo_fallback_banner: Optional[ttk.Frame] = banner

        def dismiss():
            self._dismiss_turbo_fallback_banner()

        ttk.Label(
            banner,
            style="Accent.TLabel",
            text=(
                "⚠ Turbo Scan wasn't available for this scan — used the "
                f"regular scan instead ({reason})."
            ),
        ).pack(side=LEFT)
        ttk.Button(banner, text="✕", width=3, command=dismiss).pack(side=RIGHT)

        banner.pack(side=TOP, fill=X, before=self.toolbar_frame)

    def _dismiss_turbo_fallback_banner(self):
        banner = getattr(self, "_turbo_fallback_banner", None)
        if banner is not None:
            banner.destroy()
            self._turbo_fallback_banner = None

    def _show_inaccessible_paths_banner(self, inaccessible_nodes):
        """Dismissible banner listing how many paths this scan couldn't
        read at all -- a directory that failed to list has no children in
        the tree, so its entire subtree is silently missing from the
        total, not just underestimated. Being an Administrator doesn't
        guarantee access to every folder (System Volume Information is the
        classic example: its ACL excludes the Administrators group
        outright), so this can happen even on an elevated scan. Same
        dismissible-banner pattern as _show_turbo_fallback_banner, but the
        list itself only ever comes from this specific scan's own results
        -- see _show_inaccessible_paths_window."""
        self._dismiss_inaccessible_paths_banner()

        self._last_inaccessible_paths = inaccessible_nodes

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._inaccessible_paths_banner: Optional[ttk.Frame] = banner

        count = len(inaccessible_nodes)
        noun = "path" if count == 1 else "paths"

        def dismiss():
            self._dismiss_inaccessible_paths_banner()

        ttk.Label(
            banner,
            style="Accent.TLabel",
            text=(
                f"⚠ {count} {noun} couldn't be read (permissions) — "
                f"the total above may be missing whatever they contain."
            ),
        ).pack(side=LEFT)
        ttk.Button(
            banner,
            text="View List",
            command=self._show_inaccessible_paths_window,
        ).pack(side=LEFT, padx=(10, 0))
        ttk.Button(banner, text="✕", width=3, command=dismiss).pack(side=RIGHT)

        banner.pack(side=TOP, fill=X, before=self.toolbar_frame)

    def _dismiss_inaccessible_paths_banner(self):
        banner = getattr(self, "_inaccessible_paths_banner", None)
        if banner is not None:
            banner.destroy()
            self._inaccessible_paths_banner = None

    def _show_inaccessible_paths_window(self):
        inaccessible_nodes = getattr(self, "_last_inaccessible_paths", [])

        existing = getattr(self, "_inaccessible_paths_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._inaccessible_paths_win = win
        win.configure(bg=COLORS["bg"])
        win.title(f"Couldn't Be Read — {len(inaccessible_nodes)} path(s)")
        win.geometry(f"{px(780)}x{px(420)}")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Inaccessible Paths window iconbitmap failed", exc_info=True)

        ttk.Label(
            win,
            padding=(10, 8),
            style="Accent.TLabel",
            text=(
                "These paths returned a permission error during the last scan, so "
                "they (and anything inside them, for a folder) were counted as 0 "
                "bytes rather than skipped from the total silently. A common cause: "
                "a folder's permissions exclude even Administrators (e.g. "
                "C:\\System Volume Information, or a backup tool's own storage) — "
                "running this app elevated doesn't override that."
            ),
            wraplength=px(740),
            justify=LEFT,
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("path", "type")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("path", text="Path")
        tv.heading("type", text="Type")
        tv.column("path", width=px(620), anchor=W, stretch=True)
        tv.column("type", width=px(80), anchor=W, stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        iid_to_node = {}
        for index, inaccessible_node in enumerate(
            sorted(inaccessible_nodes, key=lambda n: n.path.lower())
        ):
            iid = tv.insert(
                "",
                END,
                values=(inaccessible_node.path, "Folder" if inaccessible_node.is_dir else "File"),
                tags=("odd" if index % 2 else "even",),
            )
            iid_to_node[iid] = inaccessible_node

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def reveal_selected():
            sel = tv.focus()
            inaccessible_node = iid_to_node.get(sel)
            if inaccessible_node:
                self._reveal(inaccessible_node.path, is_dir=inaccessible_node.is_dir)

        def copy_selected_path():
            sel = tv.focus()
            inaccessible_node = iid_to_node.get(sel)
            if inaccessible_node:
                self.root.clipboard_clear()
                self.root.clipboard_append(inaccessible_node.path)

        ttk.Button(
            button_bar,
            text=f"Reveal in {FILE_MANAGER_NAME}",
            command=reveal_selected,
        ).pack(side=LEFT)
        ttk.Button(button_bar, text="Copy Path", command=copy_selected_path).pack(side=LEFT, padx=6)

        tv.bind("<Double-1>", lambda _e: reveal_selected())
