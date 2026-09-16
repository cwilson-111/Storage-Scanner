"""Main window: header, toolbar, tree view, scan lifecycle, row actions.

A mixin composed into StorageScannerApp (storage_scanner/app.py) alongside
DuplicatesMixin, HistoryMixin, and FileWindowsMixin — split out so each
window/feature area can be read and tested on its own.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
from tkinter import (
    BooleanVar, BOTH, BOTTOM, E, END, LEFT, Menu, RIGHT, StringVar, TOP, W, X,
    filedialog, messagebox, simpledialog, ttk,
)

from history import get_app_metadata, set_app_metadata, set_budget
from storage_scanner import turbo_scan
from storage_scanner.audit import recycle_and_log
from storage_scanner.drive_info import is_ntfs_fixed_drive
from storage_scanner.file_ops import relaunch_elevated_windows, run_elevated_scan_macos
from storage_scanner.formatting import bar, human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME, IS_MACOS, IS_ROOT, IS_WINDOWS, TRASH_NAME,
)
from storage_scanner.search import parse_size
from storage_scanner.serialization import dict_to_node
from storage_scanner.settings import COLORS, FONT_MONO_BOLD, heat_color


class MainWindowMixin:
    def _build_toolbar(self):
        bar_frame = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        bar_frame.pack(side=TOP, fill=X)
        self.toolbar_frame = bar_frame

        ttk.Label(bar_frame, text="▸ LOCATION", style="Accent.TLabel").pack(side=LEFT)

        self.path_var = StringVar()
        self.path_combo = ttk.Combobox(
            bar_frame, textvariable=self.path_var,
            values=self._list_drives(),
        )
        self.path_combo.pack(side=LEFT, padx=6, fill=X, expand=True)
        self.path_combo.bind("<Return>", lambda e: self.start_scan())

        ttk.Button(bar_frame, text="Browse…", command=self.browse).pack(side=LEFT)
        self.scan_btn = ttk.Button(
            bar_frame, text="Scan", command=self.start_scan, style="Primary.TButton",
        )
        self.scan_btn.pack(side=LEFT, padx=6)
        self.cancel_btn = ttk.Button(
            bar_frame, text="Cancel", command=self.cancel_scan, state="disabled"
        )
        self.cancel_btn.pack(side=LEFT)

        if (IS_MACOS or IS_WINDOWS) and not IS_ROOT:
            self.elevate_btn = ttk.Button(
                bar_frame, text="🔒 Run as Admin", command=self._request_elevation,
            )
            self.elevate_btn.pack(side=LEFT, padx=(6, 0))

        # Adding a tools menu dropdown
        self.tools_btn = ttk.Button(
            bar_frame,
            text="Tools ▼",
            command=self._show_tools_menu,
            state="disabled",
        )
        self.tools_btn.pack(side=LEFT, padx=6)

        # Grouped into submenus that follow the order you'd actually use them
        # in — explore what's there, clean some of it up, then check history/
        # trust — rather than one flat, ever-growing list of unrelated tools.
        self.tools_menu = Menu(self.root, tearoff=0)

        explore_menu = Menu(self.tools_menu, tearoff=0)
        explore_menu.add_command(label="Treemap", command=self.show_treemap)
        explore_menu.add_command(label="Search & Filter", command=self.show_search_window)
        explore_menu.add_command(label="Largest Files", command=self.show_top_files)
        explore_menu.add_command(label="File Types Breakdown", command=self.show_file_types)
        self.tools_menu.add_cascade(label="Explore", menu=explore_menu)

        cleanup_menu = Menu(self.tools_menu, tearoff=0)
        cleanup_menu.add_command(label="Find Duplicate Files", command=self.show_duplicates)
        cleanup_menu.add_command(label="Cleanup Recommendations", command=self.show_cleanup_recommendations)
        self.tools_menu.add_cascade(label="Clean Up", menu=cleanup_menu)

        history_menu = Menu(self.tools_menu, tearoff=0)
        history_menu.add_command(label="Growth History", command=self.show_growth_history)
        history_menu.add_command(label="Audit Log", command=self.show_audit_log)
        history_menu.add_command(label="Storage Budgets", command=self.show_budgets)
        self.tools_menu.add_cascade(label="History & Trust", menu=history_menu)

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
            bar_frame, textvariable=self.top_count_var, width=5, state="disabled",
            values=("25", "50", "100"),
        )
        self.top_count_combo.pack(side=RIGHT, padx=(0, 6))
        # Re-running with a new count is instant, so update live on selection.
        self.top_count_combo.bind(
            "<<ComboboxSelected>>", lambda e: self.show_top_files()
        )
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
        self.tree.heading("#0", text="Name",
                          command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="Size",
                          command=lambda: self._sort_by("size"))
        self.tree.heading("alloc", text="On Disk",
                          command=lambda: self._sort_by("size"))
        self.tree.heading("percent", text="% of Parent",
                          command=lambda: self._sort_by("size"))
        self.tree.heading("items", text="Files",
                          command=lambda: self._sort_by("items"))
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
        self.tree.tag_configure("error", foreground=COLORS["error"],
                                font=FONT_MONO_BOLD)
        self.tree.tag_configure("placeholder", foreground=COLORS["muted"])
        self.tree.tag_configure("link", foreground=COLORS["accent2"],
                                font=FONT_MONO_BOLD)
        self.tree.tag_configure("cloud", foreground=COLORS["muted"],
                                font=FONT_MONO_BOLD)
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
        self.menu.add_separator()
        self.menu.add_command(label=f"Delete (to {TRASH_NAME})",
                              command=self._delete_selected)
        self.tree.bind("<Button-3>", self._show_menu)

        # Keyboard: Delete recycles the selection, F5 re-scans.
        self.tree.bind("<Delete>", lambda e: self._delete_selected())
        self.root.bind("<F5>", lambda e: self.start_scan())
    def _build_statusbar(self):
        status = ttk.Frame(self.root, padding=(8, 2))
        status.pack(side=BOTTOM, fill=X)
        self.status_var = StringVar(value="Pick a drive or folder, then click Scan.")
        ttk.Label(status, textvariable=self.status_var, anchor=W).pack(
            side=LEFT, fill=X, expand=True
        )
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220)
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
            title="Select a folder to analyze", initialdir=initialdir,
        )
        if chosen:
            self.path_var.set(os.path.normpath(chosen))
    def _request_elevation(self):
        current = self.path_var.get().strip().strip('"')

        if IS_MACOS:
            # Relaunching the whole GUI as root can't show a window on
            # macOS (see run_elevated_scan_macos's docstring), so only the
            # scan itself runs elevated — this window stays open throughout.
            target = current if os.path.isdir(current) else None
            if not target:
                messagebox.showerror(
                    "Storage Scanner", "Choose a valid folder to scan first."
                )
                return
            if not messagebox.askyesno(
                "Scan with Elevated Permissions",
                "This re-scans the selected folder with root filesystem "
                "access so folders your account can't open get counted too, "
                "instead of under-counting their size.\n\n"
                "You'll be asked for your Mac password. A few notes:\n"
                "• This window stays open — only the scan itself runs "
                "elevated, nothing else changes or restarts.\n"
                "• Deletions still go through Finder's Trash, not raw root "
                "access, so they stay just as safe as before.\n"
                "• macOS still protects some system-integrity files even "
                "from root, so a small number of paths may remain "
                "unreadable regardless.\n\n"
                "Continue?",
                icon="warning",
            ):
                return
            self._start_elevated_scan_macos(target)
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
        try:
            usage = shutil.disk_usage(path)
            return usage.total
        except Exception:
            logger.warning("disk_usage(%r) failed", path, exc_info=True)
            return 0

    # -- Scan lifecycle ---------------------------------------------------- #
    def _on_toggle_turbo_scan(self):
        set_app_metadata("turbo_scan_enabled", "1" if self.turbo_scan_var.get() else "0")

    def _resolve_turbo_scan_consent(self, target):
        """Whether Turbo Scan should be attempted for this specific scan.

        False if the toggle is off, or the drive isn't a local fixed NTFS
        volume (scan_with_best_engine would fall back on its own either
        way, but this skips popping a prompt for a scan that was never
        going to use Turbo Scan). If already elevated, there's no new UAC
        prompt about to happen, so nothing needs consenting to. Otherwise
        asks fresh every time (matches the macOS elevation prompt's own
        not-cached precedent in _request_elevation) — declining falls back
        to the Compatible engine for just this scan, the toggle stays on.
        """
        if not IS_WINDOWS or not self.turbo_scan_var.get():
            return False
        if IS_ROOT:
            return True
        if not is_ntfs_fixed_drive(target):
            return True
        return messagebox.askyesno(
            "Turbo Scan (Experimental)",
            "Turbo Scan reads the NTFS Master File Table directly instead "
            "of walking folders one at a time, which can be dramatically "
            "faster on large drives.\n\n"
            "This needs a one-time administrator prompt (UAC) for this "
            "scan. A few notes:\n"
            "• This window stays open throughout — unlike \"Run as "
            "Admin\", nothing restarts.\n"
            "• It only reads the volume; nothing is ever written or "
            "modified.\n"
            "• If anything about it fails, this scan automatically falls "
            "back to the regular scan — you'll still get a result either "
            "way.\n\n"
            "Continue with Turbo Scan?",
            icon="question",
        )

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        target = self.path_var.get().strip().strip('"')
        if not target or not os.path.exists(target):
            messagebox.showerror("Storage Scanner", f"Path does not exist:\n{target}")
            return

        turbo_enabled = self._resolve_turbo_scan_consent(target)

        # Reset state.
        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None

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
                target, self.progress_q, self.cancel_event, turbo_enabled=turbo_enabled,
            )
            self.progress_q.put(("done", (node, report)))
        except Exception as exc:  # noqa: BLE001 - report any scan failure to UI
            logger.exception("Scan of %r failed", target)
            self.progress_q.put(("error", str(exc)))

    def _start_elevated_scan_macos(self, target):
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showerror("Storage Scanner", "A scan is already running.")
            return

        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None

        self.scan_btn.config(state="disabled")
        self.elevate_btn.config(state="disabled")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self._start_indeterminate_progress()
        self.status_var.set(
            f"Requesting elevated access for {target} … "
            "(enter your Mac password in the prompt)"
        )

        self.scan_thread = threading.Thread(
            target=self._elevated_scan_worker_macos, args=(target,), daemon=True
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_progress)

    def _elevated_scan_worker_macos(self, target):
        ok, output = run_elevated_scan_macos(target)
        if not ok:
            self.progress_q.put(("error", output))
            return
        try:
            node = dict_to_node(json.loads(output))
        except (ValueError, KeyError) as exc:
            logger.exception("Elevated scan of %r produced unparseable output", target)
            self.progress_q.put(
                ("error", f"Elevated scan produced invalid output: {exc}")
            )
            return
        self.progress_q.put(("done", (node, None)))
    def _poll_progress(self):
        try:
            while True:
                kind, payload = self.progress_q.get_nowait()
                if kind == "progress":
                    self.status_var.set(f"Scanning … {payload:,} files counted")
                elif kind == "done":
                    node, report = payload
                    self._finish_scan(node, report)
                    return
                elif kind == "error":
                    self._finish_error(payload)
                    return
        except queue.Empty:
            pass
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
            return

        self.root_node = node
        root_iid = self._insert_node("", node, parent_size=node.size or 1)
        self.tree.item(root_iid, open=True)
        self._populate_children(root_iid, node)
        self.tools_btn.config(state="normal")
        self.top_count_combo.config(state="readonly")

        engine_prefix = ""
        if report is not None and report.engine == turbo_scan.ENGINE_TURBO:
            throughput = report.file_count / report.elapsed_seconds if report.elapsed_seconds > 0 else 0
            engine_prefix = (
                f"⚡ Turbo Scan (NTFS MFT) · {report.file_count:,} files in "
                f"{report.elapsed_seconds:.1f}s ({throughput:,.0f} files/sec)  —  "
            )

        if report is not None and report.fallback_reason:
            self._show_turbo_fallback_banner(report.fallback_reason)
        else:
            self._dismiss_turbo_fallback_banner()

        self.status_var.set(
            f"{engine_prefix}{node.path}  —  {human_size(node.size)} in "
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
        self.status_var.set("Scan failed.")
        messagebox.showerror("Storage Scanner", f"Scan failed:\n{msg}")

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
            banner, style="Accent.TLabel",
            text=f"⚠ Turbo Scan wasn't available for this scan — used the regular scan instead ({reason}).",
        ).pack(side=LEFT)
        ttk.Button(banner, text="✕", width=3, command=dismiss).pack(side=RIGHT)

        banner.pack(side=TOP, fill=X, before=self.toolbar_frame)

    def _dismiss_turbo_fallback_banner(self):
        banner = getattr(self, "_turbo_fallback_banner", None)
        if banner is not None:
            banner.destroy()
            self._turbo_fallback_banner = None

    def cancel_scan(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.status_var.set("Cancelling …")

    #-- Helper Methods for Progress Bar ------------------------------------- #
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
    def _insert_node(self, parent_iid, node, parent_size, index=0):
        fraction = (node.size / parent_size) if parent_size else 0
        percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
        items = f"{node.file_count:,}" if node.is_dir else ""
        if node.error:
            tags = ["error"]
        elif node.is_cloud_placeholder:
            tags = ["cloud"]
        elif node.is_link:
            tags = ["link"]
        else:
            tags = [self._heat_tag(fraction)]   # foreground = space-hog heat
            if node.is_dir:
                tags.append("dir")              # bold, keeps heat color
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

        # A cloud placeholder's `size` is its full logical size (what it'll
        # be once downloaded); `alloc_size` is what's actually using local
        # disk right now — worth showing side by side rather than picking one.
        alloc_text = human_size(node.alloc_size)
        if node.is_cloud_placeholder:
            alloc_text += " (online-only)"

        label = f"{icon} {node.name}" + ("\\" if node.is_dir and not node.name.endswith("\\") else "")
        iid = self.tree.insert(
            parent_iid, END, text=label,
            values=(human_size(node.size), alloc_text, percent, items),
            tags=tuple(tags),
        )
        self.node_by_iid[iid] = node

        # Give expandable dirs a placeholder child so the [+] arrow appears.
        if node.is_dir and node.children:
            self.tree.insert(iid, END, text="…(loading)", tags=("placeholder",))
        return iid
    def _populate_children(self, parent_iid, node):
        # Remove placeholder if present.
        kids = self.tree.get_children(parent_iid)
        if len(kids) == 1 and self.tree.item(kids[0], "text") == "…(loading)":
            self.tree.delete(kids[0])
        elif kids:
            return  # already populated

        ordered = sorted(node.children, key=self._node_sort_key,
                         reverse=self._sort_reverse)
        for index, child in enumerate(ordered):
            self._insert_node(parent_iid, child, parent_size=node.size or 1,
                              index=index)
    def _node_sort_key(self, node):
        if self._sort_key == "name":
            return node.name.lower()
        if self._sort_key == "items":
            return node.file_count
        return node.size
    def _on_open(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if node and node.is_dir:
            self._populate_children(iid, node)
    def _on_double_click(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if node and not node.is_dir:
            self._open_in_explorer()

    # -- Column sorting ---------------------------------------------------- #

    _HEADINGS = {"#0": "Name", "size": "Size", "alloc": "On Disk",
                 "percent": "% of Parent", "items": "Files"}
    def _sort_by(self, key):
        """Handle a heading click: toggle direction if it's the active key,
        else switch to it (names ascend, sizes/counts descend by default)."""
        if key == self._sort_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = (key != "name")
        self._update_heading_arrows()
        self._resort_tree()
    def _update_heading_arrows(self):
        arrow = " ▼" if self._sort_reverse else " ▲"
        # The percent column is driven by the size sort, so it shares the mark.
        active_cols = {"size": ("size", "alloc", "percent"),
                       "name": ("#0",), "items": ("items",)}[self._sort_key]
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
        kids = [k for k in self.tree.get_children(parent_iid)
                if k in self.node_by_iid]
        if not kids:
            return
        kids.sort(key=lambda iid: self._node_sort_key(self.node_by_iid[iid]),
                  reverse=self._sort_reverse)
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
    def _delete_selected(self):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node:
            return
        kind = "folder" if node.is_dir else "file"
        if not messagebox.askyesno(
            f"Delete to {TRASH_NAME}",
            f"Send this {kind} to the {TRASH_NAME}?\n\n{node.path}\n\n"
            f"{human_size(node.size)}"
            + (f" in {node.file_count:,} files" if node.is_dir else ""),
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
    def _reveal(self, path, is_dir):
        try:
            if IS_MACOS:
                if is_dir:
                    subprocess.run(["open", path])
                else:
                    subprocess.run(["open", "-R", path])
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
            messagebox.showinfo("Storage Scanner", "Budgets apply to folders, not individual files.")
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
        self.status_var.set(
            f"Budget set: {node.path} → alert above {human_size(threshold_bytes)}"
        )

    # -- Top 25 largest files --------------------------------------------- #
