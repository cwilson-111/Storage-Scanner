"""The delete service: the one way anything in the app is deleted.

Real files, a real scanned tree, the real duplicate finder, Cleanup Cart
and the app's own after-delete handling (DeletionMixin); only the Recycle
Bin is a folder here (test_recycle_windows.py uses the real one). The four
P0-5 scenarios from the roadmap are the first four tests.
"""

import os
import queue
import shutil
import sys
import threading
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import delete_service, file_ops
from storage_scanner.cart import CartManager
from storage_scanner.cleanup_cache import CachedNode
from storage_scanner.cleanup_recommendations import pick_keeper
from storage_scanner.delete_outcome import DELETED_PERMANENTLY, FAILED, RECYCLED, REFUSED
from storage_scanner.delete_service import DeletedSet, DeleteRequest, DeleteService
from storage_scanner.duplicate_finder import find_duplicate_files, settle_group
from storage_scanner.models import detached_file
from storage_scanner.scanner import scan
from storage_scanner.ui.delete_dialogs import DeletionMixin


class _Confirmer:
    def __init__(self, permanent=False, typed=False):
        self.permanent = permanent
        self.typed = typed
        self.asked = []

    def confirm_permanent(self, node, reasons):
        self.asked.append(("permanent", node.path, reasons))
        return self.permanent

    def confirm_large_folder(self, node):
        self.asked.append(("typed", node.path))
        return self.typed


class _App(DeletionMixin):
    """The app's deletion side -- Cleanup Cart, duplicate cache, scanned
    tree -- without any window. Recycling moves things into `bin_dir`."""

    def __init__(self, tmp_path, blockers=(), record=None):
        self.root_node = scan(str(tmp_path / "scan"), queue.Queue(), threading.Event())
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.cart = CartManager()
        self.duplicates = find_duplicate_files(self.root_node, skip=lambda _path: False)
        self.node_by_iid = {}
        self.tree = types.SimpleNamespace(exists=lambda _iid: False)
        self.status_var = types.SimpleNamespace(set=lambda _text: None)
        self.records = []
        self.trashed = []
        self.delete_service = DeleteService(
            scan_root=lambda: self.root_node.path,
            duplicate_groups=lambda: self.duplicates,
            record=record or (lambda **row: self.records.append(row)),
            trash=self._to_bin,
            blockers=lambda _path, _is_dir: list(blockers),
            delete_permanently=self._erase,
            exists=os.path.lexists,
        )
        self.delete_service.subscribe(self._after_nodes_deleted)

    def _refresh_cart_indicator(self):
        pass

    def _to_bin(self, path):
        self.trashed.append(path)
        shutil.move(path, str(self.bin_dir / uuid.uuid4().hex))
        return RECYCLED

    def _erase(self, path):
        (shutil.rmtree if os.path.isdir(path) else os.remove)(path)
        return DELETED_PERMANENTLY

    def node(self, relative):
        """The scanned node at `relative` (a/b/c.bin)."""
        node = self.root_node
        for name in relative.split("/"):
            node = next(child for child in node.children if child.name == name)
        return node

    def delete(self, nodes, source="Search & Filter", as_duplicate=False, confirmer=None):
        requests = [DeleteRequest(n, source, as_duplicate=as_duplicate) for n in nodes]
        return self.delete_service.delete(requests, confirmer or _Confirmer())

    def execute_cart(self):
        """What the Cleanup Cart's Execute Deletions does."""
        effective = self.cart.resolve_effective_items()
        results = self.delete_service.delete(
            [
                DeleteRequest(n, f"Cleanup Cart ({label})", self.cart.is_duplicate_item(n))
                for n, label in effective
            ],
            _Confirmer(),
        )
        for node, _label in effective:
            self.cart.remove(node)
        return results


def _cached(path, is_dir=False, size=1):
    """A Cleanup Recommendations row from an earlier session, or any
    node that isn't part of this session's tree."""
    return CachedNode(path, os.path.basename(path), is_dir, size)


def _files(tmp_path, files):
    for relative, data in files.items():
        path = tmp_path / "scan" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _duplicate_pair(tmp_path):
    _files(tmp_path, {"photos/keep.jpg": b"A" * 1000, "downloads/copy.jpg": b"A" * 1000})
    app = _App(tmp_path)
    return app, app.node("photos/keep.jpg"), app.node("downloads/copy.jpg")


def test_a_copy_deleted_in_one_window_leaves_the_cart(tmp_path):
    app, _keeper, copy = _duplicate_pair(tmp_path)
    app.cart.add(copy, "Duplicate Files", as_duplicate=True)

    [result] = app.delete([copy], "Duplicate Files", as_duplicate=True)

    assert result.outcome == RECYCLED
    assert copy not in app.cart
    # So executing the cart has nothing left to fail on.
    assert app.execute_cart() == []
    assert [row["outcome"] for row in app.records] == [RECYCLED]


def test_the_cart_never_deletes_the_last_copy_after_the_keeper_went_elsewhere(tmp_path):
    app, keeper, copy = _duplicate_pair(tmp_path)
    app.cart.add(copy, "Duplicate Files", as_duplicate=True)

    [deleted_keeper] = app.delete([keeper], "Search & Filter")
    [result] = app.execute_cart()

    assert deleted_keeper.outcome == RECYCLED
    assert result.outcome == REFUSED
    assert "wasn't deleted as a duplicate" in result.message
    assert os.path.exists(copy.path)  # one copy is left, not zero
    assert app.records[-1]["outcome"] == REFUSED


def test_a_duplicates_list_open_since_the_keeper_was_deleted_cant_delete_the_last_copy(tmp_path):
    app, keeper, copy = _duplicate_pair(tmp_path)
    stale_groups = list(app.duplicates)
    [(_size, _digest, shown)] = stale_groups

    app.delete([keeper], "Search & Filter")

    # The open window drops the group: one copy left is no duplicate of anything.
    remaining, new_keeper = settle_group(shown, keeper, DeletedSet([keeper]))
    assert (remaining, new_keeper) == ([copy], None)
    # Even a list that somehow missed that can't delete it: the service
    # finds no other copy on disk.
    app.duplicates = stale_groups
    [result] = app.delete([copy], "Duplicate Files", as_duplicate=True)
    assert result.outcome == REFUSED
    assert "last one" in result.message
    assert os.path.exists(copy.path)


def test_a_queued_folder_and_a_file_inside_it_count_once_and_leave_together(tmp_path):
    _files(tmp_path, {"folder/x.bin": b"x" * 10, "folder/y.bin": b"y" * 20, "other.bin": b"o"})
    app = _App(tmp_path)
    folder, inside = app.node("folder"), app.node("folder/x.bin")
    app.cart.add(inside, "Search & Filter")
    app.cart.add(folder, "Main tree")

    assert app.cart.total_bytes() == 30

    [result] = app.execute_cart()

    assert result.outcome == RECYCLED
    assert len(app.cart) == 0  # the file inside went with its folder
    assert (app.root_node.size, app.root_node.file_count) == (1, 1)


def test_a_group_whose_keeper_was_deleted_gets_a_new_keeper_from_the_copies_left():
    keeper, a, b = (detached_file(path, size=5) for path in ("/k", "/a", "/b"))

    remaining, new_keeper = settle_group([keeper, a, b], keeper, DeletedSet([keeper]))

    assert remaining == [a, b]
    assert new_keeper is pick_keeper([a, b])


def test_a_duplicate_row_from_an_earlier_session_is_never_deleted_as_a_duplicate(tmp_path):
    """A cold-start Cleanup Recommendations row has no group to re-check."""
    _files(tmp_path, {"a.bin": b"A" * 10})
    app = _App(tmp_path)
    row = _cached(app.node("a.bin").path, size=10)

    [result] = app.delete([row], "Cleanup Recommendations", as_duplicate=True)

    assert result.outcome == REFUSED
    assert os.path.exists(row.path)


def test_the_scan_root_is_refused_and_left_alone(tmp_path):
    _files(tmp_path, {"a.bin": b"A"})
    app = _App(tmp_path)

    [result] = app.delete([app.root_node], "Main tree")

    assert result.outcome == REFUSED
    assert "folder this scan started from" in result.message
    assert app.trashed == []
    assert os.path.isdir(app.root_node.path)
    assert app.records[0]["outcome"] == REFUSED


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 trims trailing dots")
def test_a_name_ending_in_a_dot_is_never_matched_or_deleted_in_its_siblings_place(tmp_path):
    """P0-3: Win32 opens t.bin for "t.bin.", so it once hashed as t.bin's
    duplicate and deleting it recycled t.bin, the keeper."""
    _files(tmp_path, {"t.bin": b"P" * 1000})
    dotted_path = "\\\\?\\" + str(tmp_path / "scan" / "t.bin.")
    with open(dotted_path, "wb") as f:
        f.write(b"Q" * 1000)
    try:
        app = _App(tmp_path)
        plain, dotted = app.node("t.bin"), app.node("t.bin.")

        assert app.duplicates == []
        [result] = app.delete([dotted], "Main tree")

        assert result.outcome == REFUSED
        assert f"{plain.path} instead" in result.message
        assert app.trashed == []
        assert Path(plain.path).read_bytes() == b"P" * 1000
        assert os.path.lexists(dotted_path)
    finally:
        os.remove(dotted_path)


def test_a_file_changed_since_it_was_reviewed_is_refused(tmp_path):
    """node.size was captured at scan time; if something else rewrites the
    file before Delete, the file nobody reviewed must not be recycled."""
    _files(tmp_path, {"reviewed.bin": b"x" * 100})
    app = _App(tmp_path)
    node = app.node("reviewed.bin")
    Path(node.path).write_bytes(b"y" * 250)

    [result] = app.delete([node])

    assert result.outcome == REFUSED
    assert "size changed since it was reviewed" in result.message
    assert app.trashed == []


def test_a_folder_whose_contents_changed_is_still_deleted(tmp_path):
    """A folder's own getsize isn't its recursive total, and contents drift."""
    _files(tmp_path, {"folder/a.bin": b"a"})
    app = _App(tmp_path)
    (tmp_path / "scan" / "folder" / "new.bin").write_bytes(b"n" * 50)

    [result] = app.delete([app.node("folder")])

    assert result.outcome == RECYCLED


def test_something_already_gone_is_reported_as_failed_not_recycled(tmp_path):
    _files(tmp_path, {"a.bin": b"a"})
    app = _App(tmp_path)
    node = app.node("a.bin")
    os.remove(node.path)

    [result] = app.delete([node])

    assert result.outcome == FAILED
    assert "no longer there" in result.message
    assert app.trashed == []


def test_what_the_bin_cant_hold_is_only_deleted_permanently_once_confirmed(tmp_path):
    _files(tmp_path, {"a.bin": b"a", "b.bin": b"b"})
    app = _App(tmp_path, blockers=["Q: is a subst drive"])
    a, b = app.node("a.bin"), app.node("b.bin")

    [declined] = app.delete([a], confirmer=_Confirmer(permanent=False))
    confirmer = _Confirmer(permanent=True)
    [confirmed] = app.delete([b], confirmer=confirmer)

    assert declined.outcome == REFUSED
    assert os.path.exists(a.path)
    assert confirmed.outcome == DELETED_PERMANENTLY
    assert not os.path.exists(b.path)
    assert confirmer.asked == [("permanent", b.path, ["Q: is a subst drive"])]
    assert "subst drive" in app.records[-1]["error_message"]
    assert app.trashed == []  # neither was reported as, or sent to, a recycle


def test_a_very_large_folder_needs_its_name_typed(tmp_path):
    _files(tmp_path, {"big/a.bin": b"a", "big2/a.bin": b"a"})
    app = _App(tmp_path)
    big, big2 = app.node("big"), app.node("big2")
    big.size = big2.size = 10 * 1024**3

    [not_typed] = app.delete([big], confirmer=_Confirmer(typed=False))
    [typed] = app.delete([big2], confirmer=_Confirmer(typed=True))

    assert not_typed.outcome == REFUSED
    assert os.path.isdir(big.path)
    assert typed.outcome == RECYCLED


def test_a_broken_audit_log_never_breaks_the_delete(tmp_path):
    _files(tmp_path, {"a.bin": b"a"})

    def broken(**_row):
        raise RuntimeError("disk full")

    app = _App(tmp_path, record=broken)

    [result] = app.delete([app.node("a.bin")])

    assert result.outcome == RECYCLED


def test_on_linux_a_delete_goes_to_the_xdg_trash(tmp_path, monkeypatch):
    target = tmp_path / "doomed.txt"
    target.write_text("bye")
    monkeypatch.setattr(delete_service, "IS_WINDOWS", False)
    monkeypatch.setattr(file_ops, "IS_MACOS", False)
    monkeypatch.setattr(file_ops, "IS_LINUX", True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    def no_gio(*_args, **_kwargs):
        raise OSError("gio isn't installed")

    monkeypatch.setattr(file_ops.subprocess, "run", no_gio)
    records = []
    service = DeleteService(record=lambda **row: records.append(row))

    [result] = service.delete(
        [DeleteRequest(_cached(str(target), size=target.stat().st_size), "Main tree")], _Confirmer()
    )

    assert result.outcome == RECYCLED
    assert (tmp_path / "xdg" / "Trash" / "files" / "doomed.txt").read_text() == "bye"
    assert records[0]["outcome"] == RECYCLED


def test_a_trash_that_fails_is_recorded_as_failed(tmp_path, monkeypatch):
    target = tmp_path / "locked.txt"
    target.write_text("x")
    monkeypatch.setattr(delete_service, "IS_WINDOWS", False)
    monkeypatch.setattr(file_ops, "recycle", lambda _path: False)
    records = []
    service = DeleteService(record=lambda **row: records.append(row))

    [result] = service.delete(
        [DeleteRequest(_cached(str(target), size=target.stat().st_size), "Main tree")], _Confirmer()
    )

    assert result.outcome == FAILED
    assert records[0]["outcome"] == FAILED
    assert target.exists()
