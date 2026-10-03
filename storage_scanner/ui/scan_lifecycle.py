"""A scan from start to finish: which engine (and asking about Turbo
Scan), the worker threads, the 100 ms progress poll, and putting the
finished tree on screen.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import json
import os
import queue
import threading
from tkinter import (
    BOTTOM,
    LEFT,
    RIGHT,
    TOP,
    Toplevel,
    X,
    messagebox,
    ttk,
)

from storage_scanner import (
    turbo_scan,
)
from storage_scanner.drive_info import is_ntfs_fixed_drive
from storage_scanner.file_ops import (
    relaunch_elevated_windows,
)
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    IS_ROOT,
    IS_WINDOWS,
    resource_path,
)
from storage_scanner.scan_progress_model import CANCELLED, FAILED
from storage_scanner.scanner import find_inaccessible_paths
from storage_scanner.serialization import dict_to_node
from storage_scanner.settings import COLORS, px
from storage_scanner.ui.app_state import AppMixin


class ScanLifecycleMixin(AppMixin):
    # What _ask_turbo_scan_mode returns.
    TURBO_RESTART_AS_ADMIN = "restart"

    TURBO_REGULAR_SCAN = "regular"

    TURBO_THIS_SCAN_ONLY = "once"

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
            wraplength=px(470),
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
            wraplength=px(470),
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
        if self._scan_running():
            return
        target = self.path_var.get().strip().strip('"')
        if not target or not os.path.exists(target):
            messagebox.showerror("Storage Scanner", f"Path does not exist:\n{target}")
            return

        turbo_enabled = self._resolve_turbo_scan_consent(target)
        if turbo_enabled is None:  # restarting elevated; this window is gone
            return

        self._scan_active = True
        self._begin_scan_view(target)
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self.status_var.set(f"Scanning {target} …")
        self._scan_progress_start(target)

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
        if self._scan_running():
            messagebox.showerror("Storage Scanner", "A scan is already running.")
            return

        self._scan_active = True
        self._begin_scan_view(target)
        self.scan_btn.config(state="disabled")
        self.elevate_btn.config(state="disabled")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")
        self.status_var.set(f"Requesting elevated access for {target} … {waiting_suffix}")
        self._scan_progress_start(target)
        self._scan_progress_phase(self.PHASE_WAITING_FOR_ELEVATED_SCAN)

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
                if kind == "live_tree":
                    self._live_attach(payload)
                elif kind == "done":
                    node, report = payload
                    self._finish_scan(node, report)
                    return
                elif kind == "error":
                    self._finish_error(payload)
                    return
                else:
                    self._scan_progress_message(kind, payload)
        except queue.Empty:
            pass
        self._live_tick(self._scan_progress_refresh())
        self.root.after(100, self._poll_progress)

    # -- History helper functions ------------------------------------------ #
    def _finish_scan(self, node, report=None):
        self._scan_active = False
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if hasattr(self, "elevate_btn"):
            self.elevate_btn.config(state="normal")

        if self.cancel_event.is_set():
            self._scan_progress_end(CANCELLED)
            self.status_var.set("Scan cancelled.")
            self._refresh_tools_state()
            return

        # The rows the live tree filled in (see ui/live_tree.py) are
        # rebuilt here from `node`'s final, rolled-up numbers, rather than
        # reconciled in place; then the folders opened during the scan are
        # reopened and the focused row and scroll position put back.
        view_state = self._live_view_state()
        self._live_reset()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self._more_rows.clear()

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
        self.treemap_pane.show([node])
        if view_state is not None:
            self._restore_view_state(view_state)
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

        progress_token = self._scan_progress_phase(self.PHASE_SAVING_HISTORY)
        self._history_thread = threading.Thread(
            target=self._save_history_worker,
            args=(node, progress_token),
            daemon=True,
        )
        self._history_thread.start()

    def _finish_error(self, msg):
        self._scan_active = False
        self._scan_progress_end(FAILED)
        self._live_freeze()
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if hasattr(self, "elevate_btn"):
            self.elevate_btn.config(state="normal")
        self._refresh_tools_state()
        self.status_var.set("Scan failed.")
        messagebox.showerror("Storage Scanner", f"Scan failed:\n{msg}")

    def cancel_scan(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.status_var.set("Cancelling …")
        self._scan_progress_cancel_requested()
        self._live_freeze()

    # -- Status-bar progress bar (duplicate scan; scans use ScanProgressMixin) #
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
