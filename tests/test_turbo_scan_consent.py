import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.ui import main_window
from storage_scanner.ui.main_window import MainWindowMixin


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _Root:
    destroyed = False

    def destroy(self):
        self.destroyed = True


class _App(MainWindowMixin):
    def __init__(self, choice, turbo_on=True):
        self.turbo_scan_var = _Var(turbo_on)
        self.root = _Root()
        self.asked = 0
        self._choice = choice

    def _ask_turbo_scan_mode(self):
        self.asked += 1
        return self._choice


@pytest.fixture
def not_elevated_ntfs(monkeypatch):
    """A non-elevated Windows session scanning a local NTFS drive: the only
    case where the dialog appears at all."""
    relaunches = []
    errors = []
    monkeypatch.setattr(main_window, "IS_WINDOWS", True)
    monkeypatch.setattr(main_window, "IS_ROOT", False)
    monkeypatch.setattr(main_window, "is_ntfs_fixed_drive", lambda _path: True)
    monkeypatch.setattr(
        main_window,
        "relaunch_elevated_windows",
        lambda initial=None: relaunches.append(initial) or True,
    )
    monkeypatch.setattr(
        main_window.messagebox,
        "showerror",
        lambda *a, **k: errors.append(a),
    )
    return relaunches, errors


def test_just_this_scan_uses_turbo_through_the_helper(not_elevated_ntfs, tmp_path):
    app = _App(MainWindowMixin.TURBO_THIS_SCAN_ONLY)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is True
    assert not app.root.destroyed


def test_regular_scan_skips_turbo(not_elevated_ntfs, tmp_path):
    app = _App(MainWindowMixin.TURBO_REGULAR_SCAN)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is False


def test_restart_as_admin_relaunches_on_the_same_folder_and_starts_no_scan(
    not_elevated_ntfs,
    tmp_path,
):
    relaunches, _errors = not_elevated_ntfs
    app = _App(MainWindowMixin.TURBO_RESTART_AS_ADMIN)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is None
    assert relaunches == [str(tmp_path)]
    assert app.root.destroyed


def test_a_declined_restart_falls_back_to_a_regular_scan(not_elevated_ntfs, monkeypatch, tmp_path):
    _relaunches, errors = not_elevated_ntfs
    monkeypatch.setattr(main_window, "relaunch_elevated_windows", lambda initial=None: False)
    app = _App(MainWindowMixin.TURBO_RESTART_AS_ADMIN)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is False
    assert not app.root.destroyed
    assert errors


def test_already_elevated_never_asks(not_elevated_ntfs, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window, "IS_ROOT", True)
    app = _App(MainWindowMixin.TURBO_REGULAR_SCAN)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is True
    assert app.asked == 0


def test_toggle_off_never_asks(not_elevated_ntfs, tmp_path):
    app = _App(MainWindowMixin.TURBO_THIS_SCAN_ONLY, turbo_on=False)

    assert app._resolve_turbo_scan_consent(str(tmp_path)) is False
    assert app.asked == 0
