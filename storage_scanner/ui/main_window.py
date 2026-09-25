"""Main window: header, toolbar, tree view, scan lifecycle, row actions.

A mixin composed into StorageScannerApp (storage_scanner/app.py) alongside
DuplicatesMixin, HistoryMixin, and FileWindowsMixin — split out so each
window/feature area can be read and tested on its own.
"""

import json
import os
import queue
import subprocess
import threading
import time
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    BooleanVar,
    E,
    Menu,
    StringVar,
    Toplevel,
    W,
    X,
    filedialog,
    messagebox,
    simpledialog,
    ttk,
)

from history import get_app_metadata, set_app_metadata, set_budget
from storage_scanner import turbo_scan
from storage_scanner.audit import recycle_and_log
from storage_scanner.csv_to_parquet import compress_csv_to_parquet
from storage_scanner.csv_to_parquet import default_output_path as default_parquet_path
from storage_scanner.csv_to_xlsx import convert_csv_to_xlsx
from storage_scanner.csv_to_xlsx import default_output_path as default_xlsx_path
from storage_scanner.drive_info import is_ntfs_fixed_drive
from storage_scanner.file_ops import (
    relaunch_elevated_windows,
    run_elevated_scan_linux,
    run_elevated_scan_macos,
)
from storage_scanner.formatting import bar, human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    IS_LINUX,
    IS_MACOS,
    IS_ROOT,
    IS_WINDOWS,
    TRASH_NAME,
    resource_path,
)
from storage_scanner.scan_history import drive_capacity_bytes
from storage_scanner.scanner import find_inaccessible_paths
from storage_scanner.search import parse_size
from storage_scanner.serialization import dict_to_node
from storage_scanner.settings import COLORS, FONT_MONO_BOLD, heat_color

# The scan-details strip's second row: what the scan found, as opposed to
# how it ran. One row of everything outgrew the default window width.
_SCAN_OUTCOME_FIELDS = ("Unreadable paths", "Result")


class MainWindowMixin:
    def _build_toolbar(self):
        bar_frame = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        bar_frame.pack(side=TOP, fill=X)
        self.toolbar_frame = bar_frame

        ttk.Label(bar_frame, text="▸ LOCATION", style="Accent.TLabel").pack(side=LEFT)

        self.path_var = StringVar()
        self.path_combo = ttk.Combobox(
            bar_frame,
            textvariable=self.path_var,
            values=self._list_drives(),
        )
        self.path_combo.pack(side=LEFT, padx=6, fill=X, expand=True)
        self.path_combo.bind("<Return>", lambda e: self.start_scan())

        ttk.Button(bar_frame, text="Browse…", command=self.browse).pack(side=LEFT)
        self.scan_btn = ttk.Button(
            bar_frame,
            text="Scan",
            command=self.start_scan,
            style="Primary.TButton",
        )
        self.scan_btn.pack(side=LEFT, padx=6)
        self.cancel_btn = ttk.Button(
            bar_frame, text="Cancel", command=self.cancel_scan, state="disabled"
        )
        self.cancel_btn.pack(side=LEFT)

        if (IS_MACOS or IS_WINDOWS or IS_LINUX) and not IS_ROOT:
            self.elevate_btn = ttk.Button(
                bar_frame,
                text="🔒 Run as Admin",
                command=self._request_elevation,
            )
            self.elevate_btn.pack(side=LEFT, padx=(6, 0))

        # Enabled from launch: History & Trust, Cleanup Recommendations and
        # Settings read the databases earlier runs (and earlier versions)
        # left behind. Only the items in _scan_only_tools need this
        # session's tree; _refresh_tools_state greys those out until then.
        self.tools_btn = ttk.Button(
            bar_frame,
            text="Tools ▼",
            command=self._show_tools_menu,
        )
        self.tools_btn.pack(side=LEFT, padx=6)

        # The Cleanup Cart's own persistent indicator — kept current by
        # CartMixin._refresh_cart_indicator, called after every
        # add/remove/clear/execute (see storage_scanner/ui/cart_window.py).
        self.cart_btn = ttk.Button(bar_frame, text="🛒 Cart", command=self.show_cart)
        self.cart_btn.pack(side=LEFT)

        # Grouped into submenus that follow the order you'd actually use them
        # in — explore what's there, clean some of it up, then check history/
        # trust — rather than one flat, ever-growing list of unrelated tools.
        self.tools_menu = Menu(self.root, tearoff=0)

        # (menu, entry index) for every Tools item that works on the tree
        # from a scan in this session, rather than on saved data.
        self._scan_only_tools = []

        def add_scan_only(menu, label, command):
            menu.add_command(label=label, command=command)
            self._scan_only_tools.append((menu, menu.index("end")))

        explore_menu = Menu(self.tools_menu, tearoff=0)
        add_scan_only(explore_menu, "Treemap", self.show_treemap)
        add_scan_only(explore_menu, "Search & Filter", self.show_search_window)
        add_scan_only(explore_menu, "Largest Files", self.show_top_files)
        add_scan_only(explore_menu, "File Types Breakdown", self.show_file_types)
        self.tools_menu.add_cascade(label="Explore", menu=explore_menu)

        cleanup_menu = Menu(self.tools_menu, tearoff=0)
        add_scan_only(cleanup_menu, "Find Duplicate Files", self.show_duplicates)
        cleanup_menu.add_command(
            label="Cleanup Recommendations", command=self.show_cleanup_recommendations
        )
        self.tools_menu.add_cascade(label="Clean Up", menu=cleanup_menu)

        history_menu = Menu(self.tools_menu, tearoff=0)
        history_menu.add_command(label="Growth History", command=self.show_growth_history)
        history_menu.add_command(label="Audit Log", command=self.show_audit_log)
        history_menu.add_command(label="Storage Budgets", command=self.show_budgets)
        history_menu.add_command(label="Schedule Scans…", command=self.show_schedule_scans)
        self.tools_menu.add_cascade(label="History & Trust", menu=history_menu)

        # Data Tools works on any CSV on disk, so it's usable without a scan.
        data_menu = Menu(self.tools_menu, tearoff=0)
        data_menu.add_command(
            label="Compress CSV to Parquet…", command=self.compress_csv_to_parquet
        )
        data_menu.add_command(
            label="Convert CSV to Excel (.xlsx)…", command=self.convert_csv_to_xlsx
        )
        self.tools_menu.add_cascade(label="Data Tools", menu=data_menu)

        add_scan_only(self.tools_menu, "Export Results…", self.export_results)
        self._refresh_tools_state()  # no tree yet: scan-only items start greyed out

        # Turbo Scan (NTFS MFT fast path) is Windows-only and off by
        # default — persisted the same way as the schema_version key, via
        # history.py's app_metadata table (there's no other settings
        # storage in this app to reuse).
        if IS_WINDOWS:
            self.turbo_scan_var = BooleanVar(
                value=get_app_metadata("turbo_scan_enabled", "0") == "1"
            )
            settings_menu = Menu(self.tools_menu, tearoff=0)
            settings_menu.add_checkbutton(
                label="Turbo Scan (Experimental) — NTFS MFT fast path",
                variable=self.turbo_scan_var,
                command=self._on_toggle_turbo_scan,
            )
            self.tools_menu.add_cascade(label="Settings", menu=settings_menu)

        self.top_count_var = StringVar(value="25")
        self.top_count_combo = ttk.Combobox(
            bar_frame,
            textvariable=self.top_count_var,
            width=5,
            state="disabled",
            values=("25", "50", "100"),
        )
        self.top_count_combo.pack(side=RIGHT, padx=(0, 6))
        # Re-running with a new count is instant, so update live on selection.
        self.top_count_combo.bind("<<ComboboxSelected>>", lambda e: self.show_top_files())
        ttk.Label(bar_frame, text="TOP", style="Accent.TLabel").pack(side=RIGHT, padx=(0, 4))

        if self._initial_path:
            self.path_var.set(self._initial_path)
        else:
            drives = self._list_drives()
            if drives:
                self.path_var.set(drives[0])

    def _build_tree(self):
        container = ttk.Frame(self.root, padding=(8, 4))
        container.pack(side=TOP, fill=BOTH, expand=True)

        columns = ("size", "alloc", "percent", "items")
        self.tree = ttk.Treeview(
            container, columns=columns, show="tree headings", selectmode="browse"
        )
        # Clickable headings sort that level (and every expanded level). The
        # percent/alloc columns sort by (logical) size — within a level
        # they track together closely enough to share one sort.
        self.tree.heading("#0", text="Name", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size", command=lambda: self._sort_by("size"))
        self.tree.heading("alloc", text="On Disk", command=lambda: self._sort_by("size"))
        self.tree.heading("percent", text="% of Parent", command=lambda: self._sort_by("size"))
        self.tree.heading("items", text="Files", command=lambda: self._sort_by("items"))
        self._update_heading_arrows()

        self.tree.column("#0", width=440, anchor=W, stretch=True)
        self.tree.column("size", width=110, anchor=E, stretch=False)
        self.tree.column("alloc", width=110, anchor=E, stretch=False)
        self.tree.column("percent", width=200, anchor=W, stretch=False)
        self.tree.column("items", width=90, anchor=E, stretch=False)

        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        # A row carries up to three tags that each set a *different* option, so
        # they stack cleanly: a heat tag (foreground), a type tag (font:
        # dirs bold), and a stripe tag (background). Errors override the
        # foreground to red. Heat tags are created lazily in _heat_tag().
        self.tree.tag_configure("dir", font=FONT_MONO_BOLD)
        self.tree.tag_configure("error", foreground=COLORS["error"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("placeholder", foreground=COLORS["muted"])
        self.tree.tag_configure("link", foreground=COLORS["accent2"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("cloud", foreground=COLORS["muted"], font=FONT_MONO_BOLD)
        self.tree.tag_configure("even", background=COLORS["panel"])
        self.tree.tag_configure("odd", background=COLORS["stripe"])

        # Lazy load children when a node is expanded.
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Right-click context menu.
        self.menu = Menu(self.root, tearoff=0)
        self.menu.add_command(label=f"Open in {FILE_MANAGER_NAME}", command=self._open_in_explorer)
        self.menu.add_command(label="Copy path", command=self._copy_path)
        self.menu.add_command(label="Set Budget…", command=self._set_budget_for_selected)
        self.menu.add_command(label="Add to Cart", command=self._add_selected_to_cart)
        self.menu.add_separator()
        self.menu.add_command(label=f"Delete (to {TRASH_NAME})", command=self._delete_selected)
        self.tree.bind("<Button-3>", self._show_menu)

        # Keyboard: Delete recycles the selection, F5 re-scans.
        self.tree.bind("<Delete>", lambda e: self._delete_selected())
        self.root.bind("<F5>", lambda e: self.start_scan())

    def _build_statusbar(self):
        status = ttk.Frame(self.root, padding=(8, 2))
        status.pack(side=BOTTOM, fill=X)
        self._statusbar_frame = status
        self.status_var = StringVar(value="Pick a drive or folder, then click Scan.")
        ttk.Label(status, textvariable=self.status_var, anchor=W).pack(
            side=LEFT, fill=X, expand=True
        )
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220)

        # Scan details strip: engine, timing and completeness of the last
        # finished scan, on two rows. Packed just above the status bar by
        # _show_scan_details, removed again when the next scan starts.
        self._scan_details_frame = ttk.Frame(self.root, padding=(8, 2))

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

    # -- Drive / folder selection ----------------------------------------- #
    @staticmethod
    def _list_drives():
        if IS_MACOS:
            drives = ["/"]
            volumes = "/Volumes"
            if os.path.isdir(volumes):
                for name in sorted(os.listdir(volumes)):
                    vol_path = os.path.join(volumes, name)
                    # Skip the boot volume's own /Volumes self-link.
                    if os.path.isdir(vol_path) and not os.path.islink(vol_path):
                        drives.append(vol_path)
            return drives

        if IS_LINUX:
            drives = ["/"]
            # Removable/external media conventionally show up under one of
            # these, namespaced by username on a multi-user system --
            # unlike macOS's single /Volumes, there's no one standard
            # location, so check every plausible one. os.path.ismount()
            # filters out an empty placeholder dir with nothing actually
            # mounted there (udisks2/automount tools create these upfront).
            username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
            bases = [
                b
                for b in (
                    os.path.join("/media", username) if username else None,
                    os.path.join("/run/media", username) if username else None,
                    "/mnt",
                )
                if b
            ]
            for base in bases:
                if not os.path.isdir(base):
                    continue
                for name in sorted(os.listdir(base)):
                    mount_path = os.path.join(base, name)
                    if os.path.ismount(mount_path):
                        drives.append(mount_path)
            return drives

        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{letter}:\\"
            if os.path.exists(d):
                drives.append(d)
        return drives

    def browse(self):
        current = self.path_var.get().strip().strip('"')
        initialdir = current if os.path.isdir(current) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="Select a folder to analyze",
            initialdir=initialdir,
        )
        if chosen:
            self.path_var.set(os.path.normpath(chosen))

    def compress_csv_to_parquet(self):
        csv_path = filedialog.askopenfilename(
            title="Choose a CSV file to compress",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not csv_path:
            return

        output_path = filedialog.asksaveasfilename(
            title="Save Parquet file as",
            initialdir=os.path.dirname(csv_path),
            initialfile=os.path.basename(default_parquet_path(csv_path)),
            defaultextension=".parquet",
            filetypes=[("Parquet files", "*.parquet")],
        )
        if not output_path:
            return

        result = compress_csv_to_parquet(csv_path, output_path)
        if not result.success:
            messagebox.showerror(
                "Storage Scanner", f"Could not compress to Parquet:\n{result.error}"
            )
            return

        try:
            before = os.path.getsize(csv_path)
            after = os.path.getsize(result.output_path)
            saved = f"\n\n{human_size(before)} → {human_size(after)}" if before else ""
        except OSError:
            saved = ""
        messagebox.showinfo(
            "Storage Scanner",
            f"Compressed to:\n{result.output_path}{saved}",
        )

    def convert_csv_to_xlsx(self):
        csv_path = filedialog.askopenfilename(
            title="Choose a CSV file to convert",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not csv_path:
            return

        output_path = filedialog.asksaveasfilename(
            title="Save Excel file as",
            initialdir=os.path.dirname(csv_path),
            initialfile=os.path.basename(default_xlsx_path(csv_path)),
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx")],
        )
        if not output_path:
            return

        result = convert_csv_to_xlsx(csv_path, output_path)
        if not result.success:
            messagebox.showerror("Storage Scanner", f"Could not convert to Excel:\n{result.error}")
            return

        messagebox.showinfo("Storage Scanner", f"Converted to:\n{result.output_path}")

    def _request_elevation(self):
        current = self.path_var.get().strip().strip('"')

        if IS_MACOS or IS_LINUX:
            # Relaunching the whole GUI as root can't reliably show a
            # window on either platform (see run_elevated_scan_macos's
            # and run_elevated_scan_linux's docstrings for why -- a
            # different underlying reason on each, same practical
            # conclusion), so only the scan itself runs elevated — this
            # window stays open, unprivileged, throughout.
            target = current if os.path.isdir(current) else None
            if not target:
                messagebox.showerror("Storage Scanner", "Choose a valid folder to scan first.")
                return
            if IS_MACOS:
                run_scan_fn = run_elevated_scan_macos
                auth_hint = "You'll be asked for your Mac password."
                trash_hint = "Deletions still go through Finder's Trash"
                protection_hint = "macOS still protects some system-integrity files"
                waiting_suffix = "(enter your Mac password in the prompt)"
            else:
                run_scan_fn = run_elevated_scan_linux
                auth_hint = "You'll be asked to authenticate via your desktop's PolicyKit prompt."
                trash_hint = "Deletions still go through your desktop Trash"
                protection_hint = "Some system files may still be protected even from root"
                waiting_suffix = "(enter your password in the authentication prompt)"
            if not messagebox.askyesno(
                "Scan with Elevated Permissions",
                "This re-scans the selected folder with root filesystem "
                "access so folders your account can't open get counted too, "
                "instead of under-counting their size.\n\n"
                f"{auth_hint} A few notes:\n"
                "• This window stays open — only the scan itself runs "
                "elevated, nothing else changes or restarts.\n"
                f"• {trash_hint}, not raw root access, so they stay just "
                "as safe as before.\n"
                f"• {protection_hint}, so a small number of paths may "
                "remain unreadable regardless.\n\n"
                "Continue?",
                icon="warning",
            ):
                return
            self._start_elevated_scan_headless(target, run_scan_fn, waiting_suffix)
            return

        if not messagebox.askyesno(
            "Restart with Elevated Permissions",
            "This restarts Storage Scanner as an administrator so it can read "
            "folders your account doesn't have permission to open, which "
            "avoids under-counted sizes from skipped files.\n\n"
            "Windows will show a User Account Control (UAC) prompt. A few notes:\n"
            "• Deletions still go through the Recycle Bin, not raw "
            "unrestricted access, so they stay just as safe as before.\n"
            "• Windows still protects some system files even for "
            "administrators, so a small number of paths may remain "
            "unreadable regardless.\n"
            "• The current (non-elevated) window will close once the "
            "elevated one starts.\n\n"
            "Continue?",
            icon="warning",
        ):
            return

        initial = current if os.path.isdir(current) else None
        if relaunch_elevated_windows(initial):
            self.root.destroy()
        else:
            messagebox.showerror(
                "Storage Scanner",
                "Elevation was cancelled or not accepted. Still running "
                "with normal permissions.",
            )

    def _get_drive_capacity_bytes(self, path):
        """
        Returns total capacity of the drive containing the scanned path.
        """
        return drive_capacity_bytes(path)

    # -- Scan lifecycle ---------------------------------------------------- #
    def _on_toggle_turbo_scan(self):
        set_app_metadata("turbo_scan_enabled", "1" if self.turbo_scan_var.get() else "0")

    # What _ask_turbo_scan_mode returns.
    TURBO_RESTART_AS_ADMIN = "restart"
    TURBO_THIS_SCAN_ONLY = "once"
    TURBO_REGULAR_SCAN = "regular"

    def _resolve_turbo_scan_consent(self, target):
        """Whether Turbo Scan should be attempted for this specific scan, or
        None if the app is restarting elevated instead (the caller must not
        start a scan).

        False if the toggle is off, or the drive isn't a local fixed NTFS
        volume (scan_with_best_engine would fall back on its own either
        way, but this skips popping a prompt for a scan that was never
        going to use Turbo Scan). If already elevated, there's no new UAC
        prompt about to happen, so nothing needs consenting to. Otherwise
        asks every time which way to go: restart elevated (one UAC prompt
        covers every later scan in the session), elevate just this scan
        through the headless helper (a UAC prompt per scan, which on
        machines whose policy demands credentials means typing a password
        each time), or a regular scan. The toggle stays on either way.
        """
        if not IS_WINDOWS or not self.turbo_scan_var.get():
            return False
        if IS_ROOT:
            return True
        if not is_ntfs_fixed_drive(target):
            return True

        choice = self._ask_turbo_scan_mode()

        if choice == self.TURBO_RESTART_AS_ADMIN:
            if relaunch_elevated_windows(target if os.path.isdir(target) else None):
                self.root.destroy()
                return None
            messagebox.showerror(
                "Storage Scanner",
                "Elevation was cancelled or not accepted. Scanning with the "
                "regular scan instead.",
            )
            return False

        return choice == self.TURBO_THIS_SCAN_ONLY

    def _ask_turbo_scan_mode(self):
        """Modal three-way choice for a Turbo Scan while not elevated.
        Closing the dialog counts as a regular scan."""
        dialog = Toplevel(self.root)
        dialog.title("Turbo Scan (Experimental)")
        dialog.configure(bg=COLORS["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        try:
            dialog.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic
            logger.debug("Turbo Scan dialog iconbitmap failed", exc_info=True)

        choice = {"value": self.TURBO_REGULAR_SCAN}

        ttk.Label(
            dialog,
            padding=(16, 14, 16, 4),
            wraplength=470,
            justify=LEFT,
            text=(
                "Turbo Scan reads the NTFS Master File Table directly instead "
                "of walking folders one at a time, which can be dramatically "
                "faster on large drives. It needs administrator access, and "
                "this window isn't running as administrator."
            ),
        ).pack(side=TOP, fill=X)
        ttk.Label(
            dialog,
            padding=(16, 4, 16, 10),
            wraplength=470,
            justify=LEFT,
            foreground=COLORS["muted"],
            text=(
                "• Restart as Admin: one Windows prompt now, then every scan "
                "in the restarted window uses Turbo Scan with no more "
                "prompts. The folder stays selected; click Scan again once it "
                "reopens.\n"
                "• Just This Scan: this window stays open, but Windows asks "
                "again on every Turbo Scan (on some work PCs that means "
                "typing your password each time).\n"
                "• Either way it only reads the drive; nothing is written, and "
                "if Turbo Scan fails the regular scan runs instead."
            ),
        ).pack(side=TOP, fill=X)

        buttons = ttk.Frame(dialog, padding=(16, 0, 16, 14))
        buttons.pack(side=BOTTOM, fill=X)

        def pick(value):
            choice["value"] = value
            dialog.destroy()

        ttk.Button(
            buttons,
            text="Restart as Admin",
            style="Primary.TButton",
            command=lambda: pick(self.TURBO_RESTART_AS_ADMIN),
        ).pack(side=LEFT)
        ttk.Button(
            buttons,
            text="Just This Scan",
            command=lambda: pick(self.TURBO_THIS_SCAN_ONLY),
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            buttons,
            text="Regular Scan",
            command=lambda: pick(self.TURBO_REGULAR_SCAN),
        ).pack(side=RIGHT)

        dialog.bind("<Escape>", lambda _e: pick(self.TURBO_REGULAR_SCAN))
        dialog.protocol("WM_DELETE_WINDOW", lambda: pick(self.TURBO_REGULAR_SCAN))
        dialog.grab_set()
        dialog.focus_set()
        self.root.wait_window(dialog)
        return choice["value"]

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        target = self.path_var.get().strip().strip('"')
        if not target or not os.path.exists(target):
            messagebox.showerror("Storage Scanner", f"Path does not exist:\n{target}")
            return

        turbo_enabled = self._resolve_turbo_scan_consent(target)
        if turbo_enabled is None:  # restarting elevated; this window is gone
            return

        # Reset state.
        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None
        self._live_root_node = None
        self._live_root_iid = None
        self._live_total_bytes = 0
        self._live_expanded_iids = set()
        self._last_live_refresh = 0.0
        self.duplicates = None
        self._duplicates_scan_root = None
        self.cart.clear()
        self._refresh_cart_indicator()
        self._hide_scan_details()

        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self._start_indeterminate_progress()
        self.status_var.set(f"Scanning {target} …")

        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(target, turbo_enabled), daemon=True
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_progress)

    def _scan_worker(self, target, turbo_enabled=False):
        try:
            node, report = turbo_scan.scan_with_best_engine(
                target,
                self.progress_q,
                self.cancel_event,
                turbo_enabled=turbo_enabled,
            )
            self.progress_q.put(("done", (node, report)))
        except Exception as exc:  # noqa: BLE001 - report any scan failure to UI
            logger.exception("Scan of %r failed", target)
            self.progress_q.put(("error", str(exc)))

    def _start_elevated_scan_headless(self, target, run_scan_fn, waiting_suffix):
        """Shared by macOS and Linux: both only ever run the scan itself
        elevated (via `run_scan_fn`, either run_elevated_scan_macos or
        run_elevated_scan_linux -- same (ok, json_text_or_error) return
        contract), never the whole GUI -- this window stays open and
        unprivileged throughout. See _request_elevation's call site for
        why a full relaunch-as-root isn't used on either platform."""
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showerror("Storage Scanner", "A scan is already running.")
            return

        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None
        self._live_root_node = None
        self._live_root_iid = None
        self._live_total_bytes = 0
        self._live_expanded_iids = set()
        self._last_live_refresh = 0.0
        self.duplicates = None
        self._duplicates_scan_root = None
        self.cart.clear()
        self._refresh_cart_indicator()
        self._hide_scan_details()

        self.scan_btn.config(state="disabled")
        self.elevate_btn.config(state="disabled")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self._start_indeterminate_progress()
        self.status_var.set(f"Requesting elevated access for {target} … {waiting_suffix}")

        self.scan_thread = threading.Thread(
            target=self._elevated_scan_worker_headless, args=(target, run_scan_fn), daemon=True
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_progress)

    def _elevated_scan_worker_headless(self, target, run_scan_fn):
        ok, output = run_scan_fn(target)
        if not ok:
            self.progress_q.put(("error", output))
            return
        try:
            node = dict_to_node(json.loads(output))
        except (ValueError, KeyError) as exc:
            logger.exception("Elevated scan of %r produced unparseable output", target)
            self.progress_q.put(("error", f"Elevated scan produced invalid output: {exc}"))
            return
        self.progress_q.put(("done", (node, None)))

    def _poll_progress(self):
        try:
            while True:
                kind, payload = self.progress_q.get_nowait()
                if kind == "progress":
                    self.status_var.set(f"Scanning … {payload:,} files counted")
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "root":
                    self._start_live_tree(payload)
                elif kind == "progress_bytes":
                    self._live_total_bytes = payload
                elif kind == "done":
                    node, report = payload
                    self._finish_scan(node, report)
                    return
                elif kind == "error":
                    self._finish_error(payload)
                    return
        except queue.Empty:
            pass
        self._maybe_refresh_live_tree()
        self.root.after(100, self._poll_progress)

    # -- History helper functions ------------------------------------------ #
    def _finish_scan(self, node, report=None):
        self._stop_progress()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if hasattr(self, "elevate_btn"):
            self.elevate_btn.config(state="normal")

        if self.cancel_event.is_set():
            self.status_var.set("Scan cancelled.")
            self._refresh_tools_state()
            return

        # Whatever the live-scan preview inserted (see _start_live_tree) is
        # purely provisional -- discard it and rebuild from scratch here,
        # from `node`'s own final, authoritative, rolled-up numbers, rather
        # than try to reconcile provisional rows in place.
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self._live_root_node = None

        self.root_node = node
        logger.debug(
            "_finish_scan: node %r has %d direct children, size=%s, file_count=%s, engine=%s",
            node.path,
            len(node.children),
            node.size,
            node.file_count,
            report.engine if report is not None else "compatible",
        )
        root_iid = self._insert_node("", node, parent_size=node.size or 1)
        self.tree.item(root_iid, open=True)
        self._populate_children(root_iid, node)
        logger.debug(
            "_finish_scan: after populate, tree has %d top-level row(s), "
            "root row has %d child row(s)",
            len(self.tree.get_children("")),
            len(self.tree.get_children(root_iid)),
        )
        self._refresh_tools_state()
        self.top_count_combo.config(state="readonly")

        if report is not None and report.fallback_reason:
            self._show_turbo_fallback_banner(report.fallback_reason)
        else:
            self._dismiss_turbo_fallback_banner()

        inaccessible = find_inaccessible_paths(node)
        if inaccessible:
            self._show_inaccessible_paths_banner(inaccessible)
        else:
            self._dismiss_inaccessible_paths_banner()
        self._show_scan_details(report, inaccessible)

        self.status_var.set(
            f"{node.path}  —  {human_size(node.size)} in "
            f"{node.file_count:,} files | Saving history..."
        )

        threading.Thread(
            target=self._save_history_worker,
            args=(node,),
            daemon=True,
        ).start()

    def _finish_error(self, msg):
        self._stop_progress()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if hasattr(self, "elevate_btn"):
            self.elevate_btn.config(state="normal")
        self._refresh_tools_state()
        self.status_var.set("Scan failed.")
        messagebox.showerror("Storage Scanner", f"Scan failed:\n{msg}")

    def _refresh_tools_state(self):
        """Tools is usable whenever no scan is running; the items that need
        this session's tree are enabled only once there is one."""
        self.tools_btn.config(state="normal")
        state = "normal" if self.root_node is not None else "disabled"
        for menu, index in self._scan_only_tools:
            menu.entryconfigure(index, state=state)

    # -- Live scan preview (Compatible engine only) ------------------------- #
    #
    # storage_scanner.scanner.scan() builds its Node tree in place, in a
    # background thread, as it walks -- node.children already grows live;
    # the only thing missing was the UI ever looking at it before "done".
    # Turbo Scan has no equivalent tree to preview (MFT records come back
    # in arbitrary order, not directory-walk order, so nothing resembling
    # a folder tree exists until the whole volume has been parsed) -- it
    # simply never posts a "root" message, so none of this ever activates
    # for a Turbo Scan. Everything inserted here is purely provisional:
    # _finish_scan always discards it and rebuilds from the final,
    # authoritative rolled-up tree, so a wrong/stale number here can never
    # end up on screen once the scan completes.
    _LIVE_REFRESH_INTERVAL_SECONDS = 0.5

    def _start_live_tree(self, root_node):
        """Handle a ("root", node) progress message: insert the scan
        target's own row immediately, open, so newly-discovered top-level
        children start appearing as soon as the first refresh tick finds
        them."""
        self._live_root_node = root_node
        self._live_total_bytes = 0
        self._live_expanded_iids = set()
        self._live_root_iid = self._insert_node("", root_node, parent_size=1, live=True)
        self.tree.item(self._live_root_iid, open=True)

    def _maybe_refresh_live_tree(self):
        if self._live_root_node is None:
            return
        now = time.monotonic()
        if now - self._last_live_refresh < self._LIVE_REFRESH_INTERVAL_SECONDS:
            return
        self._last_live_refresh = now
        self._refresh_live_tree()

    def _refresh_live_tree(self):
        """Update the running byte total shown on the root row, and pull
        in any newly-discovered children under it and under every row the
        user has manually expanded (self._live_expanded_iids) -- never
        recurses into rows nobody has looked at, so cost stays bounded by
        how much of the tree is actually on screen, not by how much of
        the disk has been scanned so far."""
        self.tree.set(self._live_root_iid, "size", human_size(self._live_total_bytes))
        for parent_iid in (self._live_root_iid, *self._live_expanded_iids):
            node = self.node_by_iid.get(parent_iid)
            if node is not None:
                self._sync_live_children(parent_iid, node)

    def _sync_live_children(self, parent_iid, node):
        existing_names = {
            self.node_by_iid[iid].name
            for iid in self.tree.get_children(parent_iid)
            if iid in self.node_by_iid
        }
        # node.children is still being appended to by a background worker
        # thread -- safe to iterate mid-append under the GIL (list.append
        # is atomic; at worst this snapshot misses the very latest arrival,
        # picked up on the next throttled tick instead).
        for child in list(node.children):
            if child.name in existing_names:
                continue
            index = len(self.tree.get_children(parent_iid))
            self._insert_node(parent_iid, child, parent_size=1, index=index, live=True)

    def _show_turbo_fallback_banner(self, reason):
        """Dismissible banner explaining a scan silently used the
        Compatible engine after Turbo Scan failed -- same pattern as
        app.py's _show_update_banner. Never hidden: a fallback should
        always be visible, not silently swallowed (see storage_scanner.
        turbo_scan.scan_with_best_engine's docstring)."""
        self._dismiss_turbo_fallback_banner()

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._turbo_fallback_banner = banner

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
        self._inaccessible_paths_banner = banner

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
        win.geometry("780x420")
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
            wraplength=740,
            justify=LEFT,
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("path", "type")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tv.heading("path", text="Path")
        tv.heading("type", text="Type")
        tv.column("path", width=620, anchor=W, stretch=True)
        tv.column("type", width=80, anchor=W, stretch=False)

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

    def cancel_scan(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.status_var.set("Cancelling …")

    # -- Helper Methods for Progress Bar ------------------------------------- #
    def _start_indeterminate_progress(self):
        """Show an animated progress bar when total work is unknown."""
        self.progress.config(mode="indeterminate", maximum=100, value=0)
        self.progress.pack(side=RIGHT, padx=6)
        self.progress.start(12)

    def _start_determinate_progress(self, maximum):
        """Show a percentage progress bar when total work is known."""
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=max(1, maximum), value=0)
        self.progress.pack(side=RIGHT, padx=6)

    def _update_determinate_progress(self, value):
        self.progress.config(value=value)

    def _stop_progress(self):
        self.progress.stop()
        self.progress.config(value=0)
        self.progress.pack_forget()

    # -- Treeview population (lazy) ---------------------------------------- #
    def _heat_tag(self, fraction):
        """Return a treeview tag whose foreground is the heat color for
        `fraction`, quantized to 25 buckets so we configure few tags."""
        bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
        name = f"heat{bucket}"
        if name not in self._heat_tags:
            self.tree.tag_configure(name, foreground=heat_color(bucket / 24))
            self._heat_tags.add(name)
        return name

    def _insert_node(self, parent_iid, node, parent_size, index=0, live=False):
        # `live=True` means `node` came from a scan still in progress: a
        # directory's size/alloc_size/file_count are only meaningful after
        # scanner._rollup() runs once, at the very end (see scan()'s own
        # docstring) -- showing them, or a percent-of-parent computed from
        # them, before then would just be a misleading, usually-wrong
        # placeholder. A *file*'s own size is real and known immediately,
        # so it's shown as-is; only the percent/heat-color columns (which
        # need a trustworthy parent total) stay suppressed for every live
        # row, file or directory alike.
        fraction = 0 if live else ((node.size / parent_size) if parent_size else 0)
        percent = "—" if live else f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = "" if live else (f"{node.file_count:,}" if node.is_dir else "")
        if node.error:
            tags = ["error"]
        elif node.is_cloud_placeholder:
            tags = ["cloud"]
        elif node.is_link:
            tags = ["link"]
        else:
            tags = [self._heat_tag(fraction)]  # foreground = space-hog heat
            if node.is_dir:
                tags.append("dir")  # bold, keeps heat color
        tags.append("odd" if index % 2 else "even")

        if node.error:
            icon = "⚠"
        elif node.is_cloud_placeholder:
            icon = "☁"
        elif node.is_link:
            icon = "↪"
        elif node.is_dir:
            icon = "📁"
        else:
            icon = "📄"

        if live and node.is_dir:
            # Not sized yet -- this directory's own scan may not even have
            # started (see the docstring note above).
            size_text = alloc_text = "…"
        else:
            # A cloud placeholder's `size` is its full logical size (what
            # it'll be once downloaded); `alloc_size` is what's actually
            # using local disk right now — worth showing side by side
            # rather than picking one.
            size_text = human_size(node.size)
            alloc_text = human_size(node.alloc_size)
            if node.is_cloud_placeholder:
                alloc_text += " (online-only)"

        label = f"{icon} {node.name}" + (
            "\\" if node.is_dir and not node.name.endswith("\\") else ""
        )
        iid = self.tree.insert(
            parent_iid,
            END,
            text=label,
            values=(size_text, alloc_text, percent, items),
            tags=tuple(tags),
        )
        self.node_by_iid[iid] = node

        # Give expandable dirs a placeholder child so the [+] arrow appears.
        if node.is_dir and node.children:
            self.tree.insert(iid, END, text="…(loading)", tags=("placeholder",))
        return iid

    def _populate_children(self, parent_iid, node, live=False):
        # Remove placeholder if present.
        kids = self.tree.get_children(parent_iid)
        if len(kids) == 1 and self.tree.item(kids[0], "text") == "…(loading)":
            self.tree.delete(kids[0])
        elif kids:
            return  # already populated

        ordered = sorted(node.children, key=self._node_sort_key, reverse=self._sort_reverse)
        for index, child in enumerate(ordered):
            self._insert_node(parent_iid, child, parent_size=node.size or 1, index=index, live=live)

    def _node_sort_key(self, node):
        if self._sort_key == "name":
            return node.name.lower()
        if self._sort_key == "items":
            return node.file_count
        return node.size

    def _on_open(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node or not node.is_dir:
            return
        live = self._live_root_node is not None
        self._populate_children(iid, node, live=live)
        if live:
            self._live_expanded_iids.add(iid)

    def _on_double_click(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if node and not node.is_dir:
            self._open_in_explorer()

    # -- Column sorting ---------------------------------------------------- #

    _HEADINGS = {
        "#0": "Name",
        "size": "Size",
        "alloc": "On Disk",
        "percent": "% of Parent",
        "items": "Files",
    }

    def _sort_by(self, key):
        """Handle a heading click: toggle direction if it's the active key,
        else switch to it (names ascend, sizes/counts descend by default)."""
        if key == self._sort_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = key != "name"
        self._update_heading_arrows()
        self._resort_tree()

    def _update_heading_arrows(self):
        arrow = " ▼" if self._sort_reverse else " ▲"
        # The percent column is driven by the size sort, so it shares the mark.
        active_cols = {"size": ("size", "alloc", "percent"), "name": ("#0",), "items": ("items",)}[
            self._sort_key
        ]
        for col, base in self._HEADINGS.items():
            text = base + (arrow if col in active_cols else "")
            self.tree.heading(col, text=text)

    def _resort_tree(self):
        """Re-order every already-populated level in place (preserves which
        nodes are expanded; lazy children sort on expand via _populate)."""

        def walk(parent_iid):
            self._sort_level(parent_iid)
            for iid in self.tree.get_children(parent_iid):
                node = self.node_by_iid.get(iid)
                if node and node.is_dir:
                    walk(iid)

        walk("")

    def _sort_level(self, parent_iid):
        kids = [k for k in self.tree.get_children(parent_iid) if k in self.node_by_iid]
        if not kids:
            return
        kids.sort(
            key=lambda iid: self._node_sort_key(self.node_by_iid[iid]), reverse=self._sort_reverse
        )
        for index, iid in enumerate(kids):
            self.tree.move(iid, parent_iid, index)
            self._set_stripe(iid, index)

    def _set_stripe(self, iid, index):
        """Rewrite a row's even/odd background tag, keeping its other tags."""
        tags = [t for t in self.tree.item(iid, "tags") if t not in ("even", "odd")]
        tags.append("odd" if index % 2 else "even")
        self.tree.item(iid, tags=tuple(tags))

    def _refresh_row(self, iid):
        """Recompute a row's size / percent / files text from its node."""
        node = self.node_by_iid.get(iid)
        if not node:
            return
        parent_node = self.node_by_iid.get(self.tree.parent(iid))
        parent_size = (parent_node.size if parent_node else node.size) or 1
        fraction = (node.size / parent_size) if parent_size else 0
        percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = f"{node.file_count:,}" if node.is_dir else ""
        self.tree.item(iid, values=(human_size(node.size), percent, items))

    # -- Constraints Functions --------------------------------------------- #
    def _forget_subtree(self, iid):
        """Drop an iid and all its descendants from the node map."""
        for child in self.tree.get_children(iid):
            self._forget_subtree(child)
        self.node_by_iid.pop(iid, None)

    def _remove_main_tree_row(self, iid):
        """Remove a node's row from the main tree after it's been deleted,
        rolling the removed size/count back out of every ancestor and
        refreshing whatever changed on screen. Shared by _delete_selected
        and the Cleanup Cart's batch executor (cart_window.py) for any
        cart item that still has a live row in this tree.
        """
        node = self.node_by_iid.get(iid)
        if not node:
            return

        parent_iid = self.tree.parent(iid)
        parent_node = self.node_by_iid.get(parent_iid)

        # Subtract the removed size/count from every ancestor (incl. the root
        # row, whose parent is ""). root_node is the same object as its row.
        anc = parent_iid
        while anc:
            an = self.node_by_iid.get(anc)
            if an:
                an.size -= node.size
                an.file_count -= node.file_count
            anc = self.tree.parent(anc)
        if parent_node and node in parent_node.children:
            parent_node.children.remove(node)

        self._forget_subtree(iid)
        self.tree.delete(iid)

        # Siblings' "% of parent" and the ancestor sizes all shifted — refresh.
        for index, sib in enumerate(self.tree.get_children(parent_iid)):
            self._refresh_row(sib)
            self._set_stripe(sib, index)
        anc = parent_iid
        while anc:
            self._refresh_row(anc)
            anc = self.tree.parent(anc)

        if self.root_node:
            self.status_var.set(
                f"{self.root_node.path}  —  {human_size(self.root_node.size)} "
                f"in {self.root_node.file_count:,} files"
            )

    def _delete_selected(self):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node:
            return
        kind = "folder" if node.is_dir else "file"
        if not messagebox.askyesno(
            f"Delete to {TRASH_NAME}",
            f"Send this {kind} to the {TRASH_NAME}?\n\n{node.path}\n\n"
            f"{human_size(node.size)}" + (f" in {node.file_count:,} files" if node.is_dir else ""),
            icon="warning",
        ):
            return

        if not recycle_and_log(node, source="Main tree"):
            messagebox.showerror(
                "Storage Scanner",
                f"Could not delete:\n{node.path}\n\n"
                "It may be in use, protected, or require admin rights.",
            )
            return

        self._remove_from_duplicate_cache(node)
        self._remove_main_tree_row(iid)

    # -- Context menu actions ---------------------------------------------- #
    def _show_tools_menu(self):
        """Show the Tools dropdown under the Tools button."""
        try:
            x = self.tools_btn.winfo_rootx()
            y = self.tools_btn.winfo_rooty() + self.tools_btn.winfo_height()
            self.tools_menu.tk_popup(x, y)
        finally:
            self.tools_menu.grab_release()

    def _show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _selected_node(self):
        return self.node_by_iid.get(self.tree.focus())

    def _add_selected_to_cart(self):
        node = self._selected_node()
        if node:
            self.cart.add(node, "Main tree")
            self._refresh_cart_indicator()

    def _reveal(self, path, is_dir):
        try:
            if IS_MACOS:
                if is_dir:
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["open", "-R", path])
            elif IS_LINUX:
                # No portable "select this one file" flag across file
                # managers (Nautilus/Dolphin/Nemo/etc. each have their
                # own, if any, and xdg-open has none) -- opens the file's
                # own containing folder instead, same degraded-but-
                # functional fallback for a file as for a directory.
                subprocess.run(["xdg-open", path if is_dir else os.path.dirname(path)])
            elif is_dir:
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["explorer", "/select,", path])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reveal %r", path, exc_info=True)
            messagebox.showerror("Storage Scanner", f"Could not open:\n{exc}")

    def _open_in_explorer(self):
        node = self._selected_node()
        if node:
            self._reveal(node.path, node.is_dir)

    def _copy_path(self):
        node = self._selected_node()
        if node:
            self.root.clipboard_clear()
            self.root.clipboard_append(node.path)

    def _set_budget_for_selected(self):
        node = self._selected_node()
        if not node:
            return
        if not node.is_dir:
            messagebox.showinfo(
                "Storage Scanner", "Budgets apply to folders, not individual files."
            )
            return

        response = simpledialog.askstring(
            "Set Budget",
            f"Alert when this folder's size exceeds a threshold.\n\n"
            f"{node.path}\nCurrently: {human_size(node.size)}\n\n"
            f"Enter a threshold (e.g. 50GB, 500MB):",
            parent=self.root,
        )
        if not response or not response.strip():
            return

        try:
            threshold_bytes = parse_size(response)
        except ValueError as exc:
            messagebox.showerror("Storage Scanner", str(exc))
            return
        if not threshold_bytes:
            messagebox.showerror("Storage Scanner", "Enter a size, e.g. 50GB.")
            return

        normalized = os.path.normcase(os.path.normpath(node.path))
        set_budget(normalized, threshold_bytes)
        self.status_var.set(f"Budget set: {node.path} → alert above {human_size(threshold_bytes)}")

    # -- Top 25 largest files --------------------------------------------- #
