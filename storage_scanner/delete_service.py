"""The one way anything in the app is deleted.

Every window -- the main tree, Search & Filter, Cleanup Recommendations
(delete and archive), Find Duplicate Files and the Cleanup Cart -- hands
DeleteService.delete() a list of DeleteRequests. For each one it:

1. refuses protected paths, trimmed Windows names and the scan's own root
   (delete_guard), and a file whose size changed since the scan
   (check_stale);
2. for a copy deleted *as a duplicate*, re-checks on disk that another
   copy of its group is still there (duplicate_finder), so the last copy
   can't go as "a duplicate";
3. asks for the folder's name to be typed for a very large folder;
4. on Windows, asks before deleting permanently anything the Recycle Bin
   can't hold (recycle_windows.bin_blockers), then recycles and checks the
   item really landed in the bin;
5. records the real outcome (delete_outcome) in the audit ledger, even when
   nothing was deleted;
6. tells every subscriber which nodes are gone, once per batch, so the
   Cart, the duplicate cache and every open window drop them.

Tk-free: the questions go through a Confirmer the caller supplies (the UI's
is ui/delete_dialogs.TkConfirmer).

Restoring isn't automated: a one-click "Undo" would have to find the item in
the OS's Recycle Bin/Trash and move it back reliably, and a silent "Undo
succeeded" that actually failed would be worse than none. The audit ledger
says what to look for there, and when.
"""

import os
from typing import Callable, NamedTuple, Optional, Protocol

from history import record_audit_entry
from storage_scanner import delete_guard, duplicate_finder, file_ops, recycle_windows
from storage_scanner.delete_outcome import (
    DELETED_PERMANENTLY,
    FAILED,
    RECYCLED,
    REFUSED,
    UNVERIFIED,
    is_removed,
)
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_WINDOWS

# Messages say why, not what happened: the outcome says that (the Audit Log
# shows "Refused: <message>", a dialog lists them under "Not deleted:").
COULD_NOT_DELETE = "It may be in use, protected, or require admin rights."
_TRASH_MESSAGES = {
    RECYCLED: None,
    UNVERIFIED: (
        "The Recycle Bin couldn't be read to confirm it's there; check the Recycle Bin "
        "before counting on getting it back."
    ),
    DELETED_PERMANENTLY: (
        "Windows couldn't put it in the Recycle Bin, warned it would delete it "
        "permanently, and the warning was answered Yes."
    ),
    REFUSED: (
        "Windows warned it couldn't go to the Recycle Bin and would be deleted "
        "permanently, and the warning was answered No."
    ),
    FAILED: COULD_NOT_DELETE,
}


class DeleteRequest(NamedTuple):
    node: object  # a models.Node/FileNode, or a cleanup_cache.CachedNode
    source: str  # the window, as the audit ledger shows it
    as_duplicate: bool = False  # only if another copy of its group is still on disk
    action: str = "recycle"  # what the ledger calls it ("archive" after archiving)
    scan_root: Optional[str] = None  # the root of the scan it came from, if not the current
    error_context: Optional[str] = None  # added to the ledger's message when not removed


class DeleteResult(NamedTuple):
    request: DeleteRequest
    outcome: str  # a delete_outcome value
    message: Optional[str] = None  # why, when it wasn't a plain recycle

    @property
    def removed(self):
        return is_removed(self.outcome)


class Confirmer(Protocol):
    def confirm_permanent(self, node, reasons: list[str]) -> bool:
        """Delete `node` permanently, since the Recycle Bin can't hold it?"""

    def confirm_large_folder(self, node) -> bool:
        """Has the user typed this very large folder's name to confirm?"""


def _key(path):
    return os.path.normcase(os.path.normpath(path))


class DeletedSet:
    """What one batch deleted, and whether any other node went with it: the
    node itself, or anything under a deleted folder. By path, so it works
    across FileNode views, tree folders and cached rows alike."""

    def __init__(self, nodes):
        self.nodes = list(nodes)
        self._paths = {_key(node.path) for node in self.nodes}
        self._folders = tuple(
            key if key.endswith(os.sep) else key + os.sep
            for key in (_key(node.path) for node in self.nodes if node.is_dir)
        )

    def covers(self, node):
        key = _key(node.path)
        return key in self._paths or (bool(self._folders) and key.startswith(self._folders))


def check_stale(node):
    """None if `node` still looks like what was reviewed, else why not.

    A node can sit reviewed-but-undeleted in a window for as long as the
    user likes; if something else (an installer, a sync client, a save)
    replaces the file at its path meanwhile, deleting by path would remove
    a file nobody reviewed. A size mismatch against a still-existing file is
    a cheap signal of that. Folders are never checked (their own getsize
    isn't their contents' total, and contents drift legitimately), nor is a
    missing path (reported as missing instead).
    """
    if node.is_dir:
        return None
    try:
        current_size = os.path.getsize(node.path)
    except OSError:
        return None
    if current_size != node.size:
        return (
            f"Its size changed since it was reviewed (was {node.size:,} bytes, now "
            f"{current_size:,}), so it may have been modified by something else; rescan "
            "and try again."
        )
    return None


def _send_to_trash(path):
    if IS_WINDOWS:
        return recycle_windows.recycle(path)
    return RECYCLED if file_ops.recycle(path) else FAILED


def _bin_blockers(path, is_dir):
    # The macOS and Linux Trash can take anything a user can delete.
    return recycle_windows.bin_blockers(path, is_dir) if IS_WINDOWS else []


def _delete_permanently(path):
    return recycle_windows.delete_permanently(path) if IS_WINDOWS else FAILED


def _exists(path):
    return recycle_windows.exists(path) if IS_WINDOWS else os.path.lexists(path)


class DeleteService:
    """See the module docstring. `scan_root` and `duplicate_groups` are read
    at delete time (the current scan's root path, and the app's cached Find
    Duplicate Files groups); the other arguments exist for tests."""

    def __init__(
        self,
        scan_root: Callable[[], Optional[str]] = lambda: None,
        duplicate_groups: Callable[[], Optional[list]] = lambda: None,
        record=record_audit_entry,
        trash=_send_to_trash,
        blockers=_bin_blockers,
        delete_permanently=_delete_permanently,
        exists=_exists,
    ):
        self._scan_root = scan_root
        self._duplicate_groups = duplicate_groups
        self._record = record
        self._trash = trash
        self._blockers = blockers
        self._delete_permanently = delete_permanently
        self._exists = exists
        self._listeners: list = []

    def subscribe(self, listener, alive=None):
        """Call `listener(DeletedSet)` after every batch that removed
        something, until the returned function is called or `alive()`
        returns False. Listeners run in subscription order."""
        entry = (listener, alive)
        self._listeners.append(entry)

        def unsubscribe():
            if entry in self._listeners:
                self._listeners.remove(entry)

        return unsubscribe

    def refusal(self, request):
        """Why `request` would be refused without asking anything, or None.
        Lets a window skip its "are you sure?" for a delete that can't
        happen; delete() checks again regardless."""
        node = request.node
        roots = [root for root in (self._scan_root(), request.scan_root) if root]
        return delete_guard.refusal_reason(node.path, roots) or check_stale(node)

    def delete(self, requests, confirmer):
        """Delete each request in turn; returns a DeleteResult per request."""
        results = []
        removed = []
        try:
            for request in requests:
                try:
                    result = self._delete_one(request, confirmer)
                except Exception:  # noqa: BLE001 - one bad item mustn't stop the batch
                    logger.exception("Deleting %r failed", request.node.path)
                    result = DeleteResult(request, FAILED, COULD_NOT_DELETE)
                self._audit(result)
                results.append(result)
                if result.removed:
                    removed.append(request.node)
        finally:
            if removed:
                self._notify(DeletedSet(removed))
        return results

    def _delete_one(self, request, confirmer):
        node = request.node
        reason = self.refusal(request)
        if reason:
            return DeleteResult(request, REFUSED, reason)
        if not self._exists(node.path):
            return DeleteResult(
                request,
                FAILED,
                "It's no longer there (it was moved or deleted outside Storage Scanner "
                "since the scan).",
            )
        if request.as_duplicate:
            reason = self._last_copy_reason(node)
            if reason:
                return DeleteResult(request, REFUSED, reason)
        if delete_guard.needs_typed_confirmation(node) and not confirmer.confirm_large_folder(node):
            return DeleteResult(
                request,
                REFUSED,
                "The folder's name wasn't typed to confirm deleting a folder this large.",
            )
        blockers = self._blockers(node.path, node.is_dir)
        if blockers:
            why = "; ".join(blockers)
            if not confirmer.confirm_permanent(node, blockers):
                return DeleteResult(
                    request,
                    REFUSED,
                    f"The Recycle Bin can't hold it ({why}), and deleting it permanently "
                    "wasn't confirmed.",
                )
            outcome = self._delete_permanently(node.path)
            message = (
                f"Confirmed, because the Recycle Bin can't hold it ({why})."
                if outcome == DELETED_PERMANENTLY
                else COULD_NOT_DELETE
            )
            return DeleteResult(request, outcome, message)
        outcome = self._trash(node.path)
        return DeleteResult(request, outcome, _TRASH_MESSAGES.get(outcome))

    def _last_copy_reason(self, node):
        group = duplicate_finder.find_group(self._duplicate_groups(), node)
        if group is None:
            return (
                "No other copy of it is known any more (the others were deleted, or this "
                "list is from an earlier scan), so it wasn't deleted as a duplicate. If you "
                "still want it gone, delete it from the main tree."
            )
        if not duplicate_finder.another_copy_exists(node, group):
            return (
                "No other copy with the same content is left on disk, so this is the last "
                "one and it wasn't deleted as a duplicate. If you still want it gone, "
                "delete it from the main tree."
            )
        return None

    def _audit(self, result):
        request = result.request
        node = request.node
        message = result.message
        if request.error_context and not result.removed:
            message = f"{message} — {request.error_context}"
        try:
            self._record(
                source=request.source,
                action=request.action,
                path=node.path,
                is_dir=node.is_dir,
                size_bytes=node.size,
                outcome=result.outcome,
                error_message=message,
            )
        except Exception:  # noqa: BLE001 - never let logging break the delete flow
            # This log line is the only record left, so it holds every field
            # the ledger row would have.
            logger.exception(
                "Failed to record audit log entry (source=%r action=%r path=%r "
                "is_dir=%r size_bytes=%r outcome=%r message=%r) — recorded here only, "
                "missing from the Audit Log window",
                request.source,
                request.action,
                node.path,
                node.is_dir,
                node.size,
                result.outcome,
                message,
            )

    def _notify(self, deleted):
        for entry in list(self._listeners):
            listener, alive = entry
            if alive is not None and not alive():
                self._listeners.remove(entry)
                continue
            try:
                listener(deleted)
            except Exception:  # noqa: BLE001 - one broken window mustn't block the rest
                logger.exception("A deletion listener failed")
