import ctypes
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import scanner
from storage_scanner.scanner import _measure_alloc_size, _windows_alloc_size, scan


def _run_scan(path):
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    return scan(str(path), progress_q, cancel_event)


def _by_name(node):
    return {child.name: child for child in node.children}


def test_alloc_size_matches_real_disk_usage_via_st_blocks(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 1000)

    root = _run_scan(tmp_path)
    child = _by_name(root)["a.bin"]

    expected = os.stat(f).st_blocks * 512
    assert child.alloc_size == expected
    # A small file still occupies at least one filesystem block, which is
    # usually >= its logical size — the two aren't expected to match exactly.
    assert child.alloc_size > 0


def test_hardlink_dedup_zeroes_alloc_size_too(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"x" * 1000)
    linked = tmp_path / "linked.bin"
    os.link(original, linked)

    root = _run_scan(tmp_path)
    children = _by_name(root)

    alloc_values = sorted(c.alloc_size for c in children.values())
    # One of the two hard links gets zeroed (same physical blocks, already
    # counted via the other name); the other reports the real usage.
    assert alloc_values[0] == 0
    assert alloc_values[1] > 0
    # Rolled up to the parent dir, the shared blocks are counted exactly once.
    assert root.alloc_size == alloc_values[1]


def test_measure_alloc_size_falls_back_to_logical_size_without_st_blocks():
    st_info = SimpleNamespace(st_size=12345)  # no st_blocks attribute
    assert _measure_alloc_size("/some/path", st_info) == 12345


class _FakeKernel32:
    def __init__(self, low, last_error=0):
        self.low = low
        self.last_error = last_error

    def GetCompressedFileSizeW(self, path, high_out):
        return self.low


def test_windows_alloc_size_returns_compressed_size(monkeypatch):
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=512))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)

    assert _windows_alloc_size(r"C:\file.bin", fallback=4096) == 512


def test_windows_alloc_size_falls_back_on_invalid_file_size(monkeypatch):
    fake_windll = SimpleNamespace(
        kernel32=_FakeKernel32(low=0xFFFFFFFF)
    )
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 5, raising=False)

    assert _windows_alloc_size(r"C:\file.bin", fallback=999) == 999


def test_measure_alloc_size_dispatches_to_windows_path_when_flagged(monkeypatch):
    monkeypatch.setattr(scanner, "_IS_WINDOWS", True)
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=256))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)

    st_info = SimpleNamespace(st_size=4096)
    assert _measure_alloc_size(r"C:\file.bin", st_info) == 256


@pytest.mark.parametrize(
    "attrs,expected",
    [
        (0, False),
        (0x00040000, True),   # FILE_ATTRIBUTE_RECALL_ON_OPEN
        (0x00400000, True),   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        (0x00001000, True),   # FILE_ATTRIBUTE_OFFLINE
        (0x00000020, False),  # FILE_ATTRIBUTE_ARCHIVE - unrelated bit
    ],
)
def test_cloud_placeholder_attribute_bits(attrs, expected):
    assert bool(attrs & scanner._CLOUD_PLACEHOLDER_ATTRS) is expected
