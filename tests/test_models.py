"""Invariants of the compact tree model (storage_scanner/models.py) that the
cart, duplicate groups, search results and the main tree's row map all
rely on: a FileNode keeps meaning the same file after other files are
deleted, and deleting keeps every folder's totals right."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.cleanup_cache import CachedNode
from storage_scanner.models import (
    FileNode,
    Node,
    detached_file,
    iter_file_rows,
    join_path,
    remove_from_tree,
)
from storage_scanner.scanner import _rollup


def _file(folder, name, size):
    return FileNode(folder, folder.add_file(name, size, size))


def _tree():
    root = Node(os.path.join(os.sep, "data"), "data")
    sub = Node(os.path.join(root.path, "sub"), "sub")
    root.dirs.append(sub)
    files = [_file(sub, name, size) for name, size in (("a", 10), ("b", 20), ("c", 30))]
    _file(root, "top", 5)
    _rollup(root)
    return root, sub, files


def test_deleting_a_file_keeps_later_files_in_the_same_folder_addressable():
    root, sub, (a, b, c) = _tree()
    held_c = FileNode(sub, c.index)  # e.g. queued in the cart earlier

    assert remove_from_tree(root, b)

    assert [f.name for f in sub.children] == ["a", "c"]
    assert (held_c.name, held_c.size, held_c.path) == ("c", 30, join_path(sub.path, "c"))
    assert (sub.size, sub.file_count) == (40, 2)
    assert (root.size, root.file_count) == (45, 3)
    names = sorted(f.file_names[i] for f, rows in iter_file_rows(root) for i in rows)
    assert names == ["a", "c", "top"]


def test_deleting_the_same_file_twice_changes_nothing_the_second_time():
    root, _sub, (a, _b, _c) = _tree()

    assert remove_from_tree(root, a)
    assert not remove_from_tree(root, FileNode(a.parent, a.index))
    assert (root.size, root.file_count) == (55, 3)


def test_deleting_a_folder_takes_its_whole_subtree_out_of_the_totals():
    root, sub, _files = _tree()

    assert remove_from_tree(root, sub)

    assert root.children == [FileNode(root, 0)]
    assert (root.size, root.file_count) == (5, 1)
    assert not remove_from_tree(root, sub)


def test_removing_a_node_that_was_never_part_of_the_scan_changes_nothing():
    # A Cleanup Recommendations row cached by an earlier session, deleted
    # after a new scan has loaded.
    root, _sub, _files = _tree()
    cached = CachedNode(join_path(root.path, "top"), "top", False, 5)

    assert not remove_from_tree(root, cached)
    assert (root.size, root.file_count) == (65, 4)


def test_views_of_the_same_row_are_one_dict_key():
    _root, sub, (a, b, _c) = _tree()
    queued = {a: "Search"}
    queued[FileNode(sub, a.index)] = "Main tree"

    assert len(queued) == 1
    assert FileNode(sub, b.index) not in queued


def test_a_folder_gets_its_own_row_columns_on_its_first_file():
    first, second = Node("first", "first"), Node("second", "second")

    _file(first, "only.txt", 7)

    assert list(first.file_sizes) == [7]
    assert (second.file_names, len(second.file_sizes), second.has_children) == ([], 0, False)


def test_a_detached_file_keeps_the_exact_path_it_was_given():
    path = os.path.join(os.path.abspath(os.sep), "dir", "report.pdf")

    node = detached_file(path, size=3)

    assert (node.path, node.name, node.size, node.is_dir) == (path, "report.pdf", 3, False)
