"""The app's side of the delete service (ui/delete_dialogs.py): the
byte-for-byte comparison a sampled-duplicate delete runs off the Tk thread
before anything is deleted.

No Tk here. run_busy is replaced by a stand-in that hands the work straight
back, so the test drives compare() and then the identical() the delete
service is given -- replaying the questions delete_service really asks,
one request at a time, with the earlier ones already in the bin.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import duplicate_finder
from storage_scanner.delete_service import DeleteRequest
from storage_scanner.models import FileNode, Node
from storage_scanner.ui import delete_dialogs

BIG = 8 * 1024 * 1024  # over the 3 MB threshold: only the windows were hashed
DIGEST = ("edge", "middle")


def _copies(*names):
    """FileNodes of one folder, all `BIG` bytes: a duplicate group's copies."""
    folder = Node(os.path.join(os.sep, "data"), "data")
    return [FileNode(folder, folder.add_file(name, BIG)) for name in names]


class _App(delete_dialogs.DeletionMixin):
    """Just enough app for _delete_nodes: the cached duplicate groups, and a
    _delete_now that keeps the identical() it was handed instead of
    deleting anything."""

    def __init__(self, groups):
        self.duplicates = groups
        self.root = "the main window"
        self.identical = None

    def _delete_now(self, requests, parent, report=True, identical=None):
        self.identical = identical
        return []


def _straight_through(_parent, _title, _message, work, on_done, _on_error):
    """run_busy without the worker thread or the dialog."""
    on_done(work())


def _requests(*nodes):
    return [DeleteRequest(node, "Duplicate Files", as_duplicate=True) for node in nodes]


def test_every_pair_the_batch_will_ask_about_is_compared_up_front(monkeypatch):
    """Two copies of a three-copy group are selected. Deleting the first
    consumes the (first, second) answer; delete_service then asks
    other_copy() for the second, which skips the copy already in the bin
    and asks about (second, third) -- a pair that has to be answered
    already, or identical() reads both files whole on the Tk thread."""
    a, b, c = _copies("a.bin", "b.bin", "c.bin")
    app = _App([(BIG, DIGEST, [a, b, c])])

    binned = set()
    compared = []

    monkeypatch.setattr(
        duplicate_finder, "sampled_digest", lambda path, _size: None if path in binned else DIGEST
    )

    def same_content(path_a, path_b, _size):
        compared.append(frozenset((path_a, path_b)))
        return True

    monkeypatch.setattr(duplicate_finder, "same_content", same_content)
    monkeypatch.setattr(delete_dialogs, "run_busy", _straight_through)

    app._delete_nodes(_requests(a, b))

    # Each unordered pair read once: same_content(a, b) and (b, a) are the
    # same comparison, and both selected copies ask about the other.
    up_front = list(compared)
    assert sorted(sorted(pair) for pair in up_front) == [
        [a.path, b.path],
        [a.path, c.path],
        [b.path, c.path],
    ]

    identical = app.identical
    for node in (a, b):
        assert duplicate_finder.other_copy(node, app.duplicates[0], identical) == (
            duplicate_finder.IDENTICAL
        )
        binned.add(node.path)

    assert compared == up_front, "identical() fell back to reading files on the Tk thread"


def test_a_pair_that_only_starts_matching_after_the_comparison_is_still_answered(monkeypatch):
    """The safety net: a copy whose sampled windows didn't match when the
    worker thread ran, but do by the time the batch reaches it, was never
    compared up front, so identical() has to read it then and there."""
    a, b = _copies("a.bin", "b.bin")
    app = _App([(BIG, DIGEST, [a, b])])

    digests = {a.path: DIGEST, b.path: ("other", "windows")}
    compared = []

    monkeypatch.setattr(duplicate_finder, "sampled_digest", lambda path, _size: digests[path])

    def same_content(path_a, path_b, _size):
        compared.append(frozenset((path_a, path_b)))
        return True

    monkeypatch.setattr(duplicate_finder, "same_content", same_content)
    monkeypatch.setattr(delete_dialogs, "run_busy", _straight_through)

    app._delete_nodes(_requests(a))

    assert compared == []  # nothing matched the group's windows up front

    digests[b.path] = DIGEST  # b was put back the way it was
    assert duplicate_finder.other_copy(a, app.duplicates[0], app.identical) == (
        duplicate_finder.IDENTICAL
    )
    assert compared == [frozenset((a.path, b.path))]


def test_a_delete_with_nothing_to_compare_still_runs_its_completion_callback(monkeypatch):
    """The Cleanup Cart empties itself from on_done, so the branch that
    skips the busy dialog has to call it too."""
    a, b = _copies("a.bin", "b.bin")
    app = _App([(BIG, DIGEST, [a, b])])
    monkeypatch.setattr(
        delete_dialogs, "run_busy", lambda *_args: pytest.fail("nothing needed comparing")
    )
    finished = []

    app._delete_nodes([DeleteRequest(a, "Main tree")], on_done=finished.append)

    assert finished == [[]]
    assert app.identical is None
