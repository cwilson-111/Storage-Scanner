"""The main window through a scan, on the real app (StorageScannerApp) and a
real Tk Treeview: the rows a Compatible scan fills in while it runs
(ui/live_tree.py), what Cancel and a failure leave on screen, and the
finished tree that replaces the live rows (ui/scan_lifecycle.py).

Most tests play the scan engine themselves through the app's progress
queue, one directory at a time (see _Walk), and run the app's 100 ms poll
by hand, so every state in between is seen; the last one lets real scan
threads run to the end."""

import contextlib
import os
import sys
from pathlib import Path
from tkinter import TclError

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import scanner
from storage_scanner.formatting import human_size
from storage_scanner.live_tree_model import QUEUED_ICON, SCANNING_ICON
from storage_scanner.models import Node
from storage_scanner.scan_progress import WalkTracker
from storage_scanner.ui import live_tree

# relative path -> size; folders appear through their files ("deep" is
# inside "big", so the walk reaches it last).
FILES = {
    "a.bin": 100,
    "big/x.bin": 1000,
    "big/y.bin": 500,
    "big/deep/w.bin": 2000,
    "small/z.bin": 10,
}
BIG = 1000 + 500 + 2000


class _Walk:
    """A Compatible scan of `path` held part-way: the test says which
    directory the (one) worker reads next, and it's listed from disk into
    the same kind of tree, through the same WalkTracker, that
    scanner.scan's workers build and hand the app."""

    def __init__(self, path):
        self.root = Node(str(path), path.name)
        self.tracker = WalkTracker(self.root, 1)
        self._chain = {id(self.root): ()}
        self._queued = [self.root]

    def node(self, relative=""):
        node = self.root
        for name in filter(None, relative.split("/")):
            node = next(child for child in node.dirs if child.name == name)
        return node

    def read(self, relative=""):
        node = self.node(relative)
        self._queued.remove(node)
        self.tracker.begin(0, node)
        files = nbytes = 0
        found = []
        for entry in sorted(os.scandir(node.path), key=lambda entry: entry.name):
            st = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                child = Node(entry.path, entry.name)
                child.mtime, child.atime = st.st_mtime, st.st_atime
                node.dirs.append(child)
                found.append(child)
                self._chain[id(child)] = (*self._chain[id(node)], node)
            else:
                node.add_file(entry.name, st.st_size, st.st_size, st.st_mtime, st.st_atime)
                files += 1
                nbytes += st.st_size
        self._queued.extend(found)
        chain = self._chain[id(node)]
        self.tracker.record(0, node, chain, files, nbytes, nbytes, files + len(found), found, True)

    def finish(self):
        """Read everything left and add it up, as scan() does before it
        returns the tree."""
        while self._queued:
            self.read(os.path.relpath(self._queued[0].path, self.root.path).replace(os.sep, "/"))
        scanner._rollup(self.root, own_sizes=False)
        return self.root


@pytest.fixture
def target(tmp_path):
    folder = tmp_path / "scan"
    for relative, size in FILES.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return folder


@pytest.fixture
def scanning(scanner_app, target, monkeypatch):
    """Scan clicked on `target`, with the test as the scan engine: the
    worker only hands the app its _Walk's tracker."""
    app = scanner_app
    # On screen, though fully transparent: a live row's numbers are only
    # refreshed while the row can be seen.
    window = app.root
    with contextlib.suppress(TclError):  # no window alpha on this platform
        window.attributes("-alpha", 0.0)
    window.geometry("900x600+0+0")
    window.deiconify()
    window.update()
    walk = _Walk(target)
    # Every tick re-sorts, rather than at most twice a second.
    monkeypatch.setattr(live_tree, "_RESORT_SECONDS", 0)
    monkeypatch.setattr(
        app, "_scan_worker", lambda *_args: app.progress_q.put(("live_tree", walk.tracker))
    )
    app.path_var.set(str(target))
    app.start_scan()
    app.scan_thread.join()
    return walk


def _tick(app):
    """One tick of the app's progress poll; True once the scan is over."""
    return app._drain_progress()


def _root_iid(app):
    [iid] = app.tree.get_children("")
    return iid


def _row(app, parent_iid, name):
    [iid] = [i for i in app.tree.get_children(parent_iid) if app.node_by_iid[i].name == name]
    return iid


def _rows(app, parent_iid):
    """Each row of a level as shown: (icon and name, Size)."""
    return [
        (app.tree.item(iid, "text"), app.tree.set(iid, "size"))
        for iid in app.tree.get_children(parent_iid)
    ]


def _screen(app):
    """Every row's text and values, for "nothing changed"."""
    shown = {}
    stack = list(app.tree.get_children(""))
    while stack:
        iid = stack.pop()
        shown[iid] = (app.tree.item(iid, "text"), app.tree.item(iid, "values"))
        stack.extend(app.tree.get_children(iid))
    return shown


def _open(app, iid):
    """Open a folder row the way a click on its arrow does."""
    app.tree.focus(iid)
    app.tree.item(iid, open=True)
    app.tree.event_generate("<<TreeviewOpen>>")


def _disabled(widget):
    return widget.instate(["disabled"])


def _assert_scan_given_back(app):
    assert not app._scan_running()
    assert not _disabled(app.scan_btn)
    assert _disabled(app.cancel_btn)
    assert not _disabled(app.tools_btn)
    assert not app._scan_progress_frame.winfo_manager()  # the progress line is gone


def test_rows_fill_in_and_reorder_as_each_folder_is_read(scanner_app, scanning, target):
    app = scanner_app
    # Straight away, before the engine has read anything: the target's row,
    # Scan taken away and Cancel offered.
    [root_iid] = app.tree.get_children("")
    assert app.tree.item(root_iid, "text") == f"{SCANNING_ICON} scan\\"
    assert _disabled(app.scan_btn) and not _disabled(app.cancel_btn) and _disabled(app.tools_btn)

    scanning.read()
    _tick(app)
    root_iid = _root_iid(app)
    assert app.tree.item(root_iid, "text") == f"{SCANNING_ICON} scan\\"
    assert app.tree.set(root_iid, "size") == human_size(100)
    # Its folders are queued, with no numbers yet; the file is final.
    assert _rows(app, root_iid) == [
        ("📄 a.bin", human_size(100)),
        (f"{QUEUED_ICON} big\\", "…"),
        (f"{QUEUED_ICON} small\\", "…"),
    ]

    # Scan again (F5) while this one runs: nothing starts, nothing clears.
    thread, before = app.scan_thread, _screen(app)
    app.start_scan()
    assert app.scan_thread is thread and _screen(app) == before

    scanning.read("big")
    _tick(app)
    # "big" is still reading "deep", and has outgrown everything else.
    assert app.tree.item(root_iid, "text") == f"{SCANNING_ICON} scan\\"
    assert _rows(app, root_iid) == [
        (f"{SCANNING_ICON} big\\", human_size(1500)),
        ("📄 a.bin", human_size(100)),
        (f"{QUEUED_ICON} small\\", "…"),
    ]

    scanning.read("big/deep")
    scanning.read("small")
    _tick(app)
    assert app.tree.item(root_iid, "text") == "📁 scan\\"
    assert app.tree.set(root_iid, "size") == human_size(100 + BIG + 10)
    assert _rows(app, root_iid) == [
        ("📁 big\\", human_size(BIG)),
        ("📄 a.bin", human_size(100)),
        ("📁 small\\", human_size(10)),
    ]
    assert app._scan_running()  # until the engine's result arrives


def test_a_folder_opened_mid_scan_lists_what_was_found_and_keeps_filling_in(scanner_app, scanning):
    app = scanner_app
    scanning.read()
    _tick(app)
    scanning.read("big")
    _tick(app)
    big_iid = _row(app, _root_iid(app), "big")

    _open(app, big_iid)
    assert _rows(app, big_iid) == [
        ("📄 x.bin", human_size(1000)),
        ("📄 y.bin", human_size(500)),
        (f"{QUEUED_ICON} deep\\", "…"),
    ]

    scanning.read("big/deep")
    _tick(app)
    assert _rows(app, big_iid) == [
        ("📁 deep\\", human_size(2000)),
        ("📄 x.bin", human_size(1000)),
        ("📄 y.bin", human_size(500)),
    ]
    assert app.tree.item(big_iid, "text") == "📁 big\\"


def test_cancel_freezes_the_rows_and_gives_scan_back_once_the_engine_stops(scanner_app, scanning):
    app = scanner_app
    scanning.read()
    _tick(app)
    scanning.read("big")
    _tick(app)
    before = _screen(app)

    app.cancel_scan()
    assert app.status_var.get() == "Cancelling …"
    # The worker finishes the folder it was reading; the rows stay put.
    scanning.read("small")
    _tick(app)
    assert _screen(app) == before

    app.progress_q.put(("done", (scanning.root, None)))
    assert _tick(app)
    _assert_scan_given_back(app)
    assert app.status_var.get() == "Scan cancelled."
    assert _screen(app) == before
    # What was found before the cancel can still be opened, all of it at once.
    big_iid = _row(app, _root_iid(app), "big")
    _open(app, big_iid)
    assert _rows(app, big_iid) == [
        ("📄 x.bin", human_size(1000)),
        ("📄 y.bin", human_size(500)),
        (f"{QUEUED_ICON} deep\\", "…"),
    ]


def test_a_failed_scan_says_why_keeps_the_rows_and_gives_scan_back(scanner_app, scanning, dialogs):
    app = scanner_app
    scanning.read()
    _tick(app)
    before = _screen(app)

    app.progress_q.put(("error", "The device is not ready."))
    assert _tick(app)

    _assert_scan_given_back(app)
    assert app.status_var.get() == "Scan failed."
    assert dialogs.messages("showerror") == ["Scan failed:\nThe device is not ready."]
    assert _screen(app) == before
    assert app._live_frozen


def test_deleting_waits_until_the_scan_is_over(scanner_app, scanning, target, dialogs, recycle_bin):
    app = scanner_app
    scanning.read()
    _tick(app)
    file_iid = _row(app, _root_iid(app), "a.bin")
    app.tree.selection_set(file_iid)
    app.tree.focus(file_iid)

    app._delete_selected()

    assert dialogs.messages("showinfo") == [
        "Deleting has to wait until the scan finishes or is cancelled."
    ]
    assert (target / "a.bin").exists()
    assert not list(recycle_bin.iterdir())
    assert app.tree.exists(file_iid)


def test_the_finished_tree_reopens_the_folders_opened_during_the_scan_and_keeps_the_focus(
    scanner_app, scanning, run_tk
):
    app = scanner_app
    scanning.read()
    _tick(app)
    scanning.read("big")
    _tick(app)
    big_iid = _row(app, _root_iid(app), "big")
    _open(app, big_iid)
    y_iid = _row(app, big_iid, "y.bin")
    app.tree.selection_set(y_iid)
    app.tree.focus(y_iid)

    node = scanning.finish()
    app.progress_q.put(("done", (node, None)))
    assert _tick(app)

    assert app.root_node is node
    root_iid = _root_iid(app)
    assert app.tree.item(root_iid, "text") == "📁 scan\\"
    assert app.tree.set(root_iid, "size") == human_size(100 + BIG + 10)
    # The finished rows, with the live rows' folder still open in them.
    assert _rows(app, root_iid) == [
        ("📁 big\\", human_size(BIG)),
        ("📄 a.bin", human_size(100)),
        ("📁 small\\", human_size(10)),
    ]
    big_iid = _row(app, root_iid, "big")
    assert app.tree.item(big_iid, "open")
    assert _rows(app, big_iid) == [
        ("📁 deep\\", human_size(2000)),
        ("📄 x.bin", human_size(1000)),
        ("📄 y.bin", human_size(500)),
    ]
    focused = app.tree.focus()
    assert app.node_by_iid[focused].path == str(Path(scanning.root.path) / "big" / "y.bin")
    assert app.tree.selection() == (focused,)
    assert not _disabled(app.scan_btn) and _disabled(app.cancel_btn)

    assert run_tk(lambda: not app._saving_history())
    app.root.update()
    _assert_scan_given_back(app)
    assert app.status_var.get().startswith("Scan complete. History saved.")


def test_a_real_scan_runs_to_the_end_then_a_rescan_shows_what_changed(scanner_app, target, run_tk):
    app = scanner_app

    def scan_and_wait():
        app.start_scan()
        assert _disabled(app.scan_btn) and not _disabled(app.cancel_btn)
        assert run_tk(lambda: not app._scan_running() and not app._saving_history())
        app.root.update()
        _assert_scan_given_back(app)

    app.path_var.set(str(target))
    scan_and_wait()
    assert app.status_var.get() == (
        "Scan complete. History saved. Scan the same path again to calculate growth."
    )
    root_iid = _root_iid(app)
    assert app.tree.item(root_iid, "text") == "📁 scan\\"
    assert _rows(app, root_iid) == [
        ("📁 big\\", human_size(BIG)),
        ("📄 a.bin", human_size(100)),
        ("📁 small\\", human_size(10)),
    ]
    assert app.changed_only_check.instate(["disabled"])  # nothing to compare with yet

    (target / "small" / "new.bin").write_bytes(b"x" * 4000)
    scan_and_wait()
    assert app.status_var.get().startswith("Scan complete. History saved. Growth rows:")
    root_iid = _root_iid(app)
    assert _rows(app, root_iid) == [
        ("📁 small\\", human_size(4010)),
        ("📁 big\\", human_size(BIG)),
        ("📄 a.bin", human_size(100)),
    ]
    # The Change column, from the scan saved before this one: the scan's
    # own row grew by the new file.
    assert app.tree.set(root_iid, "change").startswith(f"+{human_size(4000)} (")
    assert app.changed_only_check.instate(["!disabled"])
