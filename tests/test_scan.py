import os
import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import Node
from storage_scanner.scanner import find_inaccessible_paths, scan


def _run_scan(path):
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    return scan(str(path), progress_q, cancel_event)


def _run_scan_with_messages(path):
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    root = scan(str(path), progress_q, cancel_event)
    messages = []
    while True:
        try:
            messages.append(progress_q.get_nowait())
        except queue.Empty:
            break
    return root, messages


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


def test_scanning_a_directory_posts_a_live_root_reference_before_done(tmp_path):
    # storage_scanner.ui.main_window's live-tree preview (see scan()'s own
    # docstring) needs a reference to the exact same Node its worker
    # threads go on to mutate, posted before any scanning work happens --
    # not a copy, and not just at the very end alongside the final result.
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    root, messages = _run_scan_with_messages(tmp_path)

    root_messages = [payload for kind, payload in messages if kind == "root"]
    assert len(root_messages) == 1
    assert root_messages[0] is root


def test_scanning_a_single_file_never_posts_a_live_root_reference(tmp_path):
    # A single-file target returns instantly -- there's no in-progress
    # tree worth watching, so scan() shouldn't claim there is one.
    target = tmp_path / "solo.bin"
    target.write_bytes(b"x" * 100)
    _root, messages = _run_scan_with_messages(target)

    assert not any(kind == "root" for kind, _payload in messages)


def test_progress_bytes_tracks_towards_the_final_rolled_up_size(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"y" * 200)
    root, messages = _run_scan_with_messages(tmp_path)

    byte_totals = [payload for kind, payload in messages if kind == "progress_bytes"]
    assert byte_totals  # at least one was posted
    assert byte_totals[-1] == root.size == 300
    assert byte_totals == sorted(byte_totals)  # monotonically non-decreasing


# -- find_inaccessible_paths ------------------------------------------------ #

def test_find_inaccessible_paths_returns_empty_for_a_clean_tree():
    root = Node("/root", "root", is_dir=True)
    child = Node("/root/ok.bin", "ok.bin", is_dir=False)
    root.children.append(child)

    assert find_inaccessible_paths(root) == []


def test_find_inaccessible_paths_finds_a_directory_that_could_not_be_listed():
    root = Node("/root", "root", is_dir=True)
    locked = Node("/root/System Volume Information", "System Volume Information", is_dir=True)
    locked.error = True  # scandir() failed -- no children were ever discovered
    root.children.append(locked)

    assert find_inaccessible_paths(root) == [locked]


def test_find_inaccessible_paths_finds_a_file_whose_metadata_could_not_be_read():
    root = Node("/root", "root", is_dir=True)
    ok = Node("/root/ok.bin", "ok.bin", is_dir=False)
    bad = Node("/root/locked.bin", "locked.bin", is_dir=False)
    bad.error = True
    root.children.extend([ok, bad])

    assert find_inaccessible_paths(root) == [bad]


def test_find_inaccessible_paths_finds_errors_nested_several_levels_deep():
    root = Node("/root", "root", is_dir=True)
    sub = Node("/root/sub", "sub", is_dir=True)
    deep = Node("/root/sub/deep", "deep", is_dir=True)
    bad_file = Node("/root/sub/deep/locked.bin", "locked.bin", is_dir=False)
    bad_file.error = True
    root.children.append(sub)
    sub.children.append(deep)
    deep.children.append(bad_file)

    assert find_inaccessible_paths(root) == [bad_file]


def test_find_inaccessible_paths_collects_every_error_across_separate_branches():
    root = Node("/root", "root", is_dir=True)
    bad_a = Node("/root/a", "a", is_dir=True)
    bad_a.error = True
    bad_b = Node("/root/b.bin", "b.bin", is_dir=False)
    bad_b.error = True
    ok = Node("/root/c", "c", is_dir=True)
    root.children.extend([bad_a, bad_b, ok])

    found = find_inaccessible_paths(root)

    assert set(found) == {bad_a, bad_b}


def test_find_inaccessible_paths_includes_the_root_itself_when_it_errored():
    # A single-file scan target whose own os.stat() failed (see scan()'s
    # early-return path) -- the whole "tree" is just this one errored node.
    root = Node("/solo.bin", "solo.bin", is_dir=False)
    root.error = True

    assert find_inaccessible_paths(root) == [root]
