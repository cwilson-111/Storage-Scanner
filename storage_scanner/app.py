"""The Storage Scanner Tkinter application.

StorageScannerApp itself is composed from four mixins, each living in its
own file under storage_scanner/ui/ — split out so the toolbar/tree, history
saving, duplicate detection, and the largest-files/file-types windows can
each be read, changed, and tested without wading through the others.
"""

import os
import queue
import sys
import threading
from tkinter import Tk

from history import init_history_db
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_ROOT, resource_path
from storage_scanner.settings import apply_theme
from storage_scanner.ui.duplicate_window import DuplicatesMixin
from storage_scanner.ui.file_windows import FileWindowsMixin
from storage_scanner.ui.history_window import HistoryMixin
from storage_scanner.ui.main_window import MainWindowMixin


class StorageScannerApp(MainWindowMixin, HistoryMixin, DuplicatesMixin, FileWindowsMixin):
    def __init__(self, root, initial_path=None):
        self.root = root
        self._initial_path = initial_path
        root.title("Neural Storage Matrix — Elevated (Admin)" if IS_ROOT else "Neural Storage Matrix")
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

        init_history_db()
        self.last_scan_id = None
        self.last_previous_scan_id = None
        self.last_growth_rows = []

        
        #Progress bar for duplicates scan
        self.dup_progress_q = queue.Queue()
        self.dup_thread = None
        self.dup_cancel_event = threading.Event()

        
        self.dup_stats = {
            "files_total": 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "full_hashed": 0,
        }


        self._radar_angle = 0 #radar circle for progress


        self._build_header()
        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()
        self._animate_header()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _log_tk_callback_exception(exc, val, tb):
        logger.error("Unhandled exception in Tk callback", exc_info=(exc, val, tb))

    def _on_close(self):
        self.cancel_event.set()
        self.dup_cancel_event.set()
        self.root.destroy()


def main():
    # An elevated relaunch passes the folder that was on screen so the new,
    # privileged instance reopens in the same place instead of resetting.
    initial_path = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None
    root = Tk()
    StorageScannerApp(root, initial_path=initial_path)  # applies the dark cyber theme
    root.mainloop()
