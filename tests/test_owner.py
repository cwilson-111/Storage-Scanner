"""Tests for storage_scanner.owner: reading who owns a file or folder, and
OwnerLookup's worker thread, which the main tree's Owner column asks."""

import getpass
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.owner import OwnerLookup, owner_of


def _me():
    if sys.platform == "win32":
        return getpass.getuser().lower()
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def test_what_you_create_is_yours_and_a_missing_path_has_no_owner(tmp_path):
    made = tmp_path / "made.txt"
    made.write_text("x")
    (tmp_path / "folder").mkdir()

    owner = owner_of(str(made))

    if sys.platform == "win32":
        # "DOMAIN\name"; an elevated administrator's files belong to the group.
        domain, _sep, name = owner.rpartition("\\")
        assert domain and name.lower() in (_me(), "administrators")
    else:
        assert owner == _me()
    assert owner_of(str(tmp_path / "folder")) == owner
    assert owner_of(str(tmp_path / "gone.txt")) == ""


def _collect(lookups, count):
    found = []
    deadline = time.monotonic() + 5
    while len(found) < count and time.monotonic() < deadline:
        found += lookups.results(100)
        time.sleep(0.005)
    return found


def test_lookups_come_back_in_order_and_a_cancel_drops_everything_asked_before_it():
    started, release = threading.Event(), threading.Event()

    def lookup(path):
        if path == "slow":
            started.set()
            release.wait(5)
        if path == "broken":
            raise OSError("unreadable")
        return path.upper()

    lookups = OwnerLookup(lookup)
    lookups.request(["a", "broken", "b"])
    assert _collect(lookups, 3) == [("a", "A"), ("broken", ""), ("b", "B")]

    lookups.request(["slow", "c"])
    assert started.wait(5)
    lookups.cancel()  # a new scan, while "slow" is still being read
    lookups.request(["d"])
    release.set()

    assert _collect(lookups, 1) == [("d", "D")]
    time.sleep(0.05)
    assert lookups.results(100) == []  # neither "slow" nor "c"
