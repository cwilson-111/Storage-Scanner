"""Where the scan-history database lives, and opening it: the app-data
folder, the connection every history module uses, creating or migrating
the tables at startup (history_schema), moving a damaged file aside
(history_files), and the app_metadata key/value table the app's settings
live in.

The other history modules -- history_store (saving and removing scans),
history_queries (reading them back) and history_records (the delete
ledger, budgets and install locations) -- all connect through connect(),
which reads DB_NAME when it's called, so pointing DB_NAME elsewhere (as
the tests and benchmarks do) moves every one of them.
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path

from storage_scanner import history_files, history_schema

APP_NAME = "NeuralStorageMatrix"

if sys.platform == "darwin":
    _APP_DATA_BASE = Path.home() / "Library" / "Application Support"
elif sys.platform.startswith("linux"):
    # Matches file_ops.py's _xdg_trash_home() convention: the XDG Base
    # Directory spec's per-user data location, not LOCALAPPDATA (a Windows-
    # only env var that's never set on Linux, which used to make this fall
    # straight through to Path.home() -- dumping storage_history.db and the
    # log directory loose in the home directory instead of a proper,
    # XDG-standard app-data folder).
    _APP_DATA_BASE = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
else:
    _APP_DATA_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))

APP_DATA_DIR = _APP_DATA_BASE / APP_NAME

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_NAME = APP_DATA_DIR / "storage_history.db"

# A bare `print` here used to go to stdout unconditionally — harmless for a
# normal GUI launch, but it corrupts the `--priv-scan` helper's JSON output,
# which `do shell script` captures as its literal return value (see
# run_elevated_scan_macos). Logging instead keeps stdout clean for whichever
# process actually needs it. Using the stdlib logging module directly (not
# storage_scanner.logging_setup.logger) avoids a circular import:
# logging_setup itself imports APP_DATA_DIR from this module — both name the
# same "storage_scanner" logger either way.
logging.getLogger("storage_scanner").debug("Using database: %s", DB_NAME)


# How long a connection waits for another process's write lock before
# failing with "database is locked" (Python's default is 5 s). Saves and
# settings hold that lock for well under a second. The one long holder is
# the one-time migration in init_history_db (history_schema, plus its
# VACUUM): 4.9 s for 1.2 million version 1 folder rows, and 9.6-20.9 s for
# 1.8 million (the slow run shared the machine with the test suite) --
# 5-12 us a row. 60 s is three times the worst of those and covers 5-11
# million rows, far beyond any real version 1 history (this machine's has
# 25,846) -- so a scheduled scan that starts while the app is migrating
# waits for it instead of losing its save. It only ever delays anything
# while another process really is holding the lock.
BUSY_TIMEOUT_SECONDS = 60


def connect(**kwargs):
    """A new connection to the history database; the caller closes it."""
    return sqlite3.connect(DB_NAME, timeout=BUSY_TIMEOUT_SECONDS, **kwargs)


def connect_for_write():
    """A connection for changing history rows. Refused (history_schema.
    NewerDatabaseError) once a newer version of the app has upgraded the
    database past what this one knows -- checked on every write, not just at
    startup, since that can happen while this copy is running (a scheduled
    scan by the newer one)."""
    conn = connect()
    try:
        history_schema.checked_version(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def init_history_db():
    """Create the history tables, or migrate an older layout to the current
    one (storage_scanner.history_schema), in a single transaction, after
    copying the old file beside it (history_files.back_up). Cheap and safe
    to call on every startup and before every CLI save: once the schema is
    current it changes nothing.

    A database from a newer version of the app raises
    history_schema.NewerDatabaseError without being changed at all. A file
    that isn't a readable database raises sqlite3.DatabaseError; see
    open_history_db."""
    conn = connect(isolation_level=None)
    try:
        # Read first, before even the journal mode is set, so a database
        # from a newer version is left exactly as that version wrote it.
        history_schema.checked_version(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")

        # IMMEDIATE takes the write lock before the version is read, so a
        # second process starting at the same moment waits, then finds the
        # schema already current instead of migrating it again.
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored_version = history_schema.checked_version(conn)
            if stored_version is not None and stored_version < history_schema.SCHEMA_VERSION:
                history_files.back_up(DB_NAME, stored_version)
            migrated = history_schema.ensure_schema(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

        if migrated:
            # The old layout's pages are all free now and the new one needs
            # a fraction of them, so shrink the file once. Nothing else ever
            # vacuums: in steady state each save's pruning frees about what
            # the next save needs, and SQLite reuses free pages.
            try:
                conn.execute("VACUUM")
            except sqlite3.Error:
                logging.getLogger("storage_scanner").warning(
                    "Could not compact %s after migrating it", DB_NAME, exc_info=True
                )
    finally:
        conn.close()


def open_history_db():
    """What the app and a scheduled scan call before using the history:
    init_history_db(), except that a file SQLite can't read as a database
    at all (damaged, or not a database) is moved aside with a timestamp
    (history_files.move_aside) and a new, empty history started in its
    place -- a bad file costs its history, not the app. Returns the message
    to show the user when that happened, else None.

    Everything else still raises: a database from a newer version
    (history_schema.NewerDatabaseError) isn't damaged, and the version that
    wrote it still wants it; nor is one that is locked, or on a full disk.
    """
    try:
        init_history_db()
        return None
    except sqlite3.DatabaseError as exc:
        if not history_files.is_damaged(exc):
            raise
        error = str(exc)
    moved_to = history_files.move_aside(DB_NAME, "damaged")
    init_history_db()
    message = (
        f"The scan history file couldn't be read ({error}). It was moved to "
        f"{moved_to}, and a new, empty history was started."
    )
    logging.getLogger("storage_scanner").warning(message)
    return message


def get_app_metadata(key, default=None):
    """Read one value from the app_metadata key/value table (e.g. the
    schema version, or the update-checker's last-checked timestamp)."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_metadata WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_app_metadata(key, value):
    """Set (or update) one value in the app_metadata key/value table.

    The one write still allowed on a database from a newer version (see
    connect_for_write): a key/value row every version reads the same way,
    and refusing it would also stop the update check recording when it last
    ran -- the check that tells this user a newer version exists."""
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO app_metadata (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """,
        (key, value),
    )
    conn.commit()
    conn.close()
