"""Duplicate-file detection pipeline and the Duplicate Files window.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

import hashlib
import os
import queue
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from storage_scanner.audit import recycle_and_log
from storage_scanner.cleanup_recommendations import (
    get_sampled_duplicates_from_groups,
    is_protected_path,
    is_sampled_duplicate,
    keeper_reason,
    pick_keeper,
)
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

    # -- Content sampling --------------------------------------------------- #
    # Both digests read at offsets derived from `size`: the size the scan
    # recorded, which candidates are grouped on and is_sampled_duplicate()
    # judges. A file that's no longer that size changed since the scan; its
    # windows would no longer be the ones `size` implies, and a match could
    # claim a byte-exact coverage it never had, so it hashes to None instead.
    def _partial_hash_file(
        self, path, size, cancel_event=None, chunk_size=DUPLICATE_HASH_CHUNK_BYTES
    ):
        """BLAKE2b of the first and last `chunk_size` bytes of a file the
        scan recorded as `size` bytes.

        For a file no larger than 2 * chunk_size those two windows overlap
        or touch, so this digest already covers every byte. None if the
        file can't be read, is no longer `size` bytes, or the scan was
        cancelled.
        """
        if cancel_event and cancel_event.is_set():
            return None
        try:
            with open(path, "rb") as f:
                if os.fstat(f.fileno()).st_size != size:
                    return None
                h = hashlib.blake2b(f.read(chunk_size), digest_size=32)
                if size > chunk_size:
                    f.seek(size - chunk_size)
                    h.update(f.read(chunk_size))
                return h.hexdigest()
        except OSError:
            return None

    def _middle_hash_file(
        self, path, size, cancel_event=None, chunk_size=DUPLICATE_HASH_CHUNK_BYTES
    ):
        """BLAKE2b of the `chunk_size` bytes centered on the midpoint of a
        file the scan recorded as `size` bytes.

        The window starts at (size - chunk_size) // 2. For any file of
        2 * chunk_size < size <= 3 * chunk_size that start is <= chunk_size
        and its end is >= size - chunk_size, so together with the head and
        tail windows every byte is covered and a match is byte-exact.
        Above 3 * chunk_size the bytes between the windows are never read:
        a match there is sampled, not verified. None if the file can't be
        read, is no longer `size` bytes, or the scan was cancelled.
        """
        if cancel_event and cancel_event.is_set():
            return None
        try:
            with open(path, "rb") as f:
                if os.fstat(f.fileno()).st_size != size:
                    return None
                f.seek(max(0, (size - chunk_size) // 2))
                return hashlib.blake2b(f.read(chunk_size), digest_size=32).hexdigest()
        except OSError:
            return None

    def _find_duplicate_files(self, progress_q=None, cancel_event=None):
        """
        Find duplicate files under the scanned root.

        1. Collect files.
        2. Group by size.
        3. Hash the first and last chunk of files with matching sizes.
        4. Hash the middle chunk of files still matching, when they're
           larger than two chunks (smaller ones are already fully covered).

        Returns [(size, (edge_digest, middle_digest), nodes), ...], largest
        recoverable space first. Groups of files up to three chunks are
        byte-exact matches; larger ones only matched on the sampled windows
        (see cleanup_recommendations.is_sampled_duplicate).
        """

        if not self.root_node:
            return []

        if cancel_event is None:
            cancel_event = threading.Event()

        # A purely local counter, never self.dup_stats -- this function
        # runs from two independent callers that can be active at once
        # (show_duplicates()'s own background worker, and
        # CleanupMixin's own duplicate-candidate scan). Mutating a single
        # shared dict from either would race the other and cross-
        # contaminate whichever window is currently displaying it.
        # show_duplicates()'s live-progress display still works exactly
        # as before: it reads self.dup_stats only from the "stats"
        # messages posted below, assigned wholesale by
        # _poll_duplicate_progress, never by mutating this dict in place.
        stats = {
            "files_total": self.root_node.file_count if self.root_node else 0,
            "files_checked": 0,
            "files_skipped": 0,
            "bytes_skipped": 0,
            "partial_hashed": 0,
            "middle_hashed": 0,
        }

        # ------------------------------------------------------------
        # Phase 0: collect files from your existing scanned tree
        # ------------------------------------------------------------
        all_files = []
        stack = [self.root_node]

        while stack:
            if cancel_event.is_set():
                return []

            node = stack.pop()

            if node.is_dir:
                if self._should_skip_duplicate_scan(node.path):
                    skipped_count = 0
                    skipped_bytes = 0

                    # Count skipped files under this skipped directory.
                    count_stack = [node]
                    while count_stack:
                        skipped_node = count_stack.pop()

                        if skipped_node.is_dir:
                            count_stack.extend(skipped_node.children)
                        else:
                            skipped_count += 1
                            skipped_bytes += skipped_node.size

                    stats["files_skipped"] += skipped_count
                    stats["bytes_skipped"] += skipped_bytes

                    if progress_q:
                        progress_q.put(
                            (
                                "stats",
                                dict(stats),
                            )
                        )

                    continue

                stack.extend(node.children)

            else:
                if node.size > 0:
                    # Cloud placeholders (OneDrive Files On-Demand, etc.)
                    # report their full logical size but aren't actually on
                    # local disk — hashing one would force Windows to
                    # download it just to compare it. Skip them entirely.
                    if self._should_skip_duplicate_scan(node.path) or node.is_cloud_placeholder:
                        stats["files_skipped"] += 1
                        stats["bytes_skipped"] += node.size

                        if progress_q:
                            progress_q.put(
                                (
                                    "stats",
                                    dict(stats),
                                )
                            )
                    else:
                        all_files.append(node)

        stats["files_checked"] = len(all_files)

        if progress_q:
            progress_q.put(
                (
                    "stats",
                    dict(stats),
                )
            )

        total_files = max(1, len(all_files))

        if progress_q:
            progress_q.put(("progress", 0, total_files, f"Collecting files … 0/{total_files:,}"))

        # ------------------------------------------------------------
        # Phase 1: group files by size
        # ------------------------------------------------------------
        by_size = defaultdict(list)

        for index, node in enumerate(all_files, start=1):
            if cancel_event.is_set():
                return []

            by_size[node.size].append(node)

            if progress_q and (index % 1000 == 0 or index == total_files):
                progress_q.put(
                    (
                        "progress",
                        index,
                        total_files,
                        f"Checking file sizes … {index:,}/{total_files:,}",
                    )
                )

        # Only files with matching size can be duplicates
        same_size_groups = [nodes for nodes in by_size.values() if len(nodes) > 1]

        files_to_partial_hash = []
        for nodes in same_size_groups:
            files_to_partial_hash.extend(nodes)

        total_partial_files = max(1, len(files_to_partial_hash))

        if not files_to_partial_hash:
            return []

        if progress_q:
            progress_q.put(
                (
                    "progress",
                    0,
                    total_partial_files,
                    f"Partial hashing possible duplicates … 0/{total_partial_files:,}",
                )
            )

        # ------------------------------------------------------------
        # Phase 2: hash first + last chunk
        # ------------------------------------------------------------
        chunk_size = DUPLICATE_HASH_CHUNK_BYTES
        by_partial_hash = defaultdict(list)

        max_workers = min(
            8, (os.cpu_count() or 4) * 2
        )  # Change max workers to 4 if it gets sluggish

        def partial_job(node):
            if cancel_event.is_set():
                return node, None

            return node, self._partial_hash_file(node.path, node.size, cancel_event, chunk_size)

        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(partial_job, node) for node in files_to_partial_hash]

            for future in as_completed(futures):
                if cancel_event.is_set():
                    return []

                node, digest = future.result()
                completed += 1

                stats["partial_hashed"] = completed

                if digest:
                    by_partial_hash[(node.size, digest)].append(node)

                if progress_q and (completed % 50 == 0 or completed == total_partial_files):
                    progress_q.put(
                        (
                            "progress",
                            completed,
                            total_partial_files,
                            "Partial hashing possible duplicates … "
                            f"{completed:,}/{total_partial_files:,}",
                        )
                    )

        # ------------------------------------------------------------
        # Phase 3: hash the middle chunk of surviving candidates
        # ------------------------------------------------------------
        # Head + tail already cover every byte of a file no larger than two
        # chunks, so those keep their phase-2 key as-is; only larger files
        # need the middle window read.
        by_final_key = defaultdict(list)
        files_to_middle_hash = []

        for (size, digest), nodes in by_partial_hash.items():
            if len(nodes) < 2:
                continue
            if size <= 2 * chunk_size:
                by_final_key[(size, (digest, None))].extend(nodes)
            else:
                files_to_middle_hash.extend((node, digest) for node in nodes)

        total_middle_files = max(1, len(files_to_middle_hash))

        if files_to_middle_hash and progress_q:
            progress_q.put(
                (
                    "progress",
                    0,
                    total_middle_files,
                    f"Hashing middle of matching candidates … 0/{total_middle_files:,}",
                )
            )

        def middle_job(node, partial_digest):
            if cancel_event.is_set():
                return node, partial_digest, None

            return (
                node,
                partial_digest,
                self._middle_hash_file(node.path, node.size, cancel_event, chunk_size),
            )

        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(middle_job, node, partial_digest)
                for node, partial_digest in files_to_middle_hash
            ]

            for future in as_completed(futures):
                if cancel_event.is_set():
                    return []

                node, partial_digest, middle_digest = future.result()
                completed += 1

                stats["middle_hashed"] = completed

                if middle_digest:
                    by_final_key[(node.size, (partial_digest, middle_digest))].append(node)

                if progress_q and (completed % 10 == 0 or completed == total_middle_files):
                    progress_q.put(
                        (
                            "progress",
                            completed,
                            total_middle_files,
                            "Hashing middle of matching candidates … "
                            f"{completed:,}/{total_middle_files:,}",
                        )
                    )

        if progress_q:
            progress_q.put(("stats", dict(stats)))

        # ------------------------------------------------------------
        # Phase 4: build final duplicate list
        # ------------------------------------------------------------
        duplicates = []

        for (size, digest), nodes in by_final_key.items():
            if len(nodes) > 1:
                duplicates.append((size, digest, nodes))

        duplicates.sort(
            key=lambda item: item[0] * (len(item[2]) - 1),
            reverse=True,
        )

        return duplicates

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
            self._duplicates_scan_root = self.root_node
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
                    self.tools_btn.config(state="normal")
                    self.top_count_combo.config(state="readonly")
                    self._show_duplicates_window(duplicates)
                    return

                elif kind == "cancelled":
                    self._stop_progress()
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
        win.title(f"Duplicate Files — {len(duplicates)} groups")
        win.geometry("980x600")

        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:
            logger.debug("Duplicates window iconbitmap failed", exc_info=True)

        total_wasted = sum(size * (len(nodes) - 1) for size, _digest, nodes in duplicates)
        stats = getattr(self, "dup_stats", {})
        files_checked = stats.get("files_checked", 0)
        files_skipped = stats.get("files_skipped", 0)
        bytes_skipped = stats.get("bytes_skipped", 0)
        partial_hashed = stats.get("partial_hashed", 0)
        middle_hashed = stats.get("middle_hashed", 0)
        sampled_groups = sum(
            1 for size, _digest, _nodes in duplicates if is_sampled_duplicate(size)
        )
        window = human_size(DUPLICATE_HASH_CHUNK_BYTES)
        sampled_note = (
            f"  ({sampled_groups:,} over {human_size(3 * DUPLICATE_HASH_CHUNK_BYTES)} matched "
            f"on first/middle/last {window} only)"
            if sampled_groups
            else ""
        )

        ttk.Label(
            win,
            padding=(10, 8),
            text=(
                f"Duplicate files under {self.root_node.path}  —  "
                f"{len(duplicates):,} groups, potential cleanup: {human_size(total_wasted)}"
                f"{sampled_note}"
            ),
        ).pack(side=TOP, fill=X)

        ttk.Label(
            win,
            padding=(10, 0, 10, 8),
            text=(
                f"Checked: {files_checked:,} files  |  "
                f"Skipped system files: {files_skipped:,}  |  "
                f"Skipped size: {human_size(bytes_skipped)}  |  "
                f"Head/tail hashed: {partial_hashed:,}  |  "
                f"Middle hashed: {middle_hashed:,}"
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

        # Per-group state, so the keeper can be re-picked interactively (via
        # right-click) and always excluded from deletion — this is what
        # actually guarantees at least one copy survives per group, not just
        # a warning label asking the user to be careful.
        iid_to_node = {}
        iid_to_group = {}
        group_nodes = {}  # group_num -> [nodes...]
        group_keeper_iid = {}  # group_num -> iid currently marked "keep"

        details_var = StringVar(value="Select a row to see why it was flagged.")

        def _row_values(group_num, node, size, total_copies, role):
            copy_index = group_nodes[group_num].index(node) + 1
            return (group_num, role, human_size(size), f"{copy_index}/{total_copies}", node.path)

        row_index = 0
        for group_num, (size, _digest, nodes) in enumerate(duplicates, start=1):
            nodes = sorted(nodes, key=lambda n: n.path.lower())
            group_nodes[group_num] = nodes
            keeper = pick_keeper(nodes)

            for node in nodes:
                is_keeper = node is keeper
                tag_type = "keep" if is_keeper else "dupe"
                stripe = "odd" if row_index % 2 else "even"

                iid = tv.insert(
                    "",
                    END,
                    values=_row_values(
                        group_num,
                        node,
                        size,
                        len(nodes),
                        "Keeper" if is_keeper else "Duplicate",
                    ),
                    tags=(tag_type, stripe),
                )
                iid_to_node[iid] = node
                iid_to_group[iid] = group_num
                if is_keeper:
                    group_keeper_iid[group_num] = iid
                row_index += 1

        def make_keeper(iid):
            group_num = iid_to_group.get(iid)
            node = iid_to_node.get(iid)
            if group_num is None or node is None:
                return
            old_keeper_iid = group_keeper_iid.get(group_num)
            if old_keeper_iid == iid:
                return

            if old_keeper_iid and tv.exists(old_keeper_iid):
                tv.item(
                    old_keeper_iid,
                    tags=(
                        "dupe",
                        tv.item(old_keeper_iid, "tags")[1],
                    ),
                )
                tv.set(old_keeper_iid, "role", "Duplicate")

            tv.item(iid, tags=("keep", tv.item(iid, "tags")[1]))
            tv.set(iid, "role", "Keeper")
            group_keeper_iid[group_num] = iid
            details_var.set(
                f"Manually set as keeper for group {group_num}. "
                f"The previous keeper is now a regular duplicate."
            )

        def show_row_reason(iid):
            group_num = iid_to_group.get(iid)
            node = iid_to_node.get(iid)
            if group_num is None or node is None:
                return
            nodes = group_nodes[group_num]
            keeper_iid = group_keeper_iid.get(group_num)
            keeper = iid_to_node.get(keeper_iid, node)
            if iid == keeper_iid:
                details_var.set(f"Kept: {keeper_reason(keeper, nodes)}")
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
            if not iid:
                return
            tv.selection_set(iid)
            tv.focus(iid)
            row_menu.delete(0, END)
            if group_keeper_iid.get(iid_to_group.get(iid)) != iid:
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
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self._reveal(node.path, is_dir=False)

        def copy_selected_path():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self.root.clipboard_clear()
                self.root.clipboard_append(node.path)

        def delete_selected_duplicates():
            selected = list(tv.selection())
            keeper_iids = set(group_keeper_iid.values())
            # The keeper in each group is never a valid deletion target,
            # even if selected (e.g. via select-all) — this is what actually
            # guarantees at least one copy survives per group.
            targets = [iid for iid in selected if iid in iid_to_node and iid not in keeper_iids]
            skipped_keepers = len(selected) - len(targets)

            if not targets:
                messagebox.showinfo(
                    "Storage Scanner",
                    "Select at least one duplicate copy that isn't a group's keeper.",
                    parent=win,
                )
                return

            note = (
                f" ({skipped_keepers} selected keeper file(s) were skipped — "
                "keepers are protected and can't be deleted here.)"
                if skipped_keepers
                else ""
            )

            # Check if any target nodes are from sampled groups
            sampled_count, _ = get_sampled_duplicates_from_groups(
                [iid_to_node.get(iid) for iid in targets],
                duplicates
            )
            sampled_warning = (
                f"\n\n⚠ {sampled_count} file(s) are from sampled matches "
                "(only first, middle, and last 1 MB compared — bytes between "
                "the compared windows weren't checked)."
            ) if sampled_count else ""

            if not messagebox.askyesno(
                "Delete selected duplicates",
                f"Send {len(targets)} selected file(s) to the {TRASH_NAME}?{note}{sampled_warning}",
                icon="warning",
                parent=win,
            ):
                return
            failed = []

            for iid in targets:
                node = iid_to_node.get(iid)
                if not node:
                    continue

                if recycle_and_log(node, source="Duplicate Files"):
                    deleted_count += 1
                    iid_to_node.pop(iid, None)
                    iid_to_group.pop(iid, None)
                    tv.delete(iid)
                    self._remove_search_result_from_tree(node)
                    self._remove_from_duplicate_cache(node)
                else:
                    failed.append(node.path)

            self.status_var.set(f"Deleted {deleted_count:,} duplicate file(s) to {TRASH_NAME}.")

            if failed:
                messagebox.showerror(
                    "Storage Scanner",
                    "Some files could not be deleted:\n\n" + "\n".join(failed[:10]),
                    parent=win,
                )

        def add_selected_to_cart():
            selected = list(tv.selection())
            keeper_iids = set(group_keeper_iid.values())
            # Same exclusion as delete: a group's keeper can never be
            # queued for deletion, even via the cart.
            targets = [iid for iid in selected if iid in iid_to_node and iid not in keeper_iids]
            if not targets:
                messagebox.showinfo(
                    "Storage Scanner",
                    "Select at least one duplicate copy that isn't a group's keeper.",
                    parent=win,
                )
                return

            # Check if any target nodes are from sampled groups
            sampled_count, _ = get_sampled_duplicates_from_groups(
                [iid_to_node.get(iid) for iid in targets],
                duplicates
            )
            if sampled_count:
                if not messagebox.askyesno(
                    "Add sampled duplicates to cart",
                    f"Add {len(targets)} file(s) to the Cleanup Cart?\n\n"
                    f"⚠ {sampled_count} file(s) are from sampled matches "
                    "(only first, middle, and last 1 MB compared — bytes between "
                    "the compared windows weren't checked).",
                    icon="warning",
                    parent=win,
                ):
                    return

            for iid in targets:
                node = iid_to_node.get(iid)
                if node:
                    # Track whether this node is from a sampled group
                    is_sampled = node in sampled_nodes if sampled_count else False
                    self.cart.add(node, "Duplicate Files", is_sampled=is_sampled)
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

        if not duplicates:
            self.status_var.set("No duplicate files found.")
        else:
            self.status_var.set(
                f"Found {len(duplicates):,} duplicate groups. "
                f"Potential cleanup: {human_size(total_wasted)}"
            )

    def _remove_from_duplicate_cache(self, target_node):
        """Keep self.duplicates (the last completed "Find Duplicate Files"
        result, reused by Cleanup Recommendations -- see
        cleanup_window.show_cleanup_recommendations) consistent after a
        file or folder is deleted through *any* window, so a later reopen
        never recommends deleting something that's already gone.

        A group that drops to one remaining copy is no longer a duplicate
        of anything and is dropped entirely, not just shrunk to one row.
        """
        duplicates = self.duplicates
        if not duplicates:
            return

        if target_node.is_dir:
            stale = set()
            stack = [target_node]
            while stack:
                node = stack.pop()
                if node.is_dir:
                    stack.extend(node.children)
                else:
                    stale.add(node)
            if not stale:
                return
        else:
            stale = {target_node}

        updated = []
        changed = False
        for size, digest, nodes in duplicates:
            remaining = [n for n in nodes if n not in stale]
            if len(remaining) == len(nodes):
                updated.append((size, digest, nodes))
                continue
            changed = True
            if len(remaining) > 1:
                updated.append((size, digest, remaining))

        if changed:
            self.duplicates = updated

    def _subtract_from_ancestors(self, current, target):
        """Subtract target's size/count from every ancestor containing it."""
        if not current.is_dir:
            return False

        found = False

        for child in current.children:
            if child is target:
                found = True
                break

            if child.is_dir and self._subtract_from_ancestors(child, target):
                found = True
                break

        if found:
            current.size -= target.size
            current.file_count -= target.file_count

        return found

    # -- Shutdown ---------------------------------------------------------- #
