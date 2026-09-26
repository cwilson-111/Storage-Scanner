"""The scan progress line under the tree: one overall bar with its
percentage and live counters, then the estimate and the folder being read.
The folders themselves fill in inside the main tree (ui/live_tree.py).

A mixin composed into StorageScannerApp (storage_scanner/app.py). What it
shows comes from ScanProgressModel.view() (storage_scanner/
scan_progress_model.py); this module only owns the widgets. It only ever
runs on the UI thread: main_window._poll_progress hands it the scan's
progress messages and refreshes it on the same 100 ms tick -- so it
redraws at most ten times a second however fast messages arrive, no scan
thread touches Tk, and a refresh redraws only what changed.
"""

import threading
from tkinter import BOTTOM, LEFT, RIGHT, TOP, StringVar, W, X, ttk

from storage_scanner.scan_progress import Phase
from storage_scanner.scan_progress_model import ScanProgressModel, load_estimate
from storage_scanner.settings import COLORS

_BAR_MAXIMUM = 1000
_ANIMATION_MS = 15


def _detail(view):
    """The second line: what to expect, then the folder being read."""
    return " · ".join(text for text in (view.estimate, view.current) if text)


class ScanProgressMixin:
    # Steps the UI runs itself, shown via _scan_progress_phase.
    PHASE_SAVING_HISTORY = "Saving history"
    PHASE_WAITING_FOR_ELEVATED_SCAN = "Waiting for the elevated scan"

    def _build_scan_progress_panel(self):
        self._scan_progress_model = None
        self._scan_progress_view = None
        self._scan_progress_animating = False

        panel = ttk.Frame(self.root, padding=(8, 4, 8, 2))
        self._scan_progress_frame = panel

        line = ttk.Frame(panel)
        line.pack(side=TOP, fill=X)
        self._scan_progress_headline = StringVar()
        self._scan_progress_percent = StringVar()
        self._scan_progress_counters = StringVar()
        ttk.Label(line, textvariable=self._scan_progress_headline, style="Accent.TLabel").pack(
            side=LEFT
        )
        ttk.Label(line, textvariable=self._scan_progress_counters, foreground=COLORS["muted"]).pack(
            side=RIGHT
        )
        ttk.Label(line, textvariable=self._scan_progress_percent, style="Accent.TLabel").pack(
            side=RIGHT, padx=(6, 10)
        )
        self._scan_progress_bar = ttk.Progressbar(line, mode="determinate", maximum=_BAR_MAXIMUM)
        self._scan_progress_bar.pack(side=LEFT, fill=X, expand=True, padx=(10, 0))

        # The estimate and the folder being read share the second line.
        self._scan_progress_detail = StringVar()
        ttk.Label(
            panel, textvariable=self._scan_progress_detail, foreground=COLORS["muted"], anchor=W
        ).pack(side=TOP, fill=X, pady=(2, 0))

    def _scan_progress_start(self, target):
        """Show the progress line for a new scan of `target`, and look up
        how much work to expect on a background thread (it reads scan
        history)."""
        model = ScanProgressModel(target)
        self._scan_progress_model = model
        self._scan_progress_view = None
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
        """Finish the current scan's progress and hide the line. With a
        `token` (see _scan_progress_phase), only if that scan is still the
        current one -- a history save finishing late mustn't hide the
        progress of a scan started since."""
        model = self._scan_progress_model
        if model is None or (token is not None and token is not model):
            return
        model.finish(outcome)
        self._scan_progress_model = None
        self._scan_progress_bar.stop()
        self._scan_progress_animating = False
        self._scan_progress_frame.pack_forget()

    def _scan_progress_refresh(self):
        """Redraw what changed; returns the current ProgressView (None
        with no scan running)."""
        model = self._scan_progress_model
        if model is None:
            return None
        view = model.view()
        shown = self._scan_progress_view
        if view == shown:
            return view
        self._scan_progress_view = view
        for var, text, previous in (
            (self._scan_progress_headline, view.headline, shown and shown.headline),
            (self._scan_progress_percent, view.percent, shown and shown.percent),
            (self._scan_progress_counters, view.counters, shown and shown.counters),
            (self._scan_progress_detail, _detail(view), shown and _detail(shown)),
        ):
            if text != previous:
                var.set(text)
        if shown is None or view.fraction != shown.fraction:
            self._show_scan_progress_fraction(view.fraction)
        return view

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
