import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import cleanup_recommendations
from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE,
    CATEGORY_ORPHANED_INSTALL,
    CATEGORY_PROTECTED,
    CATEGORY_REVIEW,
    Recommendation,
    _drop_nested_under,
    build_duplicate_recommendations,
    find_orphaned_install_folders,
    find_protected_and_review_candidates,
    is_protected_path,
    pick_keeper,
)
from storage_scanner.models import FileNode, Node, join_path, row_flags
from storage_scanner.settings import DUPLICATE_HASH_CHUNK_BYTES

NOW = time.time()
DAY = 86400


def _file(
    parent,
    name,
    size=0,
    mtime=0.0,
    atime=None,
    is_cloud_placeholder=False,
    hardlink_dup=False,
):
    index = parent.add_file(
        name,
        size,
        mtime=mtime,
        atime=atime if atime is not None else mtime,
        flags=row_flags(hardlink_dup=hardlink_dup, is_cloud_placeholder=is_cloud_placeholder),
    )
    return FileNode(parent, index)


def _dir(parent, name):
    node = Node(join_path(parent.path, name), name)
    parent.dirs.append(node)
    return node


def _folder(path):
    return Node(path, os.path.basename(path))


def _protected_marker():
    """A "/System"-style marker, put through the exact same
    normcase(normpath(...)) pipeline is_protected_path() itself applies --
    needed because is_protected_path() reads DEFAULT_DUPLICATE_EXCLUDES,
    which is a *different, platform-specific* list on each OS (see
    settings.py) — real Windows/Linux excludes lists never contain
    "/System" at all, and even if they did, os.path.normpath/normcase
    rewrite "/System" into a backslash-lowercased form on Windows. These
    two tests monkeypatch that list directly so they verify is_protected_
    path()'s own matching logic without depending on which platform's
    real exclude list happens to be active."""
    return os.path.normcase(os.path.normpath(os.path.join(os.sep, "System")))


def test_protected_path_matches_known_os_markers(monkeypatch):
    monkeypatch.setattr(
        cleanup_recommendations,
        "DEFAULT_DUPLICATE_EXCLUDES",
        (_protected_marker(),),
    )
    system_path = os.path.join(os.sep, "System", "Library", "CoreServices", "foo")
    other_path = os.path.join(os.sep, "Users", "me", "Documents", "report.pdf")
    assert is_protected_path(system_path) is True
    assert is_protected_path(other_path) is False


def test_large_old_file_is_flagged_as_review_candidate():
    root = Node("/root", "root")
    old_mtime = NOW - 200 * DAY
    _file(root, "movie.mkv", size=200 * 1024 * 1024, mtime=old_mtime)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_REVIEW
    assert recs[0].recoverable_bytes == 200 * 1024 * 1024
    assert "200" in recs[0].reason or "199" in recs[0].reason  # ~200 days old


def test_recent_large_file_is_not_flagged():
    root = Node("/root", "root")
    _file(root, "recent.mkv", size=200 * 1024 * 1024, mtime=NOW - 5 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_small_old_file_is_not_flagged():
    root = Node("/root", "root")
    _file(root, "notes.txt", size=1024, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_recent_atime_prevents_review_flag_even_with_old_mtime():
    """A file touched recently (atime) shouldn't be flagged as "unused"
    just because its content hasn't changed (mtime) — e.g. a reference PDF
    you reopen often but never edit."""
    root = Node("/root", "root")
    _file(
        root,
        "reference.pdf",
        size=150 * 1024 * 1024,
        mtime=NOW - 400 * DAY,
        atime=NOW - 2 * DAY,
    )

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_system_path_is_protected_not_reviewed(monkeypatch):
    monkeypatch.setattr(
        cleanup_recommendations,
        "DEFAULT_DUPLICATE_EXCLUDES",
        (_protected_marker(),),
    )
    root = Node(os.path.join(os.sep, "System"), "System")
    _file(root, "big.bin", size=500 * 1024 * 1024, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_PROTECTED
    assert recs[0].recoverable_bytes == 0  # never suggested as reclaimable


def test_cloud_placeholder_is_protected():
    root = Node("/root", "root")
    _file(
        root,
        "bigfile.zip",
        size=500 * 1024 * 1024,
        mtime=NOW - 400 * DAY,
        is_cloud_placeholder=True,
    )

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_PROTECTED
    assert "placeholder" in recs[0].reason.lower()


def test_hardlink_dup_and_zero_size_files_are_never_flagged():
    root = Node("/root", "root")
    _file(root, "linked.bin", size=0, mtime=NOW - 400 * DAY, hardlink_dup=True)
    _file(root, "empty.bin", size=0, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_recommendations_sorted_by_recoverable_bytes_descending():
    root = Node("/root", "root")
    old = NOW - 300 * DAY
    _file(root, "small.bin", size=101 * 1024 * 1024, mtime=old)
    _file(root, "big.bin", size=900 * 1024 * 1024, mtime=old)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert [r.node.name for r in recs] == ["big.bin", "small.bin"]


def test_pick_keeper_prefers_non_transient_path():
    in_downloads = _file(_folder("/Users/me/Downloads"), "photo.jpg", mtime=100)
    # newer, but not in a transient folder
    in_pictures = _file(_folder("/Users/me/Pictures"), "photo.jpg", mtime=200)

    keeper = pick_keeper([in_downloads, in_pictures])

    assert keeper == in_pictures


def test_pick_keeper_prefers_older_mtime_when_neither_is_transient():
    pictures = _folder("/Users/me/Pictures")
    older = _file(pictures, "a.jpg", mtime=100)
    newer = _file(pictures, "b.jpg", mtime=200)

    assert pick_keeper([older, newer]) == older


def test_build_duplicate_recommendations_flags_everyone_but_the_keeper():
    keeper = _file(_folder("/Users/me/Pictures"), "keeper.jpg", mtime=100)
    copy1 = _file(_folder("/Users/me/Downloads"), "copy1.jpg", mtime=200)
    copy2 = _file(_folder("/Users/me/Desktop"), "copy2.jpg", mtime=300)

    groups = [(1024, "somehash", [keeper, copy1, copy2])]
    recs = build_duplicate_recommendations(groups)

    assert len(recs) == 2
    flagged_names = {r.node.name for r in recs}
    assert flagged_names == {"copy1.jpg", "copy2.jpg"}
    assert all(r.category == CATEGORY_DUPLICATE for r in recs)
    assert all(keeper.path in r.reason for r in recs)


@pytest.mark.parametrize(
    ("size", "risk_level"),
    [
        # Head, middle and tail windows together cover every byte up to here.
        (3 * DUPLICATE_HASH_CHUNK_BYTES, "Low"),
        # One byte more and a byte between the windows goes uncompared.
        (3 * DUPLICATE_HASH_CHUNK_BYTES + 1, "Medium"),
    ],
)
def test_duplicate_risk_is_low_only_while_every_byte_was_compared(size, risk_level):
    keeper = _file(_folder("/Users/me/Pictures"), "keeper.bin")
    copy = _file(_folder("/Users/me/Downloads"), "copy.bin")

    (rec,) = build_duplicate_recommendations([(size, ("edges", "middle"), [keeper, copy])])

    assert rec.node == copy
    assert rec.risk.split(" — ")[0] == risk_level


def _normalized(path):
    return os.path.normcase(os.path.normpath(path))


def test_exact_orphan_match_flags_the_folder_without_recursing_into_children():
    root = Node("/root", "root")
    orphan_folder = _dir(root, "Program Files/SomeApp")
    orphan_folder.size = 5000
    leftover = _file(orphan_folder, "leftover.dll", size=1234)
    orphaned_locations = {_normalized(orphan_folder.path)}

    recs = find_orphaned_install_folders(root, orphaned_locations)

    assert len(recs) == 1
    assert recs[0].node is orphan_folder
    assert recs[0].category == CATEGORY_ORPHANED_INSTALL
    assert recs[0].recoverable_bytes == 5000
    # The file inside must never be independently flagged -- deleting the
    # folder already reclaims it; a second row would double-count it.
    assert not any(r.node == leftover for r in recs)


def test_a_substring_match_that_is_not_an_exact_node_path_is_never_flagged():
    root = Node("/root", "root")
    folder = _dir(root, "Program Files/SomeApp")
    folder.size = 5000
    # orphaned_locations names a *different*, longer path that happens to
    # contain this folder's path as a substring -- must not match.
    orphaned_locations = {_normalized(folder.path + "2")}

    recs = find_orphaned_install_folders(root, orphaned_locations)

    assert recs == []


def test_a_sibling_folder_not_in_orphaned_locations_is_left_alone():
    root = Node("/root", "root")
    orphan_folder = _dir(root, "Program Files/SomeApp")
    orphan_folder.size = 5000
    sibling = _dir(root, "Program Files/OtherApp")
    sibling.size = 999
    orphaned_locations = {_normalized(orphan_folder.path)}

    recs = find_orphaned_install_folders(root, orphaned_locations)

    assert len(recs) == 1
    assert recs[0].node is orphan_folder


def test_a_subfolder_of_an_already_flagged_orphan_is_not_flagged_again():
    """Edge case: orphaned_locations names both a folder and one of its
    own subfolders (e.g. a stale/duplicate registry entry) -- once the
    parent is flagged, walking stops there, so the child is never reached
    or independently flagged."""
    root = Node("/root", "root")
    parent = _dir(root, "Program Files/SomeApp")
    parent.size = 5000
    child = _dir(parent, "SubComponent")
    child.size = 1000
    orphaned_locations = {_normalized(parent.path), _normalized(child.path)}

    recs = find_orphaned_install_folders(root, orphaned_locations)

    assert len(recs) == 1
    assert recs[0].node is parent


def test_no_matches_returns_no_recommendations():
    root = Node("/root", "root")
    _dir(root, "Program Files/StillInstalled")

    recs = find_orphaned_install_folders(root, set())

    assert recs == []


def test_drop_nested_under_removes_a_recommendation_inside_a_container():
    root = Node("/root", "root")
    container = _dir(root, "Program Files/SomeApp")
    inside = _file(container, "old_log.txt", size=100)
    elsewhere = _file(root, "unrelated.bin", size=200)

    review_recs = [
        Recommendation(
            node=inside,
            category=CATEGORY_REVIEW,
            reason="old",
            risk="Medium",
            recoverable_bytes=100,
            action="Review",
        ),
        Recommendation(
            node=elsewhere,
            category=CATEGORY_REVIEW,
            reason="old",
            risk="Medium",
            recoverable_bytes=200,
            action="Review",
        ),
    ]

    kept = _drop_nested_under(review_recs, {container.path})

    assert len(kept) == 1
    assert kept[0].node == elsewhere


def test_drop_nested_under_does_not_remove_a_sibling_with_a_shared_prefix():
    """SomeApp and SomeApp2 share a string prefix but are not nested --
    a naive (non-path-aware) prefix check would wrongly drop SomeApp2's
    contents too."""
    root = Node("/root", "root")
    container = _dir(root, "Program Files/SomeApp")
    sibling_file = _file(_dir(root, "Program Files/SomeApp2"), "notes.txt", size=50)

    review_recs = [
        Recommendation(
            node=sibling_file,
            category=CATEGORY_REVIEW,
            reason="old",
            risk="Medium",
            recoverable_bytes=50,
            action="Review",
        ),
    ]

    kept = _drop_nested_under(review_recs, {container.path})

    assert kept == review_recs


def test_drop_nested_under_with_no_containers_is_a_noop():
    root = Node("/root", "root")
    f = _file(root, "a.bin", size=10)
    recs = [
        Recommendation(
            node=f,
            category=CATEGORY_REVIEW,
            reason="old",
            risk="Medium",
            recoverable_bytes=10,
            action="Review",
        ),
    ]

    assert _drop_nested_under(recs, set()) == recs
