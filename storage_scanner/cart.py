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
    """Tracks which `Node`s are queued for deletion and where each was
    added from. `Node` has no `__eq__`/`__hash__` override, so it's
    identity-hashable by default -- the same assumption
    duplicate_window.py's own `set()`s of `Node` already rely on. Adding
    the same node twice just updates its source label, not duplicates it.
    """

    def __init__(self):
        self._nodes = {}  # Node -> source_label
        self._sampled = set()  # Nodes that are sampled duplicates

    def add(self, node, source_label, is_sampled=False):
        if node is None:
            return
        self._nodes[node] = source_label
        if is_sampled:
            self._sampled.add(node)
        else:
            self._sampled.discard(node)

    def remove(self, node):
        self._nodes.pop(node, None)
        self._sampled.discard(node)

    def clear(self):
        self._nodes.clear()
        self._sampled.clear()
    def __len__(self):
        return len(self._nodes)

    def __contains__(self, node):
        return node in self._nodes

    def total_bytes(self):
        return sum(node.size for node in self._nodes)

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
