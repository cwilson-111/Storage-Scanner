"""Tests for storage_scanner.live_tree_model: what a main-tree row shows
from a folder's running totals and scan state, and how a level re-sorts
while those totals grow.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.live_tree_model import (
    QUEUED_ICON,
    SCANNING_ICON,
    node_display,
    resorted,
    row_display,
)
from storage_scanner.models import FLAG_CLOUD_PLACEHOLDER, FileNode, Node
from storage_scanner.scan_progress import DONE, QUEUED, SCANNING

GB = 1024**3


def _folder(name="Docs", size=0, alloc_size=0, file_count=0, folder_count=0):
    node = Node(f"C:\\{name}", name)
    node.size, node.alloc_size = size, alloc_size
    node.file_count, node.folder_count = file_count, folder_count
    return node


def test_a_queued_folder_is_greyed_with_nothing_to_show_yet():
    row = row_display(_folder(), 0, 0, 0, 0, parent_size=10 * GB, state=QUEUED)

    assert row.text == f"{QUEUED_ICON} Docs\\"
    assert row.values == ("…", "…", "—", "…", "…", "", "")
    assert row.tags == ("placeholder", "dir")
    assert row.heat is None


def test_a_folder_being_read_shows_its_totals_so_far_as_a_share_of_its_parent_so_far():
    row = row_display(_folder(), 3 * GB, 2 * GB, 1234, 56, parent_size=12 * GB, state=SCANNING)

    assert row.text == f"{SCANNING_ICON} Docs\\"
    size, on_disk, percent, files, folders, _modified, _accessed = row.values
    assert (size, on_disk, files, folders) == ("3.0 GB", "2.0 GB", "1,234", "56")
    assert percent.endswith(" 25.0%")
    assert row.heat == 0.25
    assert row.tags == ("dir",)


def test_a_done_folder_looks_exactly_like_it_will_once_the_scan_finishes():
    folder = _folder(size=3 * GB, alloc_size=2 * GB, file_count=1234, folder_count=56)

    live = row_display(folder, 3 * GB, 2 * GB, 1234, 56, parent_size=12 * GB, state=DONE)

    assert live == node_display(folder, parent_size=12 * GB)
    assert live.text == "📁 Docs\\"


def test_a_file_row_is_final_but_its_share_follows_its_growing_folder():
    folder = _folder()
    index = folder.add_file("movie.mkv", 2 * GB, 2 * GB, flags=0)
    movie = FileNode(folder, index)

    early = row_display(movie, movie.size, movie.alloc_size, 1, 0, parent_size=4 * GB)
    later = row_display(movie, movie.size, movie.alloc_size, 1, 0, parent_size=8 * GB)

    assert early.values[:2] == later.values[:2] == ("2.0 GB", "2.0 GB")
    assert early.values[3:5] == ("", "") and early.text == "📄 movie.mkv"
    assert (early.heat, later.heat) == (0.5, 0.25)


def test_error_and_cloud_rows_keep_their_marks_and_no_heat_while_scanning():
    broken = _folder("Locked")
    broken.error = True
    online = _folder("OneDrive")
    online.is_cloud_placeholder = True

    locked_row = row_display(broken, 0, 0, 0, 0, parent_size=GB, state=DONE)
    cloud_row = row_display(online, GB, 0, 3, 0, parent_size=GB, state=SCANNING)

    assert (locked_row.text, locked_row.tags, locked_row.heat) == ("⚠ Locked\\", ("error",), None)
    assert cloud_row.tags == ("cloud",) and cloud_row.heat is None
    assert cloud_row.text.startswith(SCANNING_ICON)
    assert cloud_row.values[1] == "0 B (online-only)"
    holder = _folder()
    online_file = FileNode(holder, holder.add_file("a.zip", GB, 0, flags=FLAG_CLOUD_PLACEHOLDER))
    assert row_display(online_file, GB, 0, 1, 0, parent_size=GB).values[1] == "0 B (online-only)"


def test_a_level_resorts_by_its_totals_so_far_and_ties_keep_their_place():
    nodes = {
        "a": _folder("a", size=5),
        "b": _folder("b", size=0),
        "c": _folder("c", size=9),
        "d": _folder("d", size=0),
        "e": _folder("e", size=5),
    }
    shown = ["b", "a", "d", "e", "c"]

    assert resorted(shown, nodes, "size", reverse=True) == ["c", "a", "e", "b", "d"]
    nodes["e"].size = 6  # e grew past a
    assert resorted(["c", "a", "e", "b", "d"], nodes, "size", reverse=True) == [
        "c",
        "e",
        "a",
        "b",
        "d",
    ]
    assert resorted(shown, nodes, "name", reverse=False) == ["a", "b", "c", "d", "e"]


def test_owners_sort_a_to_z_ignoring_case_with_unknown_and_unreadable_ones_last():
    nodes = {name: _folder(name) for name in "abcde"}
    owners = {"a": "BUILTIN\\Users", "b": None, "c": "builtin\\administrators", "d": "", "e": "Zed"}

    def owner_of(node):
        return owners[node.name]

    shown = ["b", "a", "d", "e", "c"]
    assert resorted(shown, nodes, "owner", reverse=False, owner_of=owner_of) == [
        "c",
        "a",
        "e",
        "b",
        "d",
    ]
    assert resorted(shown, nodes, "owner", reverse=True, owner_of=owner_of)[:2] == ["b", "d"]
