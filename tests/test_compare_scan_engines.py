import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compare_scan_engines import _compare, _flatten
from storage_scanner.models import Node

FILE_PATH = os.path.join("C:\\Data", "a.txt")


def _root(size=0, alloc_size=0, file_count=0):
    node = Node("C:\\Data", "Data")
    node.size = size
    node.alloc_size = alloc_size
    node.file_count = file_count
    return node


def _tree_with_one_file(size=100, alloc_size=4096):
    root = _root(size=size, alloc_size=alloc_size, file_count=1)
    root.add_file("a.txt", size, alloc_size)
    return root


def test_flatten_includes_every_node_keyed_by_path():
    root = _tree_with_one_file()
    flat = _flatten(root)
    assert set(flat) == {"C:\\Data", FILE_PATH}
    assert flat[FILE_PATH].size == 100


def test_identical_trees_produce_no_discrepancies():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    assert _compare(compatible, turbo) == []


def test_size_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _tree_with_one_file()
    turbo.file_sizes[0] = 999

    discrepancies = _compare(compatible, turbo)

    assert len(discrepancies) == 1
    assert "size MISMATCH" in discrepancies[0]
    assert FILE_PATH in discrepancies[0]
    assert "compatible=100" in discrepancies[0]
    assert "turbo=999" in discrepancies[0]


def test_node_missing_from_turbo_is_reported():
    compatible = _tree_with_one_file()
    turbo = _root()  # no children at all

    discrepancies = _compare(compatible, turbo)

    assert any(f"MISSING FROM TURBO: {FILE_PATH}" in d for d in discrepancies)


def test_extra_node_in_turbo_is_reported():
    compatible = _root()  # no children
    turbo = _tree_with_one_file()

    discrepancies = _compare(compatible, turbo)

    assert any(f"EXTRA IN TURBO: {FILE_PATH}" in d for d in discrepancies)


def test_multiple_field_mismatches_on_one_node_are_each_reported():
    compatible = _tree_with_one_file()
    turbo = _root(size=100, alloc_size=4096, file_count=1)
    turbo.add_file("a.txt", 999, 8192)

    discrepancies = _compare(compatible, turbo)

    fields_reported = {d.split(" MISMATCH")[0] for d in discrepancies}
    assert fields_reported == {"size", "alloc_size"}


def test_is_dir_mismatch_is_reported():
    compatible = _tree_with_one_file()
    turbo = _root(size=100, alloc_size=4096, file_count=1)
    folder = Node(FILE_PATH, "a.txt")  # the same path, read as a folder
    folder.size, folder.alloc_size, folder.file_count = 100, 4096, 1
    turbo.dirs.append(folder)

    discrepancies = _compare(compatible, turbo)

    assert any("is_dir MISMATCH" in d for d in discrepancies)
