"""The finished scan's rows in the main tree: inserting them a page at a
time, sorting, the Change column, and refreshing or removing a row after
a delete. (ui/live_tree.py has the rows while a scan is running.)

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import os
from tkinter import END

from storage_scanner.formatting import human_size
from storage_scanner.history_queries import get_folder_sizes
from storage_scanner.live_tree_model import (
    change_text,
    node_display,
    resorted,
    share,
    share_text,
    sort_key_function,
)
from storage_scanner.scan_history import MIN_FOLDER_SIZE_FOR_HISTORY
from storage_scanner.settings import heat_color
from storage_scanner.ui.live_tree import PLACEHOLDER_TEXT

# A folder's rows are inserted this many at a time (_insert_page).
ROWS_PER_PAGE = 1000


class MainTreeMixin:
    # -- Treeview population (lazy) ---------------------------------------- #
    def _heat_tag(self, fraction):
        """Return a treeview tag whose foreground is the heat color for
        `fraction`, quantized to 25 buckets so we configure few tags."""
        bucket = int(max(0.0, min(1.0, fraction)) * 24 + 0.5)
        name = f"heat{bucket}"
        if name not in self._heat_tags:
            self.tree.tag_configure(name, foreground=heat_color(bucket / 24))
            self._heat_tags.add(name)
        return name

    def _insert_node(self, parent_iid, node, parent_size, index=0):
        """A finished scan's row for `node` (see live_tree_model.row_display
        for what each column shows), with a placeholder child so an
        expandable folder gets its arrow."""
        display = node_display(node, parent_size)
        iid = self.tree.insert(
            parent_iid,
            END,
            text=display.text,
            values=(*display.values, self._change_cell(node)),
            tags=self._row_tags(display, index),
        )
        self.node_by_iid[iid] = node
        if node.has_children and (
            not self._changed_only() or any(self._has_changed(c) for c in node.children)
        ):
            self.tree.insert(iid, END, text=PLACEHOLDER_TEXT, tags=("placeholder",))
        return iid

    def _populate_children(self, parent_iid, node):
        # Remove placeholder if present.
        kids = self.tree.get_children(parent_iid)
        if len(kids) == 1 and self.tree.item(kids[0], "text") == PLACEHOLDER_TEXT:
            self.tree.delete(kids[0])
        elif kids:
            return  # already populated

        ordered = self._ordered_children(node)
        self._insert_page(parent_iid, node, ordered, 0)
        if not ordered and self._changed_only():
            self.tree.insert(
                parent_iid,
                END,
                text="No folder here changed since the last scan.",
                tags=("placeholder",),
            )

    def _ordered_children(self, node):
        """A level's rows in sort order: every child, or with Changed
        folders only ticked, just the folders that changed."""
        children = node.children
        if self._changed_only():
            children = [child for child in children if self._has_changed(child)]
        return sorted(
            children,
            key=sort_key_function(self._sort_key, self._folder_change),
            reverse=self._sort_reverse,
        )

    def _insert_page(self, parent_iid, node, ordered, start):
        """Rows `start`.. of `ordered` (a level in sort order), at most
        ROWS_PER_PAGE of them, then -- if any are left -- one "N more" row
        that shows the next page when opened (_show_more_rows). Tk inserts
        about 17,000 rows a second at 1.75 KB each, so a 250,000-file folder
        used to take 20 s to open."""
        end = min(start + ROWS_PER_PAGE, len(ordered))
        parent_size = node.size or 1
        for index in range(start, end):
            self._insert_node(parent_iid, ordered[index], parent_size=parent_size, index=index)
        rest = ordered[end:]
        if rest:
            more_iid = self.tree.insert(
                parent_iid,
                END,
                text=(
                    f"… {len(rest):,} more ({human_size(sum(n.size for n in rest))}) — "
                    f"double-click or press Enter to show {min(ROWS_PER_PAGE, len(rest)):,} more"
                ),
                tags=("placeholder",),
            )
            self._more_rows[more_iid] = parent_iid

    def _show_more_rows(self, more_iid):
        """Replace a level's "N more" row with its next page of rows."""
        parent_iid = self._more_rows.pop(more_iid)
        self.tree.delete(more_iid)
        node = self.node_by_iid[parent_iid]
        shown = sum(1 for iid in self.tree.get_children(parent_iid) if iid in self.node_by_iid)
        self._insert_page(parent_iid, node, self._ordered_children(node), shown)

    def _on_open(self, _event):
        iid = self.tree.focus()
        node = self.node_by_iid.get(iid)
        if not node or not node.is_dir:
            return
        if self._live_tracker is not None:  # the tree of a scan still on screen
            self._live_open(iid, node)
        else:
            self._populate_children(iid, node)

    def _on_double_click(self, _event):
        iid = self.tree.focus()
        if iid in self._more_rows:
            self._show_more_rows(iid)
            return
        node = self.node_by_iid.get(iid)
        if node and not node.is_dir:
            self._open_in_explorer()

    _HEADINGS = {
        "#0": "Name",
        "size": "Size",
        "alloc": "On Disk",
        "percent": "% of Parent",
        "items": "Files",
        "change": "Change",
        "modified": "Modified",
        "accessed": "Accessed",
    }

    def _sort_by(self, key):
        """Handle a heading click: toggle direction if it's the active key,
        else switch to it (names ascend, sizes/counts descend by default)."""
        if key == self._sort_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = key != "name"
        self._update_heading_arrows()
        self._resort_tree()

    def _update_heading_arrows(self):
        arrow = " ▼" if self._sort_reverse else " ▲"
        # The percent column is driven by the size sort, so it shares the mark.
        active_cols = {
            "size": ("size", "percent"),
            "alloc": ("alloc",),
            "name": ("#0",),
            "items": ("items",),
            "change": ("change",),
            "modified": ("modified",),
            "accessed": ("accessed",),
        }[self._sort_key]
        for col, base in self._HEADINGS.items():
            text = base + (arrow if col in active_cols else "")
            self.tree.heading(col, text=text)

    def _resort_tree(self):
        """Re-order every already-populated level in place (preserves which
        nodes are expanded; lazy children sort on expand via _populate)."""
        if self._live_tracker is not None:
            self._live_resort()
            return

        def walk(parent_iid):
            self._sort_level(parent_iid)
            for iid in self.tree.get_children(parent_iid):
                node = self.node_by_iid.get(iid)
                if node and node.is_dir:
                    walk(iid)

        walk("")

    def _sort_level(self, parent_iid):
        """Put one populated level's rows in the current sort order: one
        Treeview.set_children call for the whole level (moving rows one at
        a time is quadratic in Tk -- 35,000 rows took minutes), then a new
        stripe only for the rows that moved an odd number of places. A row
        with no node (a folder's "loading" placeholder) stays after them."""
        tree = self.tree
        node_by_iid = self.node_by_iid
        shown = tree.get_children(parent_iid)
        if any(iid in self._more_rows for iid in shown):
            self._rebuild_paged_level(parent_iid, shown)
            return
        kids = [iid for iid in shown if iid in node_by_iid]
        order = resorted(
            kids, node_by_iid, self._sort_key, self._sort_reverse, change_of=self._folder_change
        )
        if order == kids:
            return
        tree.set_children(parent_iid, *order, *(iid for iid in shown if iid not in node_by_iid))
        was_at = {iid: index for index, iid in enumerate(kids)}
        self._restripe((iid, index) for index, iid in enumerate(order) if (index - was_at[iid]) % 2)

    def _rebuild_paged_level(self, parent_iid, shown):
        """A level showing only its first pages can't just be reordered: the
        rows on screen are the top of the old order, not the new one. So it's
        rebuilt with as many rows as it showed, in the new order (folders
        opened inside it close)."""
        node = self.node_by_iid[parent_iid]
        count = sum(1 for iid in shown if iid in self.node_by_iid)
        for iid in shown:
            self._forget_subtree(iid)
        self.tree.delete(*shown)
        ordered = self._ordered_children(node)
        start = 0
        while True:
            self._insert_page(parent_iid, node, ordered, start)
            start += ROWS_PER_PAGE
            if start >= count:
                break
            more = next((i for i, p in self._more_rows.items() if p == parent_iid), None)
            if more is None:
                break
            del self._more_rows[more]
            self.tree.delete(more)

    def _restripe(self, rows):
        """Give each (iid, index) row the even/odd background tag for that
        index, keeping its other tags. Tk's "tag add"/"tag remove" take a
        list of rows, so this is four Tk calls however many rows there are
        (tkinter doesn't wrap either, hence tk.call)."""
        by_stripe = {"even": [], "odd": []}
        for iid, index in rows:
            by_stripe["odd" if index % 2 else "even"].append(iid)
        tree = self.tree
        for stripe, other in (("even", "odd"), ("odd", "even")):
            iids = by_stripe[stripe]
            if iids:
                tree.tk.call(tree, "tag", "remove", other, iids)
                tree.tk.call(tree, "tag", "add", stripe, iids)

    def _refresh_row(self, iid):
        """Recompute a row's size / on disk / percent / files text from its node."""
        node = self.node_by_iid.get(iid)
        if not node:
            return
        parent_node = self.node_by_iid.get(self.tree.parent(iid))
        parent_size = (parent_node.size if parent_node else node.size) or 1
        self.tree.item(
            iid, values=(*node_display(node, parent_size).values, self._change_cell(node))
        )

    # -- Change since the last scan (P2-18) -------------------------------- #
    def _previous_size(self, node):
        return self._previous_folder_sizes.get(os.path.normcase(os.path.normpath(node.path)))

    def _folder_change(self, node):
        """A folder's growth since the previous saved scan, or None when
        that isn't known (a file, no previous scan, or a folder that scan
        didn't keep)."""
        if not node.is_dir or not self._previous_folder_sizes:
            return None
        previous = self._previous_size(node)
        return None if previous is None else node.size - previous

    def _change_cell(self, node):
        if not node.is_dir or not self._previous_folder_sizes:
            return ""
        return change_text(node.size, self._previous_size(node), MIN_FOLDER_SIZE_FOR_HISTORY)

    def _show_changes(self, previous_scan_id):
        """Fill the Change column against `previous_scan_id` (the scan saved
        before the one on screen), and re-sort if that's the sort key."""
        self._previous_folder_sizes = get_folder_sizes(previous_scan_id)
        self.changed_only_check.state(
            ["!disabled"] if self._previous_folder_sizes else ["disabled"]
        )
        if self._changed_only():
            self._refilter_tree()
            return
        for iid, node in self.node_by_iid.items():
            if node.is_dir and self.tree.exists(iid):
                self.tree.set(iid, "change", self._change_cell(node))
        if self._sort_key == "change":
            self._resort_tree()
        self.treemap_pane.refresh()  # its Growth colours

    def _changed_only(self):
        """Whether the finished tree lists only changed folders: Changed
        folders only is ticked and there's a previous scan to compare with."""
        return bool(self._previous_folder_sizes) and self.changed_only_var.get()

    def _has_changed(self, node):
        """Whether Changed folders only lists `node`: a folder whose size
        differs from the previous scan's, or one of 50 MB or more that scan
        didn't keep (new, or grown past the size history keeps)."""
        if not node.is_dir:
            return False
        previous = self._previous_size(node)
        if previous is None:
            return node.size >= MIN_FOLDER_SIZE_FOR_HISTORY
        return node.size != previous

    def _refilter_tree(self):
        """Rebuild the finished tree's rows after Changed folders only is
        ticked or unticked (or the comparison arrives with it ticked). The
        scan's own row stays; folders that were open and are still listed
        open again."""
        if self._live_tracker is not None:
            return  # a scan's rows are on screen; they follow when it finishes
        tree = self.tree
        open_nodes = set()

        def collect_open(parent_iid):
            for iid in tree.get_children(parent_iid):
                node = self.node_by_iid.get(iid)
                if node is not None and node.is_dir and tree.item(iid, "open"):
                    open_nodes.add(node)
                    collect_open(iid)

        def reopen(parent_iid):
            for iid in tree.get_children(parent_iid):
                node = self.node_by_iid.get(iid)
                if node in open_nodes and tree.get_children(iid):  # still has rows to show
                    self._populate_children(iid, node)
                    tree.item(iid, open=True)
                    reopen(iid)

        for top_iid in tree.get_children(""):
            node = self.node_by_iid.get(top_iid)
            if node is None:
                continue
            collect_open(top_iid)
            rows = tree.get_children(top_iid)
            for iid in rows:
                self._forget_subtree(iid)
            tree.delete(*rows)
            self._populate_children(top_iid, node)
            reopen(top_iid)

    # -- Selecting a node from elsewhere (the treemap) ------------------------- #
    def _select_in_tree(self, nodes):
        """Select the row of the last of `nodes` (a path of nodes from the
        scan's root down), opening folders and loading their pages on the
        way. Stops at the deepest one with a row (Changed folders only can
        hide some)."""
        tree = self.tree
        iid = next((i for i in tree.get_children("") if self.node_by_iid.get(i) is nodes[0]), None)
        if iid is None:
            return
        for node in nodes[1:]:
            self._populate_children(iid, self.node_by_iid[iid])
            tree.item(iid, open=True)
            child = self._find_child_row(iid, node)
            if child is None:
                break
            iid = child
        tree.selection_set(iid)
        tree.focus(iid)
        tree.see(iid)

    def _find_child_row(self, parent_iid, node):
        """The row under `parent_iid` showing `node`, loading "more" pages
        until it turns up; None if it has no row."""
        while True:
            more = None
            for iid in self.tree.get_children(parent_iid):
                shown = self.node_by_iid.get(iid)
                if shown is not None and (shown is node or shown == node):
                    return iid
                if iid in self._more_rows:
                    more = iid
            if more is None:
                return None
            self._show_more_rows(more)

    # -- Constraints Functions --------------------------------------------- #
    def _forget_subtree(self, iid):
        """Drop an iid and all its descendants from the node map."""
        for child in self.tree.get_children(iid):
            self._forget_subtree(child)
        self.node_by_iid.pop(iid, None)
        self._more_rows.pop(iid, None)

    def _remove_main_tree_row(self, iid):
        """Remove a node's row from the main tree after it's been deleted,
        rolling the removed size/count back out of every ancestor and
        refreshing whatever changed on screen: the ancestors' rows, the
        siblings whose share of their (now smaller) parent reads
        differently, and the stripe of every row below it, which moved up
        a line. Called for every deleted node that has a row here,
        whichever window deleted it (see
        DeletionMixin._remove_deleted_from_tree).
        """
        node = self.node_by_iid.get(iid)
        if not node:
            return

        tree = self.tree
        parent_iid = tree.parent(iid)
        parent_node = self.node_by_iid.get(parent_iid)
        siblings = tree.get_children(parent_iid)
        position = siblings.index(iid)
        old_parent_size = (parent_node.size if parent_node else 0) or 1

        # Subtract the removed size/count from every ancestor (incl. the root
        # row, whose parent is ""). root_node is the same object as its row.
        anc = parent_iid
        while anc:
            an = self.node_by_iid.get(anc)
            if an:
                an.size -= node.size
                an.file_count -= node.file_count
            anc = tree.parent(anc)
        if parent_node:
            parent_node.remove_child(node)

        self._forget_subtree(iid)
        tree.delete(iid)

        self._restripe(
            (sib, index) for index, sib in enumerate(siblings[position + 1 :], start=position)
        )
        if parent_node:
            # Only the siblings' "% of Parent" depends on the parent's size,
            # and a top-level row's share is of itself. In a big folder
            # most shares still read the same, so only the rest are
            # rewritten (rewriting every sibling took seconds at 35,000).
            new_parent_size = parent_node.size or 1
            for sib in siblings:
                sib_node = self.node_by_iid.get(sib)
                if sib_node is None:
                    continue
                size = sib_node.size
                text = share_text(share(size, new_parent_size))
                if text != share_text(share(size, old_parent_size)):
                    tree.set(sib, "percent", text)
        anc = parent_iid
        while anc:
            self._refresh_row(anc)
            anc = tree.parent(anc)

        if self.root_node:
            self.status_var.set(
                f"{self.root_node.path}  —  {human_size(self.root_node.size)} "
                f"in {self.root_node.file_count:,} files"
            )
        self.treemap_pane.refresh()
