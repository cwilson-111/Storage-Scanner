"""Tests for storage_scanner.mft_scan against hand-built ParsedRecord lists
(byte-level parsing is mft_parser's concern -- see tests/test_mft_parser.py;
this only exercises the record-graph-to-Node-tree construction)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.mft_parser import ParsedRecord, FileNameAttr, _pack_frn
from storage_scanner.mft_scan import build_tree

ROOT_FRN = _pack_frn(1, 5)


def _frn(record_number, sequence_number=1):
    return _pack_frn(sequence_number, record_number)


def _record(
    record_number, *, is_directory=False, names=(), sequence_number=1,
    logical_size=0, alloc_size=0, is_reparse_point=False,
    is_cloud_placeholder=False, mtime=0.0, atime=0.0, file_attributes=0,
):
    return ParsedRecord(
        frn=_frn(record_number, sequence_number), is_directory=is_directory,
        file_attributes=file_attributes, is_reparse_point=is_reparse_point,
        is_cloud_placeholder=is_cloud_placeholder, mtime=mtime, atime=atime,
        logical_size=logical_size, alloc_size=alloc_size, names=list(names),
    )


def _name(parent_frn, name, namespace=1):
    return FileNameAttr(parent_frn=parent_frn, name=name, namespace=namespace)


def _by_name(node):
    return {child.name: child for child in node.children}


def _root():
    return _record(5, is_directory=True, names=[])


def test_simple_tree_is_built_and_rolled_up():
    folder = _record(10, is_directory=True, names=[_name(ROOT_FRN, "Docs")])
    file_a = _record(11, names=[_name(_frn(10), "a.txt")], logical_size=100, alloc_size=4096)
    file_b = _record(12, names=[_name(ROOT_FRN, "b.txt")], logical_size=50, alloc_size=4096)

    tree, orphan_count = build_tree([_root(), folder, file_a, file_b], root_path="C:\\Data")

    assert orphan_count == 0
    assert tree.path == "C:\\Data"
    assert tree.is_dir

    children = _by_name(tree)
    assert set(children) == {"Docs", "b.txt"}
    assert children["b.txt"].size == 50
    assert children["b.txt"].path == "C:\\Data\\b.txt"

    docs = children["Docs"]
    assert docs.is_dir
    doc_children = _by_name(docs)
    assert set(doc_children) == {"a.txt"}
    assert doc_children["a.txt"].size == 100
    assert doc_children["a.txt"].path == "C:\\Data\\Docs\\a.txt"

    assert tree.size == 150
    assert tree.alloc_size == 4096 * 2
    assert tree.file_count == 2
    assert docs.size == 100
    assert docs.file_count == 1


def test_hardlink_occurrences_are_deduped_like_the_compatible_scanner():
    linked = _record(
        20,
        names=[_name(ROOT_FRN, "original.bin"), _name(ROOT_FRN, "linked.bin")],
        logical_size=1000, alloc_size=1000,
    )

    tree, orphan_count = build_tree([_root(), linked], root_path="C:\\Data")

    assert orphan_count == 0
    assert tree.file_count == 2
    assert tree.size == 1000  # billed once -- matches test_hardlinks_are_not_double_counted

    children = _by_name(tree)
    dup_flags = {children["original.bin"].hardlink_dup, children["linked.bin"].hardlink_dup}
    assert dup_flags == {False, True}
    sizes = {children["original.bin"].size, children["linked.bin"].size}
    assert sizes == {1000, 0}


def test_orphaned_record_with_missing_parent_is_not_attached_and_is_counted():
    missing_parent_frn = _frn(999)  # record 999 doesn't exist in this record set
    orphan = _record(30, names=[_name(missing_parent_frn, "ghost.txt")], logical_size=5)

    tree, orphan_count = build_tree([_root(), orphan], root_path="C:\\Data")

    assert orphan_count == 1
    assert tree.children == []
    assert tree.size == 0


def test_disconnected_cycle_is_unreachable_and_does_not_hang():
    # A and B only point at each other -- neither is ever reachable from
    # the real root -- so they're simply excluded, the same as any other
    # orphan. (This alone doesn't exercise the visited-directory guard --
    # see the next test for a cycle the traversal actually walks into.)
    dir_a = _record(40, is_directory=True, names=[_name(_frn(41), "A")])
    dir_b = _record(41, is_directory=True, names=[_name(_frn(40), "B")])

    tree, orphan_count = build_tree([_root(), dir_a, dir_b], root_path="C:\\Data")

    assert tree.children == []
    assert orphan_count == 2


def test_cycle_reachable_from_root_does_not_infinite_loop():
    # root -> A -> B -> A (again, as B's child) -- a cycle the traversal
    # actually walks into from the root, unlike the disconnected case
    # above. Without the visited-directory guard this would push
    # A -> B -> A -> B -> ... onto the stack forever.
    dir_a = _record(
        40, is_directory=True,
        names=[_name(ROOT_FRN, "A"), _name(_frn(41), "A_again")],
    )
    dir_b = _record(41, is_directory=True, names=[_name(_frn(40), "B")])

    tree, orphan_count = build_tree([_root(), dir_a, dir_b], root_path="C:\\Data")

    node_a = _by_name(tree)["A"]
    node_b = _by_name(node_a)["B"]
    node_a_again = _by_name(node_b)["A_again"]

    assert node_a_again.error is True
    assert node_a_again.children == []
    assert orphan_count == 0  # every occurrence (A, B, A_again) got attached somewhere


def test_directory_claiming_two_parents_is_expanded_once_with_error_on_the_repeat():
    # Invalid on real NTFS (directories can't be hard-linked), but the
    # traversal must handle it defensively rather than double-expand or loop.
    weird_dir = _record(
        50, is_directory=True,
        names=[_name(ROOT_FRN, "First"), _name(ROOT_FRN, "Second")],
    )
    inside = _record(51, names=[_name(_frn(50), "inside.txt")], logical_size=10)

    tree, orphan_count = build_tree([_root(), weird_dir, inside], root_path="C:\\Data")

    children = _by_name(tree)
    assert set(children) == {"First", "Second"}
    expanded = [c for c in children.values() if c.children]
    not_expanded = [c for c in children.values() if not c.children]
    assert len(expanded) == 1
    assert len(not_expanded) == 1
    assert not_expanded[0].error is True
    assert expanded[0].children[0].name == "inside.txt"


def test_reparse_point_directory_is_a_leaf_and_never_expanded():
    junction = _record(
        60, is_directory=True, is_reparse_point=True,
        names=[_name(ROOT_FRN, "OneDriveLink")],
    )
    # Even if something (corruptly) claims to live inside it, a reparse
    # point must never be traversed -- matches scanner.py's leaf treatment
    # of junctions/symlinks (see tests/test_scan.py's
    # test_symlinked_directory_is_not_traversed).
    ghost_child = _record(61, names=[_name(_frn(60), "inside.txt")], logical_size=1)

    tree, orphan_count = build_tree([_root(), junction, ghost_child], root_path="C:\\Data")

    link_node = _by_name(tree)["OneDriveLink"]
    assert not link_node.is_dir
    assert link_node.is_link
    assert not link_node.children
    assert orphan_count == 1  # ghost_child's parent was never expanded


def test_cloud_placeholder_reparse_point_is_not_flagged_as_a_link():
    placeholder = _record(
        62, is_reparse_point=True, is_cloud_placeholder=True,
        names=[_name(ROOT_FRN, "photo.jpg")], logical_size=2_000_000,
    )
    tree, _ = build_tree([_root(), placeholder], root_path="C:\\Data")

    node = _by_name(tree)["photo.jpg"]
    assert not node.is_link
    assert node.is_cloud_placeholder
    assert node.size == 2_000_000


def test_root_path_ending_in_separator_uses_full_path_as_name():
    tree, _ = build_tree([_root()], root_path="C:\\")
    assert tree.name == "C:\\"


def test_root_path_without_trailing_separator_uses_basename():
    tree, _ = build_tree([_root()], root_path="C:\\Users\\foo")
    assert tree.name == "foo"


def test_missing_root_record_returns_none():
    only_child = _record(10, names=[_name(ROOT_FRN, "a.txt")])
    tree, orphan_count = build_tree([only_child], root_path="C:\\Data")
    assert tree is None
    assert orphan_count == 0


def test_root_record_that_is_not_a_directory_returns_none():
    fake_root = _record(5, is_directory=False, names=[])
    tree, orphan_count = build_tree([fake_root], root_path="C:\\Data")
    assert tree is None
