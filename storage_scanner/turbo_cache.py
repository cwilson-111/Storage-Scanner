"""Persistent, on-disk cache of Turbo Scan's parsed MFT record set.

Caches the flat, whole-volume set of storage_scanner.mft_parser.ParsedRecord
fields -- never a finalized/rolled-up tree -- so storage_scanner.mft_scan's
build_tree/finalize_subtree keep running unmodified, in-memory, on every
request, for whatever subtree is actually asked for (their subtree-scoped
hard-link dedup is correct behavior, validated earlier this project; caching
a pre-finalized tree would risk caching the dedup decision for the wrong
subtree). What this module saves a caller from redoing is reading and
parsing every MFT record -- and, since records are stored as plain columns
with each hard-link name indexed by its parent, loading any of them beyond
the folder actually being rescanned (see load_subtree_records).

Two tables:
- cached_records: one row per MFT record, keyed by (volume_serial,
  record_number) -- deliberately NOT by the packed FRN (record number +
  sequence number). A USN Change Journal entry's FileReferenceNumber carries
  whatever sequence number was current at change time, which goes stale the
  instant a record slot is freed and reused for a different file. Keying by
  record_number means a reused slot's row is simply overwritten in place by
  its next refresh, with no special-case reuse-detection code anywhere.
- cached_names: one row per hard-link name (a record's `names` entries),
  keyed by (volume_serial, parent_frn, name, record_number), so a folder's
  children -- and recursively its whole subtree -- are an index range scan.

This module has no ctypes/Win32 access at all, matching mft_parser.py's own
"pure" split -- storage_scanner.usn_journal and the orchestration that ties
both together (storage_scanner.turbo_scan) are separate modules.
"""

import os
import sqlite3
from datetime import datetime

from history import APP_DATA_DIR
from storage_scanner.logging_setup import logger
from storage_scanner.mft_parser import _FRN_RECORD_NUMBER_MASK, FileNameAttr, ParsedRecord

DB_NAME = APP_DATA_DIR / "turbo_scan_cache.db"

_RECORD_FIELDS = (
    "frn",
    "is_directory",
    "file_attributes",
    "is_reparse_point",
    "is_cloud_placeholder",
    "mtime",
    "atime",
    "logical_size",
    "alloc_size",
)
_RECORD_COLUMNS = ", ".join(_RECORD_FIELDS)
# The same, from the cached_records table aliased as `r` in a join.
_R_RECORD_COLUMNS = ", ".join(f"r.{field}" for field in _RECORD_FIELDS)


def _connect():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_cache_db():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(cached_records)")
    existing_columns = {row[1] for row in cur.fetchall()}
    if existing_columns and "logical_size" not in existing_columns:
        # An older layout: one opaque JSON (record_json, before 2026-09-17)
        # or pickle (record_blob, before 2026-09-24) value per record, which
        # this version neither reads nor wants to keep -- loading any of it
        # meant deserializing the whole volume on every rescan. Wiping all
        # three tables forces exactly one full rescan on the next call:
        # correct, just not cached yet. Dropping only cached_records would
        # leave a cached_volumes row that still passes the record_size
        # check, pointing at an empty record set.
        cur.execute("DROP TABLE IF EXISTS cached_names")
        cur.execute("DROP TABLE cached_records")
        cur.execute("DROP TABLE IF EXISTS cached_volumes")
        conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cached_volumes (
            volume_serial           INTEGER PRIMARY KEY,
            volume_root             TEXT NOT NULL,
            usn_journal_id          INTEGER,
            next_usn                INTEGER,
            root_frn                INTEGER NOT NULL,
            record_size             INTEGER NOT NULL,
            full_scan_completed_at  TEXT NOT NULL,
            last_refreshed_at       TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cached_records (
            volume_serial         INTEGER NOT NULL,
            record_number         INTEGER NOT NULL,
            frn                   INTEGER NOT NULL,
            is_directory          INTEGER NOT NULL,
            file_attributes       INTEGER NOT NULL,
            is_reparse_point      INTEGER NOT NULL,
            is_cloud_placeholder  INTEGER NOT NULL,
            mtime                 REAL NOT NULL,
            atime                 REAL NOT NULL,
            logical_size          INTEGER NOT NULL,
            alloc_size            INTEGER NOT NULL,
            PRIMARY KEY (volume_serial, record_number),
            FOREIGN KEY (volume_serial) REFERENCES cached_volumes(volume_serial) ON DELETE CASCADE
        ) WITHOUT ROWID
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cached_names (
            volume_serial   INTEGER NOT NULL,
            parent_frn      INTEGER NOT NULL,
            name            TEXT NOT NULL,
            record_number   INTEGER NOT NULL,
            namespace       INTEGER NOT NULL,
            PRIMARY KEY (volume_serial, parent_frn, name, record_number),
            FOREIGN KEY (volume_serial) REFERENCES cached_volumes(volume_serial) ON DELETE CASCADE
        ) WITHOUT ROWID
    """)

    # Replacing or deleting one record's names during an incremental refresh.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_cached_names_record
        ON cached_names(volume_serial, record_number)
    """)

    conn.commit()
    conn.close()


class TurboCacheCorruptError(Exception):
    """The cache database itself is damaged -- a truncated write, a disk
    error, anything SQLite reports as a malformed database rather than a
    busy one. Distinct from a real bug elsewhere so the caller can
    invalidate just this volume's cache and fall back to a full scan: the
    same self-healing remedy already used for a stale or wrapped USN
    journal (see turbo_scan.scan_subtree_using_cache), rather than
    repeating the same failure (and Compatible-engine fallback) on every
    future scan of this volume forever."""


def _reading(query):
    """Runs `query(cursor)` on a fresh connection, turning a damaged
    database into TurboCacheCorruptError. A locked database
    (OperationalError) is not corruption and propagates unchanged."""
    conn = _connect()
    try:
        return query(conn.cursor())
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as exc:
        raise TurboCacheCorruptError(f"Turbo Scan cache is damaged: {exc}") from exc
    finally:
        conn.close()


def _record_row(volume_serial, record):
    return (
        volume_serial,
        record.frn & _FRN_RECORD_NUMBER_MASK,
        record.frn,
        int(record.is_directory),
        record.file_attributes,
        int(record.is_reparse_point),
        int(record.is_cloud_placeholder),
        record.mtime,
        record.atime,
        record.logical_size,
        record.alloc_size,
    )


def _name_rows(volume_serial, records):
    for record in records:
        record_number = record.frn & _FRN_RECORD_NUMBER_MASK
        for name_attr in record.names:
            yield (
                volume_serial,
                name_attr.parent_frn,
                name_attr.name,
                record_number,
                name_attr.namespace,
            )


def _record_from_row(row, names=None):
    """A ParsedRecord from the _RECORD_COLUMNS values in `row`."""
    frn, is_dir, attrs, is_reparse, is_cloud, mtime, atime, size, alloc = row
    return ParsedRecord(
        frn=frn,
        is_directory=bool(is_dir),
        file_attributes=attrs,
        is_reparse_point=bool(is_reparse),
        is_cloud_placeholder=bool(is_cloud),
        mtime=mtime,
        atime=atime,
        logical_size=size,
        alloc_size=alloc,
        names=names if names is not None else [],
    )


_INSERT_RECORD = """
    INSERT OR REPLACE INTO cached_records
        (volume_serial, record_number, frn, is_directory, file_attributes,
         is_reparse_point, is_cloud_placeholder, mtime, atime, logical_size, alloc_size)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_NAME = """
    INSERT OR REPLACE INTO cached_names
        (volume_serial, parent_frn, name, record_number, namespace)
    VALUES (?, ?, ?, ?, ?)
"""


def get_cached_volume(volume_serial):
    """The cached_volumes row for `volume_serial` as a dict, or None if this
    volume has never been cached."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM cached_volumes WHERE volume_serial = ?", (volume_serial,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row is not None else None


def save_full_scan(volume_serial, volume_root, root_frn, record_size, records):
    """Replace this volume's entire cached record set with `records` (a
    fresh full Turbo Scan's output) in one transaction, and upsert its
    cached_volumes row. Does NOT touch usn_journal_id/next_usn -- those are
    set separately via save_journal_cursor(), captured only after the
    caller has established a USN journal resume point post-scan (capturing
    it any earlier would risk losing changes made while the scan itself was
    still running)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO cached_volumes
            (volume_serial, volume_root, root_frn, record_size,
             full_scan_completed_at, last_refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(volume_serial) DO UPDATE SET
            volume_root = excluded.volume_root,
            root_frn = excluded.root_frn,
            record_size = excluded.record_size,
            full_scan_completed_at = excluded.full_scan_completed_at,
            last_refreshed_at = excluded.last_refreshed_at
    """,
        (volume_serial, volume_root, root_frn, record_size, now, now),
    )

    cur.execute("DELETE FROM cached_names WHERE volume_serial = ?", (volume_serial,))
    cur.execute("DELETE FROM cached_records WHERE volume_serial = ?", (volume_serial,))
    cur.executemany(_INSERT_RECORD, (_record_row(volume_serial, r) for r in records))
    cur.executemany(_INSERT_NAME, _name_rows(volume_serial, records))

    conn.commit()
    conn.close()
    logger.debug(
        "turbo_cache: saved full scan of volume %s (%d records)",
        volume_serial,
        len(records),
    )


def save_journal_cursor(volume_serial, usn_journal_id, next_usn):
    """Record the USN resume point captured right after a full scan
    finished, or after a successful incremental refresh."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE cached_volumes
        SET usn_journal_id = ?, next_usn = ?, last_refreshed_at = ?
        WHERE volume_serial = ?
    """,
        (usn_journal_id, next_usn, now, volume_serial),
    )
    conn.commit()
    conn.close()


def apply_incremental_changes(volume_serial, upserts, deletes, new_next_usn):
    """One transaction: replace each freshly re-parsed dirty record (and
    its names -- a rename or move changes them), delete any record_number
    in `deletes` (a record that no longer parses / is no longer in use),
    then advance the volume's USN cursor."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()

    touched = [r.frn & _FRN_RECORD_NUMBER_MASK for r in upserts] + list(deletes)
    cur.executemany(
        "DELETE FROM cached_names WHERE volume_serial = ? AND record_number = ?",
        ((volume_serial, record_number) for record_number in touched),
    )
    cur.executemany(
        "DELETE FROM cached_records WHERE volume_serial = ? AND record_number = ?",
        ((volume_serial, record_number) for record_number in deletes),
    )
    cur.executemany(_INSERT_RECORD, (_record_row(volume_serial, r) for r in upserts))
    cur.executemany(_INSERT_NAME, _name_rows(volume_serial, upserts))

    cur.execute(
        """
        UPDATE cached_volumes
        SET next_usn = ?, last_refreshed_at = ?
        WHERE volume_serial = ?
    """,
        (new_next_usn, now, volume_serial),
    )

    conn.commit()
    conn.close()
    logger.debug(
        "turbo_cache: applied incremental refresh to volume %s (%d upserts, %d deletes)",
        volume_serial,
        len(upserts),
        len(deletes),
    )


def find_record_by_path(volume_serial, root_frn, parts):
    """Walk `parts` (path components below the volume root) down the cached
    names, comparing each the way turbo_read.find_subtree_node does
    (os.path.normcase). Returns (ParsedRecord without names, the on-disk
    spelling of each component), or None if any component is missing --
    or sits inside a reparse point, which a scan never expands (only the
    requested folder itself is followed, see mft_scan._make_node).

    Raises TurboCacheCorruptError if the database is damaged."""

    def query(cur):
        cur.execute(
            f"SELECT {_RECORD_COLUMNS} FROM cached_records "
            "WHERE volume_serial = ? AND record_number = ? AND frn = ?",
            (volume_serial, root_frn & _FRN_RECORD_NUMBER_MASK, root_frn),
        )
        row = cur.fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        actual_parts = []

        for index, part in enumerate(parts):
            if index and not (record.is_directory and not record.is_reparse_point):
                return None  # a file or an unexpanded link has no children
            cur.execute(
                f"SELECT n.name, {_R_RECORD_COLUMNS} "
                "FROM cached_names n JOIN cached_records r "
                "ON r.volume_serial = n.volume_serial AND r.record_number = n.record_number "
                "WHERE n.volume_serial = ? AND n.parent_frn = ?",
                (volume_serial, record.frn),
            )
            wanted = os.path.normcase(part)
            match = next((r for r in cur if os.path.normcase(r[0]) == wanted), None)
            if match is None:
                return None
            actual_parts.append(match[0])
            record = _record_from_row(match[1:])

        return record, actual_parts

    return _reading(query)


# Every folder a scan of the target would expand -- the target itself, even
# a reparse point (the requested root is always followed), then every
# directory below it that isn't one -- and every name directly inside them.
# UNION (not UNION ALL) makes a corrupt parent loop terminate. CROSS JOIN
# pins SQLite's join order so each expanded folder drives a primary-key
# lookup of its own children; left to itself, the planner scanned every
# name on the volume once per folder (measured: 15x slower than the old
# whole-volume load at 20k files, worse with size).
_SUBTREE_QUERY = f"""
    WITH RECURSIVE expanded(frn) AS (
        SELECT frn FROM cached_records
        WHERE volume_serial = :volume AND record_number = :target_number AND frn = :target
        UNION
        SELECT r.frn
        FROM expanded e
        CROSS JOIN cached_names n
        CROSS JOIN cached_records r
        WHERE n.volume_serial = :volume AND n.parent_frn = e.frn
          AND r.volume_serial = :volume AND r.record_number = n.record_number
          AND r.is_directory = 1 AND r.is_reparse_point = 0
    )
    SELECT n.record_number, n.parent_frn, n.name, n.namespace,
           {_R_RECORD_COLUMNS}
    FROM expanded e
    CROSS JOIN cached_names n
    CROSS JOIN cached_records r
    WHERE n.volume_serial = :volume AND n.parent_frn = e.frn
      AND r.volume_serial = :volume AND r.record_number = n.record_number
"""


def load_subtree_records(volume_serial, target_record):
    """`target_record` plus every cached record beneath it, as ParsedRecords
    ready for mft_scan.build_tree(root_record_number=target's). Each
    record's `names` holds only its links inside this subtree, so a file
    hard-linked from elsewhere isn't counted as an orphan. Loads nothing
    outside the subtree: a rescan of one folder costs that folder, not the
    volume.

    Raises TurboCacheCorruptError if the database is damaged."""

    target_number = target_record.frn & _FRN_RECORD_NUMBER_MASK

    def query(cur):
        records = {}
        cur.execute(
            _SUBTREE_QUERY,
            {"volume": volume_serial, "target": target_record.frn, "target_number": target_number},
        )
        for record_number, parent_frn, name, namespace, *columns in cur:
            record = records.get(record_number)
            if record is None:
                record = records[record_number] = _record_from_row(columns)
            record.names.append(FileNameAttr(parent_frn=parent_frn, name=name, namespace=namespace))
        return records

    records = _reading(query)
    # The target's own row only appears above when it links to itself (the
    # volume root's "." entry); build_tree skips the root's names anyway.
    records.setdefault(target_number, target_record)
    return list(records.values())


def invalidate_volume(volume_serial):
    """Drop everything cached for `volume_serial` (cascades to
    cached_records and cached_names), forcing the next scan of it to do a
    full rebuild. Called whenever a cache/journal invalidation trigger
    fires -- a mismatched USN journal ID, a wrapped journal, a damaged
    database, or any other refresh failure."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM cached_volumes WHERE volume_serial = ?", (volume_serial,))
    conn.commit()
    conn.close()
    logger.debug("turbo_cache: invalidated cache for volume %s", volume_serial)
