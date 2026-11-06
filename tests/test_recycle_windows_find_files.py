"""_find_files' kernel32 handle, against a fake ctypes.WinDLL so this runs
anywhere -- tests/test_recycle_windows.py needs a real Recycle Bin, this
needs only the prototype setup.

recycle() polls for a recycled item's $I file for up to a second, every
10 ms, and each poll reaches _find_files once per SID folder; every
ctypes.WinDLL() is a LoadLibraryW this module never frees. So the handle
is resolved once (recycle_windows._kernel32), not per call.
"""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import recycle_windows

_INVALID_HANDLE = wintypes.HANDLE(-1).value
_LIVE_HANDLE = 7


class _FakeFunction:
    """A ctypes function pointer, counting the prototypes assigned to it."""

    def __init__(self, result):
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "prototypes_set", 0)

    def __setattr__(self, name, value):
        if name in ("argtypes", "restype"):
            object.__setattr__(self, "prototypes_set", self.prototypes_set + 1)
        object.__setattr__(self, name, value)

    def __call__(self, *args):
        return self.result


class _FindFirstFileExW(_FakeFunction):
    """One matching file, written into the caller's _WIN32_FIND_DATAW the
    way the real call fills it in."""

    def __init__(self, name, filetime):
        super().__init__(_LIVE_HANDLE)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "filetime", filetime)

    def __call__(self, pattern, info_level, data_ref, *rest):
        data = data_ref._obj
        data.cFileName = self.name
        data.ftLastWriteTime.dwLowDateTime = self.filetime & 0xFFFFFFFF
        data.ftLastWriteTime.dwHighDateTime = self.filetime >> 32
        return self.result


class _FakeKernel32:
    """ctypes.WinDLL("kernel32"), counting how often it was loaded."""

    loads = 0
    found = None  # (name, FILETIME ticks), or None for an empty folder

    def __init__(self, name, use_last_error=False):
        type(self).loads += 1
        if self.found is None:
            self.FindFirstFileExW = _FakeFunction(_INVALID_HANDLE)
        else:
            self.FindFirstFileExW = _FindFirstFileExW(*self.found)
        self.FindNextFileW = _FakeFunction(0)  # no second match
        self.FindClose = _FakeFunction(1)


@pytest.fixture
def kernel32(monkeypatch):
    """The fake in place of ctypes.WinDLL, with _find_files' cached handle
    cleared on both sides so the real one and this one never meet."""
    recycle_windows._kernel32.cache_clear()
    _FakeKernel32.loads = 0
    _FakeKernel32.found = None
    monkeypatch.setattr(ctypes, "WinDLL", _FakeKernel32, raising=False)
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: recycle_windows._ERROR_FILE_NOT_FOUND, raising=False
    )
    yield _FakeKernel32
    recycle_windows._kernel32.cache_clear()


def _polls():
    """How many times one recycle() can call _find_files per SID folder."""
    return int(recycle_windows._BIN_ARRIVAL_SECONDS / recycle_windows._BIN_POLL_SECONDS)


def test_a_full_poll_of_the_bin_loads_kernel32_and_sets_its_prototypes_once(kernel32):
    pattern = "C:\\$Recycle.Bin\\S-1-5-21-1\\$I*"

    results = [recycle_windows._find_files(pattern) for _ in range(_polls())]

    assert results == [[]] * _polls()
    loaded = recycle_windows._kernel32()
    assert kernel32.loads == 1
    assert loaded.FindFirstFileExW.prototypes_set == 2  # restype, then argtypes
    assert loaded.FindNextFileW.prototypes_set == 1
    assert loaded.FindClose.prototypes_set == 1


def test_find_files_reports_the_name_and_write_time_of_a_match(kernel32):
    # 2024-01-01T00:00:00Z as Windows counts it: 100 ns steps since 1601.
    kernel32.found = ("$IA1B2C3.txt", recycle_windows._FILETIME_EPOCH + 1_704_067_200 * 10_000_000)

    found = recycle_windows._find_files("C:\\$Recycle.Bin\\S-1-5-21-1\\$I*")

    assert found == [("$IA1B2C3.txt", 1_704_067_200.0)]


def test_an_unreadable_folder_is_told_apart_from_an_empty_one(kernel32, monkeypatch):
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)  # access denied

    assert recycle_windows._find_files("C:\\$Recycle.Bin\\S-1-5-18\\$I*") is None
