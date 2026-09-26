"""Tests for storage_scanner.scan_progress -- the running totals a
Compatible scan keeps for the whole walk and for every folder of its tree,
the folder states the main tree shows while it fills in, and the Throttle
that limits how often progress is reported -- and for what scanner.scan()
posts with them.
"""

import queue
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import scanner
from storage_scanner.models import Node, iter_folders
from storage_scanner.scan_progress import DONE, QUEUED, SCANNING, Throttle, WalkTracker


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _tree():
    """T with Docs (holding Sub) and Media, as the scan would find them."""
    root = Node("T", "T")
    docs, media = Node("T/Docs", "Docs"), Node("T/Media", "Media")
    sub = Node("T/Docs/Sub", "Sub")
    return root, docs, media, sub


def _state(tracker, node):
    return tracker.folders([node])[0][3]


def _totals(tracker, node):
    return tracker.folders([node])[0][:3]


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


def test_a_folder_at_any_depth_goes_queued_scanning_done_once_its_subtree_is_read():
    root, docs, media, sub = _tree()
    tracker = WalkTracker(root, workers=2)
    tracker.begin(0, root)
    root.dirs += [docs, media]
    tracker.record(0, root, (), 1, 10, 4, 3, [docs, media], finished=True)
    assert [_state(tracker, n) for n in (root, docs, media)] == [SCANNING, QUEUED, QUEUED]

    tracker.begin(1, docs)
    assert _state(tracker, docs) == SCANNING  # being read, nothing found yet
    docs.dirs.append(sub)
    tracker.record(1, docs, (root,), 3, 300, 12, 4, [sub], finished=True)
    # Docs' own listing is finished, but the subfolder it found isn't.
    assert (_state(tracker, docs), _state(tracker, sub)) == (SCANNING, QUEUED)

    tracker.begin(0, sub)
    tracker.record(0, sub, (root, docs), 2, 20, 8, 2, [], finished=True)
    assert [_state(tracker, n) for n in (root, docs, sub, media)] == [
        SCANNING,
        DONE,
        DONE,
        QUEUED,
    ]

    tracker.begin(1, media)
    tracker.record(1, media, (root,), 0, 0, 0, 0, [], finished=True)
    assert _state(tracker, root) == DONE
    snapshot = tracker.snapshot()
    assert (snapshot.files, snapshot.bytes, snapshot.alloc_bytes) == (6, 330, 24)
    assert (snapshot.folders, snapshot.pending) == (4, 0)


def test_every_folder_above_a_directory_shows_what_has_been_read_under_it():
    root, docs, _media, sub = _tree()
    tracker = WalkTracker(root, workers=1)
    tracker.begin(0, root)
    root.dirs.append(docs)
    tracker.record(0, root, (), 1, 100, 4096, 1, [docs], finished=True)
    tracker.begin(0, docs)
    docs.dirs.append(sub)
    tracker.record(0, docs, (root,), 2, 50, 8192, 3, [sub], finished=True)
    tracker.begin(0, sub)
    # Still being read: part-way counts reach every folder above it.
    tracker.record(0, sub, (root, docs), 1000, 7000, 4_096_000, 1000, [], finished=False)

    assert _totals(tracker, sub) == (7000, 4_096_000, 1000)
    assert _totals(tracker, docs) == (7050, 4_104_192, 1002)
    assert _totals(tracker, root) == (7150, 4_108_288, 1003)
    assert _state(tracker, root) == SCANNING
    assert tracker.snapshot().current_entries == 1000


def test_a_subfolder_found_part_way_through_a_big_folder_cant_finish_it_early():
    # A big folder reports every FLUSH_EVERY_ENTRIES entries, queueing the
    # subfolders found so far; one of those can be read completely before
    # the big folder's own listing ends.
    root = Node("T", "T")
    big, early = Node("T/Big", "Big"), Node("T/Big/Early", "Early")
    tracker = WalkTracker(root, workers=2)
    tracker.begin(0, root)
    root.dirs.append(big)
    tracker.record(0, root, (), 0, 0, 0, 1, [big], finished=True)
    tracker.begin(0, big)
    big.dirs.append(early)
    tracker.record(0, big, (root,), 999, 9990, 9990, 1000, [early], finished=False)

    tracker.begin(1, early)
    tracker.record(1, early, (root, big), 1, 1, 1, 1, [], finished=True)
    assert _state(tracker, big) == SCANNING
    assert _state(tracker, root) == SCANNING

    tracker.record(0, big, (root,), 5, 50, 50, 5, [], finished=True)
    assert tracker.folders([big, root]) == [(10041, 10041, 1005, DONE), (10041, 10041, 1005, DONE)]


def test_the_current_folder_is_the_one_being_read_the_longest():
    clock = _Clock()
    root = Node("T", "T")
    slow, fast = Node("T/Slow", "Slow"), Node("T/Fast", "Fast")
    tracker = WalkTracker(root, workers=2, clock=clock)
    tracker.begin(0, root)
    tracker.record(0, root, (), 0, 0, 0, 2, [slow, fast], finished=True)

    tracker.begin(0, slow)
    clock.now += 5
    tracker.begin(1, fast)
    clock.now += 1
    snapshot = tracker.snapshot()
    assert (snapshot.current_path, snapshot.current_seconds) == ("T/Slow", 6)

    tracker.record(0, slow, (root,), 0, 0, 0, 0, [], finished=True)
    assert tracker.snapshot().current_path == "T/Fast"
    tracker.record(1, fast, (root,), 0, 0, 0, 0, [], finished=True)
    assert tracker.snapshot().current_path is None


# -- what scanner.scan() posts ---------------------------------------------- #


def _messages(progress_q):
    messages = []
    while not progress_q.empty():
        messages.append(progress_q.get_nowait())
    return messages


def _sample_tree(tmp_path):
    (tmp_path / "top.bin").write_bytes(b"t" * 7)
    docs = tmp_path / "docs"
    (docs / "deep").mkdir(parents=True)
    for i in range(5):
        (docs / f"d{i}.bin").write_bytes(b"d" * (10 + i))
        (docs / "deep" / f"e{i}.bin").write_bytes(b"e" * 3)
    (tmp_path / "empty").mkdir()


def test_every_folders_running_totals_end_at_exactly_the_rolled_up_numbers(tmp_path, monkeypatch):
    # A tiny flush size sends every folder through the part-way reporting
    # path, subfolders found mid-listing included.
    monkeypatch.setattr(scanner, "FLUSH_EVERY_ENTRIES", 2)
    _sample_tree(tmp_path)
    live = {}
    real_rollup = scanner._rollup

    def rollup_after_recording_the_live_totals(root, own_sizes=True):
        live.update({f.path: (f.size, f.alloc_size, f.file_count) for f in iter_folders(root)})
        real_rollup(root, own_sizes)

    monkeypatch.setattr(scanner, "_rollup", rollup_after_recording_the_live_totals)
    progress_q = queue.Queue()
    root = scanner.scan(str(tmp_path), progress_q, threading.Event())

    # The rows don't jump when the finished tree replaces them, and the
    # roll-up replaced the running totals instead of adding to them.
    final = {f.path: (f.size, f.alloc_size, f.file_count) for f in iter_folders(root)}
    assert live == final
    assert root.file_count == 11 and root.size == 7 + sum(10 + i for i in range(5)) + 15

    messages = _messages(progress_q)
    kinds = [kind for kind, _payload in messages]
    assert kinds[0] == "live_tree" and messages[0][1].root is root
    tracker = messages[0][1]
    assert {state for *_totals, state in tracker.folders(list(iter_folders(root)))} == {DONE}
    walks = [payload for kind, payload in messages if kind == "walk"]
    assert (walks[-1].files, walks[-1].bytes, walks[-1].alloc_bytes) == (
        root.file_count,
        root.size,
        root.alloc_size,
    )
    assert (walks[-1].folders, walks[-1].pending) == (4, 0)  # root, docs, deep, empty
    # Adding up folder sizes is the last step, after the last walk update.
    assert kinds[-1] == "phase"
    assert "walk" not in kinds[kinds.index("phase") :]


def test_a_cancelled_scan_stops_without_counting_anything_as_done(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"a")
    cancel_event = threading.Event()
    cancel_event.set()
    progress_q = queue.Queue()

    root = scanner.scan(str(tmp_path), progress_q, cancel_event)

    messages = _messages(progress_q)
    final = [payload for kind, payload in messages if kind == "walk"][-1]
    assert (final.files, final.folders) == (0, 0)
    tracker = next(payload for kind, payload in messages if kind == "live_tree")
    assert tracker.folders([root])[0][3] != DONE


def test_a_finished_scan_leaves_no_worker_thread_holding_its_tree(tmp_path):
    # Workers left waiting on the queue kept the tracker -- and through it
    # every scan's whole tree -- alive until the app closed.
    _sample_tree(tmp_path)
    before = threading.active_count()

    scanner.scan(str(tmp_path), queue.Queue(), threading.Event(), workers=4)

    assert threading.active_count() == before
