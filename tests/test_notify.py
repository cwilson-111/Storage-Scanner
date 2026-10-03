import contextlib
import shutil
import struct
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import notify
from storage_scanner.budgets import BudgetBreach

GB = 1024**3

# A folder name is user data: it must come through as text, never as markup
# or script, on every platform.
NASTY = r"C:\Tom & Jerry <backup> 'x' \"y\" $(calc)"


def _breach(current, threshold):
    return BudgetBreach(
        path="c:\\data",
        threshold_bytes=threshold,
        current_size_bytes=current,
        as_of="2026-09-24T00:00:00+00:00",
        is_stale=False,
    )


def test_budget_message_uses_the_path_as_typed_and_human_sizes():
    title, body = notify.budget_breach_message(r"C:\Data", _breach(12 * GB, 10 * GB))

    assert "over budget" in title
    assert body.startswith(r"C:\Data is ")
    assert "12.0 GB" in body and "10.0 GB" in body


def test_toast_xml_keeps_special_characters_as_text():
    root = ET.fromstring(notify.windows_toast_xml("Title & <co>", NASTY))

    texts = [t.text for t in root.iter("text")]
    assert texts == ["Title & <co>", NASTY]


def test_macos_and_linux_pass_text_as_separate_arguments():
    mac = notify.macos_command("Title", NASTY)
    linux = notify.linux_command("Title", NASTY)

    assert mac[-2:] == ["Title", NASTY]
    assert not any(NASTY in part for part in mac[:-1])
    assert linux[-2:] == ["Title", NASTY]


@pytest.mark.windows
@pytest.mark.parametrize(
    "ask",
    [lambda: notify.notify("Title", "Body"), notify.check_windows_toasts],
    ids=["toast", "check"],
)
@pytest.mark.parametrize(
    "setting, shown, where",
    [
        ("Enabled", True, ""),
        ("", True, ""),  # no app ID Windows knows had a setting: nothing blocks it
        ("DisabledForUser", False, "this account"),
        ("DisabledForApplication", False, "Notifications > Storage Scanner"),
        ("DisabledByGroupPolicy", False, "Group Policy"),
        ("DisabledBySomethingNew", False, "DisabledBySomethingNew"),
    ],
)
def test_windows_toast_blocked_by_its_setting_is_reported_with_where_to_fix_it(
    monkeypatch, ask, setting, shown, where
):
    # The OS query: the PowerShell script prints ToastNotifier.Setting,
    # and the toast script posts only when Windows wouldn't drop it.
    def fake_powershell(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=setting, stderr="")

    monkeypatch.setattr(notify, "IS_WINDOWS", True)
    monkeypatch.setattr(notify, "register_windows_app_id", lambda: None)
    monkeypatch.setattr(notify.subprocess, "run", fake_powershell)

    ok, reason = ask()

    assert ok is shown
    assert where in reason


@pytest.mark.windows
def test_windows_check_that_cannot_ask_says_so_instead_of_blocked(monkeypatch):
    def no_powershell(command, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(notify.subprocess, "run", no_powershell)

    shown, error = notify.check_windows_toasts()

    assert shown is None
    assert "cannot find the file" in error


@pytest.mark.windows
def test_app_id_registration_outlives_a_one_file_bundle_and_is_fully_removed(monkeypatch, tmp_path):
    import winreg

    test_root = rf"Software\StorageScannerTests\{uuid.uuid4().hex}"
    monkeypatch.setattr(notify, "APP_ID_KEY", test_root + r"\AppUserModelId")
    # A one-file build's bundled files live in a temporary folder that is
    # deleted when the process exits.
    bundle = tmp_path / "_MEI12345"
    bundle.mkdir()
    shutil.copyfile(ROOT / "icon.ico", bundle / "icon.ico")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

    try:
        notify.register_windows_app_id()
        notify.register_windows_app_id()  # every toast registers again
        shutil.rmtree(bundle)

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, notify.APP_ID_KEY) as key:
            name, _ = winreg.QueryValueEx(key, "DisplayName")
            icon, _ = winreg.QueryValueEx(key, "IconUri")
        assert name == "Storage Scanner"
        # Windows shows no toast icon from an .ico: it gets the icon's
        # largest image, as a PNG.
        png = Path(icon).read_bytes()
        assert png.startswith(b"\x89PNG") and struct.unpack(">II", png[16:24]) == (256, 256)

        notify.unregister_windows_app_id()
        notify.unregister_windows_app_id()  # nothing left to remove is fine

        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, notify.APP_ID_KEY)
        assert not Path(icon).exists()
    finally:
        for key in (notify.APP_ID_KEY, test_root, r"Software\StorageScannerTests"):
            with contextlib.suppress(OSError):  # gone already, or another run's key is left
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
