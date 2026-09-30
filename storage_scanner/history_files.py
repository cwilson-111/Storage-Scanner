"""Files kept beside the scan-history database (history.py): the copy made
before a migration, and a damaged database moved out of the way so the app
can start a new one.

Free of any import from history.py for the same reason as history_schema:
storage_scanner.logging_setup imports history, so the database path is
passed in, and the log is reached by the logger's name.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Union

logger = logging.getLogger("storage_scanner")

# SQLite's primary result codes for a file that isn't a readable database:
# SQLITE_CORRUPT ("database disk image is malformed") and SQLITE_NOTADB
# ("file is not a database"). Anything else -- a lock held too long, a full
# disk, a folder without write access -- says nothing about the file itself.
_DAMAGED_CODES = (11, 26)
_DAMAGED_MESSAGES = ("database disk image is malformed", "file is not a database")

# The files SQLite keeps beside a database in WAL mode. They belong to it:
# left behind, a new database of the same name would read the old one's
# write-ahead log as its own.
_COMPANION_SUFFIXES = ("-wal", "-shm")


def is_damaged(exc: sqlite3.DatabaseError) -> bool:
    """Whether exc says the database file itself can't be read."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        # Before Python 3.11 the exception doesn't carry the code.
        return str(exc) in _DAMAGED_MESSAGES
    return code & 0xFF in _DAMAGED_CODES


def sibling_path(db_path: Union[str, Path], label: str) -> Path:
    """<name>.<label>-<YYYYMMDD-HHMMSS><suffix> beside db_path, numbered if
    that name is taken (two in the same second)."""
    db_path = Path(db_path)
    stem = f"{db_path.stem}.{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    candidate = db_path.with_name(stem + db_path.suffix)
    number = 2
    while candidate.exists():
        candidate = db_path.with_name(f"{stem}-{number}{db_path.suffix}")
        number += 1
    return candidate


def back_up(db_path: Union[str, Path], version: int) -> Path:
    """Copy the database at db_path to <name>.v<version>-backup-<time>
    beside it, and return where. SQLite's backup API reads it through a
    connection of its own, so the copy holds what's still only in the -wal
    file too.

    Meant to be called while the migrating connection holds the write lock
    (BEGIN IMMEDIATE) but hasn't changed anything yet: nothing can commit
    in between, so this reads exactly the database about to be migrated.
    It can't read through that connection itself -- SQLite refuses to back
    up from a connection with a write transaction open, and Python's
    backup() retries that forever. A copy that fails part way is removed
    and the error raised: the caller doesn't migrate without one."""
    target = sibling_path(db_path, f"v{version}-backup")
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    except BaseException:
        copy.close()
        target.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    copy.close()
    logger.info("Copied %s to %s before migrating it", db_path, target)
    return target


def move_aside(db_path: Union[str, Path], label: str) -> Path:
    """Rename the database (and its -wal and -shm files) to
    <name>.<label>-<time> beside it, and return the database's new path.
    The database goes first: if something still has it open, that fails
    before anything has moved."""
    db_path = Path(db_path)
    target = sibling_path(db_path, label)
    os.replace(db_path, target)
    for suffix in _COMPANION_SUFFIXES:
        companion = Path(f"{db_path}{suffix}")
        if companion.exists():
            os.replace(companion, f"{target}{suffix}")
    return target
