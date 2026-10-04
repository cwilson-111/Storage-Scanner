"""The fixtures turbo_checklist.py scans: the files it writes, the changes
it makes to them, the sizes each scan must then show, and the scratch
folder they all live in. Everything here but writing to that folder is
plain logic, tested without elevation (tests/test_turbo_checklist.py).
"""

import os
import shutil
import subprocess
from dataclasses import dataclass

from storage_scanner.models import iter_folders

CREATE, GROW, RENAME, DELETE = "create", "grow", "rename", "delete"


@dataclass(frozen=True)
class Change:
    """One change to a file of the tracked folder (names relative to it)."""

    action: str
    name: str
    size: int = 0  # CREATE, GROW: the file's size afterwards
    new_name: str = ""  # RENAME, into another folder too


# name -> bytes, each at least 10,000: a small enough file lives inside its
# MFT record, and the Compatible engine bills that as a whole cluster.
TRACKED_FILES = {
    "grow.bin": 10_000,
    "old_name.txt": 20_000,
    "move_me.bin": 25_000,
    "delete_me.bin": 30_000,
    "during_full_read.bin": 10_000,
    "during_incremental.bin": 10_000,
    os.path.join("nested", "keep.bin"): 50_000,
}
KNOWN_CHANGES = (
    Change(CREATE, os.path.join("nested", "created.bin"), size=40_000),
    Change(GROW, "grow.bin", size=250_000),
    Change(RENAME, "old_name.txt", new_name="new_name.txt"),
    Change(RENAME, "move_me.bin", new_name=os.path.join("nested", "moved.bin")),
    Change(DELETE, "delete_me.bin"),
)
GROWN_DURING_FULL_READ = Change(GROW, "during_full_read.bin", size=3_000_000)
GROWN_DURING_INCREMENTAL = Change(GROW, "during_incremental.bin", size=2_000_000)

# Compressible text: NTFS-compressed under ntfs\, Compact OS-compressed under wof\.
COMPRESSED_FILES = {
    os.path.join("ntfs", "text.txt"): 4_000_000,
    os.path.join("ntfs", "deeper", "text.txt"): 400_000,
    os.path.join("wof", "text.txt"): 4_000_000,
}
JUNCTION_TARGET_FILES = {"a.bin": 100_000, "b.bin": 100_000, os.path.join("sub", "c.bin"): 100_000}
BESIDE_THE_JUNCTION = {"beside.bin": 15_000}


def apply_changes(files, changes):
    """`files` (name -> size) as `changes` leave it, without the disk."""
    result = dict(files)
    for change in changes:
        if change.action in (CREATE, GROW):
            result[change.name] = change.size
        elif change.action == RENAME:
            result[change.new_name] = result.pop(change.name)
        elif change.action == DELETE:
            del result[change.name]
        else:
            raise ValueError(f"unknown change {change.action!r}")
    return result


def _either(before, after):
    return {name: tuple(sorted({before.get(name, size), size})) for name, size in after.items()}


def expected_sizes():
    """name -> the sizes it may show, for each scan of the tracked folder in
    order: the full read, the incremental scan after KNOWN_CHANGES, the last
    one. A file grown during a scan may show either size in that scan, and
    only its new size in every scan after it."""
    after_full_read = apply_changes(TRACKED_FILES, [GROWN_DURING_FULL_READ])
    after_changes = apply_changes(after_full_read, KNOWN_CHANGES)
    final = apply_changes(after_changes, [GROWN_DURING_INCREMENTAL])
    return [
        _either(TRACKED_FILES, after_full_read),
        _either(after_changes, final),
        _either(final, final),
    ]


def file_sizes(root):
    """name relative to `root` -> size, for every file in its tree."""
    skip = len(root.path.rstrip("\\/")) + 1
    return {f.path[skip:]: f.size for folder in iter_folders(root) for f in folder.files()}


def size_problems(actual, expected):
    """Readable lines for where `actual` (name -> size) differs from
    `expected` (name -> the sizes allowed); empty when it doesn't."""
    problems = [f"{name}: missing" for name in sorted(set(expected) - set(actual))]
    problems += [f"{name}: shouldn't be there" for name in sorted(set(actual) - set(expected))]
    for name in sorted(set(actual) & set(expected)):
        if actual[name] not in expected[name]:
            allowed = " or ".join(f"{size:,}" for size in expected[name])
            problems.append(f"{name}: {actual[name]:,} bytes, expected {allowed}")
    return problems


def describe_change(change):
    if change.action == CREATE:
        return f"created {change.name}"
    if change.action == GROW:
        return f"grew {change.name} to {change.size:,} bytes"
    if change.action == RENAME:
        return f"renamed {change.name} to {change.new_name}"
    return f"deleted {change.name}"


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = b"Storage Scanner checklist fixture.\r\n"
    with open(path, "wb") as f:
        f.write((line * (size // len(line) + 1))[:size])


def write_files(folder, files):
    """Write `files` (name -> size) under `folder`, as compressible text."""
    for name, size in files.items():
        _write(os.path.join(folder, name), size)


def make_change(folder, change):
    """Make `change` on disk, to the tracked folder `folder`."""
    path = os.path.join(folder, change.name)
    if change.action == CREATE:
        _write(path, change.size)
    elif change.action == GROW:
        with open(path, "ab") as f:
            f.write(b"+" * (change.size - os.path.getsize(path)))
    elif change.action == RENAME:
        new_path = os.path.join(folder, change.new_name)
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(path, new_path)
    else:
        os.remove(path)


class ScanWatcher:
    """The progress queue handed to a scan: keeps every phase it posts and,
    given a `change`, makes it in `folder` at the first `label` phase whose
    count is past `after` -- while the scan is under way."""

    def __init__(self, folder=None, change=None, label=None, after=-1):
        self.folder, self.change, self.label, self.after = folder, change, label, after
        self.phases = []
        self.made_at = None  # the Phase the change was made at

    def put(self, item):
        kind, phase = item
        if kind != "phase":
            return
        self.phases.append(phase)
        if (
            self.change is not None
            and self.made_at is None
            and phase.label == self.label
            and (phase.done or 0) > self.after
        ):
            make_change(self.folder, self.change)
            self.made_at = phase

    def make_change_if_missed(self):
        """Make the change now if the scan never got to it, so the steps
        after still know what's on disk. True if it had to."""
        if self.change is None or self.made_at is not None:
            return False
        make_change(self.folder, self.change)
        return True


def run_tool(args):
    """Run a console tool (compact, mklink); raises with its output if it fails."""
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        output = (done.stdout + done.stderr).strip()
        raise RuntimeError(f"{' '.join(args)} failed ({done.returncode}): {output}")


def make_junction(junction, target):
    run_tool(["cmd", "/c", "mklink", "/J", junction, target])


def remove_scratch(scratch):
    """Delete the scratch folder, or say why it couldn't be. shutil.rmtree
    removes a junction as a link, never what it points to."""
    try:
        shutil.rmtree(scratch)
    except OSError as exc:
        return f"could not delete the scratch folder {scratch}: {exc}"
    return None
