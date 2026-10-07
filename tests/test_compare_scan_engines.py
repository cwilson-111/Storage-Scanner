import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compare_scan_engines import (
    CHANGED,
    EXTRA,
    HARDLINK_ORDER,
    MISMATCH,
    MISSING,
    OPEN_FOR_WRITING,
    PREALLOCATED,
    RESIDENT,
    ROLLUP,
    UNEXPLAINED,
    UNREADABLE,
    Discrepancy,
    _compare,
    _flatten,
    classify,
    on_disk_spelling,
)
from live_file_state import LiveState, live_file_state
from storage_scanner.models import FLAG_HARDLINK_DUP, Node

DATA = "C:\\Data"
FILE_PATH = os.path.join(DATA, "a.txt")


def _root(size=0, alloc_size=0, file_count=0):
    node = Node(DATA, "Data")
    node.size = size
    node.alloc_size = alloc_size
    node.file_count = file_count
    return node


def _tree_with_one_file(size=100, alloc_size=4096, flags=0):
    root = _root(size=size, alloc_size=alloc_size, file_count=1)
    root.add_file("a.txt", size, alloc_size, flags=flags)
    return root


def test_flatten_includes_every_node_keyed_by_path():
    root = _tree_with_one_file()
    flat = _flatten(root)
    assert set(flat) == {DATA, FILE_PATH}
    assert flat[FILE_PATH].size == 100


def test_identical_trees_produce_no_discrepancies():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    assert _compare(compatible, turbo) == []


def test_size_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    turbo.set_file_size(0, 999, turbo.file_allocs[0])

    discrepancies = _compare(compatible, turbo)

    assert discrepancies == [Discrepancy(MISMATCH, FILE_PATH, "size", 100, 999)]
    assert str(discrepancies[0]) == f"size MISMATCH at {FILE_PATH}: compatible=100 turbo=999"


def test_node_missing_from_turbo_is_reported():
    compatible = _tree_with_one_file()
    turbo = _root()  # no children at all

    discrepancies = _compare(compatible, turbo)

    assert Discrepancy(MISSING, FILE_PATH) in discrepancies


def test_extra_node_in_turbo_is_reported():
    compatible = _root()  # no children
    turbo = _tree_with_one_file()

    discrepancies = _compare(compatible, turbo)

    assert Discrepancy(EXTRA, FILE_PATH) in discrepancies


def test_multiple_field_mismatches_on_one_node_are_each_reported():
    compatible = _tree_with_one_file()
    turbo = _root(size=100, alloc_size=4096, file_count=1)
    turbo.add_file("a.txt", 999, 8192)

    discrepancies = _compare(compatible, turbo)

    assert {d.field for d in discrepancies} == {"size", "alloc_size"}


def test_is_dir_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _root(size=100, alloc_size=4096, file_count=1)
    folder = Node(FILE_PATH, "a.txt")  # the same path, read as a folder
    folder.size, folder.alloc_size, folder.file_count = 100, 4096, 1
    turbo.dirs.append(folder)

    discrepancies = _compare(compatible, turbo)

    assert any(d.field == "is_dir" for d in discrepancies)


def _stat(mtime=0.0, size=0, nlink=1):
    return SimpleNamespace(st_mtime=mtime, st_size=size, st_nlink=nlink)


def _categories(compatible, turbo, stats=None, since=None, live=None):
    """(path, field or kind) -> category, for every discrepancy. `live` maps
    a path to its LiveState; any other path's is unknown."""
    stats, live = stats or {}, live or {}
    classified = classify(
        _compare(compatible, turbo),
        compatible,
        turbo,
        since,
        stat_now=stats.get,
        live=lambda path: live.get(path, LiveState(None, False)),
    )
    return {(d.path, d.field or d.kind): category for d, category in classified}


def test_turbo_billing_what_ntfs_has_allocated_past_the_end_is_preallocated():
    # A real open models.db-wal: 230,752 bytes, Compatible 233,472 on disk,
    # Turbo and NTFS 327,680.
    compatible = _tree_with_one_file(size=230_752, alloc_size=233_472)
    turbo = _tree_with_one_file(size=230_752, alloc_size=327_680)
    stats = {FILE_PATH: _stat(mtime=10.0, size=230_752), DATA: _stat(mtime=10.0)}

    agrees = {FILE_PATH: LiveState(327_680, False)}
    differs = {FILE_PATH: LiveState(262_144, False)}

    assert (
        _categories(compatible, turbo, stats, since=1_000.0, live=agrees)[(FILE_PATH, "alloc_size")]
        == PREALLOCATED
    )
    assert (
        _categories(compatible, turbo, stats, since=1_000.0, live=differs)[
            (FILE_PATH, "alloc_size")
        ]
        == UNEXPLAINED
    )


def test_a_file_open_for_writing_is_a_moving_target_only_on_a_live_run():
    # An Edge DIPS-wal: 8,272 bytes to Compatible, 0 in its MFT record, its
    # modified time older than the run.
    compatible = _tree_with_one_file(size=8_272, alloc_size=12_288)
    turbo = _tree_with_one_file(size=0, alloc_size=0)
    stats = {FILE_PATH: _stat(mtime=10.0, size=8_272), DATA: _stat(mtime=10.0)}
    live = {FILE_PATH: LiveState(16_384, True)}

    on_a_live_run = _categories(compatible, turbo, stats, since=1_000.0, live=live)
    in_the_callers_folder = _categories(compatible, turbo, stats, since=None, live=live)

    assert on_a_live_run[(FILE_PATH, "size")] == OPEN_FOR_WRITING
    assert in_the_callers_folder[(FILE_PATH, "size")] == UNEXPLAINED


@pytest.mark.windows
def test_live_file_state_sees_a_writer_and_the_allocation(tmp_path):
    path = tmp_path / "log.bin"
    with open(path, "ab") as writer:
        writer.write(b"x" * 10_000)
        writer.flush()
        while_open = live_file_state(str(path))
    after_close = live_file_state(str(path))

    assert while_open.open_for_writing
    assert not after_close.open_for_writing
    assert after_close.allocation >= 10_000


def test_a_file_modified_after_the_run_started_changed_and_its_folder_total_follows():
    compatible = _tree_with_one_file(size=100)
    turbo = _tree_with_one_file(size=999)
    stats = {FILE_PATH: _stat(mtime=1_000.0, size=999), DATA: _stat()}

    found = _categories(compatible, turbo, stats, since=1_000.0)

    assert found == {(FILE_PATH, "size"): CHANGED, (DATA, "size"): ROLLUP}


def test_without_a_start_time_nothing_counts_as_changed():
    # A folder only the caller writes to: any difference is a finding.
    compatible = _tree_with_one_file(size=100)
    turbo = _tree_with_one_file(size=999)
    stats = {FILE_PATH: _stat(mtime=1_000.0, size=999)}

    found = _categories(compatible, turbo, stats, since=None)

    assert found == {(FILE_PATH, "size"): UNEXPLAINED, (DATA, "size"): ROLLUP}


def test_a_file_gone_now_changed_during_the_run():
    compatible = _tree_with_one_file()
    turbo = _root(size=100, alloc_size=4096, file_count=1)

    found = _categories(compatible, turbo, stats={}, since=1_000.0)

    assert found[(FILE_PATH, MISSING)] == CHANGED


def test_a_file_older_than_the_run_with_its_old_size_is_unexplained():
    compatible = _tree_with_one_file(size=100)
    turbo = _tree_with_one_file(size=999)
    stats = {FILE_PATH: _stat(mtime=10.0, size=100), DATA: _stat(mtime=10.0)}

    found = _categories(compatible, turbo, stats, since=1_000.0)

    assert found[(FILE_PATH, "size")] == UNEXPLAINED


def test_engines_counting_different_links_of_a_hard_linked_file_is_hard_link_order():
    compatible = _tree_with_one_file(size=100, alloc_size=4096)
    turbo = _tree_with_one_file(size=0, alloc_size=0, flags=FLAG_HARDLINK_DUP)

    linked = _categories(compatible, turbo, {FILE_PATH: _stat(size=100, nlink=2)})
    single = _categories(compatible, turbo, {FILE_PATH: _stat(size=100, nlink=1)})

    assert {linked[(FILE_PATH, f)] for f in ("hardlink_dup", "size", "alloc_size")} == {
        HARDLINK_ORDER
    }
    assert single[(FILE_PATH, "hardlink_dup")] == UNEXPLAINED


def test_whats_below_a_folder_compatible_couldnt_list_is_unreadable_to_it():
    compatible = _root()
    locked = Node(os.path.join(DATA, "Locked"), "Locked")
    locked.error = True
    compatible.dirs.append(locked)
    compatible.error = True  # rolled up from Locked, as scanner._rollup does
    turbo = _root()
    turbo.dirs.append(Node(locked.path, "Locked"))
    turbo.dirs[0].add_file("secret.bin", 10)
    turbo.add_file("new.txt", 10)  # beside it, in a folder that was listed

    found = _categories(compatible, turbo)

    assert found[(os.path.join(locked.path, "secret.bin"), EXTRA)] == UNREADABLE
    assert found[(os.path.join(DATA, "new.txt"), EXTRA)] == UNEXPLAINED


def test_a_small_file_billed_its_length_by_turbo_and_a_cluster_by_compatible_is_resident():
    compatible = _tree_with_one_file(size=600, alloc_size=4096)
    resident = _tree_with_one_file(size=600, alloc_size=600)
    unbilled = _tree_with_one_file(size=600, alloc_size=0)
    too_big = _tree_with_one_file(size=10_000, alloc_size=10_000)
    rounded = _tree_with_one_file(size=10_000, alloc_size=12_288)

    assert _categories(compatible, resident)[(FILE_PATH, "alloc_size")] == RESIDENT
    assert _categories(compatible, unbilled)[(FILE_PATH, "alloc_size")] == UNEXPLAINED
    assert _categories(rounded, too_big)[(FILE_PATH, "alloc_size")] == UNEXPLAINED


def test_a_folder_total_with_no_difference_below_it_is_unexplained():
    compatible = _tree_with_one_file(size=100)
    turbo = _tree_with_one_file(size=100)
    turbo.size = 200

    assert _categories(compatible, turbo) == {(DATA, "size"): UNEXPLAINED}


@pytest.mark.windows
def test_a_path_typed_in_another_case_is_compared_in_its_on_disk_spelling(tmp_path):
    # Turbo Scan's tree is named the way NTFS stores it; asking the
    # Compatible engine for C:\WINDOWS would make every path differ.
    real = tmp_path / "MixedCase" / "Sub.Folder"
    real.mkdir(parents=True)

    assert on_disk_spelling(str(real).lower()) == os.path.realpath(real)


@pytest.mark.windows
def test_a_junction_asked_for_in_another_case_stays_the_junction(tmp_path):
    import _winapi

    target = tmp_path / "Target"
    target.mkdir()
    junction = tmp_path / "TheJunction"
    _winapi.CreateJunction(str(target), str(junction))
    spelled_parent = os.path.realpath(tmp_path)

    assert on_disk_spelling(str(junction).upper()) == os.path.join(spelled_parent, "TheJunction")
