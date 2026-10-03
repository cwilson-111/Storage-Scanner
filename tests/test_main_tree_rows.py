"""The finished scan's main tree on a real Tk Treeview: the order and the
even/odd stripes of its rows after a heading click and after a delete
(roadmap P1-7 moved both to whole-level Tk calls; benchmarks/main_tree.py
times them)."""

import os
import queue
import threading
import time
from tkinter import BooleanVar, StringVar, TclError, Tk, Toplevel

import pytest

from storage_scanner import scanner
from storage_scanner.app import StorageScannerApp
from storage_scanner.formatting import human_size
from storage_scanner.live_tree_model import date_text, node_display
from storage_scanner.settings import apply_theme
from storage_scanner.ui import main_tree

# name -> size in bytes. Names differ in case so the name sort is seen to
# ignore it; every size differs so the size sort has one right answer.
TOP_FILES = {"b.txt": 300, "A.txt": 100, "c.txt": 500, "d.txt": 200, "E.txt": 400}
SUB_FILES = {"y.bin": 10, "X.bin": 50}  # "sub": 60 bytes in 2 files
ZED_FILES = {"only.bin": 1000}  # "Zed": the biggest row, never opened


class _TreeOnly(StorageScannerApp):
    """The app with just its theme and main tree -- no history database,
    no update check, no other windows."""

    def __init__(self, root):
        self.root = root
        self.root_node = None
        self.scan_thread = None
        self._previous_folder_sizes = {}
        self._more_rows = {}  # a level's "N more" row -> its parent row
        self.node_by_iid = {}
        self._heat_tags = set()
        self._sort_key = "size"
        self._sort_reverse = True
        self.status_var = StringVar(master=root)
        self.show_treemap_var = BooleanVar(master=root, value=True)  # the toolbar's
        apply_theme(root)
        self._build_tree()


_TK_ROOT: list = []  # the one Tk interpreter, made by the first test that needs it


@pytest.fixture
def app(capsys):
    """The trimmed app in a window of its own.

    Every test shares one Tk interpreter, made while pytest's output
    capture is off and never deleted before the process exits. Made under
    capture (and deleted after each test, or not), the next interpreter in
    the process often failed to start: "couldn't read file ... init.tcl:
    No error", in test_make_sbom's Tcl() or in the next test here. Tcl
    keeps the standard handles its first interpreter found, and under
    capture those are pytest's temporary files, closed once the test ends;
    a reused handle number is the likely culprit."""
    if not _TK_ROOT:
        with capsys.disabled():
            try:
                root = Tk()
            except TclError as exc:  # no display
                pytest.skip(f"Tk unavailable: {exc}")
        root.withdraw()
        _TK_ROOT.append(root)
    window = Toplevel(_TK_ROOT[0])
    window.withdraw()
    app = _TreeOnly(window)
    try:
        yield app
    finally:
        window.destroy()
        # Dropped here, on the main thread: left to the garbage collector,
        # a Tk variable can be freed on a scanner worker thread, which Tk
        # refuses ("main thread is not in main loop") after a 1 s wait.
        app.status_var = None
        app.show_treemap_var = None
        app.changed_only_var = None
        app.treemap_pane = None  # its StringVars


def _write(folder, files):
    folder.mkdir(exist_ok=True)
    for name, size in files.items():
        (folder / name).write_bytes(b"x" * size)


@pytest.fixture
def scanned(app, tmp_path):
    """A real folder, scanned and shown the way _finish_scan shows it: the
    root row open with its rows listed, then "sub" opened."""
    _write(tmp_path, TOP_FILES)
    _write(tmp_path / "sub", SUB_FILES)
    _write(tmp_path / "Zed", ZED_FILES)
    root_node = scanner.scan(str(tmp_path), queue.Queue(), threading.Event())
    app.root_node = root_node
    root_iid = app._insert_node("", root_node, parent_size=root_node.size or 1)
    app.tree.item(root_iid, open=True)
    app._populate_children(root_iid, root_node)
    sub_iid = _row(app, root_iid, "sub")
    app._populate_children(sub_iid, app.node_by_iid[sub_iid])
    app.tree.item(sub_iid, open=True)
    return root_iid


def _row(app, parent_iid, name):
    [iid] = [i for i in app.tree.get_children(parent_iid) if app.node_by_iid[i].name == name]
    return iid


def _names(app, parent_iid):
    return [app.node_by_iid[iid].name for iid in app.tree.get_children(parent_iid)]


def _assert_striped(app, parent_iid):
    """Every row of the level carries exactly the stripe of its position,
    and still its type tag (folders are bold)."""
    for index, iid in enumerate(app.tree.get_children(parent_iid)):
        tags = app.tree.item(iid, "tags")
        stripes = [t for t in tags if t in ("even", "odd")]
        assert stripes == ["odd" if index % 2 else "even"], (index, tags)
        assert ("dir" in tags) == app.node_by_iid[iid].is_dir, tags


def test_heading_clicks_order_every_open_level_and_restripe_it(app, scanned):
    root_iid = scanned
    sub_iid = _row(app, root_iid, "sub")
    zed_iid = _row(app, root_iid, "Zed")
    by_size = ["Zed", "c.txt", "E.txt", "b.txt", "d.txt", "A.txt", "sub"]
    assert _names(app, root_iid) == by_size  # the default: biggest first

    app._sort_by("name")
    assert _names(app, root_iid) == ["A.txt", "b.txt", "c.txt", "d.txt", "E.txt", "sub", "Zed"]
    assert _names(app, sub_iid) == ["X.bin", "y.bin"]
    _assert_striped(app, root_iid)
    _assert_striped(app, sub_iid)
    # An opened folder stays open with its rows; one never opened keeps
    # just its placeholder until it is.
    assert app.tree.item(sub_iid, "open")
    [placeholder] = app.tree.get_children(zed_iid)
    assert placeholder not in app.node_by_iid

    app._sort_by("name")  # the same heading again: the other way round
    assert _names(app, root_iid) == ["Zed", "sub", "E.txt", "d.txt", "c.txt", "b.txt", "A.txt"]
    assert _names(app, sub_iid) == ["y.bin", "X.bin"]
    _assert_striped(app, root_iid)
    _assert_striped(app, sub_iid)

    app._sort_by("size")
    assert _names(app, root_iid) == by_size
    assert _names(app, sub_iid) == ["X.bin", "y.bin"]
    _assert_striped(app, root_iid)

    app._sort_by("name")
    app._sort_by("items")  # most files first; equal counts keep their order
    assert _names(app, root_iid) == ["sub", "A.txt", "b.txt", "c.txt", "d.txt", "E.txt", "Zed"]
    _assert_striped(app, root_iid)

    app.tree.focus(zed_iid)
    app._on_open(None)  # opening it lists its rows in the current order
    assert _names(app, zed_iid) == ["only.bin"]


def test_a_deleted_row_leaves_the_rest_in_order_restriped_with_fresh_shares(app, scanned):
    root_iid = scanned
    root_node = app.node_by_iid[root_iid]
    total = sum(TOP_FILES.values()) + sum(SUB_FILES.values()) + sum(ZED_FILES.values())
    assert root_node.size == total
    app._sort_by("name")

    def delete(parent_iid, name):
        iid = _row(app, parent_iid, name)
        os.remove(app.node_by_iid[iid].path)
        app._remove_main_tree_row(iid)
        return iid

    def assert_shares(parent_iid):
        parent_size = app.node_by_iid[parent_iid].size
        for iid in app.tree.get_children(parent_iid):
            expected = node_display(app.node_by_iid[iid], parent_size).values
            assert app.tree.set(iid, "percent") == expected[2], app.node_by_iid[iid].name

    # A row from the middle: the rows below it move up onto the other stripe.
    gone = delete(root_iid, "c.txt")
    assert not app.tree.exists(gone) and gone not in app.node_by_iid
    assert _names(app, root_iid) == ["A.txt", "b.txt", "d.txt", "E.txt", "sub", "Zed"]
    _assert_striped(app, root_iid)
    assert_shares(root_iid)
    assert root_node.size == total - 500
    assert app.tree.set(root_iid, "size") == human_size(total - 500)

    # The first row: every other row moves.
    delete(root_iid, "A.txt")
    assert _names(app, root_iid) == ["b.txt", "d.txt", "E.txt", "sub", "Zed"]
    _assert_striped(app, root_iid)
    assert_shares(root_iid)

    # A row one level down: its folder's and the root's rows shrink too.
    sub_iid = _row(app, root_iid, "sub")
    delete(sub_iid, "X.bin")
    assert _names(app, sub_iid) == ["y.bin"]
    _assert_striped(app, sub_iid)
    assert_shares(sub_iid)
    assert app.tree.set(sub_iid, "size") == human_size(10)
    assert app.tree.set(sub_iid, "items") == "1"
    assert root_node.file_count == 5
    assert app.tree.set(root_iid, "size") == human_size(total - 500 - 100 - 50)
    assert app.status_var.get().endswith("in 5 files")

    # A later click sorts what's left.
    app._sort_by("size")
    assert _names(app, root_iid) == ["Zed", "E.txt", "b.txt", "d.txt", "sub"]
    _assert_striped(app, root_iid)


def test_the_change_column_shows_growth_since_the_last_scan_and_sorts_by_it(
    app, scanned, monkeypatch
):
    root_iid = scanned
    sub, zed = (app.node_by_iid[_row(app, root_iid, name)] for name in ("sub", "Zed"))
    previous = {
        os.path.normcase(os.path.normpath(sub.path)): sub.size + 500,  # shrank by 500 B
        os.path.normcase(os.path.normpath(zed.path)): max(zed.size - 2048, 1),  # grew
    }
    monkeypatch.setattr(main_tree, "get_folder_sizes", lambda scan_id: previous)

    app._show_changes(previous_scan_id=1)

    assert app.tree.set(_row(app, root_iid, "sub"), "change").startswith("−500 B")
    assert app.tree.set(_row(app, root_iid, "Zed"), "change").startswith("+")
    # Files and folders the last scan didn't keep (all under 50 MB) say nothing.
    assert app.tree.set(root_iid, "change") == ""
    app._sort_by("change")
    folders = [n for n in _names(app, root_iid) if app.node_by_iid[_row(app, root_iid, n)].is_dir]
    assert folders == ["Zed", "sub"]
    _assert_striped(app, root_iid)


def test_a_selection_with_a_folder_and_rows_inside_it_acts_on_the_folder_once(app, scanned):
    root_iid = scanned
    sub_iid = _row(app, root_iid, "sub")
    picked = [_row(app, sub_iid, "X.bin"), sub_iid, _row(app, root_iid, "A.txt")]
    app.tree.selection_set(picked)

    names = sorted(node.name for node in app._selected_nodes())

    assert names == ["A.txt", "sub"]  # X.bin goes with its folder


def test_the_modified_column_shows_each_rows_time_and_sorts_newest_first(app, tmp_path):
    _write(tmp_path, {"old.txt": 1, "mid.txt": 1, "new.txt": 1})
    for days_ago, name in ((300, "old.txt"), (20, "mid.txt"), (1, "new.txt")):
        stamp = time.time() - days_ago * 86400
        os.utime(tmp_path / name, (stamp, stamp))
    root_node = scanner.scan(str(tmp_path), queue.Queue(), threading.Event())
    app.root_node = root_node
    root_iid = app._insert_node("", root_node, parent_size=root_node.size or 1)
    app._populate_children(root_iid, root_node)

    app._sort_by("modified")

    assert _names(app, root_iid) == ["new.txt", "mid.txt", "old.txt"]
    old = app.node_by_iid[_row(app, root_iid, "old.txt")]
    assert app.tree.set(_row(app, root_iid, "old.txt"), "modified") == date_text(old.mtime)
    assert date_text(old.mtime)[:4].isdigit()
    _assert_striped(app, root_iid)


def test_changed_folders_only_lists_changed_folders_until_unticked(app, scanned, monkeypatch):
    root_iid = scanned
    sub, zed = (app.node_by_iid[_row(app, root_iid, name)] for name in ("sub", "Zed"))
    assert app.changed_only_check.instate(["disabled"])  # nothing to compare with yet

    # Zed is the same size as last time; "sub" wasn't kept by that scan and
    # is over the (lowered) history threshold, so it counts as new.
    monkeypatch.setattr(main_tree, "MIN_FOLDER_SIZE_FOR_HISTORY", 50)
    previous = {os.path.normcase(os.path.normpath(zed.path)): zed.size}
    monkeypatch.setattr(main_tree, "get_folder_sizes", lambda scan_id: previous)
    app._show_changes(previous_scan_id=1)
    assert app.changed_only_check.instate(["!disabled"])

    app.changed_only_var.set(True)
    app._refilter_tree()
    assert _names(app, root_iid) == ["sub"]  # no files, no unchanged folder
    # Nothing under "sub" changed, so it has nothing to expand.
    assert app.tree.get_children(_row(app, root_iid, "sub")) == ()

    previous[os.path.normcase(os.path.normpath(zed.path))] = zed.size + 1  # Zed shrank
    previous[os.path.normcase(os.path.normpath(sub.path))] = sub.size  # sub didn't
    app._show_changes(previous_scan_id=1)  # arrives with the box still ticked
    assert _names(app, root_iid) == ["Zed"]

    previous[os.path.normcase(os.path.normpath(zed.path))] = zed.size  # nothing changed
    app._refilter_tree()
    [message] = app.tree.get_children(root_iid)
    assert app.tree.item(message, "text") == "No folder here changed since the last scan."

    app.changed_only_var.set(False)
    app._refilter_tree()
    assert sorted(_names(app, root_iid)) == sorted([*TOP_FILES, "sub", "Zed"])
    _assert_striped(app, root_iid)


def test_a_big_level_shows_a_page_then_more_on_request_and_sorts_its_true_top(
    app, tmp_path, monkeypatch
):
    monkeypatch.setattr(main_tree, "ROWS_PER_PAGE", 2)
    _write(tmp_path, TOP_FILES)  # five files: 500, 400, 300, 200, 100 bytes
    root_node = scanner.scan(str(tmp_path), queue.Queue(), threading.Event())
    root_iid = app._insert_node("", root_node, parent_size=root_node.size)
    app._populate_children(root_iid, root_node)

    rows = app.tree.get_children(root_iid)
    shown = [
        app.node_by_iid[i].name for i in app.tree.get_children(root_iid) if i in app.node_by_iid
    ]
    assert shown == ["c.txt", "E.txt"]  # biggest first
    [more] = [iid for iid in rows if iid in app._more_rows]
    assert "3 more" in app.tree.item(more, "text")

    app.tree.focus(more)
    app._open_focused()  # Enter on the "more" row
    assert [
        app.node_by_iid[i].name for i in app.tree.get_children(root_iid) if i in app.node_by_iid
    ] == [
        "c.txt",
        "E.txt",
        "b.txt",
        "d.txt",
    ]

    app._sort_by("size")  # now smallest first: the page must be the smallest four
    shown = [
        app.node_by_iid[i].name for i in app.tree.get_children(root_iid) if i in app.node_by_iid
    ]
    assert shown == ["A.txt", "d.txt", "b.txt", "E.txt"]
    assert any(iid in app._more_rows for iid in app.tree.get_children(root_iid))
