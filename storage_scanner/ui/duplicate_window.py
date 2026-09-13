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
    BOTH, BOTTOM, E, END, LEFT, RIGHT, TOP, Toplevel, W, X, messagebox, ttk,
)

from storage_scanner.file_ops import recycle
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, TRASH_NAME, resource_path
from storage_scanner.settings import COLORS, DEFAULT_DUPLICATE_EXCLUDES


class DuplicatesMixin:
    def _should_skip_duplicate_scan(self, path):
        
        """Return True if this path should be ignored during duplicate scans."""
        normalized = os.path.normcase(os.path.normpath(path))


        return any(part in normalized for part in DEFAULT_DUPLICATE_EXCLUDES)


    # -- Delete to Recycle Bin --------------------------------------------- #
    def _partial_hash_file(self, path, cancel_event=None, chunk_size=1024 * 1024):
        """
        Hash the first and last chunk of a file.

        This is much faster than full hashing large files. 
        Used only as a filtering stage before full hashing.
        """
        try:

            if cancel_event and cancel_event.is_set():
                return None
            
            file_size = os.path.getsize(path)

            h = hashlib.blake2b(digest_size=16)

            with open(path, "rb") as f:
                first_chunk = f.read(chunk_size)
                h.update(first_chunk)

                if file_size > chunk_size:
                    seek_pos = max(0, file_size - chunk_size)
                    f.seek(seek_pos)
                    last_chunk = f.read(chunk_size)
                    h.update(last_chunk)

            return h.hexdigest()

        except (OSError, PermissionError):
            return None
    def _full_hash_file(self, path, cancel_event = None, chunk_size=1024 * 1024):
        """
        Full-file hash used only after size and partial hash match.

        This confirms the duplicate safely.
        """
        try:
            h = hashlib.blake2b(digest_size=32)

            with open(path, "rb") as f:
                while True:

                    if cancel_event and cancel_event.is_set():
                        return None

                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    h.update(chunk)

            return h.hexdigest()

        except (OSError, PermissionError):
            return None
    def _find_duplicate_files(self, progress_q=None, cancel_event=None):
        """
        Find duplicate files under the scanned root.

        Optimized for very large scans:
        1. Collect files.
        2. Group by size.
        3. Partial-hash files with matching sizes.
        4. Full-hash only files with matching size + partial hash.
        """

        if not self.root_node:
            return []

        if cancel_event is None:
            cancel_event = threading.Event()

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

                    self.dup_stats["files_skipped"] += skipped_count
                    self.dup_stats["bytes_skipped"] += skipped_bytes

                    if progress_q:
                        progress_q.put((
                            "stats",
                            dict(self.dup_stats),
                        ))

                    continue

                stack.extend(node.children)

            else:
                if node.size > 0:
                    if self._should_skip_duplicate_scan(node.path):
                        self.dup_stats["files_skipped"] += 1
                        self.dup_stats["bytes_skipped"] += node.size

                        if progress_q:
                            progress_q.put((
                                "stats",
                                dict(self.dup_stats),
                            ))
                    else:
                        all_files.append(node)

        self.dup_stats["files_checked"] = len(all_files)

        if progress_q:
            progress_q.put((
                "stats",
                dict(self.dup_stats),
            ))

        total_files = max(1, len(all_files))

        if progress_q:
            progress_q.put((
                "progress",
                0,
                total_files,
                f"Collecting files … 0/{total_files:,}"
            ))

        # ------------------------------------------------------------
        # Phase 1: group files by size
        # ------------------------------------------------------------
        by_size = defaultdict(list)

        for index, node in enumerate(all_files, start=1):
            if cancel_event.is_set():
                return []

            by_size[node.size].append(node)

            if progress_q and (index % 1000 == 0 or index == total_files):
                progress_q.put((
                    "progress",
                    index,
                    total_files,
                    f"Checking file sizes … {index:,}/{total_files:,}"
                ))

        # Only files with matching size can be duplicates
        same_size_groups = [
            nodes for nodes in by_size.values()
            if len(nodes) > 1
        ]

        files_to_partial_hash = []
        for nodes in same_size_groups:
            files_to_partial_hash.extend(nodes)

        total_partial_files = max(1, len(files_to_partial_hash))

        if not files_to_partial_hash:
            return []

        if progress_q:
            progress_q.put((
                "progress",
                0,
                total_partial_files,
                f"Partial hashing possible duplicates … 0/{total_partial_files:,}"
            ))

        # ------------------------------------------------------------
        # Phase 2: partial hash
        # ------------------------------------------------------------
        by_partial_hash = defaultdict(list)

        max_workers = min(8, (os.cpu_count() or 4) * 2) # Change max workers to 4 if it gets sluggish

        def partial_job(node):
            if cancel_event.is_set():
                return node, None

            digest = self._partial_hash_file(node.path, cancel_event)
            return node, digest

        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(partial_job, node)
                for node in files_to_partial_hash
            ]

            for future in as_completed(futures):
                if cancel_event.is_set():
                    return []

                node, digest = future.result()
                completed += 1

                self.dup_stats["partial_hashed"] = completed

                if digest:
                    by_partial_hash[(node.size, digest)].append(node)

                if progress_q and (completed % 50 == 0 or completed == total_partial_files):
                    progress_q.put((
                        "progress",
                        completed,
                        total_partial_files,
                        f"Partial hashing possible duplicates … {completed:,}/{total_partial_files:,}"
                    ))

        # ------------------------------------------------------------
        # Phase 3: full hash only files that matched partial hash
        # ------------------------------------------------------------
        files_to_full_hash = []

        for nodes in by_partial_hash.values():
            if len(nodes) > 1:
                files_to_full_hash.extend(nodes)

        total_full_files = max(1, len(files_to_full_hash))

        if not files_to_full_hash:
            return []

        if progress_q:
            progress_q.put((
                "progress",
                0,
                total_full_files,
                f"Full hashing confirmed candidates … 0/{total_full_files:,}"
            ))

        by_full_hash = defaultdict(list)

        def full_job(node):
            if cancel_event.is_set():
                return node, None

            digest = self._full_hash_file(node.path, cancel_event)
            return node, digest

        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(full_job, node)
                for node in files_to_full_hash
            ]

            for future in as_completed(futures):
                if cancel_event.is_set():
                    return []

                node, digest = future.result()
                completed += 1

                self.dup_stats["full_hashed"] = completed

                if digest:
                    by_full_hash[(node.size, digest)].append(node)

                if progress_q and (completed % 10 == 0 or completed == total_full_files):
                    progress_q.put((
                        "progress",
                        completed,
                        total_full_files,
                        f"Full hashing confirmed candidates … {completed:,}/{total_full_files:,}"
                    ))

        # ------------------------------------------------------------
        # Phase 4: build final duplicate list
        # ------------------------------------------------------------
        duplicates = []

        for (size, digest), nodes in by_full_hash.items():
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
            messagebox.showinfo(
                "Storage Scanner",
                "Duplicate scan is already running."
            )
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
            "full_hashed": 0,
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
            else:
                self.duplicates = duplicates
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
                    messagebox.showerror(
                        "Storage Scanner",
                        f"Duplicate scan failed:\n{error_msg}"
                    )
                    return

        except queue.Empty:
            pass

        self.root.after(100, self._poll_duplicate_progress)
    def _show_duplicates_window(self,duplicates):

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
        full_hashed = stats.get("full_hashed", 0)

        ttk.Label(
            win,
            padding=(10, 8),
            text=(
                f"Duplicate files under {self.root_node.path}  —  "
                f"{len(duplicates):,} groups, potential cleanup: {human_size(total_wasted)}"
            ),
        ).pack(side=TOP, fill=X)

        ttk.Label(
            win,
            padding=(10, 0, 10, 8),
            text=(
                f"Checked: {files_checked:,} files  |  "
                f"Skipped system files: {files_skipped:,}  |  "
                f"Skipped size: {human_size(bytes_skipped)}  |  "
                f"Partial hashed: {partial_hashed:,}  |  "
                f"Full hashed: {full_hashed:,}"
            ),
            style="Accent.TLabel",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("group", "size", "copies", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")

        tv.heading("group", text="Group")
        tv.heading("size", text="Size")
        tv.heading("copies", text="Copies")
        tv.heading("path", text="Path")

        tv.column("group", width=70, anchor=E, stretch=False)
        tv.column("size", width=110, anchor=E, stretch=False)
        tv.column("copies", width=70, anchor=E, stretch=False)
        tv.column("path", width=700, anchor=W, stretch=True)

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

        iid_to_node = {}

        row_index = 0
        for group_num, (size, _digest, nodes) in enumerate(duplicates, start=1):
            # Sort shortest path first; usually the "original" is easier to inspect.
            nodes = sorted(nodes, key=lambda n: n.path.lower())

            for copy_index, node in enumerate(nodes, start=1):
                tag_type = "keep" if copy_index == 1 else "dupe"
                stripe = "odd" if row_index % 2 else "even"

                iid = tv.insert(
                    "",
                    END,
                    values=(
                        group_num,
                        human_size(size),
                        f"{copy_index}/{len(nodes)}",
                        node.path,
                    ),
                    tags=(tag_type, stripe),
                )
                iid_to_node[iid] = node
                row_index += 1

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
            nodes = [iid_to_node[iid] for iid in selected if iid in iid_to_node]

            if not nodes:
                return

            if not messagebox.askyesno(
                "Delete selected duplicates",
                f"Send {len(nodes)} selected file(s) to the {TRASH_NAME}?\n\n"
                "Warning: this does not automatically protect one copy per group. "
                "Only delete files you intentionally selected.",
                icon="warning",
                parent=win,
            ):
                return

            deleted_count = 0
            failed = []

            for iid in selected:
                node = iid_to_node.get(iid)
                if not node:
                    continue

                if recycle(node.path):
                    deleted_count += 1
                    iid_to_node.pop(iid, None)
                    tv.delete(iid)
                    self._remove_node_from_scan_tree(node)
                else:
                    failed.append(node.path)

            self.status_var.set(
                f"Deleted {deleted_count:,} duplicate file(s) to {TRASH_NAME}."
            )

            if failed:
                messagebox.showerror(
                    "Storage Scanner",
                    "Some files could not be deleted:\n\n" + "\n".join(failed[:10]),
                    parent=win,
                )

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
    def _remove_node_from_scan_tree(self, target_node):
        """Remove a deleted file node from the in-memory scan tree and update sizes.

        This keeps the current scan somewhat accurate after deleting from the
        duplicate window. It does not fully refresh every visible tree row;
        press F5 to rescan for a perfect view.
        """
        if not self.root_node or target_node.is_dir:
            return

        stack = [(self.root_node, None)]

        while stack:
            node, parent = stack.pop()

            if node is target_node:
                if parent and target_node in parent.children:
                    parent.children.remove(target_node)

                # Subtract size and count from ancestors.
                self._subtract_from_ancestors(self.root_node, target_node)
                return

            if node.is_dir:
                for child in node.children:
                    stack.append((child, node))
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
