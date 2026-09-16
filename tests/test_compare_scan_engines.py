import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compare_scan_engines import _compare, _flatten
from storage_scanner.models import Node


def _make_node(path, name, is_dir, size=0, alloc_size=0, file_count=0):
    node = Node(path, name, is_dir)
    node.size = size
    node.alloc_size = alloc_size
    node.file_count = file_count
    return node


def _tree_with_one_file():
    root = _make_node("C:\\Data", "Data", True, size=100, alloc_size=4096, file_count=1)
    child = _make_node("C:\\Data\\a.txt", "a.txt", False, size=100, alloc_size=4096, file_count=1)
    root.children.append(child)
    return root


def test_flatten_includes_every_node_keyed_by_path():
    root = _tree_with_one_file()
    flat = _flatten(root)
    assert set(flat) == {"C:\\Data", "C:\\Data\\a.txt"}
    assert flat["C:\\Data\\a.txt"].size == 100


def test_identical_trees_produce_no_discrepancies():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    assert _compare(compatible, turbo) == []


def test_size_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    _flatten(turbo)["C:\\Data\\a.txt"].size = 999

    discrepancies = _compare(compatible, turbo)

    assert len(discrepancies) == 1
    assert "size MISMATCH" in discrepancies[0]
    assert "C:\\Data\\a.txt" in discrepancies[0]
    assert "compatible=100" in discrepancies[0]
    assert "turbo=999" in discrepancies[0]


def test_node_missing_from_turbo_is_reported():
    compatible = _tree_with_one_file()
    turbo = _make_node("C:\\Data", "Data", True)  # no children at all

    discrepancies = _compare(compatible, turbo)

    assert any("MISSING FROM TURBO: C:\\Data\\a.txt" in d for d in discrepancies)


def test_extra_node_in_turbo_is_reported():
    compatible = _make_node("C:\\Data", "Data", True)  # no children
    turbo = _tree_with_one_file()

    discrepancies = _compare(compatible, turbo)

    assert any("EXTRA IN TURBO: C:\\Data\\a.txt" in d for d in discrepancies)


def test_multiple_field_mismatches_on_one_node_are_each_reported():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    turbo_child = _flatten(turbo)["C:\\Data\\a.txt"]
    turbo_child.size = 999
    turbo_child.alloc_size = 8192

    discrepancies = _compare(compatible, turbo)

    fields_reported = {d.split(" MISMATCH")[0] for d in discrepancies}
    assert fields_reported == {"size", "alloc_size"}


def test_is_dir_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    _flatten(turbo)["C:\\Data\\a.txt"].is_dir = True

    discrepancies = _compare(compatible, turbo)

    assert any("is_dir MISMATCH" in d for d in discrepancies)
