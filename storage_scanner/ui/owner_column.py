"""The main tree's Owner column: who owns each row's file or folder.

The scan never reads owners (storage_scanner/owner.py says why). The
finished tree asks for the owners of the rows on screen -- whenever those
change: a scroll, a folder opened or closed, a resize (Tk calls the tree's
yscrollcommand, _owner_watch), or a re-sort (which says so itself) -- and
the cells fill in a moment later: storage_scanner.owner's OwnerLookup
reads them on a worker thread, and the Tk thread collects them every
POLL_MS while any are outstanding. Owners are kept by path for the tree on
screen, so a row shown again (scrolled back to, re-sorted, refreshed after
a delete, another page) costs nothing. A new scan forgets them; the rows of
a scan still running get none.

Sorting by Owner needs the owner of every row a level lists, on screen or
not, so then every row asks for its own as it's inserted (a level lists at
most ROWS_PER_PAGE rows at a time). Rows sort by the owners already known,
A to Z, with the rest (not looked up yet, or blank) after them; a level
re-sorts once its rows' owners have come in. A level of more than
ROWS_PER_PAGE rows doesn't read every row's owner to sort: its next page
lists rows nobody has looked up yet, which then take their place among the
others.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

from storage_scanner.owner import OwnerLookup
from storage_scanner.ui.app_state import AppMixin

# How often the Tk thread collects owners while any are outstanding, and
# how many it writes per tick (one Tk call each, about 10 µs).
POLL_MS = 50
CELLS_PER_TICK = 2000


class OwnerColumnMixin(AppMixin):
    def _init_owner_column(self):
        self._owner_lookup = OwnerLookup()
        self._owners = {}  # path -> owner ("" when it can't be read)
        self._owner_rows = {}  # path -> [(iid, node)] rows waiting for it
        self._owner_batch = []  # paths to send at the next idle moment
        self._owner_polling = False
        self._owner_view_pending = False  # a look at the rows on screen is due
        self._owner_levels = set()  # parent rows to re-sort once owners settle

    def _owner_reset(self):
        """A new scan: its paths may have new owners."""
        self._owner_lookup.cancel()
        self._owners.clear()
        self._owner_rows.clear()
        self._owner_batch.clear()
        self._owner_levels.clear()

    def _owner_watch(self, set_scrollbar):
        """The main tree's yscrollcommand: moves the scrollbar, then asks
        for the owners of the rows now on screen."""

        def scrolled(first, last):
            set_scrollbar(first, last)
            self._owner_view_changed()

        return scrolled

    def _known_owner(self, node):
        """`node`'s owner, "" when it can't be read, None when not looked up
        yet (live_tree_model.sort_key_function's owner_of)."""
        return self._owners.get(node.path)

    def _owner_row_added(self, iid, node, path):
        """A finished tree's row whose owner isn't known yet: it waits for
        one already asked for, asks now when sorting by Owner, and otherwise
        asks once it's on screen."""
        if path in self._owner_rows or self._sort_key == "owner":
            self._want_owner(iid, node, path)

    def _owner_view_changed(self):
        """Look up the owners of the rows on screen at the next idle moment
        (once, however often the view changed before then)."""
        if self._owner_view_pending or self._live_tracker is not None:
            return
        self._owner_view_pending = True
        self.root.after_idle(self._want_visible_owners)

    def _want_visible_owners(self):
        self._owner_view_pending = False
        if self._live_tracker is not None or not self.tree.winfo_exists():
            return
        node_by_iid = self.node_by_iid
        owners = self._owners
        for iid in self._visible_rows():
            node = node_by_iid.get(iid)
            if node is not None:
                path = node.path
                if path not in owners:
                    self._want_owner(iid, node, path)

    def _visible_rows(self):
        """The rows on screen, top to bottom: one identify call a line (the
        tree scrolls by whole rows of one height)."""
        first = self._first_visible_row()
        top, height = self._live_row_top, self._live_row_height
        if not first or top is None or not height:
            return []
        tree = self.tree
        rows = [first]
        bottom = tree.winfo_height()
        y = top + 1 + height
        while y < bottom:
            iid = tree.identify_row(y)
            if not iid:
                break
            rows.append(iid)
            y += height
        return rows

    def _want_all_owners(self):
        """Sorting by Owner: ask for the owner of every row the finished
        tree lists, on screen or not."""
        if self._live_tracker is not None:
            return
        owners = self._owners
        for iid, node in self.node_by_iid.items():
            path = node.path
            if path not in owners:
                self._want_owner(iid, node, path)

    def _want_owner(self, iid, node, path):
        """Fill row `iid`'s Owner cell once `path`'s owner is known. The
        rows asked for by one event go as one request."""
        rows = self._owner_rows.get(path)
        if rows is not None:  # already asked for, by this row or one since replaced
            if (iid, node) not in rows:
                rows.append((iid, node))
            return
        self._owner_rows[path] = [(iid, node)]
        if not self._owner_batch:
            self.root.after_idle(self._send_owner_batch)
        self._owner_batch.append(path)

    def _send_owner_batch(self):
        batch, self._owner_batch = self._owner_batch, []
        if batch:
            self._owner_lookup.request(batch)
        if not self._owner_polling:
            self._owner_polling = True
            self.root.after(POLL_MS, self._poll_owners)

    def _poll_owners(self):
        self._owner_polling = False
        if not self.tree.winfo_exists():
            return
        tree = self.tree
        node_by_iid = self.node_by_iid
        sorted_by_owner = self._sort_key == "owner"
        for path, owner in self._owner_lookup.results(CELLS_PER_TICK):
            self._owners[path] = owner
            for iid, node in self._owner_rows.pop(path, ()):
                if node_by_iid.get(iid) is not node:
                    continue  # the row is gone (deleted, re-sorted, a new scan)
                if owner:
                    tree.set(iid, "owner", owner)
                if sorted_by_owner:
                    self._owner_levels.add(tree.parent(iid))
        if self._owner_rows:
            self._owner_polling = True
            self.root.after(POLL_MS, self._poll_owners)
            return
        # Re-sorted once the owners stop coming, not on every tick: a level
        # with more pages sorts all of its rows each time.
        levels, self._owner_levels = self._owner_levels, set()
        if self._sort_key != "owner" or self._live_tracker is not None:
            return
        for parent_iid in levels:
            if parent_iid == "" or (parent_iid in node_by_iid and tree.exists(parent_iid)):
                self._sort_level(parent_iid)
