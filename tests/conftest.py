"""Test-session setup that has to happen before any app module is imported.

storage_scanner.history_db works out its app-data folder (the history
database, and the default log folder) from LOCALAPPDATA, XDG_DATA_HOME or
HOME when it's imported, and storage_scanner.logging_setup attaches its log
file handler at import time, so a fixture would be too late: by then a test
run has already written into the real app data. Pytest imports this file
before collecting any test module, so every one of those is pointed at a
throwaway folder here, and the session refuses to start if the database or
the log still resolves inside the real app-data folder.

It also holds the fixtures every Tk test shares: the one Tk interpreter
(tk_root), the real main window built on it (scanner_app, with message
boxes recorded by `dialogs` and a folder for the Recycle Bin), and ways to
run Tk's main loop until something is done (run_tk, scan_in_app). App
modules are imported inside them, never at the top of this file.
"""

import functools
import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every place history_db can put its app-data folder, from the environment
# as it was before the redirect below: Windows, Linux (XDG) and macOS.
_REAL_HOME = Path.home()
_REAL_APP_DATA_BASES = [
    Path(os.environ.get("LOCALAPPDATA") or _REAL_HOME),
    Path(os.environ.get("XDG_DATA_HOME") or _REAL_HOME / ".local" / "share"),
    _REAL_HOME / "Library" / "Application Support",
]

_sandbox = Path(tempfile.mkdtemp(prefix="storage-scanner-test-"))
for _name in ("LOCALAPPDATA", "XDG_DATA_HOME", "HOME"):
    os.environ[_name] = str(_sandbox / "appdata")
os.environ["STORAGE_SCANNER_LOG_DIR"] = str(_sandbox / "logs")


def pytest_configure(config):
    from storage_scanner import (
        history_db,
        logging_setup,  # attaches the log handler
    )

    config.addinivalue_line(
        "markers", "windows: Windows-only (Win32 APIs, drive letters, NTFS, Task Scheduler)"
    )

    real_dirs = [(base / history_db.APP_NAME).resolve() for base in _REAL_APP_DATA_BASES]
    log_files = (
        [Path(logging_setup.log_dir) / logging_setup.LOG_FILE_NAME]
        if logging_setup.log_dir is not None
        else []
    )
    for path in [Path(history_db.DB_NAME), *log_files]:
        resolved = path.resolve()
        for real_dir in real_dirs:
            if resolved.is_relative_to(real_dir):
                raise pytest.UsageError(
                    f"{resolved} is inside the real app-data folder {real_dir}; "
                    "refusing to run tests against real user data"
                )


def pytest_collection_modifyitems(config, items):
    """Skip tests marked `windows` on any other OS: they call Win32 APIs or
    rely on Windows path rules (drive letters, case-insensitive names)."""
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="Windows only")
    for item in items:
        if "windows" in item.keywords:
            item.add_marker(skip)


_TK_ROOT: list = []  # the one Tk interpreter, made by the first test that needs it


@pytest.fixture
def tk_root(capsys):
    """The Tk interpreter every Tk test shares; each test builds its own
    Toplevel on it and destroys it afterwards. Skips without a display.

    Made while pytest's output capture is off and never deleted before the
    process exits. Made under capture (and deleted after each test, or
    not), the next interpreter in the process often failed to start:
    "couldn't read file ... init.tcl: No error", in test_make_sbom's Tcl()
    or in the next Tk test. Tcl keeps the standard handles its first
    interpreter found, and under capture those are pytest's temporary
    files, closed once the test ends; a reused handle number is the likely
    culprit.

    An exception in a Tk callback (a button's command, a binding, an
    after() job) never reaches the test that set it off -- Tk reports it
    and carries on -- so it fails the test here instead."""
    from tkinter import TclError, Tk

    if not _TK_ROOT:
        with capsys.disabled():
            try:
                root = Tk()
            except TclError as exc:  # no display
                pytest.skip(f"Tk unavailable: {exc}")
        root.withdraw()
        _TK_ROOT.append(root)
    root = _TK_ROOT[0]
    errors = []
    root.report_callback_exception = lambda *exc_info: errors.append(exc_info)
    yield root
    del root.report_callback_exception
    if errors:
        import traceback

        pytest.fail(
            "A Tk callback raised:\n"
            + "\n".join("".join(traceback.format_exception(*exc_info)) for exc_info in errors)
        )


class Dialogs:
    """The message boxes the app shows, recorded instead of shown: each is
    (kind, title, message). A yes/no question takes the next of `answers`;
    one nobody expected fails the test rather than waiting for a click."""

    def __init__(self):
        self.shown = []
        self.answers = []

    def messages(self, kind):
        return [message for shown_kind, _title, message in self.shown if shown_kind == kind]

    def _show(self, kind, title=None, message=None, **_options):
        self.shown.append((kind, title, message))
        return "ok"

    def _ask(self, title=None, message=None, **_options):
        self.shown.append(("askyesno", title, message))
        assert self.answers, f"unexpected question: {title}: {message}"
        return self.answers.pop(0)


@pytest.fixture
def dialogs(monkeypatch):
    from tkinter import messagebox

    recorded = Dialogs()
    for kind in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, kind, functools.partial(recorded._show, kind))
    monkeypatch.setattr(messagebox, "askyesno", recorded._ask)
    return recorded


@pytest.fixture
def recycle_bin(tmp_path_factory, monkeypatch):
    """A folder standing in for the Recycle Bin: every delete the app makes
    from here on (through the real DeleteService) moves the item into it.
    Nothing is ever deleted permanently."""
    from storage_scanner.delete_outcome import FAILED, RECYCLED
    from storage_scanner.delete_service import DeleteService
    from storage_scanner.ui import delete_dialogs

    bin_dir = tmp_path_factory.mktemp("recycle-bin")

    def to_bin(path):
        shutil.move(path, str(bin_dir / uuid.uuid4().hex))
        return RECYCLED

    monkeypatch.setattr(
        delete_dialogs,
        "DeleteService",
        functools.partial(
            DeleteService,
            trash=to_bin,
            blockers=lambda _path, _is_dir: [],
            delete_permanently=lambda _path: FAILED,
        ),
    )
    return bin_dir


def run_tk_until(root, done, timeout=20.0):
    """Run Tk's main loop until `done()` is true (or `timeout` seconds
    pass), so after() jobs run and worker threads can hand results back
    to Tk (which they can only do while the main loop runs).

    On Windows a thread posts the main window a WM_NULL every 50 ms
    meanwhile. Tk can stall in GetMessageW with nothing queued, its after()
    jobs waiting until some window message arrives (see "Found while
    testing" in the roadmap's 2026-10-10 code review): seen in test runs
    that start a scan right after building the window, as these do, where
    one in a few runs of the new UI tests hung for good without it."""
    deadline = time.monotonic() + timeout

    def check():
        if done() or time.monotonic() > deadline:
            root.quit()
        else:
            root.after(10, check)

    stop = threading.Event()
    waker = None
    if sys.platform == "win32":
        import ctypes

        hwnd = int(root.frame(), 16)
        post = ctypes.windll.user32.PostMessageW

        def wake():
            while not stop.wait(0.05):
                post(hwnd, 0, 0, 0)  # WM_NULL: does nothing but wake the loop

        waker = threading.Thread(target=wake, daemon=True)
        waker.start()
    root.after(10, check)
    try:
        root.mainloop()
    finally:
        stop.set()
        if waker is not None:
            waker.join()
    return done()


@pytest.fixture
def scanner_app(tk_root, dialogs, recycle_bin, tmp_path_factory, monkeypatch):
    """The real main window (StorageScannerApp) in a Toplevel of its own,
    with its scan history in a throwaway database, the light palette, a
    folder for the Recycle Bin (`recycle_bin`) and message boxes recorded
    (`dialogs`). No update check, and no launch-time budget check or
    first-run guide."""
    from tkinter import Toplevel

    from storage_scanner import appearance, history_db
    from storage_scanner.app import StorageScannerApp

    class HeadlessApp(StorageScannerApp):
        def _check_for_update_worker(self):
            pass

        def _check_budgets_on_launch(self):
            pass

        def _show_onboarding_on_launch(self):
            pass

    data_dir = tmp_path_factory.mktemp("appdata")
    monkeypatch.setattr(history_db, "DB_NAME", str(data_dir / "storage_history.db"))
    history_db.init_history_db()
    history_db.set_app_metadata(appearance.SETTING_KEY, appearance.LIGHT)

    window = Toplevel(tk_root)
    window.withdraw()
    app = HeadlessApp(window)
    try:
        yield app
    finally:
        # Its result comes back through after(), which needs the main loop.
        thread = app._history_thread
        if thread is not None:
            run_tk_until(tk_root, lambda: not thread.is_alive())
            tk_root.update()
        # Polls and timers left behind; their Python commands go with the
        # window, which registered them.
        for job in tk_root.tk.splitlist(tk_root.tk.call("after", "info")):
            tk_root.tk.call("after", "cancel", job)
        window.destroy()
        # Dropped here, on the main thread: left to the garbage collector,
        # a Tk variable can be freed on a worker thread, which Tk refuses
        # ("main thread is not in main loop") after a 1 s wait.
        app.__dict__.clear()
        gc.collect()


@pytest.fixture
def run_tk(tk_root):
    """run_tk_until on the shared interpreter: run_tk(done, timeout=20)."""
    return functools.partial(run_tk_until, tk_root)


@pytest.fixture
def scan_in_app(scanner_app, run_tk):
    """scan_in_app(path): scan `path` in scanner_app the way clicking Scan
    does, real threads and all, and wait until the scan and its history
    save are over. Returns the scanned tree."""

    def scan(path):
        scanner_app.path_var.set(str(path))
        scanner_app.start_scan()
        assert run_tk(lambda: not scanner_app._deleting_blocked()), "the scan never finished"
        scanner_app.root.update()
        return scanner_app.root_node

    return scan
