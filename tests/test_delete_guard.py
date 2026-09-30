"""delete_guard: what no delete may touch, whichever window asks (P0-4),
and names Windows would silently shorten (P0-3)."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import delete_guard
from storage_scanner.delete_guard import (
    needs_typed_confirmation,
    refusal_reason,
    typed_name_matches,
    windows_trims_path,
)
from storage_scanner.models import Node, detached_file

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows known folders")

HOME = os.path.join(os.sep, "machine", "home", "me")
PROTECTED = [(HOME, "your home folder"), (os.path.join(HOME, "Documents"), "your Documents folder")]


def test_a_drive_or_filesystem_root_is_refused():
    root = os.path.abspath(os.sep)

    assert "root of a drive" in refusal_reason(root, protected=[])


def test_the_folder_a_scan_started_from_is_refused_but_its_contents_are_not(tmp_path):
    scan_root = str(tmp_path / "scanned")

    assert "this scan started from" in refusal_reason(scan_root, [scan_root], protected=[])
    assert refusal_reason(os.path.join(scan_root, "a.bin"), [scan_root], protected=[]) is None


def test_a_protected_folder_is_refused_but_whats_inside_it_is_not():
    documents = os.path.join(HOME, "Documents")

    assert "your Documents folder" in refusal_reason(documents, protected=PROTECTED)
    assert refusal_reason(os.path.join(documents, "old.pdf"), protected=PROTECTED) is None
    assert refusal_reason(os.path.join(documents, "Old Projects"), protected=PROTECTED) is None


def test_a_folder_holding_a_protected_folder_is_refused():
    reason = refusal_reason(os.path.dirname(HOME), protected=PROTECTED)

    assert "contains your home folder" in reason


def test_a_sibling_that_only_shares_a_name_prefix_is_not_protected():
    assert refusal_reason(HOME + "2", protected=PROTECTED) is None


def test_a_protected_folder_is_matched_whatever_the_letter_case_on_windows(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("case-insensitive paths are a Windows thing")
    assert refusal_reason(os.path.join(HOME, "DOCUMENTS"), protected=PROTECTED) is not None


@windows_only
def test_this_machines_windows_profile_and_known_folders_are_refused():
    profile = os.environ["USERPROFILE"]
    for path in (
        os.environ["WINDIR"],
        os.environ["PROGRAMFILES"],
        os.environ["PROGRAMDATA"],
        os.path.dirname(profile),  # C:\Users
        profile,
        os.environ["APPDATA"],
        os.path.dirname(os.environ["APPDATA"]),  # AppData itself
        os.environ["LOCALAPPDATA"],
    ):
        assert refusal_reason(path) is not None, path
    # Wherever Documents and Downloads really are (OneDrive can move them).
    documents, downloads = (
        "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
        "{374DE290-123F-4565-9164-39C4925E467B}",
    )
    for guid in (documents, downloads):
        known = delete_guard._known_folder_path(guid)
        assert refusal_reason(known) is not None, known
        assert refusal_reason(os.path.join(known, "report.pdf")) is None
    assert refusal_reason(os.path.join(os.environ["LOCALAPPDATA"], "Temp", "x.tmp")) is None


def test_on_linux_home_and_system_folders_are_refused():
    folders = delete_guard._posix_folders(is_macos=False)
    home = os.path.expanduser("~")

    for path in (
        "/usr",
        "/etc",
        home,
        os.path.join(home, "Documents"),
        os.path.join(home, ".config"),
    ):
        assert refusal_reason(path, protected=folders) is not None, path
    assert refusal_reason(os.path.join(home, "Documents", "old.pdf"), protected=folders) is None


def test_on_macos_library_and_the_users_folders_are_refused():
    folders = delete_guard._posix_folders(is_macos=True)
    home = os.path.expanduser("~")

    for path in ("/System", "/Applications", os.path.join(home, "Library"), home):
        assert refusal_reason(path, protected=folders) is not None, path
    assert refusal_reason(os.path.join(home, "Movies", "clip.mov"), protected=folders) is None


@pytest.mark.parametrize(
    "path",
    ["C:\\data\\t.bin.", "C:\\data\\t.bin ", "C:\\data\\folder.\\t.bin", "\\\\nas\\share\\x \\y"],
)
def test_names_windows_would_shorten_are_spotted(path):
    assert windows_trims_path(path)


@pytest.mark.parametrize("path", ["C:\\", "C:\\data\\t.bin", "C:\\data\\.git\\config", "C:\\a..b"])
def test_ordinary_names_are_not(path):
    assert not windows_trims_path(path)


@windows_only
def test_a_trimmed_name_is_refused_and_says_what_windows_would_act_on():
    reason = refusal_reason("C:\\data\\t.bin.")

    assert "C:\\data\\t.bin instead" in reason


def test_a_folder_needs_its_name_typed_from_10_gb_or_50000_files():
    folder = Node("/data/big", "big")
    folder.size, folder.file_count = 10 * 1024**3 - 1, 49_999
    assert not needs_typed_confirmation(folder)

    folder.size = 10 * 1024**3
    assert needs_typed_confirmation(folder)

    folder.size, folder.file_count = 1, 50_000
    assert needs_typed_confirmation(folder)

    assert not needs_typed_confirmation(detached_file("/data/huge.iso", size=50 * 1024**3))


def test_only_the_folders_own_name_confirms_it():
    folder = Node("/data/Old Projects", "Old Projects")

    assert typed_name_matches(" Old Projects ", folder)
    assert not typed_name_matches("old projects", folder)
    assert not typed_name_matches("", folder)
