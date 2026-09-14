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

from history import record_audit_entry
from storage_scanner.file_ops import recycle
from storage_scanner.logging_setup import logger


def recycle_and_log(node, source, action="recycle"):
    """Send `node` (a Node) to the Recycle Bin/Trash and record the outcome
    in the audit ledger. Drop-in replacement for calling file_ops.recycle()
    directly — same True/False return, plus a durable log entry either way.

    `action` defaults to "recycle" but callers that remove the original as
    part of a larger operation (e.g. archive.py, after compressing it) can
    pass their own label so the audit log reflects what actually happened.
    """
    success = recycle(node.path)
    try:
        record_audit_entry(
            source=source,
            action=action,
            path=node.path,
            is_dir=node.is_dir,
            size_bytes=node.size,
            success=success,
            error_message=None if success else "Recycle/Trash operation failed",
        )
    except Exception:  # noqa: BLE001 - never let logging break the delete flow
        logger.exception("Failed to record audit log entry for %r", node.path)
    return success
