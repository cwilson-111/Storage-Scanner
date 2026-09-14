import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE, CATEGORY_PROTECTED, CATEGORY_REVIEW,
    build_duplicate_recommendations, find_protected_and_review_candidates,
    is_protected_path, pick_keeper,
)
from storage_scanner.models import Node

NOW = time.time()
DAY = 86400


def _file(parent, name, size=0, mtime=0.0, atime=None, is_cloud_placeholder=False):
    node = Node(f"{parent.path}/{name}", name, is_dir=False)
    node.size = size
    node.mtime = mtime
    node.atime = atime if atime is not None else mtime
    node.is_cloud_placeholder = is_cloud_placeholder
    parent.children.append(node)
    return node


def _dir(parent, name):
    node = Node(f"{parent.path}/{name}", name, is_dir=True)
    parent.children.append(node)
    return node


def test_protected_path_matches_known_os_markers():
    assert is_protected_path("/System/Library/CoreServices/foo") is True
    assert is_protected_path("/Users/me/Documents/report.pdf") is False


def test_large_old_file_is_flagged_as_review_candidate():
    root = Node("/root", "root", is_dir=True)
    old_mtime = NOW - 200 * DAY
    _file(root, "movie.mkv", size=200 * 1024 * 1024, mtime=old_mtime)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_REVIEW
    assert recs[0].recoverable_bytes == 200 * 1024 * 1024
    assert "200" in recs[0].reason or "199" in recs[0].reason  # ~200 days old


def test_recent_large_file_is_not_flagged():
    root = Node("/root", "root", is_dir=True)
    _file(root, "recent.mkv", size=200 * 1024 * 1024, mtime=NOW - 5 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_small_old_file_is_not_flagged():
    root = Node("/root", "root", is_dir=True)
    _file(root, "notes.txt", size=1024, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_recent_atime_prevents_review_flag_even_with_old_mtime():
    """A file touched recently (atime) shouldn't be flagged as "unused"
    just because its content hasn't changed (mtime) — e.g. a reference PDF
    you reopen often but never edit."""
    root = Node("/root", "root", is_dir=True)
    _file(
        root, "reference.pdf", size=150 * 1024 * 1024,
        mtime=NOW - 400 * DAY, atime=NOW - 2 * DAY,
    )

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_system_path_is_protected_not_reviewed():
    root = Node("/System", "System", is_dir=True)
    _file(root, "big.bin", size=500 * 1024 * 1024, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_PROTECTED
    assert recs[0].recoverable_bytes == 0  # never suggested as reclaimable


def test_cloud_placeholder_is_protected():
    root = Node("/root", "root", is_dir=True)
    _file(
        root, "bigfile.zip", size=500 * 1024 * 1024,
        mtime=NOW - 400 * DAY, is_cloud_placeholder=True,
    )

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert len(recs) == 1
    assert recs[0].category == CATEGORY_PROTECTED
    assert "placeholder" in recs[0].reason.lower()


def test_hardlink_dup_and_zero_size_files_are_never_flagged():
    root = Node("/root", "root", is_dir=True)
    dup = _file(root, "linked.bin", size=0, mtime=NOW - 400 * DAY)
    dup.hardlink_dup = True
    _file(root, "empty.bin", size=0, mtime=NOW - 400 * DAY)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert recs == []


def test_recommendations_sorted_by_recoverable_bytes_descending():
    root = Node("/root", "root", is_dir=True)
    old = NOW - 300 * DAY
    _file(root, "small.bin", size=101 * 1024 * 1024, mtime=old)
    _file(root, "big.bin", size=900 * 1024 * 1024, mtime=old)

    recs = find_protected_and_review_candidates(root, now=NOW)

    assert [r.node.name for r in recs] == ["big.bin", "small.bin"]


def test_pick_keeper_prefers_non_transient_path():
    in_downloads = Node("/Users/me/Downloads/photo.jpg", "photo.jpg", is_dir=False)
    in_downloads.mtime = 100
    in_pictures = Node("/Users/me/Pictures/photo.jpg", "photo.jpg", is_dir=False)
    in_pictures.mtime = 200  # newer, but not in a transient folder

    keeper = pick_keeper([in_downloads, in_pictures])

    assert keeper is in_pictures


def test_pick_keeper_prefers_older_mtime_when_neither_is_transient():
    older = Node("/Users/me/Pictures/a.jpg", "a.jpg", is_dir=False)
    older.mtime = 100
    newer = Node("/Users/me/Pictures/b.jpg", "b.jpg", is_dir=False)
    newer.mtime = 200

    assert pick_keeper([older, newer]) is older


def test_build_duplicate_recommendations_flags_everyone_but_the_keeper():
    keeper = Node("/Users/me/Pictures/keeper.jpg", "keeper.jpg", is_dir=False)
    keeper.mtime = 100
    copy1 = Node("/Users/me/Downloads/copy1.jpg", "copy1.jpg", is_dir=False)
    copy1.mtime = 200
    copy2 = Node("/Users/me/Desktop/copy2.jpg", "copy2.jpg", is_dir=False)
    copy2.mtime = 300

    groups = [(1024, "somehash", [keeper, copy1, copy2])]
    recs = build_duplicate_recommendations(groups)

    assert len(recs) == 2
    flagged_names = {r.node.name for r in recs}
    assert flagged_names == {"copy1.jpg", "copy2.jpg"}
    assert all(r.category == CATEGORY_DUPLICATE for r in recs)
    assert all(keeper.path in r.reason for r in recs)
