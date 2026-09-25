"""Deletion audit ledger: every delete/recycle action the app performs is
recorded here, regardless of which window triggered it, so there's always
a durable record of what was removed, when, and from where.

Restoring from an entry isn't automated. A one-click "Undo" would need to
reliably locate the item inside the OS's Recycle Bin/Trash and move it
back — on Windows that needs COM interop (IFileOperation) well beyond
plain ctypes, and even on macOS it can collide with a same-named file
already in the Trash. A silent "Undo succeeded" that actually failed would
be worse than no undo button at all, so recovery instead relies on the
OS's own Recycle Bin/Trash — which every deletion in this app already
goes through — and this ledger is what tells you *what* to go look for
there, and when.
"""

import os

from history import record_audit_entry
from storage_scanner.file_ops import recycle
from storage_scanner.logging_setup import logger


def check_stale(node):
    """None if `node` is safe to delete, or a message explaining why not.

    Public (not just recycle_and_log's own internal guard) so a caller
    that needs to explain *why* a delete was refused -- e.g. the Cleanup
    Cart's batch executor, which surfaces a per-item reason rather than a
    generic failure -- can check this ahead of time without duplicating
    the logic.

    A Node is built at scan time and can sit reviewed-but-undeleted in a
    UI list for as long as the user takes to look it over; recycle() then
    acts on `node.path` alone, with no idea whether the file there is
    still the one that was actually reviewed. If something else (an
    installer, a sync client, a save) replaces that path with a different
    file in the meantime, the wrong file gets recycled and the audit
    ledger logs the *original* file's stale size against it. A size
    mismatch against a still-existing file at the same path is a cheap,
    reliable enough signal that this isn't the same file anymore.

    Directories are never checked: a folder's own os.path.getsize() is
    its directory-entry size, not the recursive total `node.size` holds,
    so they're never comparable — and a folder's contents legitimately
    drift during a long review session without that being a problem.
    A missing/inaccessible path isn't treated as stale either — that's
    recycle()'s own failure to report normally, not a swapped-file risk.
    """
    if node.is_dir:
        return None
    try:
        current_size = os.path.getsize(node.path)
    except OSError:
        return None
    if current_size != node.size:
        return (
            f"Refused to delete: file size changed since it was reviewed "
            f"(was {node.size:,} bytes, now {current_size:,}) — it may "
            f"have been modified by something else; rescan and try again."
        )
    return None


def recycle_and_log(node, source, action="recycle", extra_error_context=None):
    """Send `node` (a Node) to the Recycle Bin/Trash and record the outcome
    in the audit ledger. Drop-in replacement for calling file_ops.recycle()
    directly — same True/False return, plus a durable log entry either way.

    `action` defaults to "recycle" but callers that remove the original as
    part of a larger operation (e.g. archive.py, after compressing it) can
    pass their own label so the audit log reflects what actually happened.

    `extra_error_context`, if given, is appended to the failure message —
    e.g. archive.py uses it to record where the .zip ended up when removing
    the original afterward fails, so that path isn't lost from the ledger.

    Refuses to delete (success=False, nothing sent to the Recycle Bin/
    Trash) if the file at `node.path` has changed since it was scanned —
    see check_stale.
    """
    stale_message = check_stale(node)
    if stale_message is not None:
        success = False
        error_message = stale_message
        logger.warning("recycle_and_log refused a stale target: %s", stale_message)
    else:
        success = recycle(node.path)
        error_message = None if success else "Recycle/Trash operation failed"
        if not success and extra_error_context:
            error_message = f"{error_message} — {extra_error_context}"
    try:
        record_audit_entry(
            source=source,
            action=action,
            path=node.path,
            is_dir=node.is_dir,
            size_bytes=node.size,
            success=success,
            error_message=error_message,
        )
    except Exception:  # noqa: BLE001 - never let logging break the delete flow
        # The delete itself already happened; this is the only remaining
        # durable record of it if the audit DB write fails, so log every
        # field the ledger row would have held, not just the path.
        logger.exception(
            "Failed to record audit log entry (source=%r action=%r path=%r "
            "is_dir=%r size_bytes=%r success=%r) — recorded here only, "
            "missing from the Audit Log window",
            source,
            action,
            node.path,
            node.is_dir,
            node.size,
            success,
        )
    return success
