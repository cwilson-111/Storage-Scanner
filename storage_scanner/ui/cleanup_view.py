"""The Cleanup Recommendations window: review-first cleanup candidates.

CleanupMixin (ui/cleanup_window.py) opens one of these for the app's live
scan, or for the rows last computed for some path (cleanup_cache) when
nothing has been scanned this session. Every row shows why it was flagged,
an estimated recoverable size, a risk level, and a proposed action. Archive
Selected lives in ui/cleanup_archive.py.
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
    StringVar,
    Toplevel,
    X,
    messagebox,
    ttk,
)

from history import (
    get_installed_install_locations,
    get_known_install_location_count,
    get_orphaned_install_locations,
    record_install_locations_snapshot,
)
from storage_scanner import cleanup_cache
from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE,
    CATEGORY_ORPHANED_INSTALL,
    CATEGORY_PROTECTED,
    CATEGORY_REVIEW,
    _drop_nested_under,
    build_duplicate_recommendations,
    find_orphaned_install_folders,
    find_protected_and_review_candidates,
    registry_read_looks_short,
)
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import (
    FILE_MANAGER_NAME,
    IS_WINDOWS,
    TRASH_NAME,
    resource_path,
)
from storage_scanner.settings import COLORS, px
from storage_scanner.ui.cleanup_archive import archive_review_candidates

_CATEGORY_TAGS = {
    CATEGORY_PROTECTED: "protected",
    CATEGORY_REVIEW: "review",
    CATEGORY_DUPLICATE: "duplicate",
    CATEGORY_ORPHANED_INSTALL: "orphaned_install",
}

_FIRST_RUN_SUFFIX = "  (orphaned-install detection is still learning this machine's installed apps)"
_SHORT_READ_SUFFIX = (
    "  (orphaned-install detection skipped: the installed-apps list came back incomplete)"
)


def _merge_with_orphans(metadata_recs, other_recs, orphan_recs):
    """metadata_recs + other_recs, with anything nested under a
    newly-found orphan folder dropped first so the same bytes
    never appear as two separate recommendations."""
    all_recs = metadata_recs + other_recs
    if orphan_recs:
        container_paths = {r.node.path for r in orphan_recs}
        all_recs = _drop_nested_under(all_recs, container_paths)
        all_recs += orphan_recs
    all_recs.sort(key=lambda r: r.recoverable_bytes, reverse=True)
    return all_recs


class CleanupWindow:
    """One Cleanup Recommendations window for `scan_path`: the app's live
    scan when there is one, otherwise the rows last cached for that path.
    `win` is its Toplevel. Nothing here is ever deleted without an explicit
    selection and confirmation, and Protected rows can never be deleted at
    all."""

    def __init__(self, app, scan_path):
        self.app = app
        self.scan_path = scan_path
        self.live = app.root_node is not None
        self.scan_tree = app.root_node  # what live rows are from; cached rows are from no tree
        self.iid_to_rec = {}

        self.win = Toplevel(app.root)
        self.win.configure(bg=COLORS["bg"])
        self.win.title("Cleanup Recommendations")
        self.win.geometry(f"{px(1020)}x{px(600)}")
        try:
            self.win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Cleanup Recommendations window iconbitmap failed", exc_info=True)

        self.summary_var = StringVar(value="Scanning for recommendations…")
        ttk.Label(
            self.win,
            textvariable=self.summary_var,
            style="Accent.TLabel",
            padding=(10, 8),
        ).pack(side=TOP, fill=X)
        self.tv = self._build_table()

        if self.live:
            self._load_live()
        else:
            self._load_cached()

        self._build_buttons()
        app._watch_deletions(self.win, self.forget_deleted)

    # ----- building -----

    def _build_table(self):
        frame = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("category", "name", "reason", "risk", "recoverable", "action")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tv.heading("category", text="Category")
        tv.heading("name", text="Item")
        tv.heading("reason", text="Why flagged")
        tv.heading("risk", text="Risk")
        tv.heading("recoverable", text="Recoverable")
        tv.heading("action", text="Proposed action")
        tv.column("category", width=px(110), anchor="w", stretch=False)
        tv.column("name", width=px(170), anchor="w", stretch=False)
        tv.column("reason", width=px(330), anchor="w", stretch=True)
        tv.column("risk", width=px(150), anchor="w", stretch=False)
        tv.column("recoverable", width=px(90), anchor="e", stretch=False)
        tv.column("action", width=px(210), anchor="w", stretch=False)

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
        tv.tag_configure("orphaned_install", foreground=COLORS["error"])
        tv.bind("<Double-1>", lambda _e: self.reveal_selected())
        return tv

    def _build_buttons(self):
        button_bar = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)
        ttk.Label(
            button_bar,
            text="Protected items can never be deleted from this window.",
            foreground=COLORS["muted"],
        ).pack(side=LEFT)
        ttk.Button(button_bar, text="Rescan", command=self.do_rescan).pack(side=LEFT, padx=(10, 0))
        ttk.Button(
            button_bar,
            text=f"Reveal in {FILE_MANAGER_NAME}",
            command=self.reveal_selected,
        ).pack(side=RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Delete Selected", command=self.delete_selected).pack(
            side=RIGHT
        )
        ttk.Button(
            button_bar, text="Archive Selected", command=lambda: archive_review_candidates(self)
        ).pack(side=RIGHT, padx=(0, 6))
        ttk.Button(
            button_bar,
            text="Add Selected to Cart",
            command=self.add_selected_to_cart,
        ).pack(side=RIGHT, padx=(0, 6))

    # ----- loading -----

    def _load_cached(self):
        # Cold start: nothing scanned this session -- show whatever
        # was last actually computed for scan_path, instantly,
        # no scan and no re-hashing needed.
        cached_recs = cleanup_cache.load_recommendations(self.scan_path)
        computed_at = cleanup_cache.get_computed_at(self.scan_path)
        when = computed_at.replace("T", " ") if computed_at else "an earlier session"
        self.populate(cached_recs)
        self.summarize(
            cached_recs,
            suffix=f"  (cached from {when} for {self.scan_path} — Rescan to refresh)",
        )
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)

    def _load_live(self):
        app = self.app
        # Phase 1: Protected + Review candidates come straight from
        # metadata already in the scanned tree — fast enough to run
        # synchronously.
        metadata_recs = find_protected_and_review_candidates(app.root_node)
        self.populate(metadata_recs)
        self.summarize(metadata_recs, suffix="  (scanning for duplicate files…)")

        # Phase 2: Duplicate candidates need content hashing, and orphaned-
        # install detection needs a registry read + a SQLite write — both
        # real I/O, so both run off the main thread in the (common)
        # non-cached branch below. If "Find Duplicate Files" has already
        # been run for this exact scan, reuse that result instead of
        # hashing every file a second time — and so those results are
        # never lost just because that window got closed. app.duplicates
        # persists across windows (see app.py/duplicate_window.py);
        # _duplicates_scan_root guards against reusing a stale result left
        # over from a since-replaced scan of a different path. Orphan
        # detection is fast enough (a registry read, not a hash) to run
        # synchronously even in this "instant" cached branch without
        # meaningfully changing how it feels to open.
        cached_duplicates = app.duplicates
        if cached_duplicates is not None and app._duplicates_scan_root is app.root_node:
            orphan_recs, orphan_note = self.compute_orphan_recommendations()
            all_recs = _merge_with_orphans(
                metadata_recs, build_duplicate_recommendations(cached_duplicates), orphan_recs
            )
            self.populate(all_recs)
            suffix = "  (duplicate results reused from Find Duplicate Files)"
            suffix += orphan_note
            self.summarize(all_recs, suffix=suffix)
            cleanup_cache.save_recommendations(self.scan_path, all_recs)
            self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        else:
            cancel_event = threading.Event()
            result_q = queue.Queue()
            threading.Thread(
                target=self._find_duplicates, args=(cancel_event, result_q), daemon=True
            ).start()
            self.win.after(150, self._poll_duplicates, cancel_event, result_q, metadata_recs)
            self.win.protocol("WM_DELETE_WINDOW", lambda: (cancel_event.set(), self.win.destroy()))

    def _find_duplicates(self, cancel_event, result_q):
        """Worker thread: hash for duplicates, then look for orphaned
        installs, and hand the result to _poll_duplicates."""
        try:
            groups = self.app._find_duplicate_files(cancel_event=cancel_event)
            orphan_recs, orphan_note = self.compute_orphan_recommendations()
            result_q.put(("done", (groups, orphan_recs, orphan_note)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Duplicate scan for cleanup recommendations failed")
            result_q.put(("error", str(exc)))

    def _poll_duplicates(self, cancel_event, result_q, metadata_recs):
        app, win = self.app, self.win
        if not win.winfo_exists():
            cancel_event.set()
            return
        try:
            kind, payload = result_q.get_nowait()
        except queue.Empty:
            win.after(150, self._poll_duplicates, cancel_event, result_q, metadata_recs)
            return
        if kind == "done":
            groups, orphan_recs, orphan_note = payload
            app.duplicates = groups
            app._duplicates_scan_root = app.root_node
            all_recs = _merge_with_orphans(
                metadata_recs, build_duplicate_recommendations(groups), orphan_recs
            )
            self.populate(all_recs)
            self.summarize(all_recs, suffix=orphan_note)
            cleanup_cache.save_recommendations(self.scan_path, all_recs)
        else:
            self.summarize(metadata_recs, suffix=f"  (duplicate scan failed: {payload})")

    def compute_orphan_recommendations(self):
        """Windows-only: read the uninstall registry, update this
        app's own persistent snapshot of install locations, and flag
        any folder matching a location whose owning app is no longer
        installed. ([], "") on any other platform. The second value is
        a note for the summary: on the very first snapshot ever taken
        (nothing to compare against yet -- see history.py's
        known_install_locations docstring for why that first-run gap is
        the accepted tradeoff for staying exact-match-only), or when
        the registry read came back short and was ignored.
        """
        if not IS_WINDOWS:
            return [], ""
        from storage_scanner.installed_apps import get_candidate_installed_apps

        is_first_run = get_known_install_location_count() == 0
        apps = get_candidate_installed_apps()
        previously_installed = len(get_installed_install_locations())
        if registry_read_looks_short(len(apps), previously_installed):
            logger.warning(
                "Uninstall registry read returned %d apps against %d last time; "
                "skipping orphaned-install detection",
                len(apps),
                previously_installed,
            )
            return [], _SHORT_READ_SUFFIX
        record_install_locations_snapshot(apps)
        orphaned_locations = {loc for loc, *_rest in get_orphaned_install_locations()}
        orphan_recs = find_orphaned_install_folders(
            self.app.root_node, orphaned_locations, get_installed_install_locations()
        )
        return orphan_recs, _FIRST_RUN_SUFFIX if is_first_run else ""

    # ----- rows -----

    def populate(self, recommendations):
        tv = self.tv
        tv.delete(*tv.get_children())
        self.iid_to_rec.clear()
        for index, rec in enumerate(recommendations):
            cat_tag = _CATEGORY_TAGS.get(rec.category, "review")
            iid = tv.insert(
                "",
                END,
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
            self.iid_to_rec[iid] = rec

    def summarize(self, recommendations, suffix=""):
        recoverable = sum(
            r.recoverable_bytes for r in recommendations if r.category != CATEGORY_PROTECTED
        )
        self.summary_var.set(
            f"{len(recommendations):,} recommendation(s) — "
            f"{human_size(recoverable)} potentially recoverable"
            f"{suffix}"
        )

    def resave_cache(self):
        # Keep the persisted cache in sync with what's still actually
        # shown after a delete/archive -- otherwise a cold-start reopen
        # (or another session) would recommend deleting something
        # that's already gone. See _load_cached, which is the only
        # consumer of this when there's no live scan at all to fall
        # back on.
        cleanup_cache.save_recommendations(self.scan_path, list(self.iid_to_rec.values()))

    def forget_deleted(self, deleted):
        # With a live scan, a duplicate row also goes once its file is no
        # longer a spare copy of a group that still exists (its group's
        # other copies were deleted, or it became the keeper).
        spare_copies = (
            {rec.node for rec in build_duplicate_recommendations(self.app.duplicates or [])}
            if self.live
            else None
        )
        gone = [
            iid
            for iid, rec in self.iid_to_rec.items()
            if deleted.covers(rec.node)
            or (
                spare_copies is not None
                and rec.category == CATEGORY_DUPLICATE
                and rec.node not in spare_copies
            )
        ]
        for iid in gone:
            del self.iid_to_rec[iid]
            self.tv.delete(iid)
        if gone:
            self.resave_cache()
            self.summarize(list(self.iid_to_rec.values()))

    # ----- actions -----

    def reveal_selected(self):
        sel = self.tv.focus()
        rec = self.iid_to_rec.get(sel)
        if rec:
            self.app._reveal(rec.node.path, is_dir=rec.node.is_dir)

    def do_rescan(self):
        self.app.path_var.set(self.scan_path)
        self.win.destroy()
        self.app.start_scan()

    def cached_folders_need_rescan(self, recs):
        """True if `recs` holds a folder from a cached (earlier-session)
        run, after offering to rescan: nobody has seen what's in it now,
        so the delete service won't delete it (delete_service.check_stale)."""
        if self.live or not any(rec.node.is_dir for rec in recs):
            return False
        if messagebox.askyesno(
            "Cleanup Recommendations",
            "Folders in these saved results can't be deleted until they've been "
            "scanned again: what's in them may have changed.\n\nRescan "
            f"{self.scan_path} now?",
            parent=self.win,
        ):
            self.do_rescan()
        return True

    def _deletable_selection(self):
        """The selected (iid, recommendation) pairs. Protected rows are
        silently skipped even if selected (e.g. via select-all) — they're
        never a valid deletion target here, cart included."""
        return [
            (iid, self.iid_to_rec[iid])
            for iid in self.tv.selection()
            if iid in self.iid_to_rec and self.iid_to_rec[iid].category != CATEGORY_PROTECTED
        ]

    def _nothing_deletable_selected(self):
        messagebox.showinfo(
            "Cleanup Recommendations",
            "Select at least one non-protected recommendation first.",
            parent=self.win,
        )

    def requests_for(self, targets):
        return [
            DeleteRequest(
                rec.node,
                "Cleanup Recommendations",
                as_duplicate=rec.category == CATEGORY_DUPLICATE,
                scan_root=self.scan_path,
                tree=self.scan_tree,
            )
            for _iid, rec in targets
        ]

    def delete_selected(self):
        app, win = self.app, self.win
        targets = self._deletable_selection()
        if not targets:
            self._nothing_deletable_selected()
            return
        if app._refuse_delete_during_scan(parent=win) or self.cached_folders_need_rescan(
            [rec for _iid, rec in targets]
        ):
            return

        kind = "item" if len(targets) == 1 else "items"
        if not messagebox.askyesno(
            f"Delete to {TRASH_NAME}",
            f"Send {len(targets)} selected {kind} to the {TRASH_NAME}?",
            icon="warning",
            parent=win,
        ):
            return

        # Deleted rows leave this list through forget_deleted.
        app._delete_nodes(self.requests_for(targets), win)

    def add_selected_to_cart(self):
        targets = [rec for _iid, rec in self._deletable_selection()]
        if not targets:
            self._nothing_deletable_selected()
            return
        if self.cached_folders_need_rescan(targets):
            return
        for rec in targets:
            self.app.cart.add(
                rec.node,
                "Cleanup Recommendations",
                as_duplicate=rec.category == CATEGORY_DUPLICATE,
            )
        self.app._refresh_cart_indicator()
