"""Tests for duplicate_finder.prune_groups: keeping the cached "Find Duplicate
Files" result (reused by Cleanup Recommendations and the delete service's
last-copy check) consistent after a file or folder is deleted through any
window, so a later reopen never recommends deleting something that's
already gone, or a lone survivor as "a duplicate".
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.delete_service import DeletedSet
from storage_scanner.duplicate_finder import prune_groups
from storage_scanner.models import FileNode, Node, detached_file


def _node(path, size=100):
    return detached_file(path, size=size)


def test_deleting_one_copy_from_a_three_way_group_leaves_the_group_intact():
    a, b, c = _node("/a"), _node("/b"), _node("/c")

    groups = prune_groups([(100, "digest1", [a, b, c])], DeletedSet([a]))

    assert len(groups) == 1
    _size, _digest, nodes = groups[0]
    assert set(nodes) == {b, c}


def test_deleting_one_copy_from_a_two_way_group_drops_the_whole_group():
    a, b = _node("/a"), _node("/b")

    # Only one copy remains -- it's no longer a duplicate of anything, so
    # the group must be dropped entirely, not shrunk to a single-item group.
    assert prune_groups([(100, "digest1", [a, b])], DeletedSet([a])) == []


def test_deleting_an_unrelated_node_leaves_other_groups_untouched():
    a, b = _node("/a"), _node("/b")
    x, y = _node("/x"), _node("/y")

    groups = prune_groups(
        [(100, "digest1", [a, b]), (200, "digest2", [x, y])], DeletedSet([_node("/unrelated")])
    )

    assert len(groups) == 2


def test_deleting_a_folder_removes_every_duplicate_file_nested_inside_it():
    folder = Node("/root/sub", "sub")
    inside_a = FileNode(folder, folder.add_file("a", 100))
    inside_b = _node("/root/sub/deeper/b")  # a different view, same folder on disk
    outside_c = _node("/root/other/c")
    sibling_d = _node("/root/sub2/d")  # shares the folder's name as a prefix only

    groups = prune_groups(
        [
            (100, "digest1", [inside_a, inside_b, _node("/root/other/e")]),
            (200, "digest2", [outside_c, sibling_d]),
        ],
        DeletedSet([folder]),
    )

    # The first group lost both copies under the folder, leaving one: gone.
    # The second is untouched: /root/sub2 isn't inside /root/sub.
    assert groups == [(200, "digest2", [outside_c, sibling_d])]
