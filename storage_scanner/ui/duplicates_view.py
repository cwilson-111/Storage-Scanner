"""The Duplicate Files window: one row per copy, grouped, with the copy the
app would keep marked Keeper.

DuplicatesMixin (ui/duplicate_window.py) runs the finder and opens one of
these with its groups. Deleting a copy goes through the delete service,
which re-checks the group on disk first; a delete from any window reaches
this one through forget_deleted.
"""

from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    E,
    Menu,
    StringVar,
    Toplevel,
    W,
    X,
    messagebox,
    ttk,
)
from typing import TYPE_CHECKING

from storage_scanner.cleanup_recommendations import (
    get_sampled_duplicates_from_groups,
    is_sampled_duplicate,
    keeper_reason,
    pick_keeper,
    sampled_match_warning,
)
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.duplicate_finder import settle_group
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    IS_MACOS,
    TRASH_NAME,
    resource_path,
)
from storage_scanner.settings import COLORS, DUPLICATE_HASH_CHUNK_BYTES, px

if TYPE_CHECKING:
    from storage_scanner.models import FileNode, Node
    from storage_scanner.ui.app_state import AppState

# How much of a big file's start, middle and end a match compared, as the
# header and the row details word it.
_WINDOW_TEXT = human_size(DUPLICATE_HASH_CHUNK_BYTES)


class DuplicatesWindow:
    """One Duplicate Files window over `duplicates`, a list of
    (size, digest, nodes) groups from duplicate_finder for `scan_tree`, the
    app's current tree. `win` is its Toplevel."""

    def __init__(self, app: "AppState", scan_tree: "Node", duplicates):
        self.app = app
        self.scan_path = scan_tree.path
        self.scan_tree = scan_tree  # what every row here is from

        # Per-group state, kept in step with deletes from any window
        # (forget_deleted). The keeper can be re-picked (right-click) and is
        # never a deletion target here; the delete service's on-disk re-check
        # guarantees a copy survives even if this list is somehow behind.
        self.iid_to_node: dict[str, FileNode] = {}
        self.iid_to_group: dict[str, int] = {}
        self.group_nodes: dict[int, list[FileNode]] = {}  # sorted by path
        self.group_size: dict[int, int] = {}  # bytes per copy
        self.group_keeper: dict[int, FileNode] = {}  # the copy marked Keeper
        self.group_rows: dict[int, list[str]] = {}  # the group's row iids

        self.win = Toplevel(app.root)
        self.win.configure(bg=COLORS["bg"])
        self.win.geometry(f"{px(980)}x{px(600)}")
        try:
            self.win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            logger.debug("Duplicates window iconbitmap failed", exc_info=True)

        self.header_var = StringVar()
        self.details_var = StringVar(value="Select a row to see why it was flagged.")
        self._build_header(getattr(app, "dup_stats", {}))
        self.tv = self._build_table()
        self._build_footer()
        self._insert_groups(duplicates)
        self.group_count, self.total_wasted = self.summarize()
        app._watch_deletions(self.win, self.forget_deleted)

    # ----- building -----

    def _build_header(self, stats):
        ttk.Label(self.win, padding=(10, 8), textvariable=self.header_var).pack(side=TOP, fill=X)
        ttk.Label(
            self.win,
            padding=(10, 0, 10, 8),
            text=(
                f"Checked: {stats.get('files_checked', 0):,} files  |  "
                f"Skipped system files: {stats.get('files_skipped', 0):,}  |  "
                f"Skipped size: {human_size(stats.get('bytes_skipped', 0))}  |  "
                f"Head/tail hashed: {stats.get('partial_hashed', 0):,}  |  "
                f"Middle hashed: {stats.get('middle_hashed', 0):,}"
            ),
            style="Accent.TLabel",
        ).pack(side=TOP, fill=X)

    def _build_table(self):
        frame = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("group", "role", "size", "copies", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width, right_aligned, stretch in (
            ("group", "Group", 60, True, False),
            ("role", "Role", 80, False, False),
            ("size", "Size", 100, True, False),
            ("copies", "Copies", 60, True, False),
            ("path", "Path", 620, False, True),
        ):
            tv.heading(col, text=text)
            tv.column(col, width=px(width), anchor=E if right_aligned else W, stretch=stretch)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        tv.tag_configure("keep", foreground=COLORS["accent2"])
        tv.tag_configure("dupe", foreground=COLORS["fg"])

        tv.bind("<<TreeviewSelect>>", lambda _e: self.show_row_reason(tv.focus()))
        # Right-click: let the user override which copy in a group is kept,
        # instead of only ever trusting the automatic heuristic.
        self.row_menu = Menu(self.win, tearoff=0)
        tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", self.show_row_menu)
        tv.bind("<Double-1>", lambda _e: self.reveal_selected())
        return tv

    def _build_footer(self):
        ttk.Label(
            self.win,
            textvariable=self.details_var,
            style="Accent.TLabel",
            padding=(10, 4),
        ).pack(side=BOTTOM, fill=X)

        button_bar = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)
        ttk.Button(
            button_bar, text=f"Reveal in {FILE_MANAGER_NAME}", command=self.reveal_selected
        ).pack(side=LEFT)
        ttk.Button(button_bar, text="Copy Path", command=self.copy_selected_path).pack(
            side=LEFT, padx=6
        )
        ttk.Button(button_bar, text="Add Selected to Cart", command=self.add_selected_to_cart).pack(
            side=LEFT
        )
        ttk.Button(
            button_bar, text="Delete Selected", command=self.delete_selected_duplicates
        ).pack(side=RIGHT)

    def _insert_groups(self, duplicates):
        row_index = 0
        for group_num, (size, _digest, nodes) in enumerate(duplicates, start=1):
            self.group_nodes[group_num] = sorted(nodes, key=lambda n: n.path.lower())
            self.group_size[group_num] = size
            self.group_keeper[group_num] = pick_keeper(self.group_nodes[group_num])
            self.group_rows[group_num] = []
            for node in self.group_nodes[group_num]:
                tag = "keep" if node == self.group_keeper[group_num] else "dupe"
                iid = self.tv.insert(
                    "",
                    END,
                    values=self.row_values(group_num, node),
                    tags=(tag, "odd" if row_index % 2 else "even"),
                )
                self.iid_to_node[iid] = node
                self.iid_to_group[iid] = group_num
                self.group_rows[group_num].append(iid)
                row_index += 1

    # ----- rows and groups -----

    def row_values(self, group_num, node):
        nodes = self.group_nodes[group_num]
        role = "Keeper" if node == self.group_keeper[group_num] else "Duplicate"
        copies = f"{nodes.index(node) + 1}/{len(nodes)}"
        return (group_num, role, human_size(self.group_size[group_num]), copies, node.path)

    def refresh_group(self, group_num):
        for iid in self.group_rows[group_num]:
            node = self.iid_to_node[iid]
            tag = "keep" if node == self.group_keeper[group_num] else "dupe"
            self.tv.item(
                iid,
                values=self.row_values(group_num, node),
                tags=(tag, self.tv.item(iid, "tags")[1]),
            )

    def summarize(self):
        """Refresh the header and title; (group count, bytes reclaimable)."""
        sizes = [(self.group_size[g], len(nodes)) for g, nodes in self.group_nodes.items()]
        wasted = sum(size * (copies - 1) for size, copies in sizes)
        sampled = sum(1 for size, _copies in sizes if is_sampled_duplicate(size))
        sampled_note = (
            f"  ({sampled:,} over {human_size(3 * DUPLICATE_HASH_CHUNK_BYTES)} matched "
            f"on first/middle/last {_WINDOW_TEXT} only)"
            if sampled
            else ""
        )
        self.header_var.set(
            f"Duplicate files under {self.scan_path}  —  {len(sizes):,} groups, "
            f"potential cleanup: {human_size(wasted)}{sampled_note}"
        )
        self.win.title(f"Duplicate Files — {len(sizes)} groups")
        return len(sizes), wasted

    def forget_deleted(self, deleted):
        """A delete from any window: drop the deleted copies, dissolve a
        group left with one copy (it's no longer a duplicate of anything)
        and re-pick a group's keeper if it was the one deleted."""
        affected = {
            self.iid_to_group[iid] for iid, node in self.iid_to_node.items() if deleted.covers(node)
        }
        for group_num in affected:
            remaining, keeper = settle_group(
                self.group_nodes[group_num], self.group_keeper[group_num], deleted
            )
            for iid in list(self.group_rows[group_num]):
                if keeper is None or self.iid_to_node[iid] not in remaining:
                    self.tv.delete(iid)
                    del self.iid_to_node[iid]
                    del self.iid_to_group[iid]
                    self.group_rows[group_num].remove(iid)
            if keeper is None:
                for state in (
                    self.group_nodes,
                    self.group_size,
                    self.group_keeper,
                    self.group_rows,
                ):
                    del state[group_num]
                continue
            self.group_nodes[group_num] = remaining
            self.group_keeper[group_num] = keeper
            self.refresh_group(group_num)
        if affected:
            self.summarize()

    def make_keeper(self, iid):
        group_num = self.iid_to_group.get(iid)
        if group_num is None or self.iid_to_node[iid] == self.group_keeper[group_num]:
            return
        self.group_keeper[group_num] = self.iid_to_node[iid]
        self.refresh_group(group_num)
        self.details_var.set(
            f"Manually set as keeper for group {group_num}. "
            f"The previous keeper is now a regular duplicate."
        )

    def show_row_reason(self, iid):
        group_num = self.iid_to_group.get(iid)
        if group_num is None:
            return
        node = self.iid_to_node[iid]
        keeper = self.group_keeper[group_num]
        if node == keeper:
            self.details_var.set(f"Kept: {keeper_reason(keeper, self.group_nodes[group_num])}")
        elif is_sampled_duplicate(node.size):
            self.details_var.set(
                f"Likely duplicate of the keeper ({keeper.path}): same size and same "
                f"first, middle and last {_WINDOW_TEXT}; the bytes between weren't compared."
            )
        else:
            self.details_var.set(f"Duplicate of the keeper ({keeper.path}).")

    def show_row_menu(self, event):
        iid = self.tv.identify_row(event.y)
        if not iid or iid not in self.iid_to_group:
            return
        self.tv.selection_set(iid)
        self.tv.focus(iid)
        self.row_menu.delete(0, END)
        if self.group_keeper[self.iid_to_group[iid]] != self.iid_to_node[iid]:
            self.row_menu.add_command(
                label="Make this the keeper", command=lambda: self.make_keeper(iid)
            )
        else:
            self.row_menu.add_command(label="This copy is already the keeper", state="disabled")
        self.row_menu.tk_popup(event.x_root, event.y_root)

    # ----- actions -----

    def reveal_selected(self):
        node = self.iid_to_node.get(self.tv.focus())
        if node:
            self.app._reveal(node.path, is_dir=False)

    def copy_selected_path(self):
        node = self.iid_to_node.get(self.tv.focus())
        if node:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(node.path)

    def selected_copies(self):
        """(the selected non-keeper copies, how many selected keepers were
        left out). The keeper in each group is never a deletion target,
        even if selected (e.g. via select-all)."""
        selected = [iid for iid in self.tv.selection() if iid in self.iid_to_node]
        copies = [
            self.iid_to_node[iid]
            for iid in selected
            if self.iid_to_node[iid] != self.group_keeper[self.iid_to_group[iid]]
        ]
        return copies, len(selected) - len(copies)

    def _no_copy_selected(self):
        messagebox.showinfo(
            "Storage Scanner",
            "Select at least one duplicate copy that isn't a group's keeper.",
            parent=self.win,
        )

    def delete_selected_duplicates(self):
        app, win = self.app, self.win
        targets, skipped_keepers = self.selected_copies()
        if not targets:
            self._no_copy_selected()
            return
        if app._refuse_delete_during_scan(parent=win):
            return

        note = (
            f" ({skipped_keepers} selected keeper file(s) were skipped — "
            "keepers are protected and can't be deleted here.)"
            if skipped_keepers
            else ""
        )
        sampled_count, _sampled = get_sampled_duplicates_from_groups(targets, app.duplicates or [])
        if not messagebox.askyesno(
            "Delete selected duplicates",
            f"Send {len(targets)} selected file(s) to the {TRASH_NAME}?{note}"
            + sampled_match_warning(sampled_count, "file(s)"),
            icon="warning",
            parent=win,
        ):
            return
        # Deleted rows leave this window through forget_deleted.
        app._delete_nodes(
            [
                DeleteRequest(node, "Duplicate Files", as_duplicate=True, tree=self.scan_tree)
                for node in targets
            ],
            win,
        )

    def add_selected_to_cart(self):
        # Same exclusion as delete: a group's keeper can never be queued
        # for deletion, even via the cart.
        app = self.app
        targets, _skipped = self.selected_copies()
        if not targets:
            self._no_copy_selected()
            return

        sampled_count, sampled_nodes = get_sampled_duplicates_from_groups(
            targets, app.duplicates or []
        )
        if sampled_count and not messagebox.askyesno(
            "Add sampled duplicates to cart",
            f"Add {len(targets)} file(s) to the Cleanup Cart?"
            + sampled_match_warning(sampled_count, "file(s)"),
            icon="warning",
            parent=self.win,
        ):
            return

        for node in targets:
            app.cart.add(
                node,
                "Duplicate Files",
                is_sampled=node in sampled_nodes,
                as_duplicate=True,
            )
        app._refresh_cart_indicator()
