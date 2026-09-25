"""Tests for storage_scanner.installed_apps against a faked winreg module --
the same dependency-injection technique tests/test_run_elevated_scan_windows.py
already uses for ctypes.windll, applied to winreg instead. No real Windows
registry access happens here.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage_scanner.installed_apps as installed_apps


class _FakeKey:
    """What winreg.OpenKey returns -- a context manager remembering which
    (hive, path) it was opened for, so EnumKey/QueryValueEx on it can look
    the right entry back up in the fake registry tree."""

    def __init__(self, registry, hive, path):
        self._registry = registry
        self.hive = hive
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeWinreg:
    """Minimal fake of the winreg module surface installed_apps.py uses.

    `subkeys`: {hive: {path: [name, ...]}} -- backs OpenKey+EnumKey.
    `values`: {hive: {path: {value_name: value}}} -- backs QueryValueEx.
    Matches real winreg's error behavior: OpenKey on a path with no
    registered entry raises OSError; EnumKey raises OSError once `index`
    runs past the last subkey (winreg's own end-of-enumeration signal,
    not a sentinel return value); QueryValueEx raises FileNotFoundError
    for a missing value name.
    """

    def __init__(self, subkeys=None, values=None):
        self.subkeys = subkeys or {}
        self.values = values or {}
        self.opened_paths = []  # (hive, path) -- lets a test assert on what was read

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def OpenKey(self, hive, path):
        self.opened_paths.append((hive, path))
        exists = path in self.subkeys.get(hive, {}) or path in self.values.get(hive, {})
        if not exists:
            raise OSError(f"[WinError 2] no such key: {hive}\\{path}")
        return _FakeKey(self, hive, path)

    def EnumKey(self, key, index):
        names = self.subkeys.get(key.hive, {}).get(key.path, [])
        if index >= len(names):
            raise OSError("[WinError 259] no more data is available")
        return names[index]

    def QueryValueEx(self, key, value_name):
        entry = self.values.get(key.hive, {}).get(key.path, {})
        if value_name not in entry:
            raise FileNotFoundError(f"no such value: {value_name}")
        return entry[value_name], 1  # (value, registry type) -- type unused here


_HKLM = FakeWinreg.HKEY_LOCAL_MACHINE
_HKCU = FakeWinreg.HKEY_CURRENT_USER
_HKLM_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_HKLM_WOW_PATH = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
_HKCU_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _app_entry(
    subkeys, values, hive, uninstall_path, name, display_name=None, install_location=None
):
    subkeys.setdefault(hive, {}).setdefault(uninstall_path, []).append(name)
    entry_path = f"{uninstall_path}\\{name}"
    entry = {}
    if display_name is not None:
        entry["DisplayName"] = display_name
    if install_location is not None:
        entry["InstallLocation"] = install_location
    values.setdefault(hive, {})[entry_path] = entry


def test_reads_apps_from_hklm_and_hkcu(monkeypatch):
    subkeys, values = {}, {}
    _app_entry(
        subkeys, values, _HKLM, _HKLM_PATH, "App1", "First App", r"C:\Program Files\First App"
    )
    _app_entry(
        subkeys,
        values,
        _HKCU,
        _HKCU_PATH,
        "App2",
        "Second App",
        r"C:\Users\me\AppData\Local\Second App",
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    apps = installed_apps.get_installed_apps()

    assert ("First App", r"C:\Program Files\First App") in apps
    assert ("Second App", r"C:\Users\me\AppData\Local\Second App") in apps
    assert len(apps) == 2


def test_reads_apps_from_wow6432node(monkeypatch):
    """32-bit apps on a 64-bit OS register under the WOW6432Node redirect
    -- missing this hive/path would silently under-report installed apps."""
    subkeys, values = {}, {}
    _app_entry(
        subkeys,
        values,
        _HKLM,
        _HKLM_WOW_PATH,
        "OldApp",
        "Old 32-bit App",
        r"C:\Program Files (x86)\Old App",
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    apps = installed_apps.get_installed_apps()

    assert apps == [("Old 32-bit App", r"C:\Program Files (x86)\Old App")]


def test_entry_missing_display_name_is_skipped(monkeypatch):
    subkeys, values = {}, {}
    _app_entry(
        subkeys, values, _HKLM, _HKLM_PATH, "NoName", install_location=r"C:\Program Files\Foo"
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    assert installed_apps.get_installed_apps() == []


def test_entry_missing_install_location_is_skipped(monkeypatch):
    """Most registry entries skip InstallLocation entirely (per-user
    tools, browser extensions, update packages) -- this is the common
    case, not an error, and these were never orphan-detection candidates
    anyway since nothing could later match their (nonexistent) location."""
    subkeys, values = {}, {}
    _app_entry(subkeys, values, _HKLM, _HKLM_PATH, "NoLocation", display_name="Some Tool")
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    assert installed_apps.get_installed_apps() == []


def test_a_subkey_that_vanishes_mid_enumeration_does_not_crash(monkeypatch):
    """Another process can uninstall something while this read is in
    progress -- EnumKey lists a name whose OpenKey then 404s. Must be
    skipped, not raise out of the whole read."""
    subkeys, values = {}, {}
    _app_entry(
        subkeys,
        values,
        _HKLM,
        _HKLM_PATH,
        "StillHere",
        "Still Here",
        r"C:\Program Files\Still Here",
    )
    # A name enumerated but never given a values entry at all -- OpenKey
    # on its full path will find nothing and raise OSError, exactly like
    # a real vanished subkey.
    subkeys[_HKLM][_HKLM_PATH].append("Vanished")
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    apps = installed_apps.get_installed_apps()

    assert apps == [("Still Here", r"C:\Program Files\Still Here")]


def test_missing_uninstall_key_on_this_machine_is_not_an_error(monkeypatch):
    """A 32-bit OS has no WOW6432Node tree at all -- OpenKey on the
    top-level uninstall path itself should raise OSError and be treated
    as "nothing here," not propagate."""
    fake = FakeWinreg({}, {})  # nothing registered anywhere
    monkeypatch.setattr(installed_apps, "winreg", fake)

    assert installed_apps.get_installed_apps() == []


def test_get_installed_install_locations_filters_to_candidate_roots(monkeypatch):
    subkeys, values = {}, {}
    _app_entry(
        subkeys, values, _HKLM, _HKLM_PATH, "InScope", "In Scope App", r"C:\Program Files\In Scope"
    )
    _app_entry(
        subkeys,
        values,
        _HKLM,
        _HKLM_PATH,
        "OutOfScope",
        "Out of Scope App",
        r"D:\Games\Out of Scope",
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    locations = installed_apps.get_installed_install_locations()

    assert len(locations) == 1
    normalized = next(iter(locations))
    assert "in scope" in normalized
    assert "out of scope" not in normalized


def test_get_installed_install_locations_normalizes_case_and_path_form(monkeypatch):
    subkeys, values = {}, {}
    trailing_slash_location = "C:\\Program Files\\SomeApp\\"  # trailing separator
    _app_entry(
        subkeys,
        values,
        _HKLM,
        _HKLM_PATH,
        "App",
        "An App",
        trailing_slash_location,
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    locations = installed_apps.get_installed_install_locations()

    import os

    assert next(iter(locations)) == os.path.normcase(os.path.normpath(trailing_slash_location))


def test_get_candidate_installed_apps_filters_but_keeps_raw_path_form(monkeypatch):
    """Unlike get_installed_install_locations, this keeps display names
    and leaves paths un-normalized -- history.record_install_locations_
    snapshot does that normalization itself, once, for every caller."""
    subkeys, values = {}, {}
    _app_entry(
        subkeys, values, _HKLM, _HKLM_PATH, "InScope", "In Scope App", r"C:\Program Files\In Scope"
    )
    _app_entry(
        subkeys,
        values,
        _HKLM,
        _HKLM_PATH,
        "OutOfScope",
        "Out of Scope App",
        r"D:\Games\Out of Scope",
    )
    fake = FakeWinreg(subkeys, values)
    monkeypatch.setattr(installed_apps, "winreg", fake)

    apps = installed_apps.get_candidate_installed_apps()

    assert apps == [("In Scope App", r"C:\Program Files\In Scope")]
