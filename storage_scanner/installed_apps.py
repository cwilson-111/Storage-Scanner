"""Windows uninstall-registry reader: what's currently installed, and where.

Windows-only, and the first registry-reading code in this app. Callers
must gate on `IS_WINDOWS` before ever importing this module (see
storage_scanner.platform_support), matching how every other OS-specific
capability here (Turbo Scan's MFT access, elevated-scan relaunch) is
isolated into its own function rather than branched inline everywhere.

Every winreg.* call is routed through the module-level `winreg` name
rather than called as a bare global, so a test can substitute a fake
implementation (`monkeypatch.setattr(installed_apps, "winreg", fake)`) --
the same dependency-injection idiom tests/test_run_elevated_scan_windows.py
already uses for ctypes.windll, applied to winreg instead.
"""

import os
import winreg

_UNINSTALL_SUBPATHS_BY_HIVE_ATTR = (
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def _uninstall_keys():
    """The three places a Windows install can register itself: the native
    64-bit tree, the WOW6432Node redirect 32-bit apps land in on a 64-bit
    OS, and a per-user (no-admin-required) install under HKCU. Missing
    any one of these would silently under-report what's actually
    installed.

    Reads winreg.HKEY_LOCAL_MACHINE/HKEY_CURRENT_USER fresh on every call
    via getattr(winreg, ...) rather than capturing them once at module
    import time -- capturing them at import would permanently bind to
    the *real* winreg module's constants even after a test replaces the
    module-level `winreg` name with a fake, since the constants would
    already have been read from the real one before the patch ever runs.
    """
    return tuple(
        (getattr(winreg, hive_attr), path) for hive_attr, path in _UNINSTALL_SUBPATHS_BY_HIVE_ATTR
    )


# Windows never localizes these literal on-disk folder names -- Explorer's
# display-name localization ("Program Files" showing translated in some
# locales) is a shell-virtualization layer over the real path, which is
# always these exact English segments. Trailing separators appended at
# use (_is_candidate_root) so "Program Files" doesn't also match an
# unrelated "Program FilesBackup" folder.
_CANDIDATE_ROOT_MARKERS = (
    "\\program files\\",
    "\\program files (x86)\\",
    "\\appdata\\local\\",
    "\\appdata\\roaming\\",
)


def _is_candidate_root(path):
    """True if `path` sits under one of the folders orphaned-install
    detection is scoped to. Anything outside these (a game on a D: drive,
    a portable app run from anywhere else) is out of scope -- not because
    it can't also be orphaned, but because InstallLocation values outside
    these conventional roots are far more likely to be a coincidental
    string match than a real leftover-detection signal."""
    normalized = os.path.normcase(os.path.normpath(path))
    return any(marker in normalized for marker in _CANDIDATE_ROOT_MARKERS)


def _read_subkey(hive, uninstall_path, name):
    """(display_name, install_location) for one uninstall subkey, or None
    if it's missing either value, or vanished mid-enumeration (a real,
    ordinary race -- another process can uninstall something while this
    read is in progress)."""
    try:
        with winreg.OpenKey(hive, f"{uninstall_path}\\{name}") as subkey:
            display_name, _type = winreg.QueryValueEx(subkey, "DisplayName")
            install_location, _type = winreg.QueryValueEx(subkey, "InstallLocation")
    except OSError:
        return None
    if not display_name or not install_location:
        return None
    return display_name, install_location


def get_installed_apps():
    """[(display_name, install_location), ...] for every currently
    installed app that both names itself and records where it lives --
    most registry entries skip InstallLocation entirely (a per-user tool,
    a browser extension, an update package), which is fine: those were
    never candidates for orphaned-install detection anyway, since nothing
    would ever match their (nonexistent) InstallLocation later either.
    """
    apps = []
    for hive, uninstall_path in _uninstall_keys():
        try:
            with winreg.OpenKey(hive, uninstall_path) as key:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, index)
                    except OSError:
                        break  # past the last subkey -- winreg's own end-of-enum signal
                    index += 1
                    entry = _read_subkey(hive, uninstall_path, name)
                    if entry is not None:
                        apps.append(entry)
        except OSError:
            # This particular hive/path doesn't exist on this machine (e.g.
            # no WOW6432Node on a 32-bit OS) -- not an error, just nothing
            # to read here.
            continue
    return apps


def get_installed_install_locations():
    """The current, candidate-root-filtered set[str] of normalized
    InstallLocation values -- the "ground truth right now" snapshot that
    history.record_install_locations_snapshot consumes to notice, over
    time, when one of these disappears."""
    locations = set()
    for _display_name, install_location in get_installed_apps():
        if _is_candidate_root(install_location):
            locations.add(os.path.normcase(os.path.normpath(install_location)))
    return locations


def get_candidate_installed_apps():
    """[(display_name, install_location), ...], filtered to just the apps
    whose InstallLocation falls under a candidate root (Program Files/
    AppData) -- the exact input shape
    history.record_install_locations_snapshot expects. Paths are left in
    their raw, as-registered form (not normalized) -- that normalization
    happens once, centrally, inside record_install_locations_snapshot
    itself, the same way for every caller."""
    return [
        (display_name, install_location)
        for display_name, install_location in get_installed_apps()
        if _is_candidate_root(install_location)
    ]
