"""The dialog shown when something fails inside the window.

Tk only logs an exception raised in a button's or menu's callback, so in a
windowed build a failing action used to do nothing visible at all (the
Growth History bug of P1-4 left an empty window). ErrorDialogMixin makes
StorageScannerApp.report_callback_exception log the error and then say so,
with a button that opens the log folder.
"""

from tkinter import BOTTOM, LEFT, RIGHT, TOP, Toplevel, X, ttk

from storage_scanner import logging_setup
from storage_scanner.logging_setup import logger
from storage_scanner.settings import COLORS

TITLE = "Something went wrong"


def summary_text(exc_value):
    """One line naming the error, for the dialog: the full traceback is in
    the log."""
    text = str(exc_value).strip().splitlines()
    detail = f": {text[0]}" if text else ""
    return f"{type(exc_value).__name__}{detail}"


class ErrorDialogMixin:
    _error_dialog = None

    def _report_tk_callback_exception(self, exc, val, tb):
        logger.error("Unhandled exception in Tk callback", exc_info=(exc, val, tb))
        try:
            self._show_error_dialog(val)
        except Exception:  # noqa: BLE001 - the dialog must never raise in its turn
            logger.exception("Could not show the error dialog")

    def _show_error_dialog(self, exc_value):
        """One dialog at a time: an error that repeats (a timer's callback)
        mustn't stack a new window on every tick."""
        if self._error_dialog is not None and self._error_dialog.winfo_exists():
            return
        dialog = Toplevel(self.root)
        self._error_dialog = dialog
        dialog.title(TITLE)
        dialog.configure(bg=COLORS["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)

        folder = logging_setup.log_dir
        where = f"\n\nThe details are in the log, in {folder}." if folder else ""
        ttk.Label(
            dialog,
            padding=(16, 14, 16, 10),
            wraplength=520,
            justify=LEFT,
            text=f"That didn't work.\n\n{summary_text(exc_value)}{where}",
        ).pack(side=TOP, fill=X)

        buttons = ttk.Frame(dialog, padding=(16, 0, 16, 14))
        buttons.pack(side=BOTTOM, fill=X)
        if folder:
            ttk.Button(
                buttons,
                text="Open Log Folder",
                command=lambda: self._reveal(str(folder), is_dir=True),
            ).pack(side=LEFT)
        close = ttk.Button(buttons, text="Close", command=dialog.destroy)
        close.pack(side=RIGHT)
        close.focus_set()
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
