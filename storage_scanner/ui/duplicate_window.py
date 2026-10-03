"""Running the duplicate finder from the main window, then opening the
Duplicate Files window (ui/duplicates_view.py) with what it found.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Finding
the groups is storage_scanner.duplicate_finder's.
"""

import queue
import threading
from tkinter import messagebox

from storage_scanner.cleanup_recommendations import is_protected_path
from storage_scanner.duplicate_finder import find_duplicate_files
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.ui.app_state import AppMixin
from storage_scanner.ui.duplicates_view import DuplicatesWindow


class DuplicatesMixin(AppMixin):
    def _should_skip_duplicate_scan(self, path):
        """Return True if this path should be ignored during duplicate scans."""
        return is_protected_path(path)

    def _find_duplicate_files(self, progress_q=None, cancel_event=None):
        """duplicate_finder.find_duplicate_files over this session's tree."""
        return find_duplicate_files(
            self.root_node, progress_q, cancel_event, skip=self._should_skip_duplicate_scan
        )

    def show_duplicates(self):
        if not self.root_node:
            return

        if self.dup_thread and self.dup_thread.is_alive():
            messagebox.showinfo("Storage Scanner", "Duplicate scan is already running.")
            return

        self.dup_cancel_event.clear()
        self.cancel_btn.config(text="Cancel", state="normal")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")

        self.dup_stats = {
            "files_total": self.root_node.file_count if self.root_node else 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "middle_hashed": 0,
        }

        total_files = max(1, self.root_node.file_count)
        self._start_determinate_progress(total_files)
        self.status_var.set("Preparing duplicate scan …")

        self.dup_thread = threading.Thread(
            target=self._duplicate_worker,
            daemon=True,
        )
        self.dup_thread.start()

        self.root.after(100, self._poll_duplicate_progress)

    def _duplicate_worker(self):
        root = self.root_node  # what's hashed, even if a rescan replaces it meanwhile
        try:
            duplicates = self._find_duplicate_files(
                progress_q=self.dup_progress_q,
                cancel_event=self.dup_cancel_event,
            )

            if self.dup_cancel_event.is_set():
                self.dup_progress_q.put(("cancelled", None))
                return
            self.duplicates = duplicates
            # Tied to the exact root_node these results came from, so a
            # rescan of a different path (which sets root_node to a new
            # object) can never be mistaken for still having a valid
            # cached duplicate set -- see start_scan's matching reset.
            self._duplicates_scan_root = root
            self.dup_progress_q.put(("done", duplicates))

        except Exception as exc:
            logger.exception("Duplicate scan failed")
            self.dup_progress_q.put(("error", str(exc)))

    def _poll_duplicate_progress(self):
        try:
            while True:
                msg = self.dup_progress_q.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _kind, current, total, text = msg
                    self.progress.config(maximum=max(1, total))
                    self._update_determinate_progress(current)

                    percent = (current / max(1, total)) * 100
                    skipped = self.dup_stats.get("files_skipped", 0)
                    skipped_bytes = self.dup_stats.get("bytes_skipped", 0)

                    self.status_var.set(
                        f"{text}  ({percent:5.1f}%)  |  "
                        f"Skipped: {skipped:,} files / {human_size(skipped_bytes)}"
                    )

                elif kind == "stats":
                    _kind, stats = msg
                    self.dup_stats = stats

                elif kind == "done":
                    _kind, duplicates = msg
                    self._stop_progress()
                    # A scan started since (and cancelled this search, too
                    # late): the groups are from the tree it replaced.
                    scan_tree = self._duplicates_scan_root
                    if self._scan_running() or scan_tree is None or scan_tree is not self.root_node:
                        return
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self._show_duplicates_window(scan_tree, duplicates)
                    return

                elif kind == "cancelled":
                    self._stop_progress()
                    if self._scan_running():  # the scan that cancelled it owns these now
                        return
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan cancelled.")
                    return

                elif kind == "error":
                    _kind, error_msg = msg
                    self._stop_progress()
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan failed.")
                    messagebox.showerror("Storage Scanner", f"Duplicate scan failed:\n{error_msg}")
                    return

        except queue.Empty:
            pass

        self.root.after(100, self._poll_duplicate_progress)

    def _show_duplicates_window(self, scan_tree, duplicates):
        existing = getattr(self, "_duplicates_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()
        view = DuplicatesWindow(self, scan_tree, duplicates)
        self._duplicates_win = view.win
        if not view.group_count:
            self.status_var.set("No duplicate files found.")
        else:
            self.status_var.set(
                f"Found {view.group_count:,} duplicate groups. "
                f"Potential cleanup: {human_size(view.total_wasted)}"
            )
