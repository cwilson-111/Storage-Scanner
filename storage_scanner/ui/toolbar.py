"""The main window's toolbar and its Tools menu (with Settings and Help),
the drive list, and asking for administrator rights.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import os
from tkinter import (
    LEFT,
    RIGHT,
    TOP,
    BooleanVar,
    Menu,
    StringVar,
    X,
    filedialog,
    messagebox,
    ttk,
)

from history import get_app_metadata, set_app_metadata
from storage_scanner import (
    appearance,
    diagnostics,
    explorer_menu,
    history_retention,
    turbo_cache,
    update_check,
)
from storage_scanner.file_ops import (
    relaunch_elevated_windows,
    run_elevated_scan_linux,
    run_elevated_scan_macos,
)
from storage_scanner.formatting import human_size
from storage_scanner.platform_support import (
    IS_LINUX,
    IS_MACOS,
    IS_ROOT,
    IS_WINDOWS,
)
from storage_scanner.ui.main_window import TREEMAP_SHADED_KEY, TREEMAP_SHOWN_KEY

# Settings ▸ Keep Every Saved Scan For: (label, stored value).
_HISTORY_KEEP_ALL_CHOICES = (
    ("7 days", "7"),
    ("30 days", "30"),
    ("90 days", "90"),
    ("1 year", "365"),
    ("Forever (never thin out history)", history_retention.KEEP_FOREVER),
)


class ToolbarMixin:
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
        # The treemap pane under the tree (ui/treemap_pane.py) and the
        # cushion shading of its tiles, each on unless it was turned off.
        self.show_treemap_var = BooleanVar(value=get_app_metadata(TREEMAP_SHOWN_KEY, "1") != "0")
        explore_menu.add_checkbutton(
            label="Show Treemap", variable=self.show_treemap_var, command=self._toggle_treemap
        )
        self.shade_treemap_var = BooleanVar(value=get_app_metadata(TREEMAP_SHADED_KEY, "1") != "0")
        explore_menu.add_checkbutton(
            label="Shade Treemap Tiles",
            variable=self.shade_treemap_var,
            command=self._toggle_treemap_shading,
        )
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

        self._add_data_tools_menu(self.tools_menu)
        add_scan_only(self.tools_menu, "Export Results…", self.export_results)
        self._refresh_tools_state()  # no tree yet: scan-only items start greyed out

        # Settings persist the same way as the schema_version key, via
        # history.py's app_metadata table (there's no other settings storage
        # in this app to reuse). Turbo Scan (NTFS MFT fast path) is
        # Windows-only and off by default.
        settings_menu = Menu(self.tools_menu, tearoff=0)
        if IS_WINDOWS:
            self.turbo_scan_var = BooleanVar(
                value=get_app_metadata("turbo_scan_enabled", "0") == "1"
            )
            settings_menu.add_checkbutton(
                label="Turbo Scan (Experimental) — NTFS MFT fast path",
                variable=self.turbo_scan_var,
                command=self._on_toggle_turbo_scan,
            )
            settings_menu.add_command(
                label="Clear Turbo Scan Cache…", command=self._clear_turbo_cache
            )
            self.explorer_menu_var = BooleanVar(value=explorer_menu.is_installed())
            settings_menu.add_checkbutton(
                label=f"Add “{explorer_menu.MENU_TEXT}” to Folder Right-Click Menu",
                variable=self.explorer_menu_var,
                command=self._on_toggle_explorer_menu,
            )
        self.update_check_var = BooleanVar(
            value=get_app_metadata(update_check.ENABLED_KEY, "1") != "0"
        )
        settings_menu.add_checkbutton(
            label="Check for Updates on Launch",
            variable=self.update_check_var,
            command=lambda: set_app_metadata(
                update_check.ENABLED_KEY, "1" if self.update_check_var.get() else "0"
            ),
        )
        # How long every saved scan is kept before older history thins out
        # (storage_scanner.history_retention); applied at the next save.
        self.history_keep_all_var = StringVar(
            value=get_app_metadata(
                history_retention.KEEP_ALL_DAYS_KEY,
                str(history_retention.DEFAULT_KEEP_ALL_DAYS),
            )
        )
        keep_all_menu = Menu(settings_menu, tearoff=0)
        for label, value in _HISTORY_KEEP_ALL_CHOICES:
            keep_all_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.history_keep_all_var,
                command=self._on_change_history_keep_all,
            )
        settings_menu.add_cascade(label="Keep Every Saved Scan For", menu=keep_all_menu)
        # Light, dark, or the system's choice (storage_scanner/appearance.py).
        self.appearance_var = StringVar(
            value=get_app_metadata(appearance.SETTING_KEY, appearance.SYSTEM)
        )
        appearance_menu = Menu(settings_menu, tearoff=0)
        for value, label in appearance.CHOICES:
            appearance_menu.add_radiobutton(
                label=label,
                value=value,
                variable=self.appearance_var,
                command=self._on_change_appearance,
            )
        settings_menu.add_cascade(label="Appearance", menu=appearance_menu)
        self.tools_menu.add_cascade(label="Settings", menu=settings_menu)

        # Last, where a Help menu conventionally sits: the first-run guide
        # (storage_scanner/ui/onboarding_window.py) opens itself once and
        # tells the user it can be reopened from here.
        help_menu = Menu(self.tools_menu, tearoff=0)
        help_menu.add_command(label="Getting Started…", command=self.show_onboarding)
        help_menu.add_separator()
        help_menu.add_command(label="Copy Diagnostic Info", command=self._copy_diagnostic_info)
        help_menu.add_command(label="Report a Problem…", command=self._report_a_problem)
        self.tools_menu.add_cascade(label="Help", menu=help_menu)

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

    # -- Scan lifecycle ---------------------------------------------------- #
    def _on_toggle_turbo_scan(self):
        set_app_metadata("turbo_scan_enabled", "1" if self.turbo_scan_var.get() else "0")

    def _on_toggle_explorer_menu(self):
        """Settings ▸ Add "Scan with Storage Scanner" to Folder Right-Click Menu."""
        try:
            if self.explorer_menu_var.get():
                explorer_menu.install()
                done = "Folders' right-click menus now have “Scan with Storage Scanner”"
            else:
                explorer_menu.uninstall()
                done = "Removed “Scan with Storage Scanner” from folders' right-click menus"
        except (ValueError, OSError) as exc:
            self.explorer_menu_var.set(explorer_menu.is_installed())
            messagebox.showerror("Storage Scanner", str(exc))
            return
        self.status_var.set(done + " (on Windows 11, under “Show more options”).")

    def _report_a_problem(self):
        import webbrowser  # here, not at startup: only this menu item needs it

        webbrowser.open(diagnostics.ISSUES_URL)

    def _copy_diagnostic_info(self):
        """Help ▸ Copy Diagnostic Info (storage_scanner/diagnostics.py)."""
        self.root.clipboard_clear()
        self.root.clipboard_append(diagnostics.diagnostic_text())
        self.status_var.set(
            "Copied diagnostic info (no file or folder names) — paste it into a bug report."
        )

    def _clear_turbo_cache(self):
        """Settings ▸ Clear Turbo Scan Cache: say how big it is, and delete
        it if asked (turbo_cache.discard)."""
        if self._scan_running():
            messagebox.showinfo("Storage Scanner", "Wait until the scan finishes.")
            return
        size = turbo_cache.cache_size_bytes()
        if not size:
            messagebox.showinfo("Storage Scanner", "The Turbo Scan cache is empty.")
            return
        if not messagebox.askyesno(
            "Clear Turbo Scan Cache",
            f"The Turbo Scan cache takes {human_size(size)}. Delete it?\n\n"
            "The next Turbo Scan of each drive reads its whole MFT again, then "
            "caches it anew.",
        ):
            return
        try:
            turbo_cache.discard()
        except OSError as exc:
            messagebox.showerror("Storage Scanner", f"Could not delete the cache:\n{exc}")
            return
        self.status_var.set(f"Cleared the Turbo Scan cache ({human_size(size)}).")

    def _on_change_appearance(self):
        set_app_metadata(appearance.SETTING_KEY, self.appearance_var.get())
        if appearance.resolve(self.appearance_var.get()) != self.appearance:
            messagebox.showinfo(
                "Appearance",
                "The new appearance applies the next time you open Storage Scanner.",
                parent=self.root,
            )

    def _on_change_history_keep_all(self):
        set_app_metadata(history_retention.KEEP_ALL_DAYS_KEY, self.history_keep_all_var.get())

    # -- Context menu actions ---------------------------------------------- #
    def _show_tools_menu(self):
        """Show the Tools dropdown under the Tools button."""
        try:
            x = self.tools_btn.winfo_rootx()
            y = self.tools_btn.winfo_rooty() + self.tools_btn.winfo_height()
            self.tools_menu.tk_popup(x, y)
        finally:
            self.tools_menu.grab_release()

    def _refresh_tools_state(self):
        """Tools is usable whenever no scan is running; the items that need
        this session's tree are enabled only once there is one."""
        self.tools_btn.config(state="normal")
        state = "normal" if self.root_node is not None else "disabled"
        for menu, index in self._scan_only_tools:
            menu.entryconfigure(index, state=state)
