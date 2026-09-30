import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from storage_scanner import history_files, history_retention, history_schema
from storage_scanner.delete_outcome import is_removed

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
# storage_scanner/file_ops.py's run_elevated_scan_macos). Logging instead
# keeps stdout clean for whichever process actually needs it. Using the
# stdlib logging module directly (not storage_scanner.logging_setup.logger)
# avoids a circular import: logging_setup itself imports APP_DATA_DIR from
# this module — both name the same "storage_scanner" logger either way.
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


def _connect(**kwargs):
    return sqlite3.connect(DB_NAME, timeout=BUSY_TIMEOUT_SECONDS, **kwargs)


def _connect_for_write():
    """A connection for changing history rows. Refused (history_schema.
    NewerDatabaseError) once a newer version of the app has upgraded the
    database past what this one knows -- checked on every write, not just at
    startup, since that can happen while this copy is running (a scheduled
    scan by the newer one)."""
    conn = _connect()
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
    conn = _connect(isolation_level=None)
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
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_metadata WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_app_metadata(key, value):
    """Set (or update) one value in the app_metadata key/value table.

    The one write still allowed on a database from a newer version (see
    _connect_for_write): a key/value row every version reads the same way,
    and refusing it would also stop the update check recording when it last
    ran -- the check that tells this user a newer version exists."""
    conn = _connect()
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


def save_scan_snapshot(
    scan_path,
    total_size,
    drive_capacity,
    file_count,
    folder_count,
    folder_sizes,
    allocated_size=None,
    drive_used=None,
    drive_free=None,
):
    """
    Saves one scan result into SQLite, then applies history retention
    (storage_scanner.history_retention) to that scan path's older scans,
    all in one transaction. allocated_size is the scanned path's on-disk
    size; drive_used/drive_free its drive's space right now (None when it
    couldn't be read).

    folder_sizes example:
    {
        "C:\\Users\\Cole\\Downloads": {
            "size": 123456789,
            "file_count": 312
        }
    }
    """

    created_at = datetime.now().isoformat(timespec="seconds")

    conn = _connect_for_write()
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO scans
                (scan_path, total_size, drive_capacity, file_count, folder_count, created_at,
                 allocated_size, drive_used, drive_free)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_path,
                    total_size,
                    drive_capacity,
                    file_count,
                    folder_count,
                    created_at,
                    allocated_size,
                    drive_used,
                    drive_free,
                ),
            )
            scan_id = cur.lastrowid
            _insert_folder_rows(cur, scan_id, folder_sizes)
            _prune_scans(cur, scan_path, scan_id, created_at)
    finally:
        conn.close()

    return scan_id


def _insert_folder_rows(cur, scan_id, folder_sizes):
    """One folder_snapshots row per folder, keyed by its path's id in
    folder_paths (added there first if it's new). Staged through a temp
    table so matching every path to its id is one join, not a query per
    folder."""
    cur.execute("""
        CREATE TEMP TABLE scan_folders (
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            file_count INTEGER NOT NULL
        )
    """)
    cur.executemany(
        "INSERT INTO temp.scan_folders VALUES (?, ?, ?)",
        (
            (folder_path, int(data.get("size", 0)), int(data.get("file_count", 0)))
            for folder_path, data in folder_sizes.items()
        ),
    )
    cur.execute("INSERT OR IGNORE INTO folder_paths (path) SELECT path FROM temp.scan_folders")
    # In path_id order: appends to the end of this scan's primary-key range.
    cur.execute(
        """
        INSERT INTO folder_snapshots (scan_id, path_id, size_bytes, file_count)
        SELECT ?, p.id, t.size_bytes, t.file_count
        FROM temp.scan_folders t
        JOIN folder_paths p ON p.path = t.path
        ORDER BY p.id
        """,
        (scan_id,),
    )
    cur.execute("DROP TABLE temp.scan_folders")


def _prune_scans(cur, scan_path, newest_scan_id, now_iso):
    """Delete the scans of `scan_path` history retention no longer keeps."""
    cur.execute(
        "SELECT value FROM app_metadata WHERE key = ?", (history_retention.KEEP_ALL_DAYS_KEY,)
    )
    row = cur.fetchone()
    keep_all_days = history_retention.keep_all_days_from_setting(row[0] if row else None)
    cur.execute("SELECT id, created_at FROM scans WHERE scan_path = ?", (scan_path,))
    pruned = history_retention.scans_to_prune(
        cur.fetchall(), datetime.fromisoformat(now_iso), keep_all_days
    )
    if pruned:
        _delete_scans(cur, pruned, in_use_scan_id=newest_scan_id)


def _delete_scans(cur, scan_ids, in_use_scan_id=None):
    """Delete these scans, their folder rows, and every folder path no
    remaining scan refers to. `in_use_scan_id`: a kept scan, whose folders
    are in use by definition, so they skip the check."""
    maybe_unused = set()
    for scan_id in scan_ids:
        cur.execute("SELECT path_id FROM folder_snapshots WHERE scan_id = ?", (scan_id,))
        maybe_unused.update(path_id for (path_id,) in cur.fetchall())
        cur.execute("DELETE FROM folder_snapshots WHERE scan_id = ?", (scan_id,))
        cur.execute("DELETE FROM scans WHERE id = ?", (scan_id,))

    if in_use_scan_id is not None:
        cur.execute("SELECT path_id FROM folder_snapshots WHERE scan_id = ?", (in_use_scan_id,))
        maybe_unused.difference_update(path_id for (path_id,) in cur.fetchall())
    # Every other candidate is checked against every kept scan (of any
    # path) with one primary-key probe each -- CROSS JOIN pins that loop
    # order, instead of scanning every folder row for the path_id.
    cur.executemany(
        """
        DELETE FROM folder_paths
        WHERE id = ?1
          AND NOT EXISTS (
              SELECT 1 FROM scans s CROSS JOIN folder_snapshots f
              WHERE f.scan_id = s.id AND f.path_id = ?1
          )
        """,
        ((path_id,) for path_id in maybe_unused),
    )


def delete_scan(scan_id):
    """Remove one saved scan from the history -- its size, its folder rows,
    and every folder path nothing else refers to -- for Growth History's
    "Remove this scan" (a test run or a broken scan that would otherwise
    skew the trend, forecast and anomalies for good). Returns whether there
    was such a scan."""
    conn = _connect_for_write()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM scans WHERE id = ?", (scan_id,))
            if cur.fetchone() is None:
                return False
            _delete_scans(cur, [scan_id])
    finally:
        conn.close()
    return True


def get_previous_scan_id(scan_path, current_scan_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM scans
        WHERE scan_path = ?
          AND id < ?
        ORDER BY id DESC
        LIMIT 1
    """,
        (scan_path, current_scan_id),
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None


def get_latest_scan_id(scan_path):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM scans
        WHERE scan_path = ?
        ORDER BY id DESC
        LIMIT 1
    """,
        (scan_path,),
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None


def get_most_recent_scan_path():
    """The scan_path of the newest saved scan of anything, or None if
    nothing has been scanned yet -- where Growth History opens when there's
    no scan this session to go by."""
    conn = _connect()
    row = conn.execute("SELECT scan_path FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def list_scans_for_path(scan_path, limit=200):
    """All saved scans of `scan_path`, most recent first.

    Feeds the snapshot-comparison picker: `get_folder_growth` and
    `get_growth_summary` already accept any two scan ids (not just
    "latest"/"previous"), so the UI just needs a list to choose from.
    """
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, created_at, total_size, file_count
        FROM scans
        WHERE scan_path = ?
        ORDER BY created_at DESC
        LIMIT ?
    """,
        (scan_path, limit),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def get_folder_growth(current_scan_id, previous_scan_id, limit=50):
    """The `limit` folders (None: all) that grew the most (shrinking ones
    last), each against the same folder in the other scan. Only folders of
    MIN_FOLDER_SIZE_FOR_HISTORY or more are kept, so a folder only one scan
    has either appeared or disappeared, or crossed that size: it's counted
    against 0, and its growth_percent is None. Equal growth is ordered by
    path."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT path, previous_size, current_size, growth_bytes, file_count FROM (
            SELECT
                p.path AS path,
                COALESCE(prev.size_bytes, 0) AS previous_size,
                curr.size_bytes AS current_size,
                curr.size_bytes - COALESCE(prev.size_bytes, 0) AS growth_bytes,
                curr.file_count AS file_count
            FROM folder_snapshots curr
            JOIN folder_paths p ON p.id = curr.path_id
            LEFT JOIN folder_snapshots prev
                ON prev.scan_id = ?
               AND prev.path_id = curr.path_id
            WHERE curr.scan_id = ?
            UNION ALL
            SELECT p.path, prev.size_bytes, 0, -prev.size_bytes, 0
            FROM folder_snapshots prev
            JOIN folder_paths p ON p.id = prev.path_id
            WHERE prev.scan_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM folder_snapshots curr
                  WHERE curr.scan_id = ? AND curr.path_id = prev.path_id
              )
        )
        ORDER BY growth_bytes DESC, path
        LIMIT ?
    """,
        (
            previous_scan_id,
            current_scan_id,
            previous_scan_id,
            current_scan_id,
            -1 if limit is None else limit,
        ),
    )

    rows = cur.fetchall()
    results = []

    for row in rows:
        folder_path, previous_size, current_size, growth_bytes, file_count = row

        if previous_size > 0 and current_size > 0:
            growth_percent = ((current_size - previous_size) / previous_size) * 100
        else:
            growth_percent = None

        if growth_bytes > 0:
            growth_type = "Growing"
        elif growth_bytes < 0:
            growth_type = "Shrinking"
        else:
            growth_type = "Unchanged"

        results.append(
            (
                folder_path,
                previous_size,
                current_size,
                growth_bytes,
                growth_percent,
                growth_type,
                file_count,
            )
        )

    conn.close()
    return results


def get_growth_summary(current_scan_id, previous_scan_id):
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT total_size, file_count, created_at
        FROM scans
        WHERE id = ?
    """,
        (current_scan_id,),
    )
    current_row = cur.fetchone()

    cur.execute(
        """
        SELECT total_size, file_count, created_at
        FROM scans
        WHERE id = ?
    """,
        (previous_scan_id,),
    )
    previous_row = cur.fetchone()

    conn.close()

    if not current_row or not previous_row:
        return {
            "current_size_bytes": None,
            "previous_size_bytes": None,
            "size_change_bytes": None,
            "size_change_percent": None,
            "current_file_count": None,
            "previous_file_count": None,
            "file_count_change": None,
            "file_count_change_percent": None,
            "tracked_folders": 0,
            "new_folders": 0,
            "largest_growth_folder": None,
            "largest_shrink_folder": None,
        }

    current_size, current_files, _ = current_row
    previous_size, previous_files, _ = previous_row

    size_change_bytes = current_size - previous_size
    size_change_percent = None
    if previous_size > 0:
        size_change_percent = (size_change_bytes / previous_size) * 100

    file_count_change = current_files - previous_files
    file_count_change_percent = None
    if previous_files > 0:
        file_count_change_percent = (file_count_change / previous_files) * 100

    growth_rows = get_folder_growth(current_scan_id, previous_scan_id, limit=None)
    tracked_folders = sum(1 for row in growth_rows if row[2] > 0)
    new_folders = sum(1 for row in growth_rows if row[1] == 0 and row[2] > 0)

    largest_growth_folder = None
    if any(row[3] > 0 for row in growth_rows):
        largest_growth_folder = max(
            (row for row in growth_rows if row[3] > 0),
            key=lambda row: row[3],
        )

    largest_shrink_folder = None
    if any(row[3] < 0 for row in growth_rows):
        largest_shrink_folder = max(
            (row for row in growth_rows if row[3] < 0),
            key=lambda row: abs(row[3]),
        )

    return {
        "current_size_bytes": current_size,
        "previous_size_bytes": previous_size,
        "size_change_bytes": size_change_bytes,
        "size_change_percent": size_change_percent,
        "current_file_count": current_files,
        "previous_file_count": previous_files,
        "file_count_change": file_count_change,
        "file_count_change_percent": file_count_change_percent,
        "tracked_folders": tracked_folders,
        "new_folders": new_folders,
        "largest_growth_folder": largest_growth_folder,
        "largest_shrink_folder": largest_shrink_folder,
    }


def get_scan_history(scan_path, limit=30):
    """(created_at, total_size, file_count, folder_count) for the newest
    `limit` scans of `scan_path`, oldest first -- the order anomaly
    detection and the usage chart read a trend in."""
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT created_at, total_size, file_count, folder_count
        FROM (
            SELECT id, created_at, total_size, file_count, folder_count
            FROM scans
            WHERE scan_path = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        )
        ORDER BY created_at ASC, id ASC
    """,
        (scan_path, limit),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def get_forecast_history(scan_path, limit=200):
    """(created_at, total_size, allocated_size) for the newest `limit` scans
    of `scan_path`, oldest first: what forecasting.forecast_days_until_full
    fits a growth rate to. allocated_size (on disk) is None on scans saved
    before it was recorded (schema version 4)."""
    conn = _connect()
    try:
        return conn.execute(
            """
            SELECT created_at, total_size, allocated_size
            FROM (
                SELECT id, created_at, total_size, allocated_size
                FROM scans
                WHERE scan_path = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC, id ASC
            """,
            (scan_path, limit),
        ).fetchall()
    finally:
        conn.close()


def get_latest_drive_free(scan_path):
    """(created_at, drive_free) of the newest scan of `scan_path` that
    recorded its drive's free space, or None if none did."""
    conn = _connect()
    try:
        return conn.execute(
            """
            SELECT created_at, drive_free
            FROM scans
            WHERE scan_path = ? AND drive_free IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (scan_path,),
        ).fetchone()
    finally:
        conn.close()


def get_scan_ids_by_created_at(scan_path, limit=30):
    """{created_at: scan_id} for the same window get_scan_history(scan_path,
    limit) returns: the newest `limit` scans. A separate lookup rather than
    adding an id column to get_scan_history()'s own row shape, since
    several existing callers (anomaly_detection.py, the forecast)
    already unpack its rows positionally and have no use
    for the id. Lets a caller that already has anomaly_detection.Anomaly
    objects (keyed by created_at) map one back to the scan ids whose
    comparison produced it, e.g. to find which folder was most responsible
    via get_folder_growth().
    """
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT created_at, id
        FROM (
            SELECT id, created_at
            FROM scans
            WHERE scan_path = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        )
        ORDER BY created_at ASC, id ASC
    """,
        (scan_path, limit),
    )

    rows = dict(cur.fetchall())
    conn.close()

    return rows


def record_audit_entry(source, action, path, is_dir, size_bytes, outcome, error_message=None):
    """Record one delete request's outcome to the audit ledger.

    This is the durable record of "what did this app remove, when, from
    where, and where did it go" — every delete in the app (main tree,
    Search & Filter, Duplicate Files, Cleanup Recommendations, the Cleanup
    Cart) writes here via storage_scanner.delete_service, including the
    ones it refused. `outcome` is a storage_scanner.delete_outcome value;
    `success` is kept as "the item is gone" for anything that reads it.
    """
    conn = _connect_for_write()
    cur = conn.cursor()

    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute(
        """
        INSERT INTO audit_log
        (created_at, source, action, path, is_dir, size_bytes, success, error_message, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            created_at,
            source,
            action,
            path,
            int(bool(is_dir)),
            int(size_bytes),
            int(is_removed(outcome)),
            error_message,
            outcome,
        ),
    )

    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    return entry_id


def get_audit_log(limit=500):
    """Every recorded audit entry, most recent first: (created_at, source,
    action, path, is_dir, size_bytes, success, error_message, outcome)."""
    conn = _connect()
    cur = conn.cursor()

    # A row with no outcome was written by an older version of the app into
    # an already-migrated database; read it the way the migration would.
    cur.execute(
        f"""
        SELECT created_at, source, action, path, is_dir, size_bytes, success, error_message,
               COALESCE(outcome, {history_schema.LEGACY_OUTCOME_SQL})
        FROM audit_log
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def set_budget(path, threshold_bytes):
    """Create or update the size budget for `path` (upsert, one per path)."""
    conn = _connect_for_write()
    cur = conn.cursor()
    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute(
        """
        INSERT INTO budgets (path, threshold_bytes, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET threshold_bytes = excluded.threshold_bytes
    """,
        (path, threshold_bytes, created_at),
    )

    conn.commit()
    conn.close()


def list_budgets():
    """Every defined budget: [(id, path, threshold_bytes, created_at), ...]."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id, path, threshold_bytes, created_at FROM budgets ORDER BY path")
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_budget(budget_id):
    conn = _connect_for_write()
    cur = conn.cursor()
    cur.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()
    conn.close()


def record_install_locations_snapshot(locations):
    """Update known_install_locations from a fresh registry read.

    `locations`: [(display_name, install_location), ...] --
    storage_scanner.installed_apps.get_installed_apps()'s own return
    shape, already filtered to the candidate roots (Program Files/
    AppData) by the caller.

    Every install_location is normalized (os.path.normcase +
    os.path.normpath) before being used as the table's primary key --
    the same real folder can legitimately be reported with different
    case or a trailing separator across two different registry reads,
    and without normalizing, that would look like the old form
    "disappearing" and a new one "newly appearing" in the same snapshot
    instead of being recognized as the same, still-installed location.

    Every location present in this snapshot is upserted with
    currently_installed = 1 (first_seen_at set only on first insert,
    last_seen_installed_at bumped every time). Every previously-known
    location NOT present in this snapshot is marked
    currently_installed = 0 -- this is the moment a formerly-installed
    app's leftover folder becomes an orphan candidate. A location can
    only ever be marked 0 if it already existed as a row from an earlier
    snapshot; nothing inserted by this same call can also be zeroed out
    by it, since a location is either present (upserted to 1) or absent
    (only then eligible to be zeroed), never both.
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect_for_write()
    cur = conn.cursor()

    normalized_locations = [
        (display_name, os.path.normcase(os.path.normpath(install_location)))
        for display_name, install_location in locations
    ]
    present_locations = [loc for _name, loc in normalized_locations]

    for display_name, install_location in normalized_locations:
        cur.execute(
            """
            INSERT INTO known_install_locations
                (install_location, display_name, first_seen_at,
                 last_seen_installed_at, currently_installed)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(install_location) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen_installed_at = excluded.last_seen_installed_at,
                currently_installed = 1
        """,
            (install_location, display_name, now, now),
        )

    if present_locations:
        placeholders = ",".join("?" for _ in present_locations)
        cur.execute(
            f"UPDATE known_install_locations SET currently_installed = 0 "
            f"WHERE install_location NOT IN ({placeholders})",
            present_locations,
        )
    else:
        cur.execute("UPDATE known_install_locations SET currently_installed = 0")

    conn.commit()
    conn.close()


def get_orphaned_install_locations():
    """[(install_location, display_name, first_seen_at,
    last_seen_installed_at), ...] for every location whose owning app is
    no longer installed as of the most recent snapshot."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT install_location, display_name, first_seen_at, last_seen_installed_at
        FROM known_install_locations
        WHERE currently_installed = 0
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_installed_install_locations():
    """The normalized install locations the most recent snapshot saw
    installed."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT install_location FROM known_install_locations WHERE currently_installed = 1"
    )
    rows = [location for (location,) in cur.fetchall()]
    conn.close()
    return rows


def get_known_install_location_count():
    """Total rows in known_install_locations, regardless of
    currently_installed. 0 means record_install_locations_snapshot has
    never been called before -- orphaned-install detection needs at
    least a second snapshot to find anything (see that function's own
    docstring), so this is what the UI checks to show a "still learning"
    note on a first run rather than silently showing zero results with
    no explanation."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM known_install_locations")
    count = cur.fetchone()[0]
    conn.close()
    return count


def get_latest_scan_snapshot(scan_path):
    """(created_at, total_size, file_count, folder_count) for the most
    recent scan of `scan_path`, or None if it's never been scanned.

    Distinct from get_scan_history() (a window of scans, oldest first, for
    charting a trend) — this is the single latest data point, for budget
    checks.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at, total_size, file_count, folder_count
        FROM scans
        WHERE scan_path = ?
        ORDER BY created_at DESC
        LIMIT 1
    """,
        (scan_path,),
    )
    row = cur.fetchone()
    conn.close()
    return row
