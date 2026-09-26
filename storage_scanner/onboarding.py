"""First-run "Before you start" guide: what it says, and whether to show it.

Plain functions with no Tk dependency, so the show-once decision and the
per-platform text can be exercised without a display. The dialog itself
lives in storage_scanner/ui/onboarding_window.py.

Every sentence below describes what the current code actually does on that
platform — keep it that way when behavior changes:

- Deletion: file_ops.recycle() (Recycle Bin via SHFileOperationW with
  FOF_ALLOWUNDO on Windows; Finder on macOS; `gio trash` or a manual XDG
  Trash move on Linux), always reached through audit.recycle_and_log().
- Permissions: scanner._scan_one()/_rollup() and main_window's ⚠ rows and
  inaccessible-paths banner; main_window._request_elevation().
- Cloud placeholders: scanner.is_cloud_placeholder_attrs() only ever sees
  Windows file attributes, so placeholders are only recognized there;
  DuplicatesMixin._find_duplicate_files skips them.
- Protected locations: settings.DEFAULT_DUPLICATE_EXCLUDES, via
  cleanup_recommendations.is_protected_path().
"""

from history import get_app_metadata, set_app_metadata
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import IS_LINUX, IS_MACOS, IS_ROOT

ONBOARDING_SEEN_KEY = "onboarding_seen"

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"

_CURRENT_PLATFORM = MACOS if IS_MACOS else LINUX if IS_LINUX else WINDOWS


def should_show_onboarding():
    """True until the guide has been marked seen. A metadata read failure
    also returns True: showing the guide again is harmless, and hiding it
    because the store is unreadable would mean a new user never sees it."""
    try:
        return get_app_metadata(ONBOARDING_SEEN_KEY) != "1"
    except Exception:  # noqa: BLE001 - never block startup over the guide
        logger.warning("Could not read %s from app metadata", ONBOARDING_SEEN_KEY, exc_info=True)
        return True


def mark_onboarding_seen():
    """Persist that the guide was shown. Returns False (and logs) if the
    write failed — the only consequence is that it shows again next launch."""
    try:
        set_app_metadata(ONBOARDING_SEEN_KEY, "1")
    except Exception:  # noqa: BLE001 - never crash the GUI over the guide
        logger.warning("Could not save %s to app metadata", ONBOARDING_SEEN_KEY, exc_info=True)
        return False
    return True


def _safe_deletion(platform):
    if platform == WINDOWS:
        where = (
            "Delete sends files and folders to the Recycle Bin instead of erasing them, "
            "though Windows erases outright anything its Recycle Bin can't hold, such as "
            "items on most network drives."
        )
    elif platform == MACOS:
        where = (
            "Delete asks Finder to move files and folders to the Trash instead of erasing "
            "them, so you can put them back from there."
        )
    else:
        where = (
            "Delete moves files and folders to your desktop Trash instead of erasing them, "
            "so you can restore them from there."
        )
    return (
        where + " Cleanup Recommendations only suggest: nothing is removed until you "
        "select it and confirm. Every delete is recorded in the Audit Log (Tools ▸ "
        "History & Trust), and a duplicate group's keeper copy can't be deleted from "
        "Find Duplicate Files."
    )


def _permission_limits(platform, is_root):
    marked = (
        "Folders this app can't open are marked ⚠, as is every folder containing them, "
        "and whatever is inside them is left out of the totals; after a scan, a banner "
        "says how many there were and can list them."
    )
    if platform == WINDOWS:
        if is_root:
            return (
                marked + " This window is already running as administrator, but a few "
                "system folders (such as System Volume Information) stay unreadable "
                "even so."
            )
        return (
            marked + " 🔒 Run as Admin restarts the app as administrator through a "
            "Windows UAC prompt so those folders get counted, though a few system "
            "folders (such as System Volume Information) stay unreadable even then."
        )
    if is_root:
        return (
            marked + " This app is already running as root, but some system files may "
            "stay unreadable even so."
        )
    if platform == MACOS:
        return (
            marked + " 🔒 Run as Admin re-scans the selected folder with root access "
            "after you enter your Mac password; this window stays open. macOS still "
            "protects some system files, so a few paths may stay unreadable."
        )
    return (
        marked + " 🔒 Run as Admin re-scans the selected folder with root access after "
        "a PolicyKit password prompt (a PolicyKit authentication agent must be "
        "running); this window stays open. Some system files may stay unreadable even "
        "to root."
    )


def _cloud_placeholders(platform):
    if platform == WINDOWS:
        return (
            "OneDrive-style online-only files are marked ☁: Size shows their full size, "
            "while On Disk shows the little they use locally, marked (online-only). "
            "Find Duplicate Files skips them rather than downloading them to compare, "
            "and Cleanup Recommendations lists them as Protected."
        )
    return (
        "Online-only cloud placeholders are only recognized on Windows. Here, a "
        "cloud-synced file that isn't stored locally still shows its full size in "
        "Size, while On Disk shows the local space the system reports for it. Find "
        "Duplicate Files doesn't skip such files, so comparing one can make your sync "
        "app download it."
    )


def _protected_locations(platform):
    if platform == WINDOWS:
        examples = (
            "the Windows folder, Program Files\\WindowsApps, ProgramData\\Microsoft, "
            "System Volume Information and the Recycle Bin"
        )
    elif platform == MACOS:
        examples = "/System, /Library/Caches, /private/var and the Trash"
    else:
        examples = "/usr, /etc, /proc, /var/cache and the Trash"
    return (
        f"System and app-managed locations, such as {examples}, are skipped by Find "
        "Duplicate Files. Cleanup Recommendations lists files there as Protected and "
        "won't delete or archive them. Delete in the main tree and in Search & Filter "
        "doesn't check this list, so look twice before removing anything there."
    )


def onboarding_sections(platform=_CURRENT_PLATFORM, is_root=IS_ROOT):
    """The guide's (heading, body) pairs for `platform` ("windows", "macos"
    or "linux"; defaults to the running OS) and elevation state."""
    return [
        ("Safe deletion", _safe_deletion(platform)),
        ("Permission limits", _permission_limits(platform, is_root)),
        ("Cloud placeholders", _cloud_placeholders(platform)),
        ("Protected locations", _protected_locations(platform)),
    ]
