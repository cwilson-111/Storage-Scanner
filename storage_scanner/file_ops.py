"""macOS/Linux Trash.

The Windows Recycle Bin lives in recycle_windows.py; delete_service.py is the
only caller of either. Elevated scans and relaunches are in elevation.py.
"""

import os
import shutil
import subprocess
from datetime import datetime
from urllib.parse import quote

from storage_scanner.platform_support import IS_LINUX, IS_MACOS


def _recycle_macos(path):
    """Move a file or folder to the macOS Trash via Finder (recoverable).

    Uses `osascript` to ask Finder to delete the path — pure stdlib, no
    extra dependency (send2trash, pyobjc, etc. not required). Finder resolves
    the name-collision itself if something with the same name is already in
    the Trash.
    """
    posix_path = os.path.abspath(path).replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{posix_path}"'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
    )
    return result.returncode == 0


def _xdg_trash_home():
    """The user's own home-volume XDG trash directory: ~/.local/share/Trash
    (or $XDG_DATA_HOME/Trash). Only correct for a path on the SAME
    filesystem as $HOME -- the XDG Trash spec calls for a per-mountpoint
    $topdir/.Trash-$uid instead for anything else (a removable drive, a
    separate /home mount, etc.), not implemented here. A known, stated
    scope limit rather than something silently gotten wrong -- matching
    how this project already documents similar single-case coverage
    elsewhere (e.g. mft_volume.py's single-contiguous-$MFT-extent note)."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data_home, "Trash")


def _recycle_linux_manual(path):
    """Move `path` into ~/.local/share/Trash per the XDG Trash spec,
    without depending on any trash-cli/gio tool being installed --
    _recycle_linux's fallback for a minimal/headless install that has
    neither. Only correct for paths on the same filesystem as $HOME (see
    _xdg_trash_home)."""
    trash_home = _xdg_trash_home()
    files_dir = os.path.join(trash_home, "files")
    info_dir = os.path.join(trash_home, "info")
    try:
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)

        abs_path = os.path.abspath(path)
        name = os.path.basename(abs_path.rstrip(os.sep)) or abs_path
        # The spec requires a unique name within the trash; a plain
        # collision counter (matching Explorer's/Finder's own "file (2)"
        # convention) is enough -- two concurrent deletes racing for the
        # exact same free name is astronomically unlikely for a
        # single-user desktop tool.
        dest_name, suffix = name, 1
        while os.path.exists(os.path.join(files_dir, dest_name)) or os.path.exists(
            os.path.join(info_dir, dest_name + ".trashinfo")
        ):
            suffix += 1
            dest_name = f"{name}.{suffix}"

        info_path = os.path.join(info_dir, dest_name + ".trashinfo")
        with open(info_path, "w", encoding="utf-8") as f:
            f.write("[Trash Info]\n")
            f.write(f"Path={quote(abs_path, safe='/')}\n")
            f.write(f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n")

        shutil.move(abs_path, os.path.join(files_dir, dest_name))
        return True
    except OSError:
        return False


def _recycle_linux(path):
    """Move a file or folder to the Linux desktop Trash (recoverable).

    Prefers `gio trash` (part of glib2, present on most GNOME/GTK-based
    desktops -- Ubuntu, Fedora Workstation, etc.): it correctly defers to
    whatever trash implementation the user's actual desktop environment
    uses, including the separate per-mountpoint trash a removable/non-home
    filesystem needs. Falls back to a manual XDG Trash-spec move (see
    _recycle_linux_manual) if `gio` isn't installed or fails, so deletion
    still stays recoverable even on a minimal/headless install with no
    desktop trash tool at all. Never depends on a third-party package (no
    send2trash) per this project's zero-runtime-dependency policy.
    """
    try:
        result = subprocess.run(
            ["gio", "trash", os.path.abspath(path)],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    except OSError:
        pass  # gio not installed -- fall through to the manual implementation
    return _recycle_linux_manual(path)


def recycle(path):
    """Move a file or folder to the macOS or Linux Trash (recoverable).

    Returns True on success, False otherwise. Windows deletes go through
    recycle_windows.recycle(), which reports more than success or failure.
    """
    if IS_MACOS:
        return _recycle_macos(path)
    if IS_LINUX:
        return _recycle_linux(path)
    raise RuntimeError("Windows deletes go through recycle_windows (see delete_service)")


def open_trash():
    """Open the platform Recycle Bin / Trash in the file manager, so a user
    reading the audit log can go find/restore something themselves. Pure
    stdlib: Windows' Recycle Bin is a virtual shell folder, not a real
    filesystem path `os.startfile` can open directly.
    """
    try:
        if IS_MACOS:
            subprocess.run(["open", os.path.expanduser("~/.Trash")], check=True)
        elif IS_LINUX:
            try:
                # "trash:///" is the GVFS URI most file managers (Nautilus,
                # Nemo, ...) render as the proper, familiar Trash view --
                # nicer than the raw ~/.local/share/Trash/files folder,
                # which mixes deleted items in with the spec's own
                # .trashinfo metadata files.
                subprocess.run(["gio", "open", "trash:///"], check=True)
            except (OSError, subprocess.CalledProcessError):
                subprocess.run(["xdg-open", _xdg_trash_home()], check=True)
        else:
            subprocess.run(["explorer.exe", "shell:RecycleBinFolder"], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
