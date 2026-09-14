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
    app.dup_stats = {
        "files_total": 0, "files_checked": 0, "files_skipped": 0,
        "bytes_skipped": 0, "partial_hashed": 0, "full_hashed": 0,
    }
    return app


def test_cloud_placeholder_excluded_from_duplicate_hashing(tmp_path):
    app = _make_app(tmp_path)
    cancel_event = threading.Event()

    duplicates = app._find_duplicate_files(cancel_event=cancel_event)

    assert len(duplicates) == 1
    _size, _digest, nodes = duplicates[0]
    names = {node.name for node in nodes}
    # keep1/keep2 are genuine duplicates and get reported; cloud.bin has the
    # same content but is a placeholder, so it must never be opened/hashed
    # (that would force it to download) and must not appear in the group.
    assert names == {"keep1.bin", "keep2.bin"}
    assert app.dup_stats["files_skipped"] == 1
