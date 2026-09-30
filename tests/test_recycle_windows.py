"""recycle_windows against the real Recycle Bin and a real subst drive
(P0-1, P0-2): what the bin can't hold is refused before anything is
deleted, and what it can hold is found in it afterwards. Audit rows go to a
list, never the real history database; whatever reaches the bin is taken
out of it again."""

import os
import string
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import recycle_windows
from storage_scanner.cleanup_cache import CachedNode
from storage_scanner.delete_outcome import RECYCLED, REFUSED
from storage_scanner.delete_service import DeleteRequest, DeleteService

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the Windows Recycle Bin")


class _Decline:
    """Answers No to "delete permanently?"."""

    def __init__(self):
        self.asked = []

    def confirm_permanent(self, node, reasons):
        self.asked.append((node.path, reasons))
        return False

    def confirm_large_folder(self, node):
        return False


def _write(path, data=b"x"):
    """Create `path` (and its folders) whatever its length."""
    full = recycle_windows.extended_path(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return path


def _delete(path, is_dir=False, confirmer=None):
    """One delete through the real service, as the main tree would ask."""
    size = 0 if is_dir else os.stat(recycle_windows.extended_path(path)).st_size
    node = CachedNode(path, os.path.basename(path), is_dir, size)
    records = []
    service = DeleteService(record=lambda **row: records.append(row))
    [result] = service.delete([DeleteRequest(node, "Main tree")], confirmer or _Decline())
    return result, records


def _deep_path(base, length):
    """A file path under `base` at least `length` characters long."""
    path = str(base)
    while len(path) < length - 10:
        path = os.path.join(path, "d" * 40)
    return os.path.join(path, "deep.txt")


@pytest.fixture
def subst_drive(tmp_path):
    """A free drive letter substituted for a temp folder, removed after."""
    target = tmp_path / "substituted"
    target.mkdir()
    free = [c for c in reversed(string.ascii_uppercase[3:]) if not os.path.exists(f"{c}:\\")]
    if not free:
        pytest.skip("no free drive letter for subst")
    drive = f"{free[0]}:"
    subprocess.run(["subst", drive, str(target)], check=True, capture_output=True)
    try:
        yield drive, target
    finally:
        subprocess.run(["subst", drive, "/D"], capture_output=True)


def test_a_file_on_a_subst_drive_is_refused_and_left_on_disk(subst_drive):
    drive, target = subst_drive
    path = _write(f"{drive}\\doomed.txt")

    result, records = _delete(path)

    assert result.outcome == REFUSED
    assert "subst drive" in result.message
    assert (target / "doomed.txt").exists()
    assert [row["outcome"] for row in records] == [REFUSED]


def test_a_path_over_260_characters_is_refused_and_left_on_disk(tmp_path):
    path = _write(_deep_path(tmp_path, 300))
    confirmer = _Decline()

    result, records = _delete(path, confirmer=confirmer)

    assert result.outcome == REFUSED
    [(_asked_path, reasons)] = confirmer.asked
    assert "characters long" in reasons[0]
    assert recycle_windows.exists(path)
    assert records[0]["outcome"] == REFUSED


def test_a_short_folder_holding_a_deep_file_is_refused(tmp_path):
    folder = tmp_path / "node_modules"
    _write(_deep_path(folder, 280))

    blockers = recycle_windows.bin_blockers(str(folder), is_dir=True)

    assert any("holds a path" in reason for reason in blockers), blockers


def test_an_ordinary_file_lands_in_the_recycle_bin(tmp_path):
    path = _write(str(tmp_path / "ordinary.txt"))
    root = recycle_windows.volume_root(path)
    if recycle_windows.bin_blockers(path, is_dir=False):
        pytest.skip(f"this machine's Recycle Bin can't take files from {root}")

    try:
        result, records = _delete(path)

        assert result.outcome == RECYCLED
        assert not os.path.exists(path)
        assert records[0]["outcome"] == RECYCLED
    finally:
        key = os.path.normcase(path)
        for original, info, item in recycle_windows.recycled_entries(root) or []:
            if os.path.normcase(original) == key:
                for leftover in (item, info):
                    if os.path.exists(leftover):
                        os.remove(leftover)
