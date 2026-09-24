import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import drive_info
from storage_scanner.drive_info import get_volume_root, is_ntfs_fixed_drive


class _FakeGetDriveTypeW:
    def __init__(self, drive_type):
        self.drive_type = drive_type
        self.calls = []

    def __call__(self, root_path):
        self.calls.append(root_path)
        return self.drive_type


class _FakeGetVolumeInformationW:
    def __init__(self, fs_name, succeed=True):
        self.fs_name = fs_name
        self.succeed = succeed
        self.calls = []

    def __call__(
        self,
        root_path,
        _vol_name_buf,
        _vol_name_size,
        _vol_serial,
        _max_component_len,
        _fs_flags,
        fs_name_buffer,
        _fs_name_size,
    ):
        self.calls.append(root_path)
        if not self.succeed:
            return 0
        fs_name_buffer.value = self.fs_name
        return 1


class _RaisingCall:
    def __call__(self, *args, **kwargs):
        raise OSError("simulated Win32 failure")


class _FakeKernel32:
    def __init__(self, drive_type=drive_info._DRIVE_FIXED, fs_name="NTFS", vol_info_succeeds=True):
        self.GetDriveTypeW = _FakeGetDriveTypeW(drive_type)
        self.GetVolumeInformationW = _FakeGetVolumeInformationW(fs_name, vol_info_succeeds)


class _FakeWinDLL:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def _patch_windll(monkeypatch, kernel32):
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(kernel32), raising=False)


def test_get_volume_root_for_a_drive_letter_path():
    assert get_volume_root(r"C:\Users\test\Documents") == "C:\\"


def test_get_volume_root_for_a_unc_path():
    assert get_volume_root(r"\\server\share\folder") == "\\\\server\\share\\"


def test_fixed_ntfs_drive_is_eligible(monkeypatch):
    _patch_windll(monkeypatch, _FakeKernel32(drive_type=drive_info._DRIVE_FIXED, fs_name="NTFS"))
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive(r"C:\Users\test") is True


def test_removable_drive_is_not_eligible(monkeypatch):
    _patch_windll(
        monkeypatch, _FakeKernel32(drive_type=drive_info._DRIVE_REMOVABLE, fs_name="NTFS")
    )
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive("E:\\") is False


def test_network_drive_is_not_eligible(monkeypatch):
    _patch_windll(monkeypatch, _FakeKernel32(drive_type=drive_info._DRIVE_REMOTE, fs_name="NTFS"))
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive(r"\\server\share") is False


def test_non_ntfs_fixed_drive_is_not_eligible(monkeypatch):
    _patch_windll(monkeypatch, _FakeKernel32(drive_type=drive_info._DRIVE_FIXED, fs_name="FAT32"))
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive("D:\\") is False


def test_volume_information_failure_is_not_eligible(monkeypatch):
    _patch_windll(
        monkeypatch,
        _FakeKernel32(drive_type=drive_info._DRIVE_FIXED, fs_name="NTFS", vol_info_succeeds=False),
    )
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive(r"C:\Users\test") is False


def test_raising_win32_call_is_not_eligible_not_an_exception(monkeypatch):
    kernel32 = _FakeKernel32(drive_type=drive_info._DRIVE_FIXED, fs_name="NTFS")
    kernel32.GetDriveTypeW = _RaisingCall()
    _patch_windll(monkeypatch, kernel32)
    monkeypatch.setattr(drive_info, "IS_WINDOWS", True)

    assert is_ntfs_fixed_drive(r"C:\Users\test") is False


def test_non_windows_platform_is_never_eligible(monkeypatch):
    _patch_windll(monkeypatch, _FakeKernel32(drive_type=drive_info._DRIVE_FIXED, fs_name="NTFS"))
    monkeypatch.setattr(drive_info, "IS_WINDOWS", False)

    assert is_ntfs_fixed_drive(r"C:\Users\test") is False
