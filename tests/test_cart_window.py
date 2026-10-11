"""The Cleanup Cart window (ui/cart_window.py) on the real app: what it
lists and totals, and what its buttons do -- deletes go through the real
DeleteService into a folder standing in for the Recycle Bin."""

import sys
from pathlib import Path
from tkinter import ttk

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.formatting import human_size
from storage_scanner.platform_support import TRASH_NAME

FILES = {"A.txt": 100, "keep.txt": 300, "sub/X.bin": 50, "sub/y.bin": 10}
SUB = 60


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "scan"
    for relative, size in FILES.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return folder


@pytest.fixture
def app(scanner_app, scan_in_app, folder):
    """The app with `folder` scanned and on screen."""
    scan_in_app(folder)
    return scanner_app


def _node(app, relative):
    node = app.root_node
    for name in relative.split("/"):
        node = next(child for child in node.children if child.name == name)
    return node


def _add_from_main_tree(app, name):
    """Select a row of the scanned folder in the main tree and pick Add to
    Cart."""
    [root_iid] = app.tree.get_children("")
    [iid] = [i for i in app.tree.get_children(root_iid) if app.node_by_iid[i].name == name]
    app.tree.selection_set(iid)
    app._add_selected_to_cart()


def _widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _widgets(child)


class _CartWindow:
    """The open Cleanup Cart window, read the way it looks."""

    def __init__(self, app):
        app.show_cart()
        self.win = app._cart_win
        [self.tv] = [w for w in _widgets(self.win) if isinstance(w, ttk.Treeview)]
        [header] = [
            w for w in _widgets(self.win) if isinstance(w, ttk.Label) and w.cget("textvariable")
        ]
        self._header = str(header.cget("textvariable"))

    @property
    def header(self):
        return self.win.getvar(self._header)

    def rows(self):
        """(Added from, Type, Size, Path) per row."""
        return [tuple(self.tv.item(iid, "values")) for iid in self.tv.get_children()]

    def select(self, path):
        [iid] = [iid for iid in self.tv.get_children() if self.tv.set(iid, "path") == str(path)]
        self.tv.selection_set(iid)

    def click(self, text):
        [button] = [
            w for w in _widgets(self.win) if isinstance(w, ttk.Button) and w.cget("text") == text
        ]
        button.invoke()


def _main_rows(app):
    [root_iid] = app.tree.get_children("")
    return [app.node_by_iid[iid].name for iid in app.tree.get_children(root_iid)]


def _fill_cart(app):
    """A folder and a file from the main tree, and a file inside that
    folder from Search & Filter."""
    _add_from_main_tree(app, "sub")
    _add_from_main_tree(app, "A.txt")
    app.cart.add(_node(app, "sub/X.bin"), "Search & Filter")
    app._refresh_cart_indicator()


def test_the_cart_lists_every_item_and_counts_a_file_inside_a_queued_folder_once(app, folder):
    _fill_cart(app)

    cart = _CartWindow(app)

    assert cart.rows() == [
        ("Main tree", "Folder", human_size(SUB), str(folder / "sub")),
        ("Main tree", "File", human_size(100), str(folder / "A.txt")),
        ("Search & Filter", "File", human_size(50), str(folder / "sub" / "X.bin")),
    ]
    assert cart.header == f"3 item(s) in cart — {human_size(SUB + 100)} reclaimable"
    assert cart.win.title() == "Cleanup Cart — 3 item(s)"
    assert app.cart_btn.cget("text") == f"🛒 Cart (3) — {human_size(SUB + 100)}"


def test_execute_sends_each_item_to_the_bin_once_and_empties_the_cart_and_the_rows(
    app, folder, dialogs, recycle_bin
):
    _fill_cart(app)
    cart = _CartWindow(app)
    dialogs.answers = [True]

    cart.click("Execute Deletions")

    # The file inside the folder goes with the folder: one question for two
    # items, and no "it's no longer there" failure for it afterwards.
    assert dialogs.messages("askyesno") == [
        f"Send 2 item(s) ({human_size(SUB + 100)}) to the {TRASH_NAME}?"
    ]
    assert dialogs.messages("showerror") == []
    assert sorted(path.name for path in folder.iterdir()) == ["keep.txt"]
    assert len(list(recycle_bin.iterdir())) == 2
    assert app.status_var.get() == f"Sent 2 item(s) to the {TRASH_NAME}."
    assert cart.rows() == []
    assert cart.header == f"0 item(s) in cart — {human_size(0)} reclaimable"
    assert app.cart_btn.cget("text") == "🛒 Cart"
    # And the main tree has lost them too.
    assert _main_rows(app) == ["keep.txt"]
    [root_iid] = app.tree.get_children("")
    assert app.tree.set(root_iid, "size") == human_size(300)


def test_an_item_changed_since_the_scan_is_refused_reported_and_taken_off_the_cart(
    app, folder, dialogs, recycle_bin
):
    _add_from_main_tree(app, "A.txt")
    _add_from_main_tree(app, "keep.txt")
    (folder / "keep.txt").write_bytes(b"x" * 301)  # something else rewrote it
    cart = _CartWindow(app)
    dialogs.answers = [True]

    cart.click("Execute Deletions")

    assert not (folder / "A.txt").exists()
    assert (folder / "keep.txt").stat().st_size == 301
    assert len(list(recycle_bin.iterdir())) == 1
    [error] = dialogs.messages("showerror")
    assert error.startswith(f"Not deleted:\n\n{folder / 'keep.txt'}\n  Its size changed")
    assert app.status_var.get() == (f"Sent 1 item(s) to the {TRASH_NAME}. 1 item(s) not deleted.")
    assert cart.rows() == []
    assert _main_rows(app) == ["keep.txt", "sub"]


def test_saying_no_to_the_question_deletes_nothing_and_keeps_the_cart(
    app, folder, dialogs, recycle_bin
):
    _fill_cart(app)
    cart = _CartWindow(app)
    rows = cart.rows()
    dialogs.answers = [False]

    cart.click("Execute Deletions")

    assert cart.rows() == rows
    assert len(app.cart) == 3
    assert (folder / "A.txt").exists() and (folder / "sub" / "X.bin").exists()
    assert not list(recycle_bin.iterdir())


def test_remove_selected_and_clear_cart_take_items_off_without_deleting_them(
    app, folder, dialogs, recycle_bin
):
    _fill_cart(app)
    cart = _CartWindow(app)

    cart.select(folder / "sub")
    cart.click("Remove Selected")

    # Without its folder, the file inside counts on its own.
    assert [row[3] for row in cart.rows()] == [str(folder / "A.txt"), str(folder / "sub" / "X.bin")]
    assert cart.header == f"2 item(s) in cart — {human_size(150)} reclaimable"
    assert app.cart_btn.cget("text") == f"🛒 Cart (2) — {human_size(150)}"

    dialogs.answers = [True]
    cart.click("Clear Cart")
    assert dialogs.messages("askyesno") == ["Remove every item from the cart? Nothing is deleted."]
    assert cart.rows() == []
    assert app.cart_btn.cget("text") == "🛒 Cart"

    cart.click("Execute Deletions")
    assert dialogs.messages("showinfo") == ["Cart is empty."]
    assert all((folder / relative).exists() for relative in FILES)
    assert not list(recycle_bin.iterdir())


def test_a_new_scan_empties_the_cart_it_listed_from_the_old_one(app, folder, run_tk):
    _fill_cart(app)
    cart = _CartWindow(app)

    app.start_scan()
    try:
        assert cart.rows() == []
        assert cart.header == f"0 item(s) in cart — {human_size(0)} reclaimable"
        assert app.cart_btn.cget("text") == "🛒 Cart"
    finally:
        assert run_tk(lambda: not app._deleting_blocked())
