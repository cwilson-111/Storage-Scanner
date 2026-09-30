"""The Duplicate Files window, and running the finder behind it.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Finding
the groups is storage_scanner.duplicate_finder's; deleting a copy goes
through the delete service, which re-checks the group on disk first.
"""

import queue
import threading
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

from storage_scanner.cleanup_recommendations import (
    get_sampled_duplicates_from_groups,
    is_protected_path,
    is_sampled_duplicate,
    keeper_reason,
    pick_keeper,
)
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.duplicate_finder import find_duplicate_files, settle_group
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    IS_MACOS,
    TRASH_NAME,
    resource_path,
)
from storage_scanner.settings import COLORS, DUPLICATE_HASH_CHUNK_BYTES


class DuplicatesMixin:
    def _should_skip_duplicate_scan(self, path):
        """Return True if this path should be ignored during duplicate scans."""
        return is_protected_path(path)

    def _find_duplicate_files(self, progress_q=None, cancel_event=None):
        """duplicate_finder.find_duplicate_files over this session's tree."""
        return find_duplicate_files(
            self.root_node, progress_q, cancel_event, skip=self._should_skip_duplicate_scan
        )

    def show_duplicates(self):
        if not self.root_node:
            return

        if self.dup_thread and self.dup_thread.is_alive():
            messagebox.showinfo("Storage Scanner", "Duplicate scan is already running.")
            return

        self.dup_cancel_event.clear()
        self.cancel_btn.config(text="Cancel", state="normal")
        self.tools_btn.config(state="disabled")
        self.top_count_combo.config(state="disabled")

        self.dup_stats = {
            "files_total": self.root_node.file_count if self.root_node else 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "middle_hashed": 0,
        }

        total_files = max(1, self.root_node.file_count)
        self._start_determinate_progress(total_files)
        self.status_var.set("Preparing duplicate scan …")

        self.dup_thread = threading.Thread(
            target=self._duplicate_worker,
            daemon=True,
        )
        self.dup_thread.start()

        self.root.after(100, self._poll_duplicate_progress)

    def _duplicate_worker(self):
        root = self.root_node  # what's hashed, even if a rescan replaces it meanwhile
        try:
            duplicates = self._find_duplicate_files(
                progress_q=self.dup_progress_q,
                cancel_event=self.dup_cancel_event,
            )

            if self.dup_cancel_event.is_set():
                self.dup_progress_q.put(("cancelled", None))
                return
            self.duplicates = duplicates
            # Tied to the exact root_node these results came from, so a
            # rescan of a different path (which sets root_node to a new
            # object) can never be mistaken for still having a valid
            # cached duplicate set -- see start_scan's matching reset.
            self._duplicates_scan_root = root
            self.dup_progress_q.put(("done", duplicates))

        except Exception as exc:
            logger.exception("Duplicate scan failed")
            self.dup_progress_q.put(("error", str(exc)))

    def _poll_duplicate_progress(self):
        try:
            while True:
                msg = self.dup_progress_q.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _kind, current, total, text = msg
                    self.progress.config(maximum=max(1, total))
                    self._update_determinate_progress(current)

                    percent = (current / max(1, total)) * 100
                    skipped = self.dup_stats.get("files_skipped", 0)
                    skipped_bytes = self.dup_stats.get("bytes_skipped", 0)

                    self.status_var.set(
                        f"{text}  ({percent:5.1f}%)  |  "
                        f"Skipped: {skipped:,} files / {human_size(skipped_bytes)}"
                    )

                elif kind == "stats":
                    _kind, stats = msg
                    self.dup_stats = stats

                elif kind == "done":
                    _kind, duplicates = msg
                    self._stop_progress()
                    # A scan started since (and cancelled this search, too
                    # late): the groups are from the tree it replaced.
                    if self._scan_running() or self._duplicates_scan_root is not self.root_node:
                        return
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self._show_duplicates_window(duplicates)
                    return

                elif kind == "cancelled":
                    self._stop_progress()
                    if self._scan_running():  # the scan that cancelled it owns these now
                        return
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan cancelled.")
                    return

                elif kind == "error":
                    _kind, error_msg = msg
                    self._stop_progress()
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self.status_var.set("Duplicate scan failed.")
                    messagebox.showerror("Storage Scanner", f"Duplicate scan failed:\n{error_msg}")
                    return

        except queue.Empty:
            pass

        self.root.after(100, self._poll_duplicate_progress)

    def _show_duplicates_window(self, duplicates):

        existing = getattr(self, "_duplicates_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._duplicates_win = win
        win.configure(bg=COLORS["bg"])
        win.geometry("980x600")

        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            logger.debug("Duplicates window iconbitmap failed", exc_info=True)

        stats = getattr(self, "dup_stats", {})
        window = human_size(DUPLICATE_HASH_CHUNK_BYTES)
        header_var = StringVar()
        scan_path = self.root_node.path
        scan_tree = self._duplicates_scan_root  # what every row here is from

        ttk.Label(win, padding=(10, 8), textvariable=header_var).pack(side=TOP, fill=X)

        ttk.Label(
            win,
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

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("group", "role", "size", "copies", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")

        tv.heading("group", text="Group")
        tv.heading("role", text="Role")
        tv.heading("size", text="Size")
        tv.heading("copies", text="Copies")
        tv.heading("path", text="Path")

        tv.column("group", width=60, anchor=E, stretch=False)
        tv.column("role", width=80, anchor=W, stretch=False)
        tv.column("size", width=100, anchor=E, stretch=False)
        tv.column("copies", width=60, anchor=E, stretch=False)
        tv.column("path", width=620, anchor=W, stretch=True)

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

        # Per-group state, kept in step with deletes from any window
        # (forget_deleted). The keeper can be re-picked (right-click) and is
        # never a deletion target here; the delete service's on-disk re-check
        # guarantees a copy survives even if this list is somehow behind.
        iid_to_node = {}
        iid_to_group = {}
        group_nodes = {}  # group_num -> [nodes], sorted by path
        group_size = {}  # group_num -> bytes per copy
        group_keeper = {}  # group_num -> the node marked Keeper
        group_rows = {}  # group_num -> [iid, ...]

        details_var = StringVar(value="Select a row to see why it was flagged.")

        def row_values(group_num, node):
            nodes = group_nodes[group_num]
            role = "Keeper" if node == group_keeper[group_num] else "Duplicate"
            copies = f"{nodes.index(node) + 1}/{len(nodes)}"
            return (group_num, role, human_size(group_size[group_num]), copies, node.path)

        def refresh_group(group_num):
            for iid in group_rows[group_num]:
                node = iid_to_node[iid]
                tag = "keep" if node == group_keeper[group_num] else "dupe"
                tv.item(
                    iid,
                    values=row_values(group_num, node),
                    tags=(tag, tv.item(iid, "tags")[1]),
                )

        def summarize():
            sizes = [(group_size[g], len(nodes)) for g, nodes in group_nodes.items()]
            wasted = sum(size * (copies - 1) for size, copies in sizes)
            sampled = sum(1 for size, _copies in sizes if is_sampled_duplicate(size))
            sampled_note = (
                f"  ({sampled:,} over {human_size(3 * DUPLICATE_HASH_CHUNK_BYTES)} matched "
                f"on first/middle/last {window} only)"
                if sampled
                else ""
            )
            header_var.set(
                f"Duplicate files under {scan_path}  —  {len(sizes):,} groups, "
                f"potential cleanup: {human_size(wasted)}{sampled_note}"
            )
            win.title(f"Duplicate Files — {len(sizes)} groups")
            return len(sizes), wasted

        row_index = 0
        for group_num, (size, _digest, nodes) in enumerate(duplicates, start=1):
            group_nodes[group_num] = sorted(nodes, key=lambda n: n.path.lower())
            group_size[group_num] = size
            group_keeper[group_num] = pick_keeper(group_nodes[group_num])
            group_rows[group_num] = []
            for node in group_nodes[group_num]:
                tag = "keep" if node == group_keeper[group_num] else "dupe"
                iid = tv.insert(
                    "",
                    END,
                    values=row_values(group_num, node),
                    tags=(tag, "odd" if row_index % 2 else "even"),
                )
                iid_to_node[iid] = node
                iid_to_group[iid] = group_num
                group_rows[group_num].append(iid)
                row_index += 1
        group_count, total_wasted = summarize()

        def forget_deleted(deleted):
            """A delete from any window: drop the deleted copies, dissolve a
            group left with one copy (it's no longer a duplicate of anything)
            and re-pick a group's keeper if it was the one deleted."""
            affected = {
                iid_to_group[iid] for iid, node in iid_to_node.items() if deleted.covers(node)
            }
            for group_num in affected:
                remaining, keeper = settle_group(
                    group_nodes[group_num], group_keeper[group_num], deleted
                )
                for iid in list(group_rows[group_num]):
                    if keeper is None or iid_to_node[iid] not in remaining:
                        tv.delete(iid)
                        del iid_to_node[iid]
                        del iid_to_group[iid]
                        group_rows[group_num].remove(iid)
                if keeper is None:
                    for state in (group_nodes, group_size, group_keeper, group_rows):
                        del state[group_num]
                    continue
                group_nodes[group_num] = remaining
                group_keeper[group_num] = keeper
                refresh_group(group_num)
            if affected:
                summarize()

        self._watch_deletions(win, forget_deleted)

        def make_keeper(iid):
            group_num = iid_to_group.get(iid)
            if group_num is None or iid_to_node[iid] == group_keeper[group_num]:
                return
            group_keeper[group_num] = iid_to_node[iid]
            refresh_group(group_num)
            details_var.set(
                f"Manually set as keeper for group {group_num}. "
                f"The previous keeper is now a regular duplicate."
            )

        def show_row_reason(iid):
            group_num = iid_to_group.get(iid)
            if group_num is None:
                return
            node = iid_to_node[iid]
            keeper = group_keeper[group_num]
            if node == keeper:
                details_var.set(f"Kept: {keeper_reason(keeper, group_nodes[group_num])}")
            elif is_sampled_duplicate(node.size):
                details_var.set(
                    f"Likely duplicate of the keeper ({keeper.path}): same size and same "
                    f"first, middle and last {window}; the bytes between weren't compared."
                )
            else:
                details_var.set(f"Duplicate of the keeper ({keeper.path}).")

        tv.bind("<<TreeviewSelect>>", lambda _e: show_row_reason(tv.focus()))

        # Right-click: let the user override which copy in a group is kept,
        # instead of only ever trusting the automatic heuristic.
        row_menu = Menu(win, tearoff=0)

        def show_row_menu(event):
            iid = tv.identify_row(event.y)
            if not iid or iid not in iid_to_group:
                return
            tv.selection_set(iid)
            tv.focus(iid)
            row_menu.delete(0, END)
            if group_keeper[iid_to_group[iid]] != iid_to_node[iid]:
                row_menu.add_command(
                    label="Make this the keeper",
                    command=lambda: make_keeper(iid),
                )
            else:
                row_menu.add_command(label="This copy is already the keeper", state="disabled")
            row_menu.tk_popup(event.x_root, event.y_root)

        tv.bind("<Button-2>" if IS_MACOS else "<Button-3>", show_row_menu)

        ttk.Label(
            win,
            textvariable=details_var,
            style="Accent.TLabel",
            padding=(10, 4),
        ).pack(side=BOTTOM, fill=X)

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def reveal_selected():
            node = iid_to_node.get(tv.focus())
            if node:
                self._reveal(node.path, is_dir=False)

        def copy_selected_path():
            node = iid_to_node.get(tv.focus())
            if node:
                self.root.clipboard_clear()
                self.root.clipboard_append(node.path)

        def selected_copies():
            """(the selected non-keeper copies, how many selected keepers were
            left out). The keeper in each group is never a deletion target,
            even if selected (e.g. via select-all)."""
            selected = [iid for iid in tv.selection() if iid in iid_to_node]
            copies = [
                iid_to_node[iid]
                for iid in selected
                if iid_to_node[iid] != group_keeper[iid_to_group[iid]]
            ]
            return copies, len(selected) - len(copies)

        def sampled_warning(count):
            return (
                f"\n\n⚠ {count} file(s) are from sampled matches "
                "(only first, middle, and last 1 MB compared — bytes between "
                "the compared windows weren't checked)."
                if count
                else ""
            )

        def delete_selected_duplicates():
            targets, skipped_keepers = selected_copies()
            if not targets:
                messagebox.showinfo(
                    "Storage Scanner",
                    "Select at least one duplicate copy that isn't a group's keeper.",
                    parent=win,
                )
                return
            if self._refuse_delete_during_scan(parent=win):
                return

            note = (
                f" ({skipped_keepers} selected keeper file(s) were skipped — "
                "keepers are protected and can't be deleted here.)"
                if skipped_keepers
                else ""
            )
            sampled_count, _sampled = get_sampled_duplicates_from_groups(
                targets, self.duplicates or []
            )
            if not messagebox.askyesno(
                "Delete selected duplicates",
                f"Send {len(targets)} selected file(s) to the {TRASH_NAME}?{note}"
                + sampled_warning(sampled_count),
                icon="warning",
                parent=win,
            ):
                return
            # Deleted rows leave this window through forget_deleted.
            self._delete_nodes(
                [
                    DeleteRequest(node, "Duplicate Files", as_duplicate=True, tree=scan_tree)
                    for node in targets
                ],
                win,
            )

        def add_selected_to_cart():
            # Same exclusion as delete: a group's keeper can never be queued
            # for deletion, even via the cart.
            targets, _skipped = selected_copies()
            if not targets:
                messagebox.showinfo(
                    "Storage Scanner",
                    "Select at least one duplicate copy that isn't a group's keeper.",
                    parent=win,
                )
                return

            sampled_count, sampled_nodes = get_sampled_duplicates_from_groups(
                targets, self.duplicates or []
            )
            if sampled_count and not messagebox.askyesno(
                "Add sampled duplicates to cart",
                f"Add {len(targets)} file(s) to the Cleanup Cart?" + sampled_warning(sampled_count),
                icon="warning",
                parent=win,
            ):
                return

            for node in targets:
                self.cart.add(
                    node,
                    "Duplicate Files",
                    is_sampled=node in sampled_nodes,
                    as_duplicate=True,
                )
            self._refresh_cart_indicator()

        ttk.Button(
            button_bar,
            text=f"Reveal in {FILE_MANAGER_NAME}",
            command=reveal_selected,
        ).pack(side=LEFT)

        ttk.Button(
            button_bar,
            text="Copy Path",
            command=copy_selected_path,
        ).pack(side=LEFT, padx=6)

        ttk.Button(
            button_bar,
            text="Add Selected to Cart",
            command=add_selected_to_cart,
        ).pack(side=LEFT)

        ttk.Button(
            button_bar,
            text="Delete Selected",
            command=delete_selected_duplicates,
        ).pack(side=RIGHT)

        tv.bind("<Double-1>", lambda _e: reveal_selected())

        if not group_count:
            self.status_var.set("No duplicate files found.")
        else:
            self.status_var.set(
                f"Found {group_count:,} duplicate groups. "
                f"Potential cleanup: {human_size(total_wasted)}"
            )
