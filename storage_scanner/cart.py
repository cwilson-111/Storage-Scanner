"""The Cleanup Cart: a cross-window queue of items to delete together.

Session-only by design -- cleared on every new scan, never persisted to
disk. A `Node` from a scan that's since been replaced by a rescan is no
longer meaningful (the tree it belonged to is gone), so there's nothing
worth surviving a restart here, unlike the audit ledger or scan history.

Pure logic, no Tkinter -- storage_scanner.ui.cart_window is the Toplevel
window built on top of this, mirroring the cleanup_recommendations.py /
cleanup_window.py split.
"""

import os


class CartManager:
    """Tracks which nodes are queued for deletion and where each was
    added from. A folder `Node` hashes by identity; a `FileNode` view hashes
    and compares by the file row it reads (see storage_scanner.models), so
    the same file looked up twice is still one entry -- the same assumption
    duplicate_window.py's own sets of nodes rely on. Adding the same node
    twice just updates its source label, not duplicates it.
    """

    def __init__(self):
        self._nodes = {}  # Node -> source_label
        self._sampled = set()  # Nodes that are sampled duplicates
        self._duplicates = set()  # Nodes queued as a copy of a duplicate group

    def add(self, node, source_label, is_sampled=False, as_duplicate=False):
        """Queue `node`. `as_duplicate`: it was queued as a redundant copy,
        so it's only deleted while another copy of its group still exists
        (see delete_service)."""
        if node is None:
            return
        self._nodes[node] = source_label
        if is_sampled:
            self._sampled.add(node)
        else:
            self._sampled.discard(node)
        if as_duplicate:
            self._duplicates.add(node)
        else:
            self._duplicates.discard(node)

    def remove(self, node):
        self._nodes.pop(node, None)
        self._sampled.discard(node)
        self._duplicates.discard(node)

    def remove_deleted(self, deleted):
        """Drop every item `deleted` (a delete_service.DeletedSet) covers --
        deleted from any window, or inside a folder that was. Returns True
        if anything was dropped."""
        gone = [node for node in self._nodes if deleted.covers(node)]
        for node in gone:
            self.remove(node)
        return bool(gone)

    def clear(self):
        self._nodes.clear()
        self._sampled.clear()
        self._duplicates.clear()

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, node):
        return node in self._nodes

    def is_duplicate_item(self, node):
        return node in self._duplicates

    def total_bytes(self):
        """What executing the cart would reclaim: an item inside a queued
        folder is already counted in the folder's size."""
        return sum(node.size for node, _label in self.resolve_effective_items())

    def count_sampled_in_effective_items(self):
        """Count how many items in effective (non-nested) items are sampled."""
        effective = self.resolve_effective_items()
        return sum(1 for node, _label in effective if node in self._sampled)

    def items(self):
        """[(node, source_label), ...], insertion order (dicts preserve it)."""
        return list(self._nodes.items())

    def resolve_effective_items(self):
        """`items()`, but dropping any entry whose path is nested under
        another entry's path.

        Both `Node`s can genuinely be queued at once (e.g. a folder added
        from the main tree, and a file inside it separately added from
        Search results before the folder was queued) -- deleting the
        parent already implies the child is gone, so keeping the child
        would either double-count its bytes in the running total, or, if
        the parent is processed first, cause a spurious "failed" result
        when the executor tries to recycle a path that no longer exists.
        The parent wins regardless of add order.
        """
        items = self.items()
        container_paths = [
            os.path.normcase(os.path.normpath(node.path)) + os.sep
            for node, _label in items
            if node.is_dir
        ]
        effective = []
        for node, label in items:
            normalized = os.path.normcase(os.path.normpath(node.path))
            # A directory's own trailing-sep container form can never match
            # its own (non-trailing-sep) normalized path via startswith, so
            # this only ever matches a genuinely different, containing entry.
            nested = any(normalized.startswith(c) for c in container_paths)
            if not nested:
                effective.append((node, label))
        return effective
