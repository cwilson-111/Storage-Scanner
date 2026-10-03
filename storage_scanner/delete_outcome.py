"""What happened to one delete request: the audit ledger's outcome column.

No imports on purpose: history_records.py/history_schema.py (the ledger itself),
recycle_windows.py (which reports what the shell actually did) and
delete_service.py (which decides) all need the same four words.
"""

RECYCLED = "recycled"  # verifiably in the Recycle Bin/Trash
DELETED_PERMANENTLY = "deleted_permanently"  # gone, not recoverable; the user confirmed it
REFUSED = "refused"  # nothing was touched: a safety check or the user said no
FAILED = "failed"  # the OS couldn't delete it
# Rows written before outcomes were recorded (schema version 3). Their
# success flag only meant "the shell call returned 0", which is also what a
# silent permanent delete returned, so they can't honestly say "recycled".
UNVERIFIED = "unverified"

LABELS = {
    RECYCLED: "Recycled",
    DELETED_PERMANENTLY: "Deleted permanently",
    REFUSED: "Refused",
    FAILED: "Failed",
    UNVERIFIED: "Removed (not verified)",
}


def is_removed(outcome):
    """True if the item is gone from where it was, whichever way."""
    return outcome in (RECYCLED, DELETED_PERMANENTLY, UNVERIFIED)
