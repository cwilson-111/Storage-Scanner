"""Deleting from any window: the app's side of storage_scanner.delete_service.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Every
window's Delete button builds DeleteRequests and calls _delete_nodes(); the
service decides and records, TkConfirmer asks its questions, and
_after_nodes_deleted takes whatever was removed out of the Cleanup Cart, the
cached duplicate groups and the scanned tree. A window that lists nodes
subscribes through _watch_deletions() to drop its own rows.
"""

from tkinter import BOTTOM, LEFT, RIGHT, TOP, StringVar, Toplevel, X, messagebox, ttk

from storage_scanner.delete_guard import typed_name_matches
from storage_scanner.delete_outcome import DELETED_PERMANENTLY, RECYCLED, UNVERIFIED
from storage_scanner.delete_service import DeleteService
from storage_scanner.duplicate_finder import prune_groups
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.models import remove_from_tree
from storage_scanner.platform_support import TRASH_NAME, resource_path
from storage_scanner.settings import COLORS, px

# How many refused/failed items one error dialog lists by name.
_MAX_LISTED = 10


def _ask(parent, title, text, confirm_text, must_type=None):
    """Modal "Cancel" / `confirm_text` question; True only for the latter.
    With `must_type` (a node), the confirm button stays disabled until the
    node's name has been typed."""
    dialog = Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=COLORS["bg"])
    dialog.resizable(False, False)
    dialog.transient(parent)
    try:
        dialog.iconbitmap(resource_path("icon.ico"))
    except Exception:  # noqa: BLE001 - icon is cosmetic
        logger.debug("Delete dialog iconbitmap failed", exc_info=True)

    answer = {"value": False}
    ttk.Label(dialog, padding=(16, 14, 16, 10), wraplength=px(520), justify=LEFT, text=text).pack(
        side=TOP, fill=X
    )

    buttons = ttk.Frame(dialog, padding=(16, 0, 16, 14))
    buttons.pack(side=BOTTOM, fill=X)

    def close(value):
        answer["value"] = value
        dialog.destroy()

    confirm = ttk.Button(buttons, text=confirm_text, command=lambda: close(True))
    confirm.pack(side=LEFT)
    cancel = ttk.Button(buttons, text="Cancel", command=lambda: close(False))
    cancel.pack(side=RIGHT)

    if must_type is not None:
        typed = StringVar()
        entry = ttk.Entry(dialog, textvariable=typed, width=50)
        entry.pack(side=TOP, fill=X, padx=16, pady=(0, 12))
        confirm.state(["disabled"])

        def on_typed(*_args):
            confirm.state(
                ["!disabled" if typed_name_matches(typed.get(), must_type) else "disabled"]
            )

        typed.trace_add("write", on_typed)
        entry.focus_set()
    else:
        cancel.focus_set()

    dialog.bind("<Escape>", lambda _e: close(False))
    dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
    dialog.grab_set()
    parent.wait_window(dialog)
    return answer["value"]


class TkConfirmer:
    """delete_service.Confirmer as modal dialogs over `parent`."""

    def __init__(self, parent):
        self.parent = parent

    def confirm_permanent(self, node, reasons):
        kind = "folder" if node.is_dir else "file"
        why = "\n".join(f"• {reason[0].upper()}{reason[1:]}." for reason in reasons)
        return _ask(
            self.parent,
            "Delete permanently?",
            f"This {kind} can't go to the {TRASH_NAME}:\n\n{node.path}\n\n{why}\n\n"
            "Delete it permanently instead? It can't be restored afterwards.",
            "Delete permanently",
        )

    def confirm_large_folder(self, node):
        return _ask(
            self.parent,
            "Delete a very large folder?",
            f"{node.path}\n\nholds {human_size(node.size)} in "
            f"{getattr(node, 'file_count', 0):,} files. To delete it, type its name "
            f"exactly:\n\n{node.name}",
            "Delete",
            must_type=node,
        )


class DeletionMixin:
    def _init_deletion(self):
        self.delete_service = DeleteService(
            scan_root=lambda: self.root_node.path if self.root_node is not None else None,
            duplicate_groups=lambda: self.duplicates,
            tree=lambda: self.root_node,
            scanning=self._deleting_blocked,
        )
        # First, so every window's own listener sees the pruned cart and
        # duplicate cache.
        self.delete_service.subscribe(self._after_nodes_deleted)

    def _watch_deletions(self, win, listener):
        """Call `listener(DeletedSet)` after each delete while `win` is open."""
        self.delete_service.subscribe(listener, alive=win.winfo_exists)

    def _delete_nodes(self, requests, parent=None, report=True):
        """Run `requests` (delete_service.DeleteRequests) through the delete
        service, asking any questions over `parent`; returns the results.
        With `report`, says what happened in the status bar, and in a dialog
        whatever wasn't simply deleted."""
        parent = parent or self.root
        results = self.delete_service.delete(requests, TkConfirmer(parent))
        if report:
            self._report_delete_results(results, parent)
        return results

    def _report_delete_results(self, results, parent):
        recycled = sum(1 for r in results if r.outcome in (RECYCLED, UNVERIFIED))
        permanent = sum(1 for r in results if r.outcome == DELETED_PERMANENTLY)
        kept = [r for r in results if not r.removed]
        summary = []
        if recycled:
            summary.append(f"Sent {recycled:,} item(s) to the {TRASH_NAME}.")
        if permanent:
            summary.append(f"Deleted {permanent:,} item(s) permanently.")
        if kept:
            summary.append(f"{len(kept):,} item(s) not deleted.")
        self.status_var.set(" ".join(summary) or "Nothing was deleted.")

        attention = kept + [r for r in results if r.outcome == UNVERIFIED]
        if not attention:
            return
        detail = "\n\n".join(
            f"{r.request.node.path}\n  {r.message}" for r in attention[:_MAX_LISTED]
        )
        if len(attention) > _MAX_LISTED:
            detail += f"\n\n…and {len(attention) - _MAX_LISTED:,} more (see the Audit Log)."
        heading = "Not deleted:" if kept and len(attention) == 1 else "Please check:"
        messagebox.showerror("Storage Scanner", f"{heading}\n\n{detail}", parent=parent)

    def _after_nodes_deleted(self, deleted):
        self.cart.remove_deleted(deleted)
        if self.duplicates:
            self.duplicates = prune_groups(self.duplicates, deleted)
        self._remove_deleted_from_tree(deleted)
        self._refresh_cart_indicator()

    def _remove_deleted_from_tree(self, deleted):
        """Take deleted nodes out of the scanned tree: through their main-tree
        row when they have one (which also fixes up every row above it),
        else from the tree itself, then redraw the rows that are shown."""
        iid_for_node = {node: iid for iid, node in self.node_by_iid.items()}
        redraw = False
        for node in deleted.nodes:
            iid = iid_for_node.get(node)
            if iid is not None and self.tree.exists(iid):
                self._remove_main_tree_row(iid)
            elif self.root_node is not None and remove_from_tree(self.root_node, node):
                redraw = True
        if redraw:
            for iid in list(self.node_by_iid):
                if self.tree.exists(iid):
                    self._refresh_row(iid)
            self.status_var.set(
                f"{self.root_node.path}  —  {human_size(self.root_node.size)} "
                f"in {self.root_node.file_count:,} files"
            )
