"""Tests for storage_scanner.settings's per-OS protected-path exclude
lists -- specifically the new Linux one (see is_protected_path in
cleanup_recommendations.py, which reuses whichever list DEFAULT_
DUPLICATE_EXCLUDES resolves to for the OS actually running).

Deliberately uses posixpath (not the ambient os.path) to mirror what
os.path.normcase/normpath actually do ON Linux, regardless of which OS
this test suite happens to be running on -- os.path IS posixpath on a
real Linux machine, but not here (this project's dev/CI machine is
Windows, where os.path.normpath would mangle these forward-slash paths
into backslashes and make every assertion below meaningless).
"""
import posixpath
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.settings import (
    _LINUX_DUPLICATE_EXCLUDES, _MACOS_DUPLICATE_EXCLUDES, _WINDOWS_DUPLICATE_EXCLUDES,
)


def _matches_on_linux(path, excludes=_LINUX_DUPLICATE_EXCLUDES):
    normalized = posixpath.normcase(posixpath.normpath(path))
    return any(marker in normalized for marker in excludes)


def test_linux_excludes_match_known_system_paths():
    for path in [
        "/etc/passwd", "/usr/bin/python3", "/proc/1/status", "/sys/class/net",
        "/boot/vmlinuz", "/var/cache/apt/archives", "/var/lib/docker",
        "/lib/systemd", "/lib64/ld-linux.so.2",
        "/home/user/.local/share/Trash/files/deleted.txt",
        "/home/user/.Trash/old.bin",
    ]:
        assert _matches_on_linux(path), path


def test_linux_excludes_do_not_match_ordinary_folders_with_colliding_prefixes():
    # The short markers (/etc/, /usr/, /lib/, /dev/, ...) carry a trailing
    # "/" specifically so they can't false-positive on an ordinary user
    # folder that just happens to start with the same letters.
    for path in [
        "/home/user/devops-notes/readme.md",
        "/home/user/usrdata/backup.tar",
        "/home/user/etcetera/notes.txt",
        "/home/user/liberty/photo.png",
        "/home/user/libraries/project",
    ]:
        assert not _matches_on_linux(path), path


def test_linux_excludes_match_ordinary_files_directly_under_home_are_not_protected():
    assert not _matches_on_linux("/home/user/Documents/report.pdf")
    assert not _matches_on_linux("/home/user/Downloads/movie.mkv")


def test_macos_and_windows_exclude_lists_unaffected():
    # Regression guard: adding the Linux list shouldn't have touched the
    # existing two.
    assert "/System" in _MACOS_DUPLICATE_EXCLUDES
    assert "/.Trash" in _MACOS_DUPLICATE_EXCLUDES
    assert r"\Windows" in _WINDOWS_DUPLICATE_EXCLUDES
    assert r"\$Recycle.Bin" in _WINDOWS_DUPLICATE_EXCLUDES
