"""Run one long job (a CSV conversion, an export) off the Tk thread.

The window keeps repainting while it runs, under a small dialog that holds
the app's input (grab_set), so the scanned tree can't be changed under a
job reading it. The job itself can't be cancelled: none of these
converters stop part-way.
"""

import queue
import threading
from tkinter import TOP, Toplevel, X, ttk

from storage_scanner.logging_setup import logger

_POLL_MS = 100


def run_busy(parent, title, message, work, on_done, on_error):
    """Call work() on a worker thread, then on_done(its result) or
    on_error(the exception) back on the Tk thread once it's over."""
    dialog = Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # it ends when the job does
    ttk.Label(dialog, text=message, padding=(16, 14, 16, 6), width=60).pack(side=TOP, fill=X)
    bar = ttk.Progressbar(dialog, mode="indeterminate", length=420)
    bar.pack(side=TOP, padx=16, pady=(0, 16))
    bar.start(15)
    dialog.grab_set()

    outcome: queue.Queue[tuple] = queue.Queue()

    def worker():
        try:
            outcome.put((True, work()))
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the UI
            logger.exception("%s failed", title)
            outcome.put((False, exc))

    def poll():
        try:
            ok, value = outcome.get_nowait()
        except queue.Empty:
            dialog.after(_POLL_MS, poll)
            return
        dialog.grab_release()
        dialog.destroy()
        (on_done if ok else on_error)(value)

    threading.Thread(target=worker, name=f"busy: {title}", daemon=True).start()
    dialog.after(_POLL_MS, poll)
