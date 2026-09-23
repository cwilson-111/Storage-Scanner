"""The Storage Scanner Tkinter application.

StorageScannerApp itself is composed from ten mixins, each living in its
own file under storage_scanner/ui/ — split out so the toolbar/tree, history
saving, duplicate detection, search/filter, the treemap, cleanup
recommendations, the audit log, budgets, export and scheduled scans, and the
largest-files/file-types windows can each be read, changed, and tested
without wading through the others.
"""

import os
import queue
import sys
import threading
import webbrowser
from tkinter import TOP, X, ttk, Tk

from history import init_history_db
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_ROOT, resource_path
from storage_scanner.settings import apply_theme
from storage_scanner.update_check import RELEASES_PAGE_URL, check_for_update
from storage_scanner.ui.audit_window import AuditMixin
from storage_scanner.ui.automation_window import AutomationMixin
from storage_scanner.ui.budget_window import BudgetMixin
from storage_scanner.ui.cleanup_window import CleanupMixin
from storage_scanner.ui.duplicate_window import DuplicatesMixin
from storage_scanner.ui.file_windows import FileWindowsMixin
from storage_scanner.ui.history_window import HistoryMixin
from storage_scanner.ui.main_window import MainWindowMixin
from storage_scanner.ui.search_window import SearchMixin
from storage_scanner.ui.treemap_window import TreemapMixin


class StorageScannerApp(
    MainWindowMixin, HistoryMixin, DuplicatesMixin, FileWindowsMixin,
    SearchMixin, TreemapMixin, CleanupMixin, AuditMixin, BudgetMixin,
    AutomationMixin,
):
    def __init__(self, root, initial_path=None):
        self.root = root
        self._initial_path = initial_path
        root.title("Storage Scanner — Elevated (Admin)" if IS_ROOT else "Storage Scanner")
        root.geometry("960x640")
        # Tkinter's default behavior for an exception raised inside a widget
        # callback (button command, bind, etc.) is to print a traceback to
        # stderr and keep going — invisible in a windowed/no-console build.
        root.report_callback_exception = self._log_tk_callback_exception
        apply_theme(root)
        try:
            root.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic; never fail over it
            logger.debug("Main window iconbitmap failed", exc_info=True)

        self.progress_q = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread = None
        self.root_node = None
        self.node_by_iid = {}   # treeview iid -> Node
        self._heat_tags = set()  # quantized heat tags configured so far
        self._sort_key = "size"  # "name" | "size" | "items"
        self._sort_reverse = True   # sizes default biggest-first

        # Live scan preview (see main_window._start_live_tree) -- only ever
        # populated for a Compatible-engine scan, which builds its Node
        # tree in place as it walks; Turbo Scan has no equivalent (its
        # tree only exists once the whole MFT has been parsed).
        self._live_root_node = None
        self._live_root_iid = None
        self._live_total_bytes = 0
        self._live_expanded_iids = set()
        self._last_live_refresh = 0.0

        init_history_db()
        self.last_scan_id = None
        self.last_previous_scan_id = None
        self.last_growth_rows = []

        
        #Progress bar for duplicates scan
        self.dup_progress_q = queue.Queue()
        self.dup_thread = None
        self.dup_cancel_event = threading.Event()

        # The last completed "Find Duplicate Files" result, kept around so
        # Cleanup Recommendations (and a reopened Duplicate Files window)
        # can reuse it instead of re-hashing every file a second time, and
        # so results aren't lost just because that window was closed. Tied
        # to the exact root_node it was computed from (see
        # DuplicatesMixin._remove_from_duplicate_cache and start_scan's
        # reset of both) so a rescan of a different path can't serve stale
        # duplicate data.
        self.duplicates = None
        self._duplicates_scan_root = None

        
        self.dup_stats = {
            "files_total": 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "full_hashed": 0,
        }


        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._check_for_update_worker, daemon=True).start()
        self.root.after(500, self._check_budgets_on_launch)

    @staticmethod
    def _log_tk_callback_exception(exc, val, tb):
        logger.error("Unhandled exception in Tk callback", exc_info=(exc, val, tb))

    def _check_for_update_worker(self):
        newer_tag = check_for_update()
        if newer_tag:
            self.root.after(0, lambda: self._show_update_banner(newer_tag))

    def _show_update_banner(self, newer_tag):
        if getattr(self, "_update_banner", None) is not None:
            return  # already showing one

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._update_banner = banner

        def open_release_page():
            webbrowser.open(RELEASES_PAGE_URL)

        def dismiss():
            banner.destroy()
            self._update_banner = None

        ttk.Label(
            banner, style="Accent.TLabel",
            text=f"⬆ A newer version ({newer_tag}) is available.",
        ).pack(side="left")
        ttk.Button(banner, text="View Release", command=open_release_page).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(banner, text="✕", width=3, command=dismiss).pack(side="right")

        banner.pack(side=TOP, fill=X, before=self.toolbar_frame)

    def _on_close(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.root.destroy()


def main():
    # A headless privileged-scan request (see storage_scanner/file_ops.py's
    # run_elevated_scan_macos): runs the scan as root and exits, never
    # touching Tk, so it never needs a window-server connection it can't get.
    if len(sys.argv) >= 3 and sys.argv[1] == "--priv-scan":
        from storage_scanner.priv_scan_cli import run_priv_scan
        run_priv_scan(sys.argv[2])
        return

    # Documented headless CLI mode: `Storage-Scanner.py --cli <path>
    # [--format json|csv] [--output FILE]` — for scripts, cron, Task
    # Scheduler, or any other automation. See storage_scanner/cli.py.
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        from storage_scanner.cli import run_cli
        sys.exit(run_cli(sys.argv[2:]))

    # A headless elevated Turbo Scan request (see storage_scanner/
    # file_ops.py's run_elevated_scan_windows): reads the NTFS MFT as
    # admin and writes the result to --output, never touching Tk. Windows'
    # elevation broker can't hand this process's stdout back to the
    # unprivileged caller the way macOS's --priv-scan can, hence a file
    # instead of stdout — see storage_scanner/mft_scan_cli.py.
    if len(sys.argv) >= 2 and sys.argv[1] == "--mft-scan":
        from storage_scanner.mft_scan_cli import run_mft_scan
        sys.exit(run_mft_scan(sys.argv[2:]))

    # A Windows elevated relaunch passes the folder that was on screen so
    # the new, privileged instance reopens in the same place instead of
    # resetting.
    initial_path = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
    root = Tk()
    StorageScannerApp(root, initial_path=initial_path)  # applies the Structural Light theme
    root.mainloop()
