"""Tests for MainWindowMixin._list_drives()'s Linux branch -- a plain
@staticmethod, pure filesystem logic, so this is fully testable without
ever constructing a real Tk window (nothing else in main_window.py has
that property, hence no broader UI test suite exists for this file).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.ui import main_window


def test_list_drives_linux_always_includes_root(monkeypatch):
    monkeypatch.setattr(main_window, "IS_MACOS", False)
    monkeypatch.setattr(main_window, "IS_LINUX", True)
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)

    assert main_window.MainWindowMixin._list_drives() == ["/"]


def test_list_drives_linux_finds_mounted_media_under_the_users_media_dir(monkeypatch):
    monkeypatch.setattr(main_window, "IS_MACOS", False)
    monkeypatch.setattr(main_window, "IS_LINUX", True)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("LOGNAME", raising=False)

    media_dir = os.path.join("/media", "alice")

    def fake_isdir(p):
        return p == media_dir

    def fake_listdir(p):
        assert p == media_dir
        return ["USB_STICK", "backup_drive"]

    mounted = {os.path.join(media_dir, "USB_STICK")}

    monkeypatch.setattr(os.path, "isdir", fake_isdir)
    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os.path, "ismount", lambda p: p in mounted)

    drives = main_window.MainWindowMixin._list_drives()

    assert drives == ["/", os.path.join(media_dir, "USB_STICK")]


def test_list_drives_linux_skips_unmounted_placeholder_directories(monkeypatch):
    # udisks2/automount tools create an empty per-device directory upfront,
    # before anything is actually mounted there -- os.path.ismount() is
    # what filters those back out.
    monkeypatch.setattr(main_window, "IS_MACOS", False)
    monkeypatch.setattr(main_window, "IS_LINUX", True)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("LOGNAME", raising=False)

    media_dir = os.path.join("/media", "alice")
    monkeypatch.setattr(os.path, "isdir", lambda p: p == media_dir)
    monkeypatch.setattr(os, "listdir", lambda p: ["not_yet_mounted"])
    monkeypatch.setattr(os.path, "ismount", lambda p: False)

    assert main_window.MainWindowMixin._list_drives() == ["/"]
