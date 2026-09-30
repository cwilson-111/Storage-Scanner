"""Which paths Storage Scanner refuses to delete, whichever window asks.

delete_service.DeleteService checks every request here before anything
touches the disk, so no window can skip it:

- a volume root (C:\\, a network share's root, a subst drive, / or any
  other mount point), and the folder the current scan started from;
- the operating system's and the user's own top-level folders -- Windows,
  Program Files (both), ProgramData, the profile root and its known
  folders (Desktop, Documents, Downloads, Pictures, Music, Videos,
  AppData...), or /, /usr, $HOME, ~/Documents... elsewhere -- and any
  folder that contains one. What's *inside* them stays deletable;
- on Windows, a path with a name ending in a dot or a space. Win32 path
  handling drops that ending, so every ordinary API -- open(), stat, and
  the shell's Recycle Bin -- silently acts on a different item ("t.bin"
  for "t.bin."). Extended \\\\?\\ paths reach the real item, but the
  Recycle Bin can't take them (SHFileOperation rejects them, and Explorer
  itself can't delete such names), so the only way to delete one would be
  permanently. Refusing, and leaving such files out of duplicate matching
  (duplicate_finder), keeps every delete recoverable and every hash about
  the file it names.

Plus the typed-name confirmation a very large folder needs (see
needs_typed_confirmation). Tk-free.
"""

import contextlib
import ctypes
import ntpath
import os
import re
import uuid
from functools import lru_cache

from storage_scanner.platform_support import IS_MACOS, IS_WINDOWS

# Deleting a folder at least this big, or holding at least this many files,
# needs its name typed to confirm: a mis-click there costs the most, and
# the Recycle Bin may not have room to hand it back.
LARGE_FOLDER_BYTES = 10 * 1024**3
LARGE_FOLDER_FILES = 50_000

# Windows Known Folder IDs (KnownFolders.h), and how a refusal names each.
_KNOWN_FOLDERS = (
    ("{F38BF404-1D43-42F2-9305-67DE0B28FC23}", "the Windows folder"),
    ("{905E63B6-C1BF-494E-B29C-65B732D3D21A}", "Program Files"),
    ("{6D809377-6AF0-444B-8957-A3773F02200E}", "Program Files"),  # x64, from a 32-bit app
    ("{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}", "Program Files (x86)"),
    ("{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}", "ProgramData"),
    ("{0762D272-C50A-4BB0-A382-697DCD729B80}", "the folder that holds every user profile"),
    ("{DFDF76A2-C82A-4D63-906A-5644AC457385}", "the Public user profile"),
    ("{5E6C858F-0E22-4760-9AFE-EA3317B67173}", "your user profile folder"),
    ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "your Desktop folder"),
    ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "your Documents folder"),
    ("{374DE290-123F-4565-9164-39C4925E467B}", "your Downloads folder"),
    ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "your Pictures folder"),
    ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "your Music folder"),
    ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "your Videos folder"),
    ("{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}", "your AppData\\Roaming folder"),
    ("{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}", "your AppData\\Local folder"),
    ("{A520A1A4-1780-4FF6-BD18-167343C5AF16}", "your AppData\\LocalLow folder"),
    ("{A52BBA46-E9E1-435F-B3D9-28DAA648C0F6}", "your OneDrive folder"),
)

# The same folders by environment variable, in case a Known Folder lookup
# fails (both are used; duplicates don't matter).
_WINDOWS_ENV_FOLDERS = (
    ("SystemRoot", "the Windows folder"),
    ("windir", "the Windows folder"),
    ("ProgramFiles", "Program Files"),
    ("ProgramW6432", "Program Files"),
    ("ProgramFiles(x86)", "Program Files (x86)"),
    ("ProgramData", "ProgramData"),
    ("PUBLIC", "the Public user profile"),
    ("USERPROFILE", "your user profile folder"),
    ("APPDATA", "your AppData\\Roaming folder"),
    ("LOCALAPPDATA", "your AppData\\Local folder"),
)

_POSIX_SYSTEM_FOLDERS = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib32",
    "/lib64",
    "/media",
    "/mnt",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
)
_MACOS_SYSTEM_FOLDERS = (
    "/Applications",
    "/Library",
    "/System",
    "/Users",
    "/Volumes",
    "/cores",
    "/private",
)
_MACOS_HOME_FOLDERS = (
    "Applications",
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
)
_LINUX_HOME_FOLDERS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Music",
    "Pictures",
    "Public",
    "Templates",
    "Videos",
    ".config",
    ".local",
    ".local/share",
)


def _known_folder_path(guid_text):
    """SHGetKnownFolderPath for one FOLDERID, or None if it has no path."""

    class _GUID(ctypes.Structure):
        _fields_ = [("raw", ctypes.c_ubyte * 16)]

    guid = _GUID.from_buffer_copy(uuid.UUID(guid_text).bytes_le)
    path_ptr = ctypes.c_wchar_p()
    try:
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
        )
        return path_ptr.value if result == 0 else None
    except OSError:
        return None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)


def _windows_folders():
    folders = []
    for guid, label in _KNOWN_FOLDERS:
        path = _known_folder_path(guid)
        if path:
            folders.append((path, label))
            if guid.startswith("{3EB685DB"):  # Roaming lives in the AppData folder itself
                folders.append((os.path.dirname(path), "your AppData folder"))
    for variable, label in _WINDOWS_ENV_FOLDERS:
        path = os.environ.get(variable)
        if path:
            folders.append((path, label))
    # The default locations too, for when a known folder has been moved
    # (to OneDrive, say) and the old folder is still there.
    profile = os.environ.get("USERPROFILE")
    if profile:
        for name in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"):
            folders.append((os.path.join(profile, name), f"your {name} folder"))
    return list(dict(folders).items())


def _posix_folders(is_macos):
    home = os.path.expanduser("~")
    system = _POSIX_SYSTEM_FOLDERS + (_MACOS_SYSTEM_FOLDERS if is_macos else ())
    folders = [(path, f"the system folder {path}") for path in system]
    folders.append((home, "your home folder"))
    for name in _MACOS_HOME_FOLDERS if is_macos else _LINUX_HOME_FOLDERS:
        folders.append((os.path.join(home, name), f"your {name} folder"))
    return folders


def _keys(path):
    """The comparable forms of `path`: as given, and with links, junctions
    and subst drives resolved (so Q:\\Documents, with Q: substituted for
    the profile, is still the Documents folder)."""
    keys = {os.path.normcase(os.path.normpath(path))}
    with contextlib.suppress(OSError, ValueError):
        keys.add(os.path.normcase(os.path.realpath(path)))
    return keys


@lru_cache(maxsize=1)
def protected_folders():
    """((comparable keys, path, label), ...) for this machine's protected
    folders. Computed once: they don't move while the app runs."""
    folders = _windows_folders() if IS_WINDOWS else _posix_folders(IS_MACOS)
    return tuple((frozenset(_keys(path)), path, label) for path, label in folders)


def name_is_trimmed_by_windows(name):
    """True for a name Win32 path handling silently shortens ("t.bin." or
    "t.bin " becomes "t.bin")."""
    return name[-1:] in (".", " ")


def windows_trims_path(path):
    """True if any name in `path` ends in a dot or a space (see the module
    docstring). Uses ntpath explicitly, so it means the same on any OS."""
    _drive, tail = ntpath.splitdrive(path)
    return any(
        name_is_trimmed_by_windows(part) and part not in (".", "..")
        for part in re.split(r"[\\/]", tail)
        if part
    )


def is_volume_root(path):
    """A drive or share root, or any other mount point."""
    _drive, tail = os.path.splitdrive(os.path.normpath(path))
    if tail in ("", "\\", "/"):
        return True
    try:
        return os.path.ismount(path)
    except (OSError, ValueError):
        return False


def _trimmed(path):
    return re.sub(r"[. ]+(?=$|[\\/])", "", path)


def refusal_reason(path, scan_roots=(), protected=None):
    """None if `path` may be deleted, else why not (a sentence or two, for
    the user and the audit ledger).

    `scan_roots`: the folder(s) the scan it came from started at.
    `protected`: [(path, label), ...] to use instead of this machine's own
    protected_folders() (tests).
    """
    if IS_WINDOWS and windows_trims_path(path):
        return (
            f"A name in {path} ends in a dot or a space. Windows drops that "
            f"ending and would act on {_trimmed(path)} instead, and the Recycle Bin "
            "can't hold such a name, so Storage Scanner won't delete it (or compare it "
            "as a duplicate). Rename it first, for example from WSL or with: "
            f'ren "\\\\?\\{path}" newname'
        )
    if is_volume_root(path):
        return (
            f"{path} is the root of a drive. Storage Scanner never deletes a "
            "whole drive; delete what's inside it instead."
        )
    keys = _keys(path)
    for root in scan_roots:
        if root and keys & _keys(root):
            return (
                f"{path} is the folder this scan started from. Delete what's "
                "inside it instead, or scan its parent folder."
            )
    folders = (
        protected_folders()
        if protected is None
        else [(frozenset(_keys(p)), p, label) for p, label in protected]
    )
    for folder_keys, _folder_path, label in folders:
        if keys & folder_keys:
            return (
                f"{path} is {label}. Storage Scanner never deletes system or "
                "profile folders themselves; what's inside them can still be deleted."
            )
    prefixes = tuple(key.rstrip("\\/") + os.sep for key in keys)
    for folder_keys, folder_path, label in folders:
        if any(folder_key.startswith(prefixes) for folder_key in folder_keys):
            return (
                f"{path} contains {label} ({folder_path}). Storage Scanner "
                "never deletes system or profile folders, or anything holding one."
            )
    return None


def needs_typed_confirmation(node):
    """True for a folder big enough that deleting it needs its name typed."""
    return bool(node.is_dir) and (
        node.size >= LARGE_FOLDER_BYTES or getattr(node, "file_count", 0) >= LARGE_FOLDER_FILES
    )


def typed_name_matches(typed, node):
    return typed.strip() == node.name.strip()
