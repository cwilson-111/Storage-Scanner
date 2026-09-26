import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import (
    FLAG_CLOUD_PLACEHOLDER,
    FLAG_HARDLINK_DUP,
    FLAG_LINK,
    Node,
    detached_file,
)
from storage_scanner.serialization import dict_to_node, node_to_dict


def _build_tree():
    root = Node("/root", "root")
    sub = Node("/root/sub", "sub")
    sub.mtime = 789.0
    sub.add_file("inner.bin", 7, 4096)
    sub.size, sub.alloc_size, sub.file_count = 7, 4096, 1
    root.dirs.append(sub)
    root.add_file(
        "file.txt",
        123,
        4096,
        mtime=456.0,
        flags=FLAG_LINK | FLAG_HARDLINK_DUP | FLAG_CLOUD_PLACEHOLDER,
    )
    root.size = 130
    root.alloc_size = 8192
    root.file_count = 2
    root.error = True
    return root


def _fields(node):
    return (
        node.path,
        node.name,
        node.is_dir,
        node.size,
        node.alloc_size,
        node.file_count,
        node.mtime,
        node.error,
        node.is_link,
        node.hardlink_dup,
        node.is_cloud_placeholder,
    )


def test_round_trip_preserves_all_fields():
    root = _build_tree()
    restored = dict_to_node(node_to_dict(root))

    assert _fields(restored) == _fields(root)
    assert [_fields(c) for c in restored.children] == [_fields(c) for c in root.children]
    assert [_fields(c) for c in restored.dirs[0].children] == [
        _fields(c) for c in root.dirs[0].children
    ]


def test_a_single_file_scan_round_trips_as_a_file():
    node = detached_file("/root/only.bin", size=5, alloc_size=4096, flags=FLAG_LINK)

    restored = dict_to_node(node_to_dict(node))

    assert _fields(restored) == _fields(node)


def test_node_to_dict_is_json_serializable():
    import json

    root = _build_tree()
    # Must not raise: every value node_to_dict produces has to be a plain
    # JSON type, since this crosses process boundaries as JSON text.
    json.dumps(node_to_dict(root))
