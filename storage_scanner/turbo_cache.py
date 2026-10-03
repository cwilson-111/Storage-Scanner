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

The tables, and how a record maps onto their rows, are in turbo_cache_schema.

This module has no ctypes/Win32 access at all, matching mft_parser.py's own
"pure" split -- storage_scanner.usn_journal and the orchestration that ties
both together (storage_scanner.turbo_scan) are separate modules.
"""

import contextlib
import os
import sqlite3
from datetime import datetime, timedelta

from storage_scanner import history_files
from storage_scanner.history_db import APP_DATA_DIR
from storage_scanner.logging_setup import logger
from storage_scanner.mft_parser import _FRN_RECORD_NUMBER_MASK, FileNameAttr
from storage_scanner.turbo_cache_schema import (
    _INSERT_NAME,
    _INSERT_RECORD,
    _R_RECORD_COLUMNS,
    _RECORD_COLUMNS,
    _create_tables_on,
    _name_rows,
    _record_from_row,
    _record_row,
)

DB_NAME = APP_DATA_DIR / "turbo_scan_cache.db"

# A drive whose cache hasn't been refreshed for this long (unplugged,
# reformatted, gone) loses it at the next full save of another drive.
STALE_VOLUME_DAYS = 90


def discard():
    """Delete the cache file outright (and its -wal/-shm). Nothing in it is
    anything but a copy of what the drives hold: the next Turbo Scan of each
    drive just reads its whole MFT again."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(f"{DB_NAME}{suffix}")


def cache_size_bytes():
    """What the cache takes on disk, -wal included."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(f"{DB_NAME}{suffix}")
    return total


def _connect():
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
    except BaseException:
        # A damaged file fails right here; left open, Windows won't let
        # discard() delete it.
        conn.close()
        raise
    return conn


def init_cache_db():
    """Create (or upgrade) the cache tables. A file SQLite can't read as a
    database is deleted and a new one started: before, a damaged cache
    meant a full read on every scan, for good."""
    try:
        _create_tables()
    except sqlite3.DatabaseError as exc:
        if not history_files.is_damaged(exc):
            raise
        logger.warning("Turbo Scan cache %s is damaged (%s); starting a new one", DB_NAME, exc)
        discard()
        _create_tables()


def _create_tables():
    conn = _connect()
    try:
        _create_tables_on(conn)
    finally:
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


def get_cached_volume(volume_serial):
    """The cached_volumes row for `volume_serial` as a dict, or None if this
    volume has never been cached. Raises TurboCacheCorruptError if the
    database is damaged."""

    def query(cur):
        cur.execute("SELECT * FROM cached_volumes WHERE volume_serial = ?", (volume_serial,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip((column[0] for column in cur.description), row))

    return _reading(query)


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

    stale_before = (datetime.now() - timedelta(days=STALE_VOLUME_DAYS)).isoformat(
        timespec="seconds"
    )
    # Cascades to their records and names.
    cur.execute(
        "DELETE FROM cached_volumes WHERE volume_serial != ? AND last_refreshed_at < ?",
        (volume_serial, stale_before),
    )
    conn.commit()
    _compact_if_mostly_empty(conn)
    conn.close()
    logger.debug(
        "turbo_cache: saved full scan of volume %s (%d records)",
        volume_serial,
        len(records),
    )


def _compact_if_mostly_empty(conn):
    """VACUUM when over a quarter of the file's pages are free. A full save
    deletes a volume's rows and writes them again, and SQLite keeps the
    freed pages: this machine's cache was 615 MB, 54% of it empty. The
    VACUUM rewrites the file (seconds for hundreds of MB), which is why it
    waits until it's worth it."""
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if free * 4 <= pages:
        return
    try:
        conn.execute("VACUUM")
    except sqlite3.Error:
        logger.warning("Could not compact the Turbo Scan cache", exc_info=True)
        return
    logger.info("turbo_cache: compacted %s (%d of %d pages were free)", DB_NAME, free, pages)


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
    or sits inside a link, which a scan never expands (see
    mft_scan._is_folder).

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
            if index and not (record.is_directory and not record.is_link):
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


# Every folder a scan of the target would expand -- the target itself, then
# every directory below it that isn't a link -- and every name directly
# inside them.
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
          AND r.is_directory = 1 AND r.is_link = 0
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
