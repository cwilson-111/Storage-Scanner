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


@pytest.fixture(autouse=True)
def _clear_cluster_size_cache():
    # _get_cluster_size caches per volume root at module scope so a real
    # scan only pays for GetDiskFreeSpaceW once per drive -- but that same
    # persistence means an earlier real call (this test file's own tests,
    # or anything else run in the same process) can silently satisfy a
    # later test's fake ctypes.windll from the cache before the fake ever
    # gets consulted. Clear it before and after every test in this file.
    scanner._cluster_size_cache.clear()
    yield
    scanner._cluster_size_cache.clear()


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
    # Isolated from cluster-size rounding (its own concern, tested below)
    # by mocking _get_cluster_size directly rather than faking
    # GetDiskFreeSpaceW here too.
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=512))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(scanner, "_get_cluster_size", lambda path: None)

    assert _windows_alloc_size(r"C:\file.bin", fallback=4096) == 512


def test_windows_alloc_size_rounds_up_to_the_cluster_size(monkeypatch):
    # The actual fix the Turbo Scan validation gate's alloc_size
    # discrepancy led to: GetCompressedFileSizeW just echoes the logical
    # size back for an ordinary (uncompressed, non-sparse) file, not
    # rounded to a whole cluster the way real NTFS allocation works.
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=10976))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(scanner, "_get_cluster_size", lambda path: 4096)

    assert _windows_alloc_size(r"C:\file.bin", fallback=0) == 12288  # ceil(10976/4096)*4096


def test_windows_alloc_size_already_cluster_aligned_is_unchanged(monkeypatch):
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=8192))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(scanner, "_get_cluster_size", lambda path: 4096)

    assert _windows_alloc_size(r"C:\file.bin", fallback=0) == 8192


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
    monkeypatch.setattr(scanner, "_get_cluster_size", lambda path: None)

    st_info = SimpleNamespace(st_size=4096)
    assert _measure_alloc_size(r"C:\file.bin", st_info) == 256


class _FakeGetDiskFreeSpaceW:
    def __init__(self, sectors_per_cluster, bytes_per_sector, fails=False):
        self.sectors_per_cluster = sectors_per_cluster
        self.bytes_per_sector = bytes_per_sector
        self.fails = fails
        self.calls = []

    def __call__(self, root_path, sectors_ref, bytes_ref, free_ref, total_ref):
        self.calls.append(root_path)
        if self.fails:
            return 0
        ctypes.cast(sectors_ref, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.sectors_per_cluster
        ctypes.cast(bytes_ref, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.bytes_per_sector
        return 1


def test_get_cluster_size_multiplies_sectors_and_bytes_per_sector(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=8, bytes_per_sector=512)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    assert scanner._get_cluster_size(r"C:\some\file.bin") == 4096
    assert fake_disk.calls == ["C:\\"]


def test_get_cluster_size_is_cached_per_volume_root(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=8, bytes_per_sector=512)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    scanner._get_cluster_size(r"C:\a.bin")
    scanner._get_cluster_size(r"C:\b\c.bin")

    assert len(fake_disk.calls) == 1  # second call served from cache, not re-queried


def test_get_cluster_size_returns_none_on_failure(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=0, bytes_per_sector=0, fails=True)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    assert scanner._get_cluster_size(r"C:\a.bin") is None


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
