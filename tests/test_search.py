import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.models import Node
from storage_scanner.search import filter_nodes, parse_size


def _file(parent, name, size=0, mtime=0.0):
    node = Node(f"{parent.path}/{name}", name, is_dir=False)
    node.size = size
    node.mtime = mtime
    parent.children.append(node)
    return node


def _dir(parent, name):
    node = Node(f"{parent.path}/{name}", name, is_dir=True)
    parent.children.append(node)
    return node


@pytest.fixture
def tree():
    root = Node("/root", "root", is_dir=True)
    sub = _dir(root, "sub")
    _file(root, "report.pdf", size=1000, mtime=100)
    _file(root, "photo.JPG", size=5_000_000, mtime=200)
    _file(sub, "notes.txt", size=50, mtime=300)
    _file(sub, "archive.zip", size=10_000_000, mtime=400)
    return root


def test_name_query_is_case_insensitive(tree):
    results = filter_nodes(tree, name_query="PHOTO")
    assert [n.name for n in results] == ["photo.JPG"]


def test_extension_filter_matches_case_insensitively_and_skips_dirs(tree):
    results = filter_nodes(tree, extensions=["jpg"])
    assert [n.name for n in results] == ["photo.JPG"]


def test_size_range_filter(tree):
    results = filter_nodes(tree, min_size=1000, max_size=5_000_000)
    assert {n.name for n in results} == {"report.pdf", "photo.JPG"}


def test_mtime_range_filter(tree):
    results = filter_nodes(tree, mtime_after=150, mtime_before=350)
    assert {n.name for n in results} == {"photo.JPG", "notes.txt"}


def test_include_dirs_false_excludes_directories(tree):
    results = filter_nodes(tree, include_dirs=False)
    assert all(not n.is_dir for n in results)


def test_include_files_false_excludes_files(tree):
    results = filter_nodes(tree, include_files=False)
    assert [n.name for n in results] == ["sub"]


def test_root_itself_is_never_included(tree):
    results = filter_nodes(tree)
    assert tree not in results


def test_combined_filters_use_and_logic(tree):
    results = filter_nodes(tree, name_query="a", min_size=5_000_000)
    # "archive.zip" and "photo.JPG" both contain no "a"... recheck: archive has 'a'
    assert [n.name for n in results] == ["archive.zip"]


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("100", 100),
        ("1kb", 1024),
        ("1 KB", 1024),
        ("2.5mb", int(2.5 * 1024 ** 2)),
        ("1gb", 1024 ** 3),
        ("1tb", 1024 ** 4),
    ],
)
def test_parse_size_valid(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["abc", "5 furlongs", "-"])
def test_parse_size_invalid_raises(text):
    with pytest.raises(ValueError):
        parse_size(text)
