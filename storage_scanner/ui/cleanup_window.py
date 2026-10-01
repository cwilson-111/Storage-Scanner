"""Cleanup Recommendations: review-first cleanup candidates.

A mixin composed into StorageScannerApp (storage_scanner/app.py). It picks
what to show and opens the window itself, CleanupWindow
(ui/cleanup_view.py). Every row shows why it was flagged, an estimated
recoverable size, a risk level, and a proposed action — nothing here is
ever deleted without an explicit selection and confirmation, and Protected
rows can never be deleted at all.
"""

from tkinter import messagebox

from storage_scanner import cleanup_cache
from storage_scanner.ui.cleanup_view import CleanupWindow


class CleanupMixin:
    def show_cleanup_recommendations(self):
        cleanup_cache.init_cleanup_cache_db()

        # A live scan this session always wins -- otherwise fall back to
        # whatever was last actually computed here (any path, any past
        # session), so opening this straight after launch shows something
        # useful instead of silently doing nothing (the old behavior when
        # self.root_node was None). See cleanup_cache.py's own docstring
        # for why this persists the *computed* recommendation rows, not
        # the raw scanned tree.
        live = self.root_node is not None
        display_scan_path = (
            self.root_node.path if live else cleanup_cache.get_most_recently_cached_scan_path()
        )
        if display_scan_path is None:
            messagebox.showinfo(
                "Cleanup Recommendations",
                "Scan a folder first to see cleanup recommendations.",
            )
            return

        existing = getattr(self, "_cleanup_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        view = CleanupWindow(self, display_scan_path)
        self._cleanup_win = view.win
