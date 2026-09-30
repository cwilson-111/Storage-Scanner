"""Tests for storage_scanner.mft_scan against hand-built ParsedRecord lists
(byte-level parsing is mft_parser's concern -- see tests/test_mft_parser.py;
this only exercises the record-graph-to-Node-tree construction).

build_tree() and finalize_subtree() are two separate steps now (the fix
for a real bug the Turbo Scan validation gate caught: deciding which
hard-link occurrence is "primary" globally, across the whole volume, can
zero out a file within the very folder being looked at just because its
OTHER occurrence -- outside that folder entirely -- happened to be picked
instead). build_tree() alone leaves every node at its full, undeduped
size; finalize_subtree() is what actually dedups + rolls up, scoped to
whatever subtree it's called on -- so most tests below call both, in the
same order production code does.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.mft_parser import FileNameAttr, ParsedRecord, _pack_frn
from storage_scanner.mft_scan import (
    LinkedFolderError,
    build_tree,
    file_node,
    finalize_subtree,
    refuse_linked_folder,
)

ROOT_FRN = _pack_frn(1, 5)


def _frn(record_number, sequence_number=1):
    return _pack_frn(sequence_number, record_number)


def _record(
    record_number,
    *,
    is_directory=False,
    names=(),
    sequence_number=1,
    logical_size=0,
    alloc_size=0,
    is_link=False,
    is_cloud_placeholder=False,
    mtime=0.0,
    atime=0.0,
    file_attributes=0,
):
    return ParsedRecord(
        frn=_frn(record_number, sequence_number),
        is_directory=is_directory,
        file_attributes=file_attributes,
        is_link=is_link,
        is_cloud_placeholder=is_cloud_placeholder,
        mtime=mtime,
        atime=atime,
        logical_size=logical_size,
        alloc_size=alloc_size,
        names=list(names),
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

    tree, orphan_count, row_frns = build_tree(
        [_root(), folder, file_a, file_b],
        root_path="C:\\Data",
    )
    finalize_subtree(tree, row_frns)

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


def test_build_tree_alone_leaves_nodes_undeduped_and_unrolled_up():
    # The whole point of splitting build_tree()/finalize_subtree(): before
    # finalize_subtree runs, nothing has been decided yet -- every
    # occurrence (even a hard-linked one) still carries its own full,
    # real size, and directory sizes haven't been summed from children.
    linked = _record(
        20,
        names=[_name(ROOT_FRN, "original.bin"), _name(ROOT_FRN, "linked.bin")],
        logical_size=1000,
        alloc_size=1000,
    )

    tree, orphan_count, _row_frns = build_tree([_root(), linked], root_path="C:\\Data")

    assert orphan_count == 0
    children = _by_name(tree)
    assert children["original.bin"].size == 1000
    assert children["linked.bin"].size == 1000  # NOT zeroed yet
    assert children["original.bin"].hardlink_dup is False
    assert children["linked.bin"].hardlink_dup is False  # NOT flagged yet
    assert tree.size == 0  # root's own size hasn't been rolled up yet


def test_row_frns_tracks_only_hard_linked_and_link_file_rows():
    # The FRN side channel finalize_subtree()/refuse_linked_folder() need:
    # a plain file row or a directory never lands in it.
    folder = _record(10, is_directory=True, names=[_name(ROOT_FRN, "Docs")])
    plain = _record(11, names=[_name(_frn(10), "plain.txt")], logical_size=1)
    linked = _record(
        12,
        names=[_name(ROOT_FRN, "original.bin"), _name(_frn(10), "linked.bin")],
        logical_size=5,
    )
    junction = _record(13, is_directory=True, is_link=True, names=[_name(ROOT_FRN, "Link")])

    tree, _orphan_count, row_frns = build_tree(
        [_root(), folder, plain, linked, junction], root_path="C:\\Data"
    )

    docs = _by_name(tree)["Docs"]
    tracked = {
        (folder_node.file_names[index], frn)
        for folder_node, rows in row_frns.items()
        for index, frn in rows
    }
    assert tracked == {
        ("original.bin", _frn(12)),
        ("linked.bin", _frn(12)),
        ("Link", _frn(13)),
    }
    assert set(row_frns) == {tree, docs}


def test_finalize_subtree_on_the_whole_tree_dedups_like_the_compatible_scanner():
    linked = _record(
        20,
        names=[_name(ROOT_FRN, "original.bin"), _name(ROOT_FRN, "linked.bin")],
        logical_size=1000,
        alloc_size=1000,
    )

    tree, orphan_count, row_frns = build_tree([_root(), linked], root_path="C:\\Data")
    finalize_subtree(tree, row_frns)

    assert orphan_count == 0
    assert tree.file_count == 2
    assert tree.size == 1000  # billed once -- matches test_hardlinks_are_not_double_counted

    children = _by_name(tree)
    dup_flags = {children["original.bin"].hardlink_dup, children["linked.bin"].hardlink_dup}
    assert dup_flags == {False, True}
    sizes = {children["original.bin"].size, children["linked.bin"].size}
    assert sizes == {1000, 0}


def test_finalize_subtree_scoped_to_a_slice_bills_the_local_occurrence_in_full():
    # The actual bug the validation gate caught: a file hard-linked into
    # two different top-level folders (e.g. C:\Windows\Fonts and
    # C:\Windows\WinSxS). When only ONE of those folders is the subtree
    # actually being scanned (as if sliced out via find_subtree_node),
    # that folder's occurrence must be billed in full -- never zeroed
    # just because a sibling occurrence outside the slice happens to
    # exist too.
    shared_file = _record(
        70,
        names=[_name(ROOT_FRN, "in_root.bin"), _name(_frn(80), "in_sub.bin")],
        logical_size=500,
        alloc_size=512,
    )
    sub_dir = _record(80, is_directory=True, names=[_name(ROOT_FRN, "Sub")])

    tree, orphan_count, row_frns = build_tree(
        [_root(), sub_dir, shared_file],
        root_path="C:\\Data",
    )
    assert orphan_count == 0

    sub_node = _by_name(tree)["Sub"]
    # Finalize ONLY the "Sub" slice, matching what find_subtree_node would
    # hand finalize_subtree for a scan targeting "C:\Data\Sub" specifically.
    finalize_subtree(sub_node, row_frns)

    in_sub = _by_name(sub_node)["in_sub.bin"]
    assert in_sub.hardlink_dup is False
    assert in_sub.size == 500  # billed in full
    assert in_sub.alloc_size == 512  # -- "in_root.bin" is outside this slice
    assert sub_node.size == 500
    assert sub_node.file_count == 1


def test_orphaned_record_with_missing_parent_is_not_attached_and_is_counted():
    missing_parent_frn = _frn(999)  # record 999 doesn't exist in this record set
    orphan = _record(30, names=[_name(missing_parent_frn, "ghost.txt")], logical_size=5)

    tree, orphan_count, row_frns = build_tree([_root(), orphan], root_path="C:\\Data")
    finalize_subtree(tree, row_frns)

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

    tree, orphan_count, _row_frns = build_tree(
        [_root(), dir_a, dir_b],
        root_path="C:\\Data",
    )

    assert tree.children == []
    assert orphan_count == 2


def test_cycle_reachable_from_root_does_not_infinite_loop():
    # root -> A -> B -> A (again, as B's child) -- a cycle the traversal
    # actually walks into from the root, unlike the disconnected case
    # above. Without the visited-directory guard this would push
    # A -> B -> A -> B -> ... onto the stack forever.
    dir_a = _record(
        40,
        is_directory=True,
        names=[_name(ROOT_FRN, "A"), _name(_frn(41), "A_again")],
    )
    dir_b = _record(41, is_directory=True, names=[_name(_frn(40), "B")])

    tree, orphan_count, _row_frns = build_tree(
        [_root(), dir_a, dir_b],
        root_path="C:\\Data",
    )

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
        50,
        is_directory=True,
        names=[_name(ROOT_FRN, "First"), _name(ROOT_FRN, "Second")],
    )
    inside = _record(51, names=[_name(_frn(50), "inside.txt")], logical_size=10)

    tree, orphan_count, _row_frns = build_tree(
        [_root(), weird_dir, inside],
        root_path="C:\\Data",
    )

    children = _by_name(tree)
    assert set(children) == {"First", "Second"}
    expanded = [c for c in children.values() if c.children]
    not_expanded = [c for c in children.values() if not c.children]
    assert len(expanded) == 1
    assert len(not_expanded) == 1
    assert not_expanded[0].error is True
    assert expanded[0].children[0].name == "inside.txt"


def test_link_directory_is_a_leaf_and_never_expanded():
    junction = _record(
        60,
        is_directory=True,
        is_link=True,
        names=[_name(ROOT_FRN, "Link")],
    )
    # Even if something (corruptly) claims to live inside it, a link must
    # never be traversed -- matches scanner.py's leaf treatment of
    # junctions/symlinks (see tests/test_scan.py's
    # test_symlinked_directory_is_not_traversed).
    ghost_child = _record(61, names=[_name(_frn(60), "inside.txt")], logical_size=1)

    tree, orphan_count, _row_frns = build_tree(
        [_root(), junction, ghost_child],
        root_path="C:\\Data",
    )

    link_node = _by_name(tree)["Link"]
    assert not link_node.is_dir
    assert link_node.is_link
    assert not link_node.children
    assert orphan_count == 1  # ghost_child's parent was never expanded


def test_a_onedrive_folder_is_walked_though_it_is_a_reparse_point():
    # OneDrive folders carry the reparse bit on disk (0x431), but not a
    # link's tag. Turbo Scan once showed them as 0-byte links and still
    # called the scan complete; the Compatible engine walks them.
    onedrive = _record(
        63, is_directory=True, file_attributes=0x431, names=[_name(ROOT_FRN, "OneDrive")]
    )
    synced = _record(64, names=[_name(_frn(63), "report.docx")], logical_size=300, alloc_size=4096)

    tree, orphan_count, row_frns = build_tree([_root(), onedrive, synced], root_path="C:\\Data")
    finalize_subtree(tree, row_frns)

    folder = _by_name(tree)["OneDrive"]
    assert folder.is_dir and not folder.is_link
    assert (folder.size, folder.alloc_size, folder.file_count) == (300, 4096, 1)
    assert orphan_count == 0


def test_cloud_placeholder_reparse_point_is_not_flagged_as_a_link():
    placeholder = _record(
        62,
        is_link=True,
        is_cloud_placeholder=True,
        names=[_name(ROOT_FRN, "photo.jpg")],
        logical_size=2_000_000,
    )
    tree, _orphan_count, row_frns = build_tree([_root(), placeholder], root_path="C:\\Data")
    finalize_subtree(tree, row_frns)

    node = _by_name(tree)["photo.jpg"]
    assert not node.is_link
    assert node.is_cloud_placeholder
    assert node.size == 2_000_000


def test_root_path_ending_in_separator_uses_full_path_as_name():
    tree, _orphan_count, _row_frns = build_tree([_root()], root_path="C:\\")
    assert tree.name == "C:\\"


def test_root_path_without_trailing_separator_uses_basename():
    tree, _orphan_count, _row_frns = build_tree([_root()], root_path="C:\\Users\\foo")
    assert tree.name == "foo"


def test_missing_root_record_returns_none():
    only_child = _record(10, names=[_name(ROOT_FRN, "a.txt")])
    tree, orphan_count, row_frns = build_tree([only_child], root_path="C:\\Data")
    assert tree is None
    assert orphan_count == 0
    assert row_frns == {}


def test_root_record_that_is_not_a_directory_returns_none():
    fake_root = _record(5, is_directory=False, names=[])
    tree, _orphan_count, _row_frns = build_tree([fake_root], root_path="C:\\Data")
    assert tree is None


# -- refuse_linked_folder -------------------------------------------------- #
# A requested scan target that's itself a junction/symlink/mount point
# can't be read from the MFT: its own directory index is always empty. It
# goes to the Compatible engine instead, which follows it (os.path.isdir).


def test_a_link_to_a_folder_asked_for_directly_is_refused():
    # Its "contents" in the MFT are nothing (NTFS refuses to make a
    # non-empty folder a junction); this once came back as an empty tree
    # where the Compatible engine found the target's 3 files.
    link_dir = _record(40, is_directory=True, is_link=True, names=[_name(ROOT_FRN, "Link")])
    records = [_root(), link_dir]
    tree, _orphan_count, row_frns = build_tree(records, root_path="C:\\Data")

    with pytest.raises(LinkedFolderError, match="junction"):
        refuse_linked_folder(_by_name(tree)["Link"], "C:\\Data\\Link", records, row_frns)


def test_file_node_is_the_same_row_build_tree_attaches():
    record = _record(
        80,
        names=[_name(ROOT_FRN, "clip.mp4")],
        logical_size=700,
        alloc_size=4096,
        mtime=12.5,
        atime=34.5,
        is_link=True,
    )
    tree, _orphan_count, _row_frns = build_tree([_root(), record], root_path="C:\\Data")
    row = _by_name(tree)["clip.mp4"]

    node = file_node(record, "C:\\Data\\clip.mp4")

    assert not node.is_dir
    assert node.path == "C:\\Data\\clip.mp4"
    assert node.name == "clip.mp4"
    for attr in ("size", "alloc_size", "mtime", "atime", "is_link", "is_cloud_placeholder"):
        assert getattr(node, attr) == getattr(row, attr), attr
    assert node.is_link  # a link to a file stays a link
