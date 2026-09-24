"""Tests for storage_scanner.file_ops's Recycle Bin / Trash support --
specifically the new Linux path (_recycle_linux, its manual XDG-Trash-spec
fallback, and open_trash's Linux branch). The existing Windows/macOS paths
are covered by tests/test_elevation.py (elevation) and exercised manually
via this project's own real-machine validation elsewhere; this file only
adds what didn't exist before.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import file_ops


class _FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


def test_recycle_dispatches_to_linux_when_on_linux(monkeypatch):
    monkeypatch.setattr(file_ops, "IS_MACOS", False)
    monkeypatch.setattr(file_ops, "IS_LINUX", True)
    called = {}

    def fake_recycle_linux(path):
        called["path"] = path
        return True

    monkeypatch.setattr(file_ops, "_recycle_linux", fake_recycle_linux)

    assert file_ops.recycle("/home/user/file.txt") is True
    assert called["path"] == "/home/user/file.txt"


def test_recycle_linux_prefers_gio_trash_when_available(monkeypatch):
    captured = {}

    def fake_run(args, capture_output=False):
        captured["args"] = args
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert file_ops._recycle_linux("/home/user/file.txt") is True
    assert captured["args"] == ["gio", "trash", os.path.abspath("/home/user/file.txt")]


def test_recycle_linux_falls_back_to_manual_when_gio_is_missing(monkeypatch, tmp_path):
    def fake_run(args, capture_output=False):
        raise FileNotFoundError("gio not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    target = tmp_path / "doomed.txt"
    target.write_text("bye")

    assert file_ops._recycle_linux(str(target)) is True
    assert not target.exists()
    assert (tmp_path / "Trash" / "files" / "doomed.txt").exists()
    assert (tmp_path / "Trash" / "info" / "doomed.txt.trashinfo").exists()


def test_recycle_linux_falls_back_to_manual_when_gio_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda args, capture_output=False: _FakeCompletedProcess(1)
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    target = tmp_path / "doomed.txt"
    target.write_text("bye")

    assert file_ops._recycle_linux(str(target)) is True
    assert not target.exists()


def test_recycle_linux_manual_writes_a_spec_compliant_trashinfo(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "notes.txt"
    target.write_text("secret")

    assert file_ops._recycle_linux_manual(str(target)) is True

    info_path = tmp_path / "Trash" / "info" / "notes.txt.trashinfo"
    text = info_path.read_text()
    assert text.startswith("[Trash Info]\n")
    assert f"Path={target}" in text or "Path=" in text  # percent-encoded, absolute
    assert "DeletionDate=" in text


def test_recycle_linux_manual_handles_a_name_collision(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    trash_files = tmp_path / "Trash" / "files"
    trash_files.mkdir(parents=True)
    (trash_files / "dup.txt").write_text("already here")

    target = tmp_path / "dup.txt"
    target.write_text("new one")

    assert file_ops._recycle_linux_manual(str(target)) is True
    assert (trash_files / "dup.txt").read_text() == "already here"  # untouched
    assert (trash_files / "dup.txt.2").read_text() == "new one"  # the new arrival, renamed


def test_recycle_linux_manual_returns_false_on_a_real_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Nothing at this path -- shutil.move will raise.
    assert file_ops._recycle_linux_manual(str(tmp_path / "does_not_exist.txt")) is False


def test_open_trash_linux_tries_gio_open_trash_uri(monkeypatch):
    monkeypatch.setattr(file_ops, "IS_MACOS", False)
    monkeypatch.setattr(file_ops, "IS_LINUX", True)
    captured = {}

    def fake_run(args, check=False):
        captured["args"] = args
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert file_ops.open_trash() is True
    assert captured["args"] == ["gio", "open", "trash:///"]


def test_open_trash_linux_falls_back_to_xdg_open_when_gio_is_missing(monkeypatch):
    monkeypatch.setattr(file_ops, "IS_MACOS", False)
    monkeypatch.setattr(file_ops, "IS_LINUX", True)
    calls = []

    def fake_run(args, check=False):
        calls.append(args)
        if args[0] == "gio":
            raise FileNotFoundError("gio not found")
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert file_ops.open_trash() is True
    assert calls[0][0] == "gio"
    assert calls[1][0] == "xdg-open"
