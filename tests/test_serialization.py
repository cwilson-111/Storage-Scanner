import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import Node
from storage_scanner.serialization import dict_to_node, node_to_dict


def _build_tree():
    root = Node("/root", "root", is_dir=True)
    child = Node("/root/file.txt", "file.txt", is_dir=False)
    child.size = 123
    child.file_count = 1
    child.mtime = 456.0
    child.is_link = True
    child.hardlink_dup = True
    child.alloc_size = 4096
    child.is_cloud_placeholder = True
    root.children.append(child)
    root.size = 123
    root.alloc_size = 4096
    root.file_count = 1
    root.error = True
    return root


def test_round_trip_preserves_all_fields():
    root = _build_tree()
    restored = dict_to_node(node_to_dict(root))

    assert restored.path == root.path
    assert restored.name == root.name
    assert restored.is_dir == root.is_dir
    assert restored.size == root.size
    assert restored.file_count == root.file_count
    assert restored.error == root.error

    child = root.children[0]
    restored_child = restored.children[0]
    assert restored_child.path == child.path
    assert restored_child.size == child.size
    assert restored_child.mtime == child.mtime
    assert restored_child.is_link == child.is_link
    assert restored_child.hardlink_dup == child.hardlink_dup
    assert restored_child.alloc_size == child.alloc_size
    assert restored_child.is_cloud_placeholder == child.is_cloud_placeholder


def test_node_to_dict_is_json_serializable():
    import json

    root = _build_tree()
    # Must not raise: every value node_to_dict produces has to be a plain
    # JSON type, since this crosses process boundaries as JSON text.
    json.dumps(node_to_dict(root))
