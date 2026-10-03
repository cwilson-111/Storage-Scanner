"""Archive Selected in the Cleanup Recommendations window: compress the
selected Review candidates to verified .zip files and remove the originals
through the delete service.

CleanupWindow (ui/cleanup_view.py) calls archive_review_candidates with
itself; this reads its table, rows and scan, and drops each archived row.
"""

import queue
import threading
import zipfile
from tkinter import (
    BOTTOM,
    TOP,
    DoubleVar,
    StringVar,
    Toplevel,
    X,
    messagebox,
    ttk,
)

from storage_scanner.archive import (
    ArchiveCancelled,
    check_archivable,
    finish_archive,
    likely_compresses_well,
    write_verified_archive,
)
from storage_scanner.cleanup_recommendations import CATEGORY_REVIEW
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import TRASH_NAME


def _archive_progress_dialog(parent):
    """(dialog, percent DoubleVar, label StringVar, cancel Event) for an
    archive run over `parent`, which it keeps from being used meanwhile."""
    dialog = Toplevel(parent)
    dialog.title("Archiving")
    dialog.resizable(False, False)
    dialog.transient(parent)
    label_var = StringVar(value="Starting …")
    progress_var = DoubleVar(value=0)
    cancel_event = threading.Event()
    ttk.Label(dialog, textvariable=label_var, padding=(16, 14, 16, 6), width=60).pack(
        side=TOP, fill=X
    )
    ttk.Progressbar(dialog, variable=progress_var, maximum=100, length=420).pack(
        side=TOP, padx=16, pady=(0, 10)
    )
    cancel = ttk.Button(dialog, text="Cancel")

    def request_cancel():
        cancel_event.set()
        cancel.state(["disabled"])
        label_var.set("Cancelling …")

    cancel.configure(command=request_cancel)
    cancel.pack(side=BOTTOM, pady=(0, 14))
    dialog.protocol("WM_DELETE_WINDOW", request_cancel)
    dialog.grab_set()
    return dialog, progress_var, label_var, cancel_event


def archive_review_candidates(view):
    """Confirm, then archive the Review candidates selected in `view`, a
    CleanupWindow."""
    app, win = view.app, view.win
    if app._refuse_delete_during_scan(parent=win):
        return
    selected = list(view.tv.selection())
    # Archive only applies to Review candidates — duplicates already
    # have a clearer "delete the copy, keep the keeper" story, and
    # Protected rows are never a valid target for anything here.
    targets = [
        (iid, view.iid_to_rec[iid])
        for iid in selected
        if iid in view.iid_to_rec and view.iid_to_rec[iid].category == CATEGORY_REVIEW
    ]
    skipped = len(selected) - len(targets)
    if not targets:
        messagebox.showinfo(
            "Cleanup Recommendations",
            "Select at least one Review candidate to archive "
            "(Archive only applies to that category).",
            parent=win,
        )
        return

    poor = [rec.node.name for _iid, rec in targets if not likely_compresses_well(rec.node.path)]
    warning = ""
    if poor:
        sample = ", ".join(poor[:5])
        more = f" and {len(poor) - 5} more" if len(poor) > 5 else ""
        warning = (
            f"\n\nNote: {len(poor)} of these ({sample}{more}) are already-compressed "
            f"formats and likely won't shrink much."
        )
    note = f" ({skipped} non-Review-candidate row(s) skipped.)" if skipped else ""

    if not messagebox.askyesno(
        "Archive selected files",
        f"Compress {len(targets)} selected file(s) to .zip and remove the "
        f"originals (via {TRASH_NAME}, fully reversible)?{note}{warning}",
        icon="warning",
        parent=win,
    ):
        return

    _ArchiveRun(view, targets).start()


class _ArchiveRun:
    """One archive run over `targets`, (iid, recommendation) pairs from
    `view`.

    Compressing a big file takes minutes, so it runs on a worker thread
    (archive.write_verified_archive); removing each original stays here on
    the Tk thread, since the delete service may ask questions in dialogs.
    """

    def __init__(self, view, targets):
        self.view = view
        self.targets = targets
        self.dialog, self.progress_var, self.label_var, self.cancel_event = (
            _archive_progress_dialog(view.win)
        )
        self.events: queue.Queue[tuple] = queue.Queue()
        self.archived = 0
        self.partial = 0  # archived, but the original couldn't be removed
        self.failed: list[str] = []
        self.cancelled = False

    def start(self):
        threading.Thread(target=self.work, daemon=True).start()
        self.view.win.after(100, self.poll)

    def remove_original(self, request):
        view = self.view
        return view.app._delete_nodes(
            [request._replace(scan_root=view.scan_path, tree=view.scan_tree)],
            view.win,
            report=False,
        )[0]

    def work(self):
        events = self.events
        for iid, rec in self.targets:
            if self.cancel_event.is_set():
                break
            events.put(("file", rec.node.name))
            refused = check_archivable(rec.node)
            if refused:
                events.put(("failed", (iid, rec, refused)))
                continue
            try:
                path = write_verified_archive(
                    rec.node,
                    self.cancel_event,
                    progress=lambda done, total: events.put(("bytes", done / total)),
                )
            except ArchiveCancelled:
                break
            except (OSError, zipfile.BadZipFile) as exc:
                logger.exception("Archiving failed for %r", rec.node.path)
                events.put(("failed", (iid, rec, str(exc))))
                continue
            events.put(("written", (iid, rec, path)))
        events.put(("done", None))

    def on_written(self, iid, rec, path):
        view = self.view
        result = finish_archive(rec.node, "Cleanup Recommendations", path, self.remove_original)
        self.archived += 1
        if not result.original_removed:
            self.partial += 1
            self.failed.append(result.error)
        # A removed original's row is already gone (forget_deleted).
        if iid in view.iid_to_rec:
            del view.iid_to_rec[iid]
            view.tv.delete(iid)

    def poll(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                self.view.win.after(100, self.poll)
                return
            if kind == "file":
                self.label_var.set(f"Compressing {payload} …")
                self.progress_var.set(0)
            elif kind == "bytes":
                self.progress_var.set(payload * 100)
            elif kind == "failed":
                _iid, rec, error = payload
                self.failed.append(f"{rec.node.path}: {error}")
            elif kind == "written":
                self.on_written(*payload)
            else:
                self.cancelled = self.cancel_event.is_set()
                self.dialog.destroy()
                self.report()
                return

    def report(self):
        view = self.view
        if self.archived:
            view.resave_cache()
        status_bits = [f"Archived {self.archived:,} file(s) (rescan to see the .zip files)."]
        if self.partial:
            status_bits.append(f"{self.partial} kept both copies (original couldn't be removed).")
        if self.cancelled:
            status_bits.append("Cancelled; the rest were left as they were.")
        view.app.status_var.set(" ".join(status_bits))
        if self.failed:
            messagebox.showerror(
                "Storage Scanner",
                "Some files could not be archived, or kept their original:\n\n"
                + "\n\n".join(self.failed[:10]),
                parent=view.win,
            )
