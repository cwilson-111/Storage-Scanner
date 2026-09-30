"""The scan-history database layout (history.py's tables) and its
migrations.

Kept apart from history.py's queries and free of any import from it:
history.py opens the connection and the transaction, this only issues the
DDL. (history.py can't be imported from here anyway -- it's what
storage_scanner.logging_setup imports APP_DATA_DIR from.)

Schema versions, recorded as app_metadata's "schema_version":

1. One folder_snapshots row per (scan, folder) holding the folder's full
   path text and the scan's created_at again, with single-column indexes.
   ~170 bytes a row, and comparing two scans joined on path text: a
   20,001-folder comparison took over a minute.
2. Folder paths interned once in folder_paths; folder_snapshots is keyed
   (scan_id, path_id) in a WITHOUT ROWID table, so one scan's rows are a
   contiguous primary-key range and "the same folder in the previous
   scan" is a primary-key lookup on two integers.
3. audit_log.outcome: what actually happened (storage_scanner.
   delete_outcome). Older rows only knew "the delete call returned
   success", which a silent permanent delete also returned, so they become
   "unverified", not "recycled"; failed rows become "failed".
4. scans.allocated_size (the scanned path's on-disk bytes) and
   scans.drive_used/drive_free (its drive's space when the scan was saved),
   which the forecast of when the drive fills works from
   (storage_scanner.forecasting). Scans saved before have NULL there: what
   those were can't be recovered.

A database whose version is newer than SCHEMA_VERSION was upgraded by a
newer copy of the app. This one reads it but never changes it
(NewerDatabaseError): it can't know what the newer layout expects.
"""

import sqlite3
from typing import Optional

from storage_scanner.delete_outcome import FAILED, UNVERIFIED

SCHEMA_VERSION = 4


class NewerDatabaseError(Exception):
    """The database was written by a newer version of the app, so this one
    won't change it. The message is meant for the user."""

    def __init__(self, stored_version: int):
        super().__init__(
            "Your scan history was saved by a newer version of Storage Scanner "
            f"(database version {stored_version}; this version knows up to "
            f"{SCHEMA_VERSION}). This version won't change it, so scans won't be "
            "added to history until you update Storage Scanner."
        )
        self.stored_version = stored_version


# The outcome of an audit row written without one (see version 3).
LEGACY_OUTCOME_SQL = f"CASE WHEN success THEN '{UNVERIFIED}' ELSE '{FAILED}' END"

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_path TEXT NOT NULL,
        total_size INTEGER NOT NULL,
        drive_capacity INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        folder_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        allocated_size INTEGER,
        drive_used INTEGER,
        drive_free INTEGER
    )
    """,
    # AUTOINCREMENT above matters now that retention deletes scans: a
    # pruned scan's id is never handed to a later scan.
    """
    CREATE INDEX IF NOT EXISTS idx_scans_path
    ON scans(scan_path)
    """,
    # No AUTOINCREMENT: an id is only ever freed once nothing refers to it
    # (see history._prune_scans), so reusing it is harmless.
    """
    CREATE TABLE IF NOT EXISTS folder_paths (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE
    )
    """,
    # The scan's created_at lives on scans only. Deliberately no index on
    # path_id alone: nothing looks a folder up across all scans, and it
    # would add ~50% to the bytes per row. (Which is also why pruning runs
    # without PRAGMA foreign_keys: enforcing the path_id reference on a
    # folder_paths delete would scan every row.)
    """
    CREATE TABLE IF NOT EXISTS folder_snapshots (
        scan_id INTEGER NOT NULL REFERENCES scans(id),
        path_id INTEGER NOT NULL REFERENCES folder_paths(id),
        size_bytes INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        PRIMARY KEY (scan_id, path_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        source TEXT NOT NULL,
        action TEXT NOT NULL,
        path TEXT NOT NULL,
        is_dir INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL,
        success INTEGER NOT NULL,
        error_message TEXT,
        outcome TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_created_at
    ON audit_log(created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        threshold_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # Orphaned-install detection (storage_scanner.installed_apps /
    # cleanup_recommendations.find_orphaned_install_folders): a snapshot
    # of every InstallLocation this app has ever seen registered, and
    # whether the app that registered it is still installed as of the
    # most recent snapshot. A single point-in-time registry read can only
    # ever say what's *currently* installed -- distinguishing "was
    # installed, now gone" (an orphan) from "never installed here at all"
    # (not evidence of anything) requires remembering install_location
    # across sessions, hence a persistent table rather than in-memory
    # state like the (session-only) Cleanup Cart.
    """
    CREATE TABLE IF NOT EXISTS known_install_locations (
        install_location TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_installed_at TEXT NOT NULL,
        currently_installed INTEGER NOT NULL
    )
    """,
)


def stored_version(conn: sqlite3.Connection) -> Optional[int]:
    """The schema version of conn's database, or None for one without the
    history tables yet (brand new). Only reads, so it's safe to call on a
    database a newer version wrote."""
    tables = {
        name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "app_metadata" in tables:
        row = conn.execute("SELECT value FROM app_metadata WHERE key = 'schema_version'").fetchone()
        if row is not None:
            return int(row[0])
    # No version recorded: a brand-new database, or one from before
    # app_metadata existed, which already had the version 1 tables.
    columns = {column[1] for column in conn.execute("PRAGMA table_info(folder_snapshots)")}
    return 1 if "folder_path" in columns else None


def checked_version(conn: sqlite3.Connection) -> Optional[int]:
    """stored_version(conn), or NewerDatabaseError if a newer version of the
    app has upgraded the database past SCHEMA_VERSION."""
    version = stored_version(conn)
    if version is not None and version > SCHEMA_VERSION:
        raise NewerDatabaseError(version)
    return version


def _copy_v1_folder_rows(conn: sqlite3.Connection) -> None:
    """Version 1 rows (folder_snapshots_v1) into the version 2 tables. Rows
    of a scan that no longer exists are dropped: nothing could read them.
    OR IGNORE because version 1 had no uniqueness constraint -- a repeated
    (scan, folder) row must not wedge every future startup on a failed
    migration."""
    conn.execute("""
        INSERT OR IGNORE INTO folder_paths (path)
        SELECT folder_path FROM folder_snapshots_v1
        WHERE scan_id IN (SELECT id FROM scans)
        ORDER BY id
    """)
    conn.execute("""
        INSERT OR IGNORE INTO folder_snapshots (scan_id, path_id, size_bytes, file_count)
        SELECT f.scan_id, p.id, f.size_bytes, f.file_count
        FROM folder_snapshots_v1 f
        JOIN folder_paths p ON p.path = f.folder_path
        WHERE f.scan_id IN (SELECT id FROM scans)
        ORDER BY f.scan_id, p.id
    """)


def _add_audit_outcome(conn: sqlite3.Connection) -> None:
    """Version 3: the outcome column, filled in for every existing row."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(audit_log)")}
    if "outcome" not in columns:
        conn.execute("ALTER TABLE audit_log ADD COLUMN outcome TEXT")
        conn.execute(f"UPDATE audit_log SET outcome = {LEGACY_OUTCOME_SQL}")


def _add_scan_space_columns(conn: sqlite3.Connection) -> None:
    """Version 4: the scanned path's on-disk size and its drive's used and
    free space, left NULL on the scans already saved."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(scans)")}
    for column in ("allocated_size", "drive_used", "drive_free"):
        if column not in columns:
            conn.execute(f"ALTER TABLE scans ADD COLUMN {column} INTEGER")


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create every history table, migrating an older database first.

    Runs inside the caller's transaction, so a migration that fails part
    way leaves the old tables exactly as they were; running it again once
    at SCHEMA_VERSION changes nothing. A database from a newer version
    raises NewerDatabaseError before anything is changed. Returns True if
    it migrated the version 1 folder layout (the caller may then reclaim
    its space).
    """
    stored = checked_version(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    migrate = stored is not None and stored < 2
    if migrate:
        # Its indexes (idx_folder_scan, idx_folder_path) go with it, and are
        # dropped along with it below.
        conn.execute("ALTER TABLE folder_snapshots RENAME TO folder_snapshots_v1")

    for statement in _TABLES:
        conn.execute(statement)
    _add_audit_outcome(conn)
    _add_scan_space_columns(conn)

    if migrate:
        _copy_v1_folder_rows(conn)
        conn.execute("DROP TABLE folder_snapshots_v1")
    if stored is not None and stored < SCHEMA_VERSION:
        conn.execute(
            """
            INSERT INTO app_metadata (key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )
    else:
        # A new database, or one already at SCHEMA_VERSION.
        conn.execute(
            "INSERT OR IGNORE INTO app_metadata (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return migrate
