"""The Cleanup Recommendations window (ui/cleanup_view.py) on the real app,
over a real scan: its rows per category, that Protected rows can't be
deleted or queued, and what deleting does to the rows and the summary.
Deletes go through the real DeleteService into a folder standing in for
the Recycle Bin."""

import functools
import os
import sys
import time
from pathlib import Path
from tkinter import ttk

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import cleanup_recommendations
from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE,
    CATEGORY_PROTECTED,
    CATEGORY_REVIEW,
)
from storage_scanner.formatting import human_size
from storage_scanner.platform_support import TRASH_NAME
from storage_scanner.settings import DEFAULT_DUPLICATE_EXCLUDES
from storage_scanner.ui import cleanup_view

LARGE = 4096  # what counts as a large file here, instead of 100 MB
OLD = 5000  # old/big.iso: large and untouched for 400 days
COPY = 2000  # photos/a.jpg, and the same bytes again in downloads/
PROTECTED = 500  # system.dll, inside a folder the OS manages


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "scan"
    marker = DEFAULT_DUPLICATE_EXCLUDES[0].strip("\\/").replace("\\", "/").split("/")
    files = {
        Path(*marker, "system.dll"): b"s" * PROTECTED,
        Path("old", "big.iso"): b"o" * OLD,
        Path("photos", "a.jpg"): b"p" * COPY,
        Path("downloads", "a (copy).jpg"): b"p" * COPY,
        Path("notes.txt"): b"n" * 30,
    }
    for relative, data in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    now = time.time()
    long_ago = now - 400 * 86400
    os.utime(folder / "old" / "big.iso", (long_ago, long_ago))
    # The older copy is the one to keep.
    os.utime(folder / "photos" / "a.jpg", (now - 1000, now - 1000))
    os.utime(folder / "downloads" / "a (copy).jpg", (now - 10, now - 10))
    return folder


@pytest.fixture
def app(scanner_app, scan_in_app, folder, monkeypatch):
    monkeypatch.setattr(
        cleanup_view,
        "find_protected_and_review_candidates",
        functools.partial(
            cleanup_recommendations.find_protected_and_review_candidates, large_bytes=LARGE
        ),
    )
    scan_in_app(folder)
    return scanner_app


def _widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _widgets(child)


class _CleanupWindow:
    """Cleanup Recommendations as opened from Tools, read the way it looks;
    `wait` lets the duplicate search behind it finish first."""

    def __init__(self, app, run_tk, wait=True):
        app.show_cleanup_recommendations()
        self.win = app._cleanup_win
        [self.tv] = [w for w in _widgets(self.win) if isinstance(w, ttk.Treeview)]
        [summary] = [
            w for w in _widgets(self.win) if isinstance(w, ttk.Label) and w.cget("textvariable")
        ]
        self._summary = str(summary.cget("textvariable"))
        if wait:
            assert run_tk(lambda: "scanning for duplicate files" not in self.summary)

    @property
    def summary(self):
        return self.win.getvar(self._summary)

    def rows(self):
        """(Category, Item, Recoverable) per row."""
        return [
            (
                self.tv.set(iid, "category"),
                self.tv.set(iid, "name"),
                self.tv.set(iid, "recoverable"),
            )
            for iid in self.tv.get_children()
        ]

    def select(self, *names):
        self.tv.selection_set(
            [iid for iid in self.tv.get_children() if self.tv.set(iid, "name") in names]
        )

    def click(self, text):
        [button] = [
            w for w in _widgets(self.win) if isinstance(w, ttk.Button) and w.cget("text") == text
        ]
        button.invoke()


def _summary_start(count, recoverable):
    return f"{count} recommendation(s) — {human_size(recoverable)} potentially recoverable"


def _protected_path(folder):
    return next(folder.rglob("system.dll"))


ALL_ROWS = [
    (CATEGORY_REVIEW, "big.iso", human_size(OLD)),
    (CATEGORY_DUPLICATE, "a (copy).jpg", human_size(COPY)),
    (CATEGORY_PROTECTED, "system.dll", "—"),
]


def test_rows_per_category_with_duplicates_added_once_their_search_is_done(app, run_tk):
    cleanup = _CleanupWindow(app, run_tk, wait=False)

    # Straight away: what the scanned tree alone says.
    assert cleanup.rows() == [ALL_ROWS[0], ALL_ROWS[2]]
    assert cleanup.summary == (_summary_start(2, OLD) + "  (scanning for duplicate files…)")

    assert run_tk(lambda: "scanning for duplicate files" not in cleanup.summary)
    # The copy outside photos/ is the one flagged; a Protected file's bytes
    # never count as recoverable.
    assert cleanup.rows() == ALL_ROWS
    assert cleanup.summary.startswith(_summary_start(3, OLD + COPY))


def test_protected_rows_are_never_deleted_or_queued_even_when_selected(
    app, run_tk, folder, dialogs, recycle_bin
):
    cleanup = _CleanupWindow(app, run_tk)

    cleanup.select("system.dll")
    cleanup.click("Delete Selected")
    cleanup.click("Add Selected to Cart")
    assert (
        dialogs.messages("showinfo")
        == ["Select at least one non-protected recommendation first."] * 2
    )
    assert len(app.cart) == 0

    cleanup.select("system.dll", "big.iso")
    cleanup.click("Add Selected to Cart")
    assert [node.name for node, _label in app.cart.items()] == ["big.iso"]
    assert app.cart_btn.cget("text") == f"🛒 Cart (1) — {human_size(OLD)}"

    dialogs.answers = [True]
    cleanup.click("Delete Selected")
    assert dialogs.messages("askyesno") == [f"Send 1 selected item to the {TRASH_NAME}?"]
    assert _protected_path(folder).exists()
    assert not (folder / "old" / "big.iso").exists()
    assert len(list(recycle_bin.iterdir())) == 1
    assert cleanup.rows() == ALL_ROWS[1:]
    assert len(app.cart) == 0  # deleted from here, so off the cart too


def test_deleting_a_duplicate_removes_its_row_and_updates_the_summary(
    app, run_tk, folder, dialogs, recycle_bin
):
    cleanup = _CleanupWindow(app, run_tk)
    cleanup.select("a (copy).jpg")
    dialogs.answers = [True]

    cleanup.click("Delete Selected")

    assert not (folder / "downloads" / "a (copy).jpg").exists()
    assert (folder / "photos" / "a.jpg").exists()  # the copy that stays
    assert dialogs.messages("showerror") == []
    assert cleanup.rows() == [ALL_ROWS[0], ALL_ROWS[2]]
    assert cleanup.summary == _summary_start(2, OLD)
    [root_iid] = app.tree.get_children("")
    assert app.tree.set(root_iid, "size") == human_size(PROTECTED + OLD + COPY + 30)


def test_a_duplicate_row_goes_once_the_copy_it_duplicates_is_deleted_elsewhere(
    app, run_tk, folder, dialogs, recycle_bin
):
    cleanup = _CleanupWindow(app, run_tk)
    [root_iid] = app.tree.get_children("")
    [photos_iid] = [
        iid for iid in app.tree.get_children(root_iid) if app.node_by_iid[iid].name == "photos"
    ]
    app.tree.selection_set(photos_iid)
    dialogs.answers = [True]

    app._delete_selected()  # the Delete key in the main tree

    assert not (folder / "photos").exists()
    # What's left in downloads/ is the last copy now, not a spare one.
    assert (folder / "downloads" / "a (copy).jpg").exists()
    assert cleanup.rows() == [ALL_ROWS[0], ALL_ROWS[2]]
    assert cleanup.summary == _summary_start(2, OLD)
