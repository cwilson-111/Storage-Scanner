"""The optional "Scan with Storage Scanner" folder menu entry (P2-22),
written to a throwaway key under HKEY_CURRENT_USER, never the real one."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import contextlib

from storage_scanner import explorer_menu

pytestmark = pytest.mark.windows

TEST_CLASSES = r"Software\StorageScannerTest\Classes"
EXE = r"C:\Program Files\Storage Scanner\StorageScanner.exe"


@pytest.fixture
def classes_key():
    import winreg

    yield TEST_CLASSES
    explorer_menu.uninstall(TEST_CLASSES)
    for path in (
        rf"{TEST_CLASSES}\Directory\Background\shell",
        rf"{TEST_CLASSES}\Directory\Background",
        rf"{TEST_CLASSES}\Directory\shell",
        rf"{TEST_CLASSES}\Directory",
        rf"{TEST_CLASSES}\Drive\shell",
        rf"{TEST_CLASSES}\Drive",
        TEST_CLASSES,
        r"Software\StorageScannerTest",
    ):
        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)


def _command(key_path):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
        return winreg.QueryValueEx(key, "")[0]


def test_installing_adds_folder_drive_and_background_entries_and_removing_takes_them_away(
    classes_key,
):
    assert not explorer_menu.is_installed(classes_key)

    explorer_menu.install(classes_key, launch_args=[EXE])

    assert explorer_menu.is_installed(classes_key)
    folder = _command(rf"{classes_key}\Directory\shell\StorageScanner")
    background = _command(rf"{classes_key}\Directory\Background\shell\StorageScanner")
    assert folder == f'"{EXE}" "%1"'
    assert background == f'"{EXE}" "%V"'
    explorer_menu.uninstall(classes_key)
    assert not explorer_menu.is_installed(classes_key)


def test_a_copy_running_from_temp_is_not_added(classes_key, monkeypatch, tmp_path):
    monkeypatch.setattr(explorer_menu, "app_launch_args", lambda: [str(tmp_path / "x.exe")])
    monkeypatch.setattr("storage_scanner.schedule.tempfile.gettempdir", lambda: str(tmp_path))

    with pytest.raises(ValueError, match="temporary"):
        explorer_menu.install(classes_key)
    assert not explorer_menu.is_installed(classes_key)


def test_the_folder_explorer_passes_is_read_back_including_a_drive_root(tmp_path):
    from storage_scanner.app import folder_argument

    assert folder_argument(str(tmp_path)) == str(tmp_path)
    assert folder_argument('C:"') == "C:\\"  # what "%1" gives for C:\
    assert folder_argument(str(tmp_path / "missing")) is None
