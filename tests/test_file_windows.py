"""What Largest Files and a File Types extension's list collect from a real
scanned tree (roadmap P2-20): which files, in what order, and the count and
bytes behind a list that stops at its limit."""

import os
import queue
import threading

import pytest

from storage_scanner import scanner
from storage_scanner.models import remove_from_tree
from storage_scanner.ui.file_windows import (
    NO_EXTENSION,
    extension_totals,
    files_with_extension,
    largest_files,
)

# relative path -> size in bytes; every size differs so "largest first" has
# one right answer.
FILES = {
    "a.JPG": 700,
    "b.jpg": 300,
    "notes.txt": 50,
    "README": 20,
    os.path.join("photos", "c.jpg"): 900,
    os.path.join("photos", "d.Jpg"): 100,
    os.path.join("photos", "deep", "e.jpg"): 500,
    os.path.join("photos", "deep", "f.txt"): 40,
}


@pytest.fixture
def tree(tmp_path):
    for rel, size in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return scanner.scan(str(tmp_path), queue.Queue(), threading.Event())


def _names(listed):
    return [node.name for node in listed.nodes]


def test_extension_list_matches_case_insensitively_across_folders_largest_first(tree):
    listed = files_with_extension(tree, ".jpg", limit=100)
    assert _names(listed) == ["c.jpg", "a.JPG", "e.jpg", "b.jpg", "d.Jpg"]
    assert (listed.matched, listed.size) == (5, 900 + 700 + 500 + 300 + 100)


def test_extension_list_limit_caps_rows_but_not_count_or_size(tree):
    listed = files_with_extension(tree, ".jpg", limit=2)
    assert _names(listed) == ["c.jpg", "a.JPG"]
    assert (listed.matched, listed.size) == (5, 2500)


def test_extension_list_agrees_with_file_types_totals(tree):
    sizes, counts = extension_totals(tree)
    assert set(sizes) == {".jpg", ".txt", NO_EXTENSION}
    for ext in sizes:
        listed = files_with_extension(tree, ext, limit=100)
        assert (listed.matched, listed.size) == (counts[ext], sizes[ext])
    assert _names(files_with_extension(tree, NO_EXTENSION, limit=100)) == ["README"]


def test_extension_list_leaves_out_deleted_files(tree):
    biggest = files_with_extension(tree, ".jpg", limit=1).nodes[0]
    assert remove_from_tree(tree, biggest)
    listed = files_with_extension(tree, ".jpg", limit=100)
    assert _names(listed) == ["a.JPG", "e.jpg", "b.jpg", "d.Jpg"]
    assert (listed.matched, listed.size) == (4, 1600)


def test_largest_files_are_the_biggest_of_any_type(tree):
    listed = largest_files(tree, 3)
    assert _names(listed) == ["c.jpg", "a.JPG", "e.jpg"]
    assert (listed.matched, listed.size) == (3, 2100)
