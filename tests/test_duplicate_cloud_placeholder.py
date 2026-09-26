import queue
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import Node
from storage_scanner.ui.duplicate_window import DuplicatesMixin


def _make_app(tmp_path, contents=b"identical content"):
    root = Node(str(tmp_path), tmp_path.name, is_dir=True)

    keep1 = tmp_path / "keep1.bin"
    keep1.write_bytes(contents)
    keep2 = tmp_path / "keep2.bin"
    keep2.write_bytes(contents)
    cloud = tmp_path / "cloud.bin"
    cloud.write_bytes(contents)  # same content, but flagged as a placeholder

    for name, path, is_placeholder in [
        ("keep1.bin", keep1, False),
        ("keep2.bin", keep2, False),
        ("cloud.bin", cloud, True),
    ]:
        node = Node(str(path), name, is_dir=False)
        node.size = len(contents)
        node.is_cloud_placeholder = is_placeholder
        root.children.append(node)
    root.size = len(contents) * 3

    app = DuplicatesMixin()
    app.root_node = root
    # pytest's tmp_path lives under /private/var, which is in the real
    # macOS exclude list — irrelevant to what this test is checking, so
    # disable it to isolate the cloud-placeholder skip specifically.
    app._should_skip_duplicate_scan = lambda path: False
    return app


def _latest_stats(progress_q):
    """The most recently posted ("stats", dict) message -- matches how
    _poll_duplicate_progress picks up stats in the real app: by reading
    posted snapshots off progress_q, never by reading back a mutated
    shared dict (see _find_duplicate_files's own local `stats` counter,
    kept independent of any caller's state so two concurrent callers -
    show_duplicates()'s own worker and CleanupMixin's separate duplicate
    scan - can't race or cross-contaminate each other)."""
    latest = None
    while True:
        try:
            msg = progress_q.get_nowait()
        except queue.Empty:
            break
        if msg[0] == "stats":
            latest = msg[1]
    return latest


def test_cloud_placeholder_excluded_from_duplicate_hashing(tmp_path):
    app = _make_app(tmp_path)
    cancel_event = threading.Event()
    progress_q = queue.Queue()

    duplicates = app._find_duplicate_files(progress_q=progress_q, cancel_event=cancel_event)

    assert len(duplicates) == 1
    _size, _digest, nodes = duplicates[0]
    names = {node.name for node in nodes}
    # keep1/keep2 are genuine duplicates and get reported; cloud.bin has the
    # same content but is a placeholder, so it must never be opened/hashed
    # (that would force it to download) and must not appear in the group.
    assert names == {"keep1.bin", "keep2.bin"}
    assert _latest_stats(progress_q)["files_skipped"] == 1


def test_two_concurrent_scans_do_not_cross_contaminate_stats(tmp_path):
    """Simulates show_duplicates()'s Duplicate Files window staying open
    (its dup_stats already showing a prior run's totals) while
    CleanupMixin's own background duplicate scan runs its own,
    independent call -- the second call must never see or mutate the
    first's numbers, in either direction."""
    app = _make_app(tmp_path)
    app.dup_stats = {  # as if a Duplicate Files window's earlier run left this
        "files_total": 999,
        "files_checked": 999,
        "files_skipped": 42,
        "bytes_skipped": 12345,
        "partial_hashed": 999,
        "middle_hashed": 999,
    }
    before = dict(app.dup_stats)

    app._find_duplicate_files(cancel_event=threading.Event())  # cleanup_window.py's own call shape

    assert app.dup_stats == before  # untouched by the second, independent call


def test_stats_are_not_exposed_via_a_shared_attribute(tmp_path):
    """_find_duplicate_files must never mutate a caller's own state as a
    side effect -- it only ever reports stats through progress_q, which is
    optional. CleanupMixin's own duplicate-candidate scan (cleanup_window.
    py) calls this with no progress_q, from a call site that has nothing
    to do with show_duplicates()'s dup_stats attribute at all."""
    app = _make_app(tmp_path)
    cancel_event = threading.Event()

    app._find_duplicate_files(cancel_event=cancel_event)  # no progress_q

    assert not hasattr(app, "dup_stats")
