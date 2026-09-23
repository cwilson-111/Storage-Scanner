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
    root = Node("/data", "data", True)
    sub = Node("/data/sub", "sub", True)
    a = Node("/data/a.txt", "a.txt", False)
    b = Node("/data/sub/b.txt", "b.txt", False)
    a.size, b.size = 5, 7
    sub.children = [b]
    sub.size, sub.file_count = 7, 1
    root.children = [a, sub]
    root.size, root.file_count = 12, 2
    return root


def test_iter_nodes_visits_parents_before_children_in_child_order():
    assert [n.name for n in iter_nodes(_tree())] == ["data", "a.txt", "sub", "b.txt"]


def test_iter_nodes_handles_a_tree_deeper_than_the_recursion_limit():
    root = node = Node("/0", "0", True)
    depth = sys.getrecursionlimit() + 100

    for i in range(1, depth):
        child = Node(f"/{i}", str(i), True)
        node.children = [child]
        node = child

    assert sum(1 for _ in iter_nodes(root)) == depth


def test_csv_has_the_header_then_one_row_per_node():
    out = io.StringIO()
    write_csv(_tree(), out)

    rows = list(csv.DictReader(io.StringIO(out.getvalue())))

    assert tuple(rows[0].keys()) == CSV_FIELDS
    assert [(r["name"], r["size"]) for r in rows] == [
        ("data", "12"), ("a.txt", "5"), ("sub", "7"), ("b.txt", "7"),
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
