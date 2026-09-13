import os
import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.scanner import scan


def _run_scan(path):
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    return scan(str(path), progress_q, cancel_event)


def _by_name(node):
    return {child.name: child for child in node.children}


def test_hardlinks_are_not_double_counted(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"x" * 1000)
    linked = tmp_path / "linked.bin"
    os.link(original, linked)

    root = _run_scan(tmp_path)

    assert root.file_count == 2
    assert root.size == 1000

    children = _by_name(root)
    dup_flags = {children["original.bin"].hardlink_dup, children["linked.bin"].hardlink_dup}
    assert dup_flags == {False, True}


def test_independent_files_are_each_counted(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"y" * 200)

    root = _run_scan(tmp_path)

    assert root.file_count == 2
    assert root.size == 300
    assert not any(child.hardlink_dup for child in root.children)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks not supported")
def test_symlinked_directory_is_not_traversed(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "inside.bin").write_bytes(b"z" * 5000)

    link_dir = tmp_path / "link_to_real"
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("could not create a symlink in this environment")

    root = _run_scan(tmp_path)
    children = _by_name(root)

    assert children["real"].is_dir
    assert children["real"].size == 5000

    link_node = children["link_to_real"]
    assert not link_node.children
    assert root.size == children["real"].size + link_node.size
