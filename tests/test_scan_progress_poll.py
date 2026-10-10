"""Background work always ends cleanly: whatever goes wrong in a scan
thread or in showing its progress, the window gets its "done" or "error"
and Scan comes back (ui/scan_lifecycle.py); a finished duplicate search
gives back every control it took (ui/duplicate_window.py)."""

import queue

import pytest

from storage_scanner.ui import duplicate_window
from storage_scanner.ui.duplicate_window import DuplicatesMixin
from storage_scanner.ui.scan_lifecycle import ScanLifecycleMixin


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, _ms, callback):
        self.scheduled.append(callback)


class _App(ScanLifecycleMixin):
    def __init__(self, tick_error=None):
        self.root = _Root()
        self.progress_q = queue.Queue()
        self._scan_active = True
        self._tick_error = tick_error
        self.finished = []
        self.frozen = False

    def _live_attach(self, _tracker):
        pass

    def _scan_progress_message(self, _kind, _payload):
        pass

    def _scan_progress_refresh(self):
        return None

    def _live_tick(self, _view):
        if self._tick_error and not self.frozen:
            raise self._tick_error

    def _live_freeze(self):
        self.frozen = True

    def _finish_scan(self, node, report=None):
        self._scan_active = False
        self.finished.append(("done", node))

    def _finish_error(self, msg):
        self._scan_active = False
        self.finished.append(("error", msg))

    def run_polls(self, limit=5):
        """Run the polls scheduled so far, and theirs, the way Tk's after()
        would."""
        for _ in range(limit):
            if not self.root.scheduled:
                return
            self.root.scheduled.pop(0)()


def test_a_failing_progress_display_still_reads_the_scans_result():
    app = _App(tick_error=RuntimeError("live tree bug"))

    app._poll_progress()  # the display fails on this tick
    assert app.root.scheduled, "the poll must go on"
    assert app.frozen

    app.progress_q.put(("done", ("tree", None)))
    app.run_polls()

    assert app.finished == [("done", "tree")]
    assert not app._scan_active
    assert not app.root.scheduled  # and polling stops once it's over


def test_a_failure_while_finishing_is_not_swallowed():
    app = _App()

    def broken_finish(_node, _report=None):
        app._scan_active = False
        raise RuntimeError("finish bug")

    app._finish_scan = broken_finish
    app.progress_q.put(("done", ("tree", None)))

    with pytest.raises(RuntimeError):
        app._poll_progress()  # Tk's error dialog shows it; Scan is already back
    assert not app.root.scheduled


@pytest.mark.parametrize(
    "run_scan",
    [
        lambda _target: (_ for _ in ()).throw(OSError("osascript missing")),
        lambda _target: (True, "[1, 2]"),  # valid JSON, not a tree
        lambda _target: (True, "not json"),
        lambda _target: (False, "cancelled at the password prompt"),
    ],
    ids=["helper-raises", "json-not-an-object", "not-json", "helper-refused"],
)
def test_the_elevated_scan_thread_always_reports_an_error(run_scan):
    app = _App()

    app._elevated_scan_worker_headless("/data", run_scan)

    kind, message = app.progress_q.get_nowait()
    assert kind == "error" and message


class _Widget:
    def __init__(self, state="normal"):
        self.state = state

    def config(self, state=None, **_options):
        if state is not None:
            self.state = state


class _DuplicatesApp(DuplicatesMixin):
    def __init__(self):
        self.root = _Root()
        self.dup_progress_q = queue.Queue()
        self.cancel_btn = _Widget("normal")  # what show_duplicates leaves
        self.tools_btn = _Widget("disabled")
        self.top_count_combo = _Widget("disabled")
        self.status_var = _Widget()
        self.status_var.set = lambda _text: None
        self.root_node = self._duplicates_scan_root = object()

    def _scan_running(self):
        return False

    def _stop_progress(self):
        pass

    def _show_duplicates_window(self, _tree, _groups):
        pass


@pytest.mark.parametrize("message", [("done", []), ("cancelled", None), ("error", "disk gone")])
def test_a_finished_duplicate_search_turns_cancel_off(message, monkeypatch):
    monkeypatch.setattr(duplicate_window.messagebox, "showerror", lambda *a, **k: None)
    app = _DuplicatesApp()
    app.dup_progress_q.put(message)

    app._poll_duplicate_progress()

    assert app.cancel_btn.state == "disabled"
    assert (app.tools_btn.state, app.top_count_combo.state) == ("normal", "readonly")
