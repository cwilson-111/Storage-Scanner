"""Cleanup Recommendations window: review-first cleanup candidates.

A mixin composed into StorageScannerApp (storage_scanner/app.py). Every row
shows why it was flagged, an estimated recoverable size, a risk level, and a
proposed action — nothing here is ever deleted without an explicit
selection and confirmation, and Protected rows can never be deleted at all.
"""

import queue
import threading
from tkinter import BOTH, BOTTOM, END, LEFT, RIGHT, StringVar, TOP, Toplevel, X, messagebox, ttk

from storage_scanner import cleanup_cache
from storage_scanner.archive import archive_file, likely_compresses_well
from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE, CATEGORY_PROTECTED, CATEGORY_REVIEW,
    build_duplicate_recommendations, find_protected_and_review_candidates,
)
from storage_scanner.audit import recycle_and_log
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, TRASH_NAME, resource_path
from storage_scanner.settings import COLORS

_CATEGORY_TAGS = {
    CATEGORY_PROTECTED: "protected",
    CATEGORY_REVIEW: "review",
    CATEGORY_DUPLICATE: "duplicate",
}


class CleanupMixin:
    def show_cleanup_recommendations(self):
        cleanup_cache.init_cleanup_cache_db()

        # A live scan this session always wins -- otherwise fall back to
        # whatever was last actually computed here (any path, any past
        # session), so opening this straight after launch shows something
        # useful instead of silently doing nothing (the old behavior when
        # self.root_node was None). See cleanup_cache.py's own docstring
        # for why this persists the *computed* recommendation rows, not
        # the raw scanned tree.
        live = self.root_node is not None
        display_scan_path = (
            self.root_node.path if live else cleanup_cache.get_most_recently_cached_scan_path()
        )
        if display_scan_path is None:
            messagebox.showinfo(
                "Cleanup Recommendations",
                "Scan a folder first to see cleanup recommendations.",
            )
            return

        existing = getattr(self, "_cleanup_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._cleanup_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Cleanup Recommendations")
        win.geometry("1020x600")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Cleanup Recommendations window iconbitmap failed", exc_info=True)

        summary_var = StringVar(value="Scanning for recommendations…")
        ttk.Label(
            win, textvariable=summary_var, style="Accent.TLabel", padding=(10, 8),
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("category", "name", "reason", "risk", "recoverable", "action")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tv.heading("category", text="Category")
        tv.heading("name", text="Item")
        tv.heading("reason", text="Why flagged")
        tv.heading("risk", text="Risk")
        tv.heading("recoverable", text="Recoverable")
        tv.heading("action", text="Proposed action")
        tv.column("category", width=110, anchor="w", stretch=False)
        tv.column("name", width=170, anchor="w", stretch=False)
        tv.column("reason", width=330, anchor="w", stretch=True)
        tv.column("risk", width=150, anchor="w", stretch=False)
        tv.column("recoverable", width=90, anchor="e", stretch=False)
        tv.column("action", width=210, anchor="w", stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])
        # Inactive/off-limits -> muted; worth a look -> warning; a
        # confident, low-risk action once identified -> accent.
        tv.tag_configure("protected", foreground=COLORS["muted"])
        tv.tag_configure("review", foreground=COLORS["warning"])
        tv.tag_configure("duplicate", foreground=COLORS["accent"])

        iid_to_rec = {}

        def populate(recommendations):
            tv.delete(*tv.get_children())
            iid_to_rec.clear()
            for index, rec in enumerate(recommendations):
                cat_tag = _CATEGORY_TAGS.get(rec.category, "review")
                iid = tv.insert(
                    "", END,
                    values=(
                        rec.category,
                        rec.node.name,
                        rec.reason,
                        rec.risk,
                        human_size(rec.recoverable_bytes) if rec.recoverable_bytes else "—",
                        rec.action,
                    ),
                    tags=(cat_tag, "odd" if index % 2 else "even"),
                )
                iid_to_rec[iid] = rec

        def summarize(recommendations, suffix=""):
            recoverable = sum(
                r.recoverable_bytes for r in recommendations
                if r.category != CATEGORY_PROTECTED
            )
            summary_var.set(
                f"{len(recommendations):,} recommendation(s) — "
                f"{human_size(recoverable)} potentially recoverable"
                f"{suffix}"
            )

        if not live:
            # Cold start: nothing scanned this session -- show whatever
            # was last actually computed for display_scan_path, instantly,
            # no scan and no re-hashing needed.
            cached_recs = cleanup_cache.load_recommendations(display_scan_path)
            computed_at = cleanup_cache.get_computed_at(display_scan_path)
            when = computed_at.replace("T", " ") if computed_at else "an earlier session"
            populate(cached_recs)
            summarize(
                cached_recs,
                suffix=f"  (cached from {when} for {display_scan_path} — Rescan to refresh)",
            )
            win.protocol("WM_DELETE_WINDOW", win.destroy)
        else:
            # Phase 1: Protected + Review candidates come straight from
            # metadata already in the scanned tree — fast enough to run
            # synchronously.
            metadata_recs = find_protected_and_review_candidates(self.root_node)
            populate(metadata_recs)
            summarize(metadata_recs, suffix="  (scanning for duplicate files…)")

            # Phase 2: Duplicate candidates need content hashing. If "Find
            # Duplicate Files" has already been run for this exact scan, reuse
            # that result instead of hashing every file a second time — and so
            # those results are never lost just because that window got closed.
            # self.duplicates persists across windows (see app.py/duplicate_
            # window.py); _duplicates_scan_root guards against reusing a stale
            # result left over from a since-replaced scan of a different path.
            cached_duplicates = self.duplicates
            cancel_event = threading.Event()

            if cached_duplicates is not None and self._duplicates_scan_root is self.root_node:
                all_recs = metadata_recs + build_duplicate_recommendations(cached_duplicates)
                all_recs.sort(key=lambda r: r.recoverable_bytes, reverse=True)
                populate(all_recs)
                summarize(all_recs, suffix="  (duplicate results reused from Find Duplicate Files)")
                cleanup_cache.save_recommendations(display_scan_path, all_recs)
                win.protocol("WM_DELETE_WINDOW", win.destroy)
            else:
                result_q = queue.Queue()

                def worker():
                    try:
                        groups = self._find_duplicate_files(cancel_event=cancel_event)
                        result_q.put(("done", groups))
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Duplicate scan for cleanup recommendations failed")
                        result_q.put(("error", str(exc)))

                def poll():
                    if not win.winfo_exists():
                        cancel_event.set()
                        return
                    try:
                        kind, payload = result_q.get_nowait()
                    except queue.Empty:
                        win.after(150, poll)
                        return
                    if kind == "done":
                        self.duplicates = payload
                        self._duplicates_scan_root = self.root_node
                        all_recs = metadata_recs + build_duplicate_recommendations(payload)
                        all_recs.sort(key=lambda r: r.recoverable_bytes, reverse=True)
                        populate(all_recs)
                        summarize(all_recs)
                        cleanup_cache.save_recommendations(display_scan_path, all_recs)
                    else:
                        summarize(metadata_recs, suffix=f"  (duplicate scan failed: {payload})")

                threading.Thread(target=worker, daemon=True).start()
                win.after(150, poll)
                win.protocol("WM_DELETE_WINDOW", lambda: (cancel_event.set(), win.destroy()))

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        def reveal_selected():
            sel = tv.focus()
            rec = iid_to_rec.get(sel)
            if rec:
                self._reveal(rec.node.path, is_dir=rec.node.is_dir)

        def resave_cache():
            # Keep the persisted cache in sync with what's still actually
            # shown after a delete/archive -- otherwise a cold-start reopen
            # (or another session) would recommend deleting something
            # that's already gone. See show_cleanup_recommendations'
            # `not live` branch, which is the only consumer of this when
            # there's no live scan at all to fall back on.
            cleanup_cache.save_recommendations(display_scan_path, list(iid_to_rec.values()))

        def do_rescan():
            self.path_var.set(display_scan_path)
            win.destroy()
            self.start_scan()

        def delete_selected():
            selected = list(tv.selection())
            # Protected rows are silently skipped even if selected (e.g. via
            # select-all) — they're never a valid deletion target here.
            targets = [
                (iid, iid_to_rec[iid]) for iid in selected
                if iid in iid_to_rec and iid_to_rec[iid].category != CATEGORY_PROTECTED
            ]
            if not targets:
                messagebox.showinfo(
                    "Cleanup Recommendations",
                    "Select at least one non-protected recommendation first.",
                    parent=win,
                )
                return

            kind = "item" if len(targets) == 1 else "items"
            if not messagebox.askyesno(
                f"Delete to {TRASH_NAME}",
                f"Send {len(targets)} selected {kind} to the {TRASH_NAME}?",
                icon="warning",
                parent=win,
            ):
                return

            deleted = 0
            failed = []
            for iid, rec in targets:
                if recycle_and_log(rec.node, source="Cleanup Recommendations"):
                    deleted += 1
                    self._remove_search_result_from_tree(rec.node)
                    self._remove_from_duplicate_cache(rec.node)
                    iid_to_rec.pop(iid, None)
                    tv.delete(iid)
                else:
                    failed.append(rec.node.path)

            if deleted:
                resave_cache()

            self.status_var.set(f"Deleted {deleted:,} item(s) to {TRASH_NAME}.")
            if failed:
                messagebox.showerror(
                    "Storage Scanner",
                    "Some items could not be deleted:\n\n" + "\n".join(failed[:10]),
                    parent=win,
                )

        def archive_selected():
            selected = list(tv.selection())
            # Archive only applies to Review candidates — duplicates already
            # have a clearer "delete the copy, keep the keeper" story, and
            # Protected rows are never a valid target for anything here.
            targets = [
                (iid, iid_to_rec[iid]) for iid in selected
                if iid in iid_to_rec and iid_to_rec[iid].category == CATEGORY_REVIEW
            ]
            skipped = len(selected) - len(targets)
            if not targets:
                messagebox.showinfo(
                    "Cleanup Recommendations",
                    "Select at least one Review candidate to archive "
                    "(Archive only applies to that category).",
                    parent=win,
                )
                return

            poor = [rec.node.name for _iid, rec in targets if not likely_compresses_well(rec.node.path)]
            warning = ""
            if poor:
                sample = ", ".join(poor[:5])
                more = f" and {len(poor) - 5} more" if len(poor) > 5 else ""
                warning = (
                    f"\n\nNote: {len(poor)} of these ({sample}{more}) are already-compressed "
                    f"formats and likely won't shrink much."
                )
            note = f" ({skipped} non-Review-candidate row(s) skipped.)" if skipped else ""

            if not messagebox.askyesno(
                "Archive selected files",
                f"Compress {len(targets)} selected file(s) to .zip and remove the "
                f"originals (via {TRASH_NAME}, fully reversible)?{note}{warning}",
                icon="warning",
                parent=win,
            ):
                return

            archived = 0
            partial = 0
            failed = []
            for iid, rec in targets:
                result = archive_file(rec.node, source="Cleanup Recommendations")
                if not result.success:
                    failed.append(f"{rec.node.path}: {result.error}")
                    continue
                archived += 1
                if result.original_removed:
                    self._remove_search_result_from_tree(rec.node)
                else:
                    partial += 1
                iid_to_rec.pop(iid, None)
                tv.delete(iid)

            if archived:
                resave_cache()

            status_bits = [f"Archived {archived:,} file(s) (rescan to see the .zip files)."]
            if partial:
                status_bits.append(f"{partial} kept both copies (original couldn't be removed).")
            self.status_var.set(" ".join(status_bits))
            if failed:
                messagebox.showerror(
                    "Storage Scanner",
                    "Some files could not be archived:\n\n" + "\n".join(failed[:10]),
                    parent=win,
                )

        ttk.Label(
            button_bar,
            text="Protected items can never be deleted from this window.",
            foreground=COLORS["muted"],
        ).pack(side=LEFT)
        ttk.Button(button_bar, text="Rescan", command=do_rescan).pack(side=LEFT, padx=(10, 0))
        ttk.Button(
            button_bar, text=f"Reveal in {FILE_MANAGER_NAME}", command=reveal_selected,
        ).pack(side=RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Delete Selected", command=delete_selected).pack(side=RIGHT)
        ttk.Button(button_bar, text="Archive Selected", command=archive_selected).pack(
            side=RIGHT, padx=(0, 6)
        )

        tv.bind("<Double-1>", lambda _e: reveal_selected())
