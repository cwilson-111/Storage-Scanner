import csv
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.export import CSV_FIELDS, export_to_file, iter_nodes, write_csv
from storage_scanner.models import Node


def _tree():
    root = Node("/data", "data")
    sub = Node("/data/sub", "sub")
    root.add_file("a.txt", 5)
    sub.add_file("b.txt", 7)
    sub.size, sub.file_count = 7, 1
    root.dirs.append(sub)
    root.size, root.file_count = 12, 2
    return root


def test_iter_nodes_visits_parents_before_children_in_child_order():
    # A folder's children are its subfolders, then its files.
    assert [n.name for n in iter_nodes(_tree())] == ["data", "sub", "b.txt", "a.txt"]


def test_iter_nodes_handles_a_tree_deeper_than_the_recursion_limit():
    root = node = Node("/0", "0")
    depth = sys.getrecursionlimit() + 100

    for i in range(1, depth):
        child = Node(f"/{i}", str(i))
        node.dirs.append(child)
        node = child

    assert sum(1 for _ in iter_nodes(root)) == depth


def test_csv_has_the_header_then_one_row_per_node():
    out = io.StringIO()
    write_csv(_tree(), out)

    rows = list(csv.DictReader(io.StringIO(out.getvalue())))

    assert tuple(rows[0].keys()) == CSV_FIELDS
    assert [(r["name"], r["size"]) for r in rows] == [
        ("data", "12"),
        ("sub", "7"),
        ("b.txt", "7"),
        ("a.txt", "5"),
    ]


def test_export_to_file_writes_json_or_csv(tmp_path):
    export_to_file(_tree(), tmp_path / "out.json", "json")
    export_to_file(_tree(), tmp_path / "out.csv", "csv")

    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["size"] == 12
    # newline="" on write: no blank rows between CSV lines on Windows.
    assert b"\r\r\n" not in (tmp_path / "out.csv").read_bytes()
    assert len((tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()) == 5


def test_export_to_file_rejects_an_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="Unknown export format"):
        export_to_file(_tree(), tmp_path / "out.xml", "xml")

    assert not (tmp_path / "out.xml").exists()
