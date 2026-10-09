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

from storage_scanner import alloc_size
from storage_scanner.alloc_size import _measure_alloc_size, _windows_alloc_size
from storage_scanner.scanner import scan


@pytest.fixture(autouse=True)
def _clear_cluster_size_cache():
    # _get_cluster_size caches per volume root at module scope so a real
    # scan only pays for GetDiskFreeSpaceW once per drive -- but that same
    # persistence means an earlier real call (this test file's own tests,
    # or anything else run in the same process) can silently satisfy a
    # later test's fake ctypes.windll from the cache before the fake ever
    # gets consulted. Clear it before and after every test in this file.
    alloc_size._cluster_size_cache.clear()
    yield
    alloc_size._cluster_size_cache.clear()


def _run_scan(path):
    progress_q = queue.Queue()
    cancel_event = threading.Event()
    return scan(str(path), progress_q, cancel_event)


def _by_name(node):
    return {child.name: child for child in node.children}


@pytest.mark.skipif(
    not hasattr(os.stat_result, "st_blocks"),
    reason="st_blocks doesn't exist on this platform (e.g. Windows) -- "
    "_measure_alloc_size's own Windows path is covered separately "
    "by the test_windows_alloc_size_* tests below.",
)
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
    def __init__(self, low, last_error=0, high=0):
        self.low = low
        self.last_error = last_error
        self.high = high

    def GetCompressedFileSizeW(self, path, high_out):
        if high_out is not None:
            ctypes.cast(high_out, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.high
        return self.low


def test_windows_alloc_size_returns_compressed_size(monkeypatch):
    # Isolated from cluster-size rounding (its own concern, tested below)
    # by mocking _get_cluster_size directly rather than faking
    # GetDiskFreeSpaceW here too.
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=512))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(alloc_size, "_get_cluster_size", lambda path: None)

    assert _windows_alloc_size(r"C:\file.bin", fallback=4096) == 512


def test_windows_alloc_size_rounds_up_to_the_cluster_size(monkeypatch):
    # The actual fix the Turbo Scan validation gate's alloc_size
    # discrepancy led to: GetCompressedFileSizeW just echoes the logical
    # size back for an ordinary (uncompressed, non-sparse) file, not
    # rounded to a whole cluster the way real NTFS allocation works.
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=10976))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(alloc_size, "_get_cluster_size", lambda path: 4096)

    assert _windows_alloc_size(r"C:\file.bin", fallback=0) == 12288  # ceil(10976/4096)*4096


def test_windows_alloc_size_already_cluster_aligned_is_unchanged(monkeypatch):
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=8192))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(alloc_size, "_get_cluster_size", lambda path: 4096)

    assert _windows_alloc_size(r"C:\file.bin", fallback=0) == 8192


def test_windows_alloc_size_keeps_the_upper_32_bits_of_a_file_over_4_gib(monkeypatch):
    # A real 9,048,948,736-byte game archive (Black Ops III's base.xpak):
    # reading only the low DWORD billed it 459,014,144 bytes on disk.
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=459_014_144, high=2))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(alloc_size, "_get_cluster_size", lambda path: 4096)

    assert _windows_alloc_size(r"C:\base.xpak", fallback=9_048_948_736) == 9_048_948_736


def test_windows_alloc_size_falls_back_on_invalid_file_size(monkeypatch):
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=0xFFFFFFFF))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 5, raising=False)

    assert _windows_alloc_size(r"C:\file.bin", fallback=999) == 999


def test_windows_alloc_size_falls_back_when_ctypes_returns_the_signed_sentinel(monkeypatch):
    """A real (non-test-faked) ctypes windll call defaults to a signed
    32-bit return type, so the real Win32 failure sentinel 0xFFFFFFFF
    actually comes back as -1, not as 0xFFFFFFFF -- unlike every other
    fake in this file, which returns the sentinel as a plain positive int
    and so never would have caught this. Confirmed via a real windows-
    latest CI run: without the `& 0xFFFFFFFF` mask in _windows_alloc_size,
    this -1 fell through the fallback check entirely and got rounded
    against the cluster size into a bogus 0 instead of `fallback`."""
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=-1))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 5, raising=False)

    assert _windows_alloc_size(r"C:\file.bin", fallback=999) == 999


def test_measure_alloc_size_dispatches_to_windows_path_when_flagged(monkeypatch):
    monkeypatch.setattr(alloc_size, "_IS_WINDOWS", True)
    fake_windll = SimpleNamespace(kernel32=_FakeKernel32(low=256))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 0, raising=False)
    monkeypatch.setattr(alloc_size, "_get_cluster_size", lambda path: None)

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
        ctypes.cast(sectors_ref, ctypes.POINTER(ctypes.c_ulong)).contents.value = (
            self.sectors_per_cluster
        )
        ctypes.cast(bytes_ref, ctypes.POINTER(ctypes.c_ulong)).contents.value = (
            self.bytes_per_sector
        )
        return 1


@pytest.mark.windows
def test_get_cluster_size_multiplies_sectors_and_bytes_per_sector(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=8, bytes_per_sector=512)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    assert alloc_size._get_cluster_size(r"C:\some\file.bin") == 4096
    assert fake_disk.calls == ["C:\\"]


@pytest.mark.windows
def test_get_cluster_size_is_cached_per_volume_root(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=8, bytes_per_sector=512)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    alloc_size._get_cluster_size(r"C:\a.bin")
    alloc_size._get_cluster_size(r"C:\b\c.bin")

    assert len(fake_disk.calls) == 1  # second call served from cache, not re-queried


def test_get_cluster_size_returns_none_on_failure(monkeypatch):
    fake_disk = _FakeGetDiskFreeSpaceW(sectors_per_cluster=0, bytes_per_sector=0, fails=True)
    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetDiskFreeSpaceW=fake_disk))
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    assert alloc_size._get_cluster_size(r"C:\a.bin") is None


@pytest.mark.parametrize(
    "attrs,expected",
    [
        (0, False),
        (0x00040000, True),  # FILE_ATTRIBUTE_RECALL_ON_OPEN
        (0x00400000, True),  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        (0x00001000, True),  # FILE_ATTRIBUTE_OFFLINE
        (0x00000020, False),  # FILE_ATTRIBUTE_ARCHIVE - unrelated bit
    ],
)
def test_cloud_placeholder_attribute_bits(attrs, expected):
    assert bool(attrs & alloc_size._CLOUD_PLACEHOLDER_ATTRS) is expected


_REPARSE_POINT = 0x00000400
_RECALL_ON_OPEN = 0x00040000
_RECALL_ON_DATA_ACCESS = 0x00400000
_OFFLINE = 0x00001000
_ARCHIVE = 0x00000020


@pytest.mark.parametrize(
    "attrs,expected",
    [
        (0, False),
        (_ARCHIVE, False),
        (_REPARSE_POINT, False),  # a plain reparse point alone isn't a placeholder
        # The real bug this covers: CompactOS/WIMBoot-compressed system
        # files set RECALL_ON_OPEN on-disk ("decompress from the WIM on
        # open") without ever being a reparse point -- confirmed on a real
        # machine, where thousands of ordinary C:\Windows\Boot files raw-
        # MFT-read this way and are NOT cloud placeholders.
        (_ARCHIVE | _RECALL_ON_OPEN, False),
        (_ARCHIVE | _OFFLINE, False),
        # A real OneDrive file not downloaded here, as os.stat/os.scandir
        # list it: the cloud filter hides its reparse bit (Turbo Scan, which
        # reads the MFT, saw it as a placeholder; checklist run 4).
        (_ARCHIVE | _RECALL_ON_DATA_ACCESS, True),
        # As the MFT holds a cloud placeholder (IO_REPARSE_TAG_CLOUD).
        (_ARCHIVE | _REPARSE_POINT | _RECALL_ON_OPEN, True),
        (_ARCHIVE | _REPARSE_POINT | _RECALL_ON_DATA_ACCESS, True),
        (_ARCHIVE | _REPARSE_POINT | _OFFLINE, True),
    ],
)
def test_is_cloud_placeholder_attrs(attrs, expected):
    assert alloc_size.is_cloud_placeholder_attrs(attrs) is expected


def _sparse_file(path, size, data_at=0, data=b""):
    """A sparse file of `size` bytes holding `data` at `data_at`, every other
    byte a hole. The holes are punched (FSCTL_SET_ZERO_DATA) after writing:
    how much NTFS allocates around a cached write past a sparse file's valid
    data varies by machine (7 units here, 453 on a CI runner), so writing
    alone doesn't make them reliably empty."""
    import msvcrt
    from ctypes import wintypes

    with open(path, "wb") as f:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
        returned = wintypes.DWORD()
        assert ctypes.windll.kernel32.DeviceIoControl(
            handle,
            0x000900C4,  # FSCTL_SET_SPARSE
            None,
            0,
            None,
            0,
            ctypes.byref(returned),
            None,
        )
        f.seek(data_at)
        f.write(data)
        f.truncate(size)
        f.flush()
        for start, end in ((0, data_at), (data_at + len(data), size)):
            if start >= end:
                continue
            zero = (ctypes.c_longlong * 2)(start, end)  # FILE_ZERO_DATA_INFORMATION
            assert ctypes.windll.kernel32.DeviceIoControl(
                handle,
                0x000980C8,  # FSCTL_SET_ZERO_DATA
                ctypes.byref(zero),
                ctypes.sizeof(zero),
                None,
                0,
                ctypes.byref(returned),
                None,
            )


@pytest.mark.windows
def test_a_sparse_file_is_billed_the_clusters_ntfs_gave_it(tmp_path):
    # NTFS gives a sparse file whole compression units (64 KiB with 4 KiB
    # clusters), and GetCompressedFileSizeW never answers more than the
    # logical size: a real 1,615-byte sparse Ollama blob read 4,096 on disk
    # in the Compatible engine and 65,536 in Turbo Scan and in its map.
    small = tmp_path / "small.bin"
    _sparse_file(small, 1615, data=b"x" * 1615)
    cluster = alloc_size._get_cluster_size(str(small))

    billed = _measure_alloc_size(str(small), os.stat(small))

    assert billed > cluster and billed % cluster == 0


@pytest.mark.windows
def test_a_sparse_files_holes_take_no_space(tmp_path):
    holes = tmp_path / "holes.bin"
    _sparse_file(holes, 200 * 1024 * 1024, data_at=100 * 1024 * 1024, data=b"x" * 10)

    assert 0 < _measure_alloc_size(str(holes), os.stat(holes)) <= 1024 * 1024
