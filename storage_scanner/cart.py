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

    def add(self, node, source_label):
        if node is None:
            return
        self._nodes[node] = source_label

    def remove(self, node):
        self._nodes.pop(node, None)

    def clear(self):
        self._nodes.clear()

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, node):
        return node in self._nodes

    def total_bytes(self):
        return sum(node.size for node in self._nodes)

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
