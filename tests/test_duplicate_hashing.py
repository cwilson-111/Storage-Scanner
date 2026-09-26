"""Tests for duplicate detection's content matching: size, then the first +
last chunk, then (for files over two chunks) the middle chunk.

Files up to three chunks are fully covered by those windows and must match
byte-exactly; larger files are a sampled match by design.
"""

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.cleanup_recommendations import is_sampled_duplicate
from storage_scanner.models import Node
from storage_scanner.settings import DUPLICATE_HASH_CHUNK_BYTES
from storage_scanner.ui import duplicate_window
from storage_scanner.ui.duplicate_window import DuplicatesMixin


def _make_app(tmp_path, files):
    """files: list of (name, contents) tuples, all added as file rows of one
    root directory node."""
    root = Node(str(tmp_path), tmp_path.name)
    for name, contents in files:
        (tmp_path / name).write_bytes(contents)
        root.add_file(name, len(contents))
    root.size = sum(len(c) for _n, c in files)

    app = DuplicatesMixin()
    app.root_node = root
    app._should_skip_duplicate_scan = lambda path: False
    return app


def _groups(app):
    duplicates = app._find_duplicate_files(cancel_event=threading.Event())
    return [{n.name for n in nodes} for _size, _digest, nodes in duplicates]


def _flip(content, index):
    data = bytearray(content)
    data[index] ^= 0xFF
    return bytes(data)


def test_small_identical_files_are_grouped_and_a_unique_file_is_not(tmp_path):
    content = b"identical small content" * 10
    app = _make_app(
        tmp_path,
        [("a.bin", content), ("b.bin", content), ("unique.bin", b"x" * len(content))],
    )

    assert _groups(app) == [{"a.bin", "b.bin"}]


def test_unreadable_file_hashes_to_none(tmp_path):
    app = DuplicatesMixin()
    missing = str(tmp_path / "does_not_exist.bin")

    assert app._partial_hash_file(missing, 10) is None
    assert app._middle_hash_file(missing, 10) is None


def test_files_that_changed_size_since_the_scan_are_not_matched(tmp_path, monkeypatch):
    """The scan saw two-chunk files, which head + tail alone cover exactly;
    both have since grown to five chunks and now differ only in the middle.
    Hashing the head and tail of the grown files would call them a
    byte-exact match, so a file whose size no longer matches the scan is
    left out instead."""
    chunk = 4
    monkeypatch.setattr(duplicate_window, "DUPLICATE_HASH_CHUNK_BYTES", chunk)
    grown = bytes(5 * chunk)
    app = _make_app(tmp_path, [("a.bin", grown), ("b.bin", _flip(grown, len(grown) // 2))])
    sizes = app.root_node.file_sizes
    for i in range(len(sizes)):
        sizes[i] = 2 * chunk  # what the scan saw, before they grew

    assert _groups(app) == []


def test_any_single_byte_difference_splits_files_up_to_three_chunks(tmp_path, monkeypatch):
    """Head, middle and tail windows together cover every byte of a file up
    to three chunks, including the in-between sizes where they overlap or
    the middle window has to exactly bridge the gap -- so every single-byte
    difference, at every position and every size, must keep files apart."""
    chunk = 4
    monkeypatch.setattr(duplicate_window, "DUPLICATE_HASH_CHUNK_BYTES", chunk)

    files = []
    for size in range(1, 3 * chunk + 1):
        original = bytes(range(size))
        files.append((f"orig_{size}", original))
        files.append((f"copy_{size}", original))
        files.extend((f"flip_{size}_{i}", _flip(original, i)) for i in range(size))

    groups = _groups(_make_app(tmp_path, files))

    assert sorted(map(sorted, groups)) == sorted(
        sorted({f"orig_{size}", f"copy_{size}"}) for size in range(1, 3 * chunk + 1)
    )


def test_large_files_differing_only_between_sampled_windows_are_grouped(tmp_path):
    """The accepted tradeoff: above three chunks, only the first, middle and
    last chunk are compared, so a difference between them goes unseen."""
    size = 4 * DUPLICATE_HASH_CHUNK_BYTES
    original = bytes(size)
    # Head is [0, 1C), middle is [1.5C, 2.5C): 1.25C falls between them.
    between_windows = _flip(original, DUPLICATE_HASH_CHUNK_BYTES + DUPLICATE_HASH_CHUNK_BYTES // 4)

    app = _make_app(tmp_path, [("a.bin", original), ("b.bin", between_windows)])

    assert _groups(app) == [{"a.bin", "b.bin"}]
    assert is_sampled_duplicate(size)


def test_large_files_differing_only_in_the_middle_chunk_are_not_grouped(tmp_path):
    size = 4 * DUPLICATE_HASH_CHUNK_BYTES
    original = bytes(size)
    middle_changed = _flip(original, size // 2)

    app = _make_app(tmp_path, [("a.bin", original), ("b.bin", middle_changed)])

    assert _groups(app) == []
