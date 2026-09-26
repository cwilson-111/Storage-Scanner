"""The main tree while a scan runs: every folder row fills in as it's read,
the way TreeSize does it -- size so far, on disk, share of its parent with
its bar and heat colour, file count, and whether it's queued (◌, greyed),
being read (⏳) or done. Rows re-sort by size as they grow, and a folder
opened mid-scan shows its own subfolders filling in too.

A mixin composed into StorageScannerApp (storage_scanner/app.py). What a
row says comes from storage_scanner.live_tree_model; the numbers come from
the Compatible engine's own tree, which its workers build in place, through
scan_progress.WalkTracker.folders(): the walk keeps every folder's running
totals there (one pass up its ancestors per directory read), so a folder
at any depth costs the same to show as a top-level one, and the scan still
posts no message per folder.

Everything runs on main_window._poll_progress's 100 ms tick. While a scan
runs its worker threads hold the GIL most of the time and Tkinter hands it
back on every call into Tcl, so what a tick costs is mostly how many Tk
calls it makes. A tick only touches what can change on screen:

- new rows: the folders and files found since the last tick, appended to
  the open levels for at most _INSERT_SECONDS a tick, so a folder with
  tens of thousands of entries fills in over a few seconds instead of
  freezing the window;
- values: only the rows in the viewport (found by walking this module's
  own copy of the order and open folders, from the one top row Tk names),
  and only those whose text changed -- a folder scrolled into view catches
  up on the next tick;
- order: open levels re-sort when new rows arrive and every _RESORT_SECONDS,
  never while a mouse button is held on the tree or its scrollbar. A
  re-sort keeps the selection and every open folder (Treeview.set_children
  moves rows without touching either), and when the focused row is on
  screen it stays on the same line; otherwise the scroll position is left
  alone.

Turbo Scan and the elevated helpers build no tree until they're done, so
the target's row shows the step they're on instead ("⏳ C:\\ — Reading the
MFT — 45%"). Cancelling freezes the rows where they were. When a scan
finishes, main_window._finish_scan rebuilds the tree from the finished
numbers, exactly as it always has, then reopens the folders that were open,
reselects the focused row and scrolls back to the same top row.
"""

import os
import time
from tkinter import END, messagebox

from storage_scanner.live_tree_model import SCANNING_ICON, resorted, row_display
from storage_scanner.models import FileNode

PLACEHOLDER_TEXT = "…(loading)"
_RESORT_SECONDS = 0.5
_INSERT_SECONDS = 0.03
_INSERTS_PER_TICK = 2000


class _Level:
    """An open folder's rows in the live tree, in the order shown."""

    __slots__ = ("node", "order", "position", "dirs_seen", "files_seen")

    def __init__(self, node):
        self.node = node
        self.order = []  # row iids
        self.position = {}  # row iid -> index in order
        self.dirs_seen = 0  # node.dirs already given a row
        self.files_seen = 0  # file rows already given a row


class LiveTreeMixin:
    def _live_reset(self):
        self._live_tracker = None  # scan_progress.WalkTracker of the scan's tree
        self._live_root_iid = None  # the target's row
        self._live_root_text = ""
        self._live_step = None
        self._live_levels = {}  # parent row iid -> _Level, in the order opened
        self._live_open_rows = set()  # level rows open right now
        self._live_parent = {}  # row iid -> its parent's row iid
        self._live_shown = {}  # row iid -> (text, values, tags) on screen
        self._live_expandable = set()  # folder rows already given an expand arrow
        self._live_frozen = False
        self._live_sorted_at = 0.0

    def _live_bind(self, tree, scrollbar):
        """Re-sorting waits while a mouse button is held on the tree or its
        scrollbar, so rows don't move under a drag or a click; a closed
        folder stops counting as open."""
        self._live_pointer_down = False
        self._live_row_top = None  # y of the first row, below the heading
        self._live_row_height = None

        def press(_event):
            self._live_pointer_down = True

        def release(_event):
            self._live_pointer_down = False

        for widget in (tree, scrollbar):
            widget.bind("<ButtonPress-1>", press, add="+")
            widget.bind("<ButtonRelease-1>", release, add="+")
        tree.bind(
            "<<TreeviewClose>>", lambda _e: self._live_open_rows.discard(tree.focus()), add="+"
        )

    def _begin_scan_view(self, target):
        """Clear the last scan's tree and everything tied to it, and show
        the target's row straight away."""
        self.cancel_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.root_node = None
        self.duplicates = None
        self._duplicates_scan_root = None
        self.cart.clear()
        self._refresh_cart_indicator()
        self._hide_scan_details()
        self._live_reset()

        path = os.path.abspath(target)
        name = path if path.endswith(os.sep) else os.path.basename(path) or path
        suffix = "\\" if os.path.isdir(path) and not name.endswith("\\") else ""
        self._live_root_text = f"{SCANNING_ICON} {name}{suffix}"
        self._live_root_iid = self.tree.insert(
            "", END, text=self._live_root_text, values=("…", "…", "", "…"), tags=("dir", "even")
        )

    def _refuse_delete_during_scan(self, parent=None):
        """True, after saying so, while a scan is running: its rows are
        still filling in from the folders being read, and a folder deleted
        mid-read would leave the scan's totals for it undefined."""
        if self.scan_thread is None or not self.scan_thread.is_alive():
            return False
        messagebox.showinfo(
            "Storage Scanner",
            "Deleting has to wait until the scan finishes or is cancelled.",
            parent=parent or self.root,
        )
        return True

    # -- the scan's tree ------------------------------------------------ #

    def _live_attach(self, tracker):
        """("live_tree", tracker): the Compatible engine's tree replaces the
        target's placeholder row."""
        if self._live_frozen or self._live_root_iid is None:
            return
        self._live_tracker = tracker
        root = tracker.root
        self.tree.delete(self._live_root_iid)
        [(size, alloc_size, file_count, state)] = tracker.folders([root])
        display = row_display(root, size, alloc_size, file_count, size or 1, state)
        tags = self._row_tags(display, 0)
        iid = self.tree.insert("", END, text=display.text, values=display.values, tags=tags)
        self.node_by_iid[iid] = root
        self._live_root_iid = iid
        self._live_parent[iid] = ""
        self._live_shown[iid] = (display.text, display.values, tags)
        self._live_expandable.add(iid)
        self._live_levels[iid] = _Level(root)
        self._live_open_rows.add(iid)
        self.tree.item(iid, open=True)

    def _live_tick(self, view):
        """One poll tick; `view` is the progress line's ProgressView."""
        if self._live_frozen or self._live_root_iid is None:
            return
        if self._live_tracker is None:
            self._show_live_step(view.step if view is not None else "")
            return
        added = 0
        deadline = time.perf_counter() + _INSERT_SECONDS
        for parent_iid, level in self._live_levels.items():
            if parent_iid in self._live_open_rows:
                added += self._live_sync_level(parent_iid, level, _INSERTS_PER_TICK, deadline)
        now = time.monotonic()
        due = added or now - self._live_sorted_at >= _RESORT_SECONDS
        if due and not self._live_pointer_down:
            self._live_sorted_at = now
            self._live_resort()
        self._live_refresh_visible()

    def _live_freeze(self):
        """Cancel or failure: leave every row as it is now."""
        self._live_frozen = True

    def _show_live_step(self, step):
        if step == self._live_step:
            return
        self._live_step = step
        text = f"{self._live_root_text} — {step}" if step else self._live_root_text
        self.tree.item(self._live_root_iid, text=text)

    def _row_tags(self, display, index):
        tags = [] if display.heat is None else [self._heat_tag(display.heat)]
        tags.extend(display.tags)
        tags.append("odd" if index % 2 else "even")
        return tuple(tags)

    def _live_sync_level(self, parent_iid, level, budget, deadline=None):
        """Give rows to the folders and files found under `level` since the
        last call: at most `budget` of them, and none once `deadline`
        (a time.perf_counter() value) has passed. Returns how many it added."""
        node = level.node
        new_dirs = node.dirs[level.dirs_seen : level.dirs_seen + budget]
        file_rows = len(node.file_names)  # rows only ever grow during a scan
        new_files = range(
            level.files_seen, min(file_rows, level.files_seen + budget - len(new_dirs))
        )
        if not new_dirs and not new_files:
            return 0
        [parent, *folders] = self._live_tracker.folders([node, *new_dirs])
        parent_size = parent[0] or 1
        added = 0
        for child, (size, alloc_size, file_count, state) in zip(new_dirs, folders):
            if deadline is not None and time.perf_counter() > deadline:
                return added
            display = row_display(child, size, alloc_size, file_count, parent_size, state)
            self._live_insert(parent_iid, level, child, display)
            level.dirs_seen += 1
            added += 1
        for index in new_files:
            if deadline is not None and time.perf_counter() > deadline:
                return added
            child = FileNode(node, index)
            display = row_display(child, child.size, child.alloc_size, 1, parent_size)
            self._live_insert(parent_iid, level, child, display)
            level.files_seen = index + 1
            added += 1
        return added

    def _live_insert(self, parent_iid, level, child, display):
        index = len(level.order)
        tags = self._row_tags(display, index)
        iid = self.tree.insert(parent_iid, END, text=display.text, values=display.values, tags=tags)
        self.node_by_iid[iid] = child
        self._live_parent[iid] = parent_iid
        self._live_shown[iid] = (display.text, display.values, tags)
        level.position[iid] = index
        level.order.append(iid)
        if child.is_dir and child.has_children:
            self._give_expand_arrow(iid)

    def _give_expand_arrow(self, iid):
        self.tree.insert(iid, END, text=PLACEHOLDER_TEXT, tags=("placeholder",))
        self._live_expandable.add(iid)

    def _live_open(self, iid, node):
        """A folder row opened while its scan's tree is on screen (running,
        or frozen after a cancel): list what's been found under it so far."""
        level = self._live_levels.get(iid)
        if level is None:
            self.tree.delete(*self.tree.get_children(iid))  # the placeholder
            self._live_expandable.add(iid)
            level = self._live_levels[iid] = _Level(node)
        self._live_open_rows.add(iid)
        if self._live_frozen:
            # No more ticks will come, so list everything now (as opening a
            # finished folder does).
            self._live_sync_level(iid, level, len(node.dirs) + len(node.file_names))
        else:
            deadline = time.perf_counter() + _INSERT_SECONDS
            self._live_sync_level(iid, level, _INSERTS_PER_TICK, deadline)
        self._live_sort_level(iid, level)

    # -- order ---------------------------------------------------------- #

    def _live_resort(self):
        """Re-sort every open level, keeping the focused row on its line."""
        tree = self.tree
        anchor = tree.focus()
        box = tree.bbox(anchor) if anchor else ""
        moved = False
        for parent_iid, level in self._live_levels.items():
            if parent_iid in self._live_open_rows:
                moved = self._live_sort_level(parent_iid, level) or moved
        if moved and box:
            self._keep_row_at(anchor, box[1])

    def _live_sort_level(self, parent_iid, level):
        order = resorted(level.order, self.node_by_iid, self._sort_key, self._sort_reverse)
        if order == level.order:
            return False
        self.tree.set_children(parent_iid, *order)
        level.order = order
        level.position = {iid: index for index, iid in enumerate(order)}
        return True

    def _keep_row_at(self, iid, y):
        """Scroll so row `iid` is back at height `y`."""
        tree = self.tree
        box = tree.bbox(iid)
        if not box:
            tree.see(iid)
            box = tree.bbox(iid)
        if box:
            rows = round((box[1] - y) / box[3])
            if rows:
                tree.yview_scroll(rows, "units")

    # -- values --------------------------------------------------------- #

    def _first_visible_row(self):
        """The row at the top of the viewport ("" for an empty tree). The
        tree scrolls by whole rows, so once the first row's position is
        known one identify call finds it."""
        tree = self.tree
        if self._live_row_top is not None:
            iid = tree.identify_row(self._live_row_top + 1)
            if iid:
                return iid
        for y in range(0, 120, 2):  # past the heading
            iid = tree.identify_row(y)
            if iid:
                box = tree.bbox(iid)
                if box:
                    self._live_row_top, self._live_row_height = box[1], box[3]
                return iid
        return ""

    def _next_row(self, iid):
        """The live row shown below `iid`, from this module's own copy of
        the order and the open folders -- no Tk calls."""
        level = self._live_levels.get(iid)
        if level is not None and level.order and iid in self._live_open_rows:
            return level.order[0]
        while iid:
            parent_iid = self._live_parent.get(iid)
            if not parent_iid:
                return None  # the root row, or not a live row
            siblings = self._live_levels[parent_iid]
            index = siblings.position[iid] + 1
            if index < len(siblings.order):
                return siblings.order[index]
            iid = parent_iid
        return None

    def _visible_live_rows(self):
        first = self._first_visible_row()
        if not first or not self._live_row_height:
            return []
        rows = [first]
        for _ in range(self.tree.winfo_height() // self._live_row_height):
            following = self._next_row(rows[-1])
            if following is None:
                break
            rows.append(following)
        return rows

    def _live_refresh_visible(self):
        tree = self.tree
        node_by_iid = self.node_by_iid
        rows = [
            (iid, node_by_iid[iid])
            for iid in self._visible_live_rows()
            if iid in self._live_parent and iid in node_by_iid
        ]
        if not rows:
            return
        folders = {}
        for iid, node in rows:
            if node.is_dir:
                folders[id(node)] = node
            parent_iid = self._live_parent[iid]
            if parent_iid:
                parent = node_by_iid[parent_iid]
                folders[id(parent)] = parent
        totals = dict(zip(folders, self._live_tracker.folders(list(folders.values()))))

        for iid, node in rows:
            parent_iid = self._live_parent[iid]
            if node.is_dir:
                size, alloc_size, file_count, state = totals[id(node)]
            else:
                size, alloc_size, file_count, state = node.size, node.alloc_size, 1, None
            if parent_iid:
                parent_size = totals[id(node_by_iid[parent_iid])][0] or 1
                index = self._live_levels[parent_iid].position.get(iid, 0)
            else:
                parent_size, index = size or 1, 0
            display = row_display(node, size, alloc_size, file_count, parent_size, state)
            tags = self._row_tags(display, index)
            shown = (display.text, display.values, tags)
            if self._live_shown.get(iid) != shown:
                tree.item(iid, text=display.text, values=display.values, tags=tags)
                self._live_shown[iid] = shown
            if node.is_dir and iid not in self._live_expandable and node.has_children:
                self._give_expand_arrow(iid)

    # -- handing over to the finished tree ------------------------------ #

    def _live_view_state(self):
        """What to restore once the finished tree replaces the live rows:
        the folders opened during the scan (parents first), the focused
        row and the top visible row -- as nodes, which the finished tree
        reuses. None when there was no live tree."""
        if self._live_tracker is None:
            return None
        opened = [
            level.node
            for iid, level in self._live_levels.items()
            if iid != self._live_root_iid and iid in self._live_open_rows
        ]
        return (
            opened,
            self.node_by_iid.get(self.tree.focus()),
            self.node_by_iid.get(self._first_visible_row()),
        )

    def _restore_view_state(self, state):
        opened, focused, top = state
        tree = self.tree
        row_of = {node: iid for iid, node in self.node_by_iid.items()}
        for node in opened:
            iid = row_of.get(node)
            if iid is None:
                continue
            self._populate_children(iid, node)
            tree.item(iid, open=True)
            for child_iid in tree.get_children(iid):
                child = self.node_by_iid.get(child_iid)
                if child is not None:
                    row_of[child] = child_iid
        if focused is not None and focused in row_of:
            tree.selection_set(row_of[focused])
            tree.focus(row_of[focused])
        if top is not None and top in row_of and self._live_row_top is not None:
            self._keep_row_at(row_of[top], self._live_row_top)
