"""Tests for the partial/full hashing stages of duplicate detection,
specifically the optimization that skips a second disk read + hash for any
file no larger than one chunk (see _partial_hash_file's docstring): its
"first chunk" read already covers the whole file, so a strong confirmation
digest is computed from those same bytes instead of _full_hash_file
reopening the file a second time.
"""

import hashlib
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import Node
from storage_scanner.ui.duplicate_window import DuplicatesMixin


def _make_app(tmp_path, files):
    """files: list of (name, contents) tuples, all added as children of one
    root directory node."""
    root = Node(str(tmp_path), tmp_path.name, is_dir=True)
    for name, contents in files:
        path = tmp_path / name
        path.write_bytes(contents)
        node = Node(str(path), name, is_dir=False)
        node.size = len(contents)
        root.children.append(node)
    root.size = sum(len(c) for _n, c in files)

    app = DuplicatesMixin()
    app.root_node = root
    app._should_skip_duplicate_scan = lambda path: False
    app.dup_stats = {
        "files_total": 0,
        "files_checked": 0,
        "files_skipped": 0,
        "bytes_skipped": 0,
        "partial_hashed": 0,
        "full_hashed": 0,
    }
    return app


# -- _partial_hash_file's new (partial_digest, full_digest) contract ------- #


def test_partial_hash_returns_a_full_digest_for_a_file_within_one_chunk(tmp_path):
    content = b"small file content"
    path = tmp_path / "small.bin"
    path.write_bytes(content)

    app = DuplicatesMixin()
    partial_digest, full_digest = app._partial_hash_file(str(path), chunk_size=1024 * 1024)

    assert partial_digest == hashlib.blake2b(content, digest_size=16).hexdigest()
    assert full_digest == hashlib.blake2b(content, digest_size=32).hexdigest()
    # And that full digest must be identical to what _full_hash_file itself
    # would produce -- this optimization must never weaken the final
    # confirmation strength, only avoid re-reading the file for it.
    assert full_digest == app._full_hash_file(str(path))


def test_partial_hash_returns_no_full_digest_for_a_file_larger_than_one_chunk(tmp_path):
    # chunk_size deliberately tiny so the test doesn't need a real multi-MB
    # file on disk to exercise the "larger than one chunk" branch.
    content = b"0123456789"
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    app = DuplicatesMixin()
    partial_digest, full_digest = app._partial_hash_file(str(path), chunk_size=4)

    assert full_digest is None
    h = hashlib.blake2b(digest_size=16)
    h.update(b"0123")  # first 4 bytes
    h.update(b"6789")  # last 4 bytes
    assert partial_digest == h.hexdigest()


def test_partial_hash_reports_none_none_for_an_unreadable_file(tmp_path):
    app = DuplicatesMixin()
    missing = tmp_path / "does_not_exist.bin"

    assert app._partial_hash_file(str(missing)) == (None, None)


# -- End-to-end: correctness is unchanged, and the second read is skipped -- #


def test_small_identical_files_are_found_as_duplicates_without_a_second_read(tmp_path, monkeypatch):
    content = b"identical small content" * 10  # well under the 1MB chunk size
    app = _make_app(
        tmp_path,
        [
            ("a.bin", content),
            ("b.bin", content),
            ("unique.bin", b"different"),
        ],
    )

    full_hash_calls = []
    real_full_hash = app._full_hash_file
    monkeypatch.setattr(
        app,
        "_full_hash_file",
        lambda path, cancel_event=None: full_hash_calls.append(path)
        or real_full_hash(path, cancel_event),
    )

    duplicates = app._find_duplicate_files(cancel_event=threading.Event())

    assert len(duplicates) == 1
    _size, _digest, nodes = duplicates[0]
    assert {n.name for n in nodes} == {"a.bin", "b.bin"}
    # The whole point of the optimization: neither small duplicate candidate
    # should trigger a second read via _full_hash_file, since the partial
    # hash pass already computed a strong confirmation digest from the same
    # bytes it had already read.
    assert full_hash_calls == []


def test_large_identical_files_still_go_through_full_hash_confirmation(tmp_path, monkeypatch):
    # Force a tiny "chunk size" for this run so the files only need to be a
    # few hundred bytes, not a real multi-MB file, to land in the "larger
    # than one chunk" branch -- proving the cache is *not* used when the
    # partial hash didn't already cover the whole file.
    tiny_chunk = 64
    content = (b"x" * 200) + b"UNIQUE-MIDDLE-BYTES" + (b"y" * 200)
    app = _make_app(tmp_path, [("a.bin", content), ("b.bin", content)])

    # _find_duplicate_files always calls _partial_hash_file with its
    # default chunk_size, so pin that default down to tiny_chunk for this
    # test instead of threading a parameter through the pipeline.
    real_partial = app._partial_hash_file
    monkeypatch.setattr(
        app,
        "_partial_hash_file",
        lambda path, cancel_event=None, chunk_size=1024 * 1024: real_partial(
            path, cancel_event, tiny_chunk
        ),
    )

    full_hash_calls = []
    real_full_hash = app._full_hash_file
    monkeypatch.setattr(
        app,
        "_full_hash_file",
        lambda path, cancel_event=None: full_hash_calls.append(path)
        or real_full_hash(path, cancel_event),
    )

    duplicates = app._find_duplicate_files(cancel_event=threading.Event())

    assert len(duplicates) == 1
    _size, _digest, nodes = duplicates[0]
    assert {n.name for n in nodes} == {"a.bin", "b.bin"}
    # Both candidates are larger than the (forced tiny) chunk size, so the
    # partial hash never covered the whole file -- the full-hash
    # confirmation pass must still run for correctness.
    assert len(full_hash_calls) == 2
