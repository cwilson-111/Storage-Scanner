"""What a row of the main tree shows, and in what order a level's rows go
-- for a finished scan and for one still running. No Tk here, so it can be
tested directly; storage_scanner/ui/main_window.py (finished rows) and
storage_scanner/ui/live_tree.py (rows filling in during a scan) put it on
screen.

While a Compatible scan runs, every folder row shows its running totals
(read through scan_progress.WalkTracker.folders) and its state: queued
rows are greyed with no numbers yet, a folder being read shows ⏳ in place
of its folder icon, and a done folder looks exactly like it will once the
scan finishes. A file's own numbers are final as soon as it's listed; only
its share of its (growing) folder changes.
"""

from dataclasses import dataclass
from typing import Optional

from storage_scanner.formatting import bar, human_size
from storage_scanner.scan_progress import QUEUED, SCANNING

QUEUED_ICON = "◌"
SCANNING_ICON = "⏳"
_NOT_YET = "…"


@dataclass(frozen=True)
class RowDisplay:
    text: str  # icon and name
    values: tuple  # (size, on disk, % of parent, files)
    tags: tuple  # style tags, besides the heat tag and the row stripe
    heat: Optional[float]  # share of the parent for the heat colour; None: no heat tag


def row_label(node, icon):
    suffix = "\\" if node.is_dir and not node.name.endswith("\\") else ""
    return f"{icon} {node.name}{suffix}"


def row_display(node, size, alloc_size, file_count, parent_size, state=None):
    """The row for `node` with these totals, as a share of `parent_size`
    (its parent's size; the root row passes its own). `state` is a folder's
    scan state while a scan is running, None once it has finished."""
    if state == QUEUED:
        return RowDisplay(
            text=row_label(node, QUEUED_ICON),
            values=(_NOT_YET, _NOT_YET, "—", _NOT_YET),
            tags=("placeholder", "dir"),
            heat=None,
        )
    fraction = min(1.0, max(0.0, size / parent_size)) if parent_size else 0.0
    percent = f"{bar(fraction)} {fraction * 100:5.1f}%"
    items = f"{file_count:,}" if node.is_dir else ""
    # A cloud placeholder's size is its full logical size (what it'll be
    # once downloaded); its on-disk size is what's actually using local
    # disk right now -- worth showing side by side rather than picking one.
    alloc_text = human_size(alloc_size)
    if node.is_cloud_placeholder:
        alloc_text += " (online-only)"

    heat = None
    if node.error:
        icon, tags = "⚠", ("error",)
    elif node.is_cloud_placeholder:
        icon, tags = "☁", ("cloud",)
    elif node.is_link:
        icon, tags = "↪", ("link",)
    else:
        icon = "📁" if node.is_dir else "📄"
        tags = ("dir",) if node.is_dir else ()  # bold, keeps the heat colour
        heat = fraction  # foreground = space-hog heat
    if state == SCANNING:
        icon = SCANNING_ICON
    return RowDisplay(
        text=row_label(node, icon),
        values=(human_size(size), alloc_text, percent, items),
        tags=tags,
        heat=heat,
    )


def node_display(node, parent_size):
    """row_display() for a finished scan's node, from its own totals."""
    return row_display(node, node.size, node.alloc_size, node.file_count, parent_size)


def sort_key_function(key):
    """The key a level's rows sort by for a heading's sort key ("name",
    "size" or "items"); sizes and counts read a folder's running totals
    while a scan is still filling them in."""
    if key == "name":
        return lambda node: node.name.lower()
    if key == "items":
        return lambda node: node.file_count
    return lambda node: node.size


def resorted(order, node_of, key, reverse):
    """`order` (a level's row ids, as shown) sorted by `key`. Rows that
    compare equal keep the order they're shown in, so a level full of
    still-empty folders doesn't reshuffle on every pass."""
    sort_key = sort_key_function(key)
    return sorted(order, key=lambda iid: sort_key(node_of[iid]), reverse=reverse)
