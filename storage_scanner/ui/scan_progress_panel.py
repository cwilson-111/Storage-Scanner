"""The scan progress panel: one overall bar with live counters and the
folder being read, plus a bar for each top-level folder of the scan target.

A mixin composed into StorageScannerApp (storage_scanner/app.py). What it
shows comes from ScanProgressModel.view() (storage_scanner/
scan_progress_model.py); this module only owns the widgets. It only ever
runs on the UI thread: main_window._poll_progress hands it the scan's
progress messages and refreshes it on the same 100 ms tick -- so it
redraws at most ten times a second however fast messages arrive, no scan
thread touches Tk, and a refresh redraws only what changed.
"""

import threading
from tkinter import BOTTOM, LEFT, RIGHT, TOP, E, StringVar, W, X, ttk

from storage_scanner.formatting import bar
from storage_scanner.scan_progress import Phase
from storage_scanner.scan_progress_model import ScanProgressModel, load_estimate
from storage_scanner.settings import COLORS

_FOLDER_ROWS_VISIBLE = 5
_FOLDER_BAR_CELLS = 16
_BAR_MAXIMUM = 1000
_ANIMATION_MS = 15


class ScanProgressMixin:
    # Steps the UI runs itself, shown via _scan_progress_phase.
    PHASE_SAVING_HISTORY = "Saving history"
    PHASE_WAITING_FOR_ELEVATED_SCAN = "Waiting for the elevated scan"

    def _build_scan_progress_panel(self):
        self._scan_progress_model = None
        self._scan_progress_view = None
        self._scan_progress_animating = False
        self._scan_progress_row_values = {}  # row key -> values on screen
        self._scan_progress_order = []

        panel = ttk.Frame(self.root, padding=(8, 6, 8, 2))
        self._scan_progress_frame = panel

        header = ttk.Frame(panel)
        header.pack(side=TOP, fill=X)
        self._scan_progress_headline = StringVar()
        self._scan_progress_percent = StringVar()
        ttk.Label(header, textvariable=self._scan_progress_headline, style="Accent.TLabel").pack(
            side=LEFT
        )
        ttk.Label(header, textvariable=self._scan_progress_percent, style="Accent.TLabel").pack(
            side=RIGHT
        )

        self._scan_progress_bar = ttk.Progressbar(panel, mode="determinate", maximum=_BAR_MAXIMUM)
        self._scan_progress_bar.pack(side=TOP, fill=X, pady=(4, 2))

        self._scan_progress_counters = StringVar()
        self._scan_progress_estimate = StringVar()
        self._scan_progress_current = StringVar()
        for var in (
            self._scan_progress_counters,
            self._scan_progress_estimate,
            self._scan_progress_current,
        ):
            ttk.Label(panel, textvariable=var, foreground=COLORS["muted"], anchor=W).pack(
                side=TOP, fill=X
            )

        folders = ttk.Frame(panel)
        self._scan_progress_folders = folders
        self._scan_progress_summary = StringVar()
        ttk.Label(folders, textvariable=self._scan_progress_summary, foreground=COLORS["fg"]).pack(
            side=TOP, anchor=W, pady=(4, 2)
        )
        table = ttk.Frame(folders)
        table.pack(side=TOP, fill=X)
        columns = ("name", "state", "size", "files", "progress")
        tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            height=_FOLDER_ROWS_VISIBLE,
            selectmode="none",
        )
        for column, text, width, anchor, stretch in (
            ("name", "Top-level folder", 300, W, True),
            ("state", "Status", 80, W, False),
            ("size", "Size so far", 100, E, False),
            ("files", "Files", 90, E, False),
            ("progress", "Progress", 170, W, False),
        ):
            tree.heading(column, text=text, anchor=anchor)
            tree.column(column, width=width, anchor=anchor, stretch=stretch)
        tree.tag_configure("scanning", foreground=COLORS["accent"])
        tree.tag_configure("queued", foreground=COLORS["muted"])
        vsb = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=LEFT, fill=X, expand=True)
        vsb.pack(side=RIGHT, fill="y")
        self._scan_progress_tree = tree

    def _scan_progress_start(self, target):
        """Show the panel for a new scan of `target`, and look up how much
        work to expect on a background thread (it reads scan history)."""
        model = ScanProgressModel(target)
        self._scan_progress_model = model
        self._scan_progress_view = None
        self._scan_progress_tree.delete(*self._scan_progress_tree.get_children())
        self._scan_progress_row_values = {}
        self._scan_progress_order = []
        self._scan_progress_frame.pack(side=BOTTOM, fill=X, after=self._statusbar_frame)
        threading.Thread(target=self._scan_estimate_worker, args=(model,), daemon=True).start()
        self._scan_progress_refresh()

    def _scan_estimate_worker(self, model):
        # Tagged with its model: an estimate that arrives after its scan
        # ended must not be applied to the next one.
        self.progress_q.put(("estimate", (model, load_estimate(model.target))))

    def _scan_progress_message(self, kind, payload):
        model = self._scan_progress_model
        if model is None:
            return
        if kind == "estimate":
            owner, payload = payload
            if owner is not model:
                return
        model.handle(kind, payload)

    def _scan_progress_phase(self, label):
        """Show a step the UI itself runs (saving history, waiting on an
        elevated helper). Returns the current scan's model, for
        _scan_progress_end's `token`."""
        model = self._scan_progress_model
        if model is not None:
            model.handle("phase", Phase(label))
            self._scan_progress_refresh()
        return model

    def _scan_progress_cancel_requested(self):
        model = self._scan_progress_model
        if model is not None:
            model.request_cancel()
            self._scan_progress_refresh()

    def _scan_progress_end(self, outcome, token=None):
        """Finish the current scan's progress and hide the panel. With a
        `token` (see _scan_progress_phase), only if that scan is still the
        current one -- a history save finishing late mustn't hide the
        panel of a scan started since."""
        model = self._scan_progress_model
        if model is None or (token is not None and token is not model):
            return
        model.finish(outcome)
        self._scan_progress_model = None
        self._scan_progress_bar.stop()
        self._scan_progress_animating = False
        self._scan_progress_frame.pack_forget()

    def _scan_progress_refresh(self):
        model = self._scan_progress_model
        if model is None:
            return
        view = model.view()
        if view == self._scan_progress_view:
            return
        self._scan_progress_view = view
        self._scan_progress_headline.set(view.headline)
        self._scan_progress_percent.set(view.percent)
        self._scan_progress_counters.set(view.counters)
        self._scan_progress_estimate.set(view.estimate)
        self._scan_progress_current.set(view.current)
        self._scan_progress_summary.set(view.folders_summary)
        self._show_scan_progress_fraction(view.fraction)
        self._show_scan_progress_folders(view.folders)

    def _show_scan_progress_fraction(self, fraction):
        progress_bar = self._scan_progress_bar
        if fraction is None:
            if not self._scan_progress_animating:
                progress_bar.config(mode="indeterminate", value=0)
                progress_bar.start(_ANIMATION_MS)
                self._scan_progress_animating = True
            return
        if self._scan_progress_animating:
            progress_bar.stop()
            self._scan_progress_animating = False
        progress_bar.config(mode="determinate", value=fraction * _BAR_MAXIMUM)

    def _show_scan_progress_folders(self, rows):
        folders = self._scan_progress_folders
        if not rows:
            folders.pack_forget()
            return
        if not folders.winfo_manager():
            folders.pack(side=TOP, fill=X)

        tree = self._scan_progress_tree
        on_screen = self._scan_progress_row_values
        for row in rows:
            values = (row.name, row.state, row.size, row.files, bar(row.fill, _FOLDER_BAR_CELLS))
            if row.key not in on_screen:
                tree.insert("", "end", iid=row.key, values=values, tags=(row.state,))
            elif on_screen[row.key] != values:
                tree.item(row.key, values=values, tags=(row.state,))
            on_screen[row.key] = values

        order = [row.key for row in rows]
        if order != self._scan_progress_order:
            for index, key in enumerate(order):
                tree.move(key, "", index)
            self._scan_progress_order = order
