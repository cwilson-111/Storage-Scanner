"""Tests for storage_scanner.scan_progress -- the running totals a
Compatible scan keeps per top-level folder, and the Throttle that limits how
often progress is reported -- and for what scanner.scan() posts with them.
"""

import queue
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import scanner
from storage_scanner.scan_progress import (
    DONE,
    QUEUED,
    SCANNING,
    FolderProgress,
    Throttle,
    WalkTracker,
)


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _folder(snapshot, name):
    return next(folder for folder in snapshot.top_folders if folder.name == name)


# -- Throttle ----------------------------------------------------------------- #


def test_throttle_lets_one_update_through_per_interval_unless_forced():
    clock = _Clock()
    throttle = Throttle(0.1, clock=clock)

    assert throttle.due()  # the first update always goes out
    clock.now += 0.05
    assert not throttle.due()
    assert throttle.due(force=True)  # a state change can't wait for the interval
    clock.now += 0.09
    assert not throttle.due()  # the interval restarts at the forced update
    clock.now += 0.01
    assert throttle.due()


# -- WalkTracker -------------------------------------------------------------- #


def test_a_top_level_folder_is_done_only_once_its_whole_subtree_has_been_read():
    tracker = WalkTracker(workers=2)
    tracker.begin(0, None, "T")
    [docs] = tracker.record(0, None, 1, 10, 2, ["Docs"], finished=True)
    assert _folder(tracker.snapshot(), "Docs").state == QUEUED

    tracker.begin(1, docs, "T/Docs")
    assert _folder(tracker.snapshot(), "Docs").state == SCANNING
    [sub] = tracker.record(1, docs, 3, 300, 4, ["Sub"], finished=True)
    # Docs' own listing is finished, but the subfolder it queued isn't.
    assert _folder(tracker.snapshot(), "Docs").state == SCANNING

    tracker.begin(0, sub, "T/Docs/Sub")
    tracker.record(0, sub, 2, 20, 2, [], finished=True)

    snapshot = tracker.snapshot()
    assert _folder(snapshot, "Docs") == FolderProgress("Docs", 5, 320, DONE)
    assert (snapshot.files, snapshot.bytes, snapshot.folders, snapshot.pending) == (6, 330, 3, 0)


def test_a_subfolder_found_part_way_through_a_big_folder_cant_finish_it_early():
    # A big folder reports every FLUSH_EVERY_ENTRIES entries, queueing the
    # subfolders found so far; one of those can be read completely before
    # the big folder's own listing ends.
    tracker = WalkTracker(workers=2)
    tracker.begin(0, None, "T")
    [big] = tracker.record(0, None, 0, 0, 1, ["Big"], finished=True)
    tracker.begin(0, big, "T/Big")
    [early] = tracker.record(0, big, 999, 9990, 1000, ["Early"], finished=False)

    tracker.begin(1, early, "T/Big/Early")
    tracker.record(1, early, 1, 1, 1, [], finished=True)
    assert _folder(tracker.snapshot(), "Big").state == SCANNING

    tracker.record(0, big, 5, 50, 5, [], finished=True)
    assert _folder(tracker.snapshot(), "Big") == FolderProgress("Big", 1005, 10041, DONE)


def test_counts_from_a_folder_still_being_read_are_visible_before_it_finishes():
    tracker = WalkTracker(workers=1)
    tracker.begin(0, None, "T")
    [big] = tracker.record(0, None, 0, 0, 1, ["Big"], finished=True)
    tracker.begin(0, big, "T/Big")
    tracker.record(0, big, 1000, 4000, 1000, [], finished=False)

    snapshot = tracker.snapshot()
    assert _folder(snapshot, "Big") == FolderProgress("Big", 1000, 4000, SCANNING)
    assert (snapshot.files, snapshot.bytes) == (1000, 4000)
    assert snapshot.current_entries == 1000


def test_top_level_folders_past_the_cap_share_one_aggregate_entry():
    tracker = WalkTracker(workers=1, max_folders=2)
    tracker.begin(0, None, "T")
    slots = tracker.record(0, None, 0, 0, 4, ["A", "B", "C", "D"], finished=True)

    snapshot = tracker.snapshot()
    assert [folder.name for folder in snapshot.top_folders] == ["A", "B"]
    assert snapshot.more_folders.folders == 2
    assert snapshot.more_folders.state == QUEUED

    tracker.begin(0, slots[2], "T/C")
    tracker.record(0, slots[2], 3, 30, 3, [], finished=True)
    more = tracker.snapshot().more_folders
    assert (more.files, more.bytes, more.state) == (3, 30, SCANNING)  # D is still unread

    tracker.begin(0, slots[3], "T/D")
    tracker.record(0, slots[3], 1, 10, 1, [], finished=True)
    assert tracker.snapshot().more_folders == FolderProgress("", 4, 40, DONE, folders=2)


def test_the_current_folder_is_the_one_being_read_the_longest():
    clock = _Clock()
    tracker = WalkTracker(workers=2, clock=clock)
    tracker.begin(0, None, "T")
    [slow, fast] = tracker.record(0, None, 0, 0, 2, ["Slow", "Fast"], finished=True)

    tracker.begin(0, slow, "T/Slow")
    clock.now += 5
    tracker.begin(1, fast, "T/Fast")
    clock.now += 1
    snapshot = tracker.snapshot()
    assert (snapshot.current_path, snapshot.current_seconds) == ("T/Slow", 6)

    tracker.record(0, slow, 0, 0, 0, [], finished=True)
    assert tracker.snapshot().current_path == "T/Fast"
    tracker.record(1, fast, 0, 0, 0, [], finished=True)
    assert tracker.snapshot().current_path is None


# -- what scanner.scan() posts ---------------------------------------------- #


def _messages(progress_q):
    messages = []
    while not progress_q.empty():
        messages.append(progress_q.get_nowait())
    return messages


def test_the_final_walk_snapshot_matches_the_rolled_up_tree(tmp_path, monkeypatch):
    # A tiny flush size sends every folder through the part-way reporting
    # path, subfolders found mid-listing included.
    monkeypatch.setattr(scanner, "FLUSH_EVERY_ENTRIES", 2)
    (tmp_path / "top.bin").write_bytes(b"t" * 7)
    docs = tmp_path / "docs"
    (docs / "deep").mkdir(parents=True)
    for i in range(5):
        (docs / f"d{i}.bin").write_bytes(b"d" * (10 + i))
        (docs / "deep" / f"e{i}.bin").write_bytes(b"e" * 3)
    (tmp_path / "empty").mkdir()

    progress_q = queue.Queue()
    root = scanner.scan(str(tmp_path), progress_q, threading.Event())

    messages = _messages(progress_q)
    walks = [payload for kind, payload in messages if kind == "walk"]
    final = walks[-1]
    assert (final.files, final.bytes) == (root.file_count, root.size)
    assert (final.folders, final.pending) == (4, 0)  # root, docs, deep, empty
    children = {child.name: child for child in root.children if child.is_dir}
    assert {f.name: (f.files, f.bytes, f.state) for f in final.top_folders} == {
        name: (child.file_count, child.size, DONE) for name, child in children.items()
    }
    assert [w.bytes for w in walks] == sorted(w.bytes for w in walks)
    # Adding up folder sizes is the last step, after the last walk update.
    kinds = [kind for kind, _payload in messages]
    assert kinds[-1] == "phase"
    assert "walk" not in kinds[kinds.index("phase") :]


def test_a_cancelled_scan_stops_without_counting_anything_as_done(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"a")
    cancel_event = threading.Event()
    cancel_event.set()
    progress_q = queue.Queue()

    scanner.scan(str(tmp_path), progress_q, cancel_event)

    final = [payload for kind, payload in _messages(progress_q) if kind == "walk"][-1]
    assert (final.files, final.folders, final.top_folders) == (0, 0, ())
