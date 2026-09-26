"""Tests for DuplicatesMixin._remove_from_duplicate_cache: keeping
self.duplicates (the cached "Find Duplicate Files" result reused by
Cleanup Recommendations) consistent after a file or folder is deleted
through any window, so a later reopen never recommends deleting something
that's already gone.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import FileNode, Node, detached_file
from storage_scanner.ui.duplicate_window import DuplicatesMixin


def _node(path, size=100):
    return detached_file(path, size=size)


def _app_with_duplicates(duplicates):
    app = DuplicatesMixin()
    app.duplicates = duplicates
    return app


def test_deleting_one_copy_from_a_three_way_group_leaves_the_group_intact():
    a, b, c = _node("/a"), _node("/b"), _node("/c")
    app = _app_with_duplicates([(100, "digest1", [a, b, c])])

    app._remove_from_duplicate_cache(a)

    assert len(app.duplicates) == 1
    _size, _digest, nodes = app.duplicates[0]
    assert set(nodes) == {b, c}


def test_deleting_one_copy_from_a_two_way_group_drops_the_whole_group():
    a, b = _node("/a"), _node("/b")
    app = _app_with_duplicates([(100, "digest1", [a, b])])

    app._remove_from_duplicate_cache(a)

    # Only one copy remains -- it's no longer a duplicate of anything, so
    # the group must be dropped entirely, not shrunk to a single-item group.
    assert app.duplicates == []


def test_deleting_an_unrelated_node_leaves_other_groups_untouched():
    a, b = _node("/a"), _node("/b")
    x, y = _node("/x"), _node("/y")
    app = _app_with_duplicates([(100, "digest1", [a, b]), (200, "digest2", [x, y])])

    app._remove_from_duplicate_cache(_node("/unrelated"))

    assert len(app.duplicates) == 2


def test_deleting_a_folder_removes_every_duplicate_file_nested_inside_it():
    folder = Node("/root/sub", "sub")
    inside_a = FileNode(folder, folder.add_file("a", 100))
    inside_b = _node("/root/sub/b")
    outside_c = _node("/root/other/c")

    app = _app_with_duplicates(
        [
            (100, "digest1", [inside_a, inside_b]),
            (200, "digest2", [outside_c, _node("/root/other/d")]),
        ]
    )

    app._remove_from_duplicate_cache(folder)

    # inside_a's group had only inside_a + inside_b -- removing inside_a
    # drops it to one copy, so the whole group is gone. The unrelated
    # second group must be untouched.
    assert len(app.duplicates) == 1
    _size, _digest, nodes = app.duplicates[0]
    assert outside_c in nodes


def test_noop_when_there_are_no_cached_duplicates_yet():
    app = DuplicatesMixin()
    app.duplicates = None

    app._remove_from_duplicate_cache(_node("/a"))  # must not raise

    assert app.duplicates is None


def test_noop_when_cached_duplicates_is_an_empty_list():
    app = _app_with_duplicates([])

    app._remove_from_duplicate_cache(_node("/a"))

    assert app.duplicates == []
