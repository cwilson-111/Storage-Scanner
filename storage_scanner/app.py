"""The Storage Scanner Tkinter application.

StorageScannerApp itself is composed from fifteen mixins, each living in its
own file under storage_scanner/ui/ — split out so the toolbar/tree, the tree
filling in during a scan, the scan progress line, history saving, duplicate
detection, search/filter, the treemap, cleanup recommendations, the audit
log, budgets, export and scheduled scans, the largest-files/file-types
windows, the Cleanup Cart, the first-run Getting Started guide, and deleting
(through storage_scanner/delete_service.py) can each be read, changed, and
tested without wading through the others.
"""

import os
import queue
import sys
import threading
import time
from tkinter import TOP, Tk, X, messagebox, ttk
from typing import Optional

from storage_scanner import appearance
from storage_scanner.cart import CartManager
from storage_scanner.history_db import get_app_metadata, open_history_db
from storage_scanner.history_schema import NewerDatabaseError
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_ROOT, resource_path
from storage_scanner.settings import apply_theme, px, use_palette
from storage_scanner.ui.audit_window import AuditMixin
from storage_scanner.ui.automation_window import AutomationMixin
from storage_scanner.ui.budget_window import BudgetMixin
from storage_scanner.ui.cart_window import CartMixin
from storage_scanner.ui.cleanup_window import CleanupMixin
from storage_scanner.ui.data_tools import DataToolsMixin
from storage_scanner.ui.delete_dialogs import DeletionMixin
from storage_scanner.ui.duplicate_window import DuplicatesMixin
from storage_scanner.ui.error_dialog import ErrorDialogMixin
from storage_scanner.ui.file_windows import FileWindowsMixin
from storage_scanner.ui.history_window import HistoryMixin
from storage_scanner.ui.live_tree import LiveTreeMixin
from storage_scanner.ui.main_tree import MainTreeMixin
from storage_scanner.ui.main_window import MainWindowMixin
from storage_scanner.ui.onboarding_window import OnboardingMixin
from storage_scanner.ui.scan_banners import ScanBannersMixin
from storage_scanner.ui.scan_lifecycle import ScanLifecycleMixin
from storage_scanner.ui.scan_progress_panel import ScanProgressMixin
from storage_scanner.ui.search_window import SearchMixin
from storage_scanner.ui.toolbar import ToolbarMixin
from storage_scanner.update_check import check_for_update, release_page_url

# How long closing waits for a history save that's still running. A save
# of a 20,000-folder scan takes about 0.05 s (benchmarks/scale.py); this is
# only a ceiling so a stuck save can't keep the window from ever closing.
CLOSE_WAIT_SECONDS = 120


class StorageScannerApp(
    MainWindowMixin,
    ToolbarMixin,
    ScanLifecycleMixin,
    ScanBannersMixin,
    MainTreeMixin,
    DataToolsMixin,
    LiveTreeMixin,
    ScanProgressMixin,
    HistoryMixin,
    DuplicatesMixin,
    FileWindowsMixin,
    SearchMixin,
    CleanupMixin,
    AuditMixin,
    BudgetMixin,
    AutomationMixin,
    CartMixin,
    OnboardingMixin,
    DeletionMixin,
    ErrorDialogMixin,
):
    def __init__(self, root, initial_path=None):
        self.root = root
        self._initial_path = initial_path
        root.title("Storage Scanner — Elevated (Admin)" if IS_ROOT else "Storage Scanner")
        # Tkinter's default for an exception raised inside a widget callback
        # (button command, bind, etc.) is a traceback on stderr, invisible
        # in a windowed build; this logs it and shows a dialog instead.
        root.report_callback_exception = self._report_tk_callback_exception
        # Opened first: the Appearance setting lives in it, and the palette
        # has to be chosen before any widget takes a colour.
        history_warning = _open_history()
        self.appearance = appearance.resolve(
            get_app_metadata(appearance.SETTING_KEY, appearance.SYSTEM)
        )
        use_palette(self.appearance)
        apply_theme(root)
        root.geometry(f"{px(960)}x{px(640)}")
        if self.appearance == appearance.DARK:
            root.after_idle(lambda: appearance.use_dark_title_bar(root))
            root.bind_class("Toplevel", "<Map>", _dark_title_bar_on_map, add="+")
        try:
            root.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001 - icon is cosmetic; never fail over it
            logger.debug("Main window iconbitmap failed", exc_info=True)

        self.progress_q = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread = None
        # True from start_scan until _finish_scan/_finish_error has handled
        # the scan's result (see LiveTreeMixin._scan_running).
        self._scan_active = False
        self._history_thread = None  # the running history save, if any
        # {normalized folder path: size} from the scan saved before the one
        # on screen: the main tree's Change column (MainWindowMixin._show_changes).
        self._previous_folder_sizes = {}
        self._more_rows = {}  # a level's "N more" row -> its parent row
        self.root_node = None
        self.node_by_iid = {}  # treeview iid -> Node
        self._heat_tags = set()  # quantized heat tags configured so far
        self._sort_key = "size"  # "name" | "size" | "alloc" | "items" | "change"
        self._sort_reverse = True  # sizes default biggest-first

        if history_warning:
            self.root.after(
                0,
                lambda: messagebox.showwarning("Scan history", history_warning, parent=root),
            )
        self.last_scan_id = None
        self.last_previous_scan_id = None
        self.last_growth_rows = []

        # Progress bar for duplicates scan
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

        # The Cleanup Cart: a cross-window queue of items to delete
        # together (storage_scanner/cart.py). Session-only, same as
        # self.duplicates above — reset alongside it wherever a rescan
        # replaces self.root_node, since cart entries hold Node references
        # tied to the old tree.
        self.cart = CartManager()
        # Every delete, from every window, goes through this service
        # (storage_scanner/delete_service.py); it keeps the cart, the
        # duplicate cache above and the tree in step afterwards.
        self._init_deletion()
        self.dup_stats = {
            "files_total": 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "middle_hashed": 0,
        }

        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()
        self._build_scan_progress_panel()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._check_for_update_worker, daemon=True).start()
        self.root.after(500, self._check_budgets_on_launch)
        # First-run guide (see storage_scanner/onboarding.py). Only the GUI
        # ever builds this class — main()'s headless modes return before it.
        self.root.after(500, self._show_onboarding_on_launch)
        # `StorageScanner.exe <folder>` (and the folder menu entry,
        # storage_scanner/explorer_menu.py) scans that folder straight away.
        if initial_path:
            self.root.after(200, self.start_scan)

    def _check_for_update_worker(self):
        newer_tag = check_for_update()
        if newer_tag:
            self.root.after(0, lambda: self._show_update_banner(newer_tag))

    def _show_update_banner(self, newer_tag):
        if getattr(self, "_update_banner", None) is not None:
            return  # already showing one

        banner = ttk.Frame(self.root, padding=(10, 6))
        self._update_banner: Optional[ttk.Frame] = banner

        def open_release_page():
            import webbrowser  # here, not at startup

            webbrowser.open(release_page_url(newer_tag))

        def dismiss():
            banner.destroy()
            self._update_banner = None

        ttk.Label(
            banner,
            style="Accent.TLabel",
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
        if self._saving_history():
            # The save runs on a daemon thread; destroying the window now
            # would end the process mid-save and lose this scan's history.
            self.status_var.set("Finishing saving this scan's history…")
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            self._close_when_saved(time.monotonic() + CLOSE_WAIT_SECONDS)
            return
        self.root.destroy()

    def _close_when_saved(self, deadline):
        if self._saving_history() and time.monotonic() < deadline:
            self.root.after(100, lambda: self._close_when_saved(deadline))
            return
        if self._saving_history():
            logger.warning("Closed before the scan history finished saving")
        self.root.destroy()


def _open_history():
    """Open (create, migrate or recover) the scan history for the GUI, and
    return what the user needs to be told about it, or None. Never raises:
    the app works without its history, so a file it can't use must not keep
    the window from opening."""
    try:
        return open_history_db()
    except NewerDatabaseError as exc:
        logger.warning("%s", exc)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.exception("Could not open the scan history")
        return (
            f"Scan history couldn't be opened ({exc}). Scanning works, but scans "
            "won't be saved to history until this is fixed. Details are in the log."
        )


def main():
    # A headless privileged-scan request (see storage_scanner/elevation.py's
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
    # elevation.py's run_elevated_scan_windows): reads the NTFS MFT as
    # admin and writes the result to --output, never touching Tk. Windows'
    # elevation broker can't hand this process's stdout back to the
    # unprivileged caller the way macOS's --priv-scan can, hence a file
    # instead of stdout — see storage_scanner/mft_scan_cli.py.
    if len(sys.argv) >= 2 and sys.argv[1] == "--mft-scan":
        from storage_scanner.mft_scan_cli import run_mft_scan

        sys.exit(run_mft_scan(sys.argv[2:]))

    # The packaged-build smoke test: build the main window hidden, close it,
    # exit 0 or 1 (see storage_scanner/selftest.py).
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest-gui":
        from storage_scanner.selftest import run_selftest_gui

        sys.exit(run_selftest_gui())

    # A folder to scan straight away: the Windows elevated relaunch passes
    # the folder that was on screen, and the Explorer menu entry the folder
    # clicked (storage_scanner/explorer_menu.py).
    initial_path = folder_argument(sys.argv[1]) if len(sys.argv) > 1 else None
    appearance.enable_dpi_awareness()  # before the first window
    root = Tk()
    StorageScannerApp(root, initial_path=initial_path)  # applies the Structural Light theme
    root.mainloop()


def _dark_title_bar_on_map(event):
    """Every window the dark theme opens gets a dark title bar too."""
    if event.widget.winfo_class() == "Toplevel":
        appearance.use_dark_title_bar(event.widget)


def folder_argument(arg):
    """The folder a command-line argument names, or None. Explorer's quoted
    "%1" turns a drive root into `C:"` -- the backslash escapes the closing
    quote -- so a trailing quote is dropped and a bare drive gets its root
    back."""
    path = arg.strip().rstrip('"')
    if len(path) == 2 and path[1] == ":" and path[0].isalpha():
        path += "\\"
    return path if os.path.isdir(path) else None
