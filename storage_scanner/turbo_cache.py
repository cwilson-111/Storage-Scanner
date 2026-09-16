"""Persistent, on-disk cache of Turbo Scan's parsed MFT record set.

Caches the flat, whole-volume list of storage_scanner.mft_parser.ParsedRecord
objects -- never a finalized/rolled-up tree -- so storage_scanner.mft_scan's
build_tree/finalize_subtree keep running unmodified, in-memory, on every
request, for whatever subtree is actually asked for (their subtree-scoped
hard-link dedup is correct behavior, validated earlier this project; caching
a pre-finalized tree would risk caching the dedup decision for the wrong
subtree). The only thing this module saves a caller from redoing is the
expensive step: reading and parsing every MFT record.

Rows are keyed by (volume_serial, record_number) -- deliberately NOT by the
packed FRN (record number + sequence number). A USN Change Journal entry's
FileReferenceNumber carries whatever sequence number was current at change
time, which goes stale the instant a record slot is freed and reused for a
different file. Keying by record_number means a reused slot's cache row is
simply overwritten in place by its next refresh, with no special-case
reuse-detection code needed anywhere in this module or its caller.

This module has no ctypes/Win32 access at all, matching mft_parser.py's own
"pure" split -- the future USN journal reader (storage_scanner.usn_journal)
and the orchestration that ties both together are separate modules.
"""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from history import APP_DATA_DIR
from storage_scanner.logging_setup import logger
from storage_scanner.mft_parser import _FRN_RECORD_NUMBER_MASK, FileNameAttr, ParsedRecord

DB_NAME = APP_DATA_DIR / "turbo_scan_cache.db"


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
            volume_serial   INTEGER NOT NULL,
            record_number   INTEGER NOT NULL,
            frn             INTEGER NOT NULL,
            record_json     TEXT NOT NULL,
            PRIMARY KEY (volume_serial, record_number),
            FOREIGN KEY (volume_serial) REFERENCES cached_volumes(volume_serial) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_cached_records_volume
        ON cached_records(volume_serial)
    """)

    conn.commit()
    conn.close()


def _record_to_json(record):
    return json.dumps(asdict(record))


def _record_from_json(text):
    data = json.loads(text)
    data["names"] = [FileNameAttr(**n) for n in data["names"]]
    return ParsedRecord(**data)


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

    cur.execute("""
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
    """, (volume_serial, volume_root, root_frn, record_size, now, now))

    cur.execute("DELETE FROM cached_records WHERE volume_serial = ?", (volume_serial,))
    cur.executemany(
        "INSERT INTO cached_records (volume_serial, record_number, frn, record_json) "
        "VALUES (?, ?, ?, ?)",
        (
            (volume_serial, record.frn & _FRN_RECORD_NUMBER_MASK, record.frn, _record_to_json(record))
            for record in records
        ),
    )

    conn.commit()
    conn.close()
    logger.debug(
        "turbo_cache: saved full scan of volume %s (%d records)",
        volume_serial, len(records),
    )


def save_journal_cursor(volume_serial, usn_journal_id, next_usn):
    """Record the USN resume point captured right after a full scan
    finished, or after a successful incremental refresh."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE cached_volumes
        SET usn_journal_id = ?, next_usn = ?, last_refreshed_at = ?
        WHERE volume_serial = ?
    """, (usn_journal_id, next_usn, now, volume_serial))
    conn.commit()
    conn.close()


def apply_incremental_changes(volume_serial, upserts, deletes, new_next_usn):
    """One transaction: upsert each freshly re-parsed dirty record, delete
    any record_number in `deletes` (a record that no longer parses / is no
    longer in use), then advance the volume's USN cursor."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()

    cur.executemany("""
        INSERT INTO cached_records (volume_serial, record_number, frn, record_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(volume_serial, record_number) DO UPDATE SET
            frn = excluded.frn,
            record_json = excluded.record_json
    """, (
        (volume_serial, record.frn & _FRN_RECORD_NUMBER_MASK, record.frn, _record_to_json(record))
        for record in upserts
    ))

    cur.executemany(
        "DELETE FROM cached_records WHERE volume_serial = ? AND record_number = ?",
        ((volume_serial, record_number) for record_number in deletes),
    )

    cur.execute("""
        UPDATE cached_volumes
        SET next_usn = ?, last_refreshed_at = ?
        WHERE volume_serial = ?
    """, (new_next_usn, now, volume_serial))

    conn.commit()
    conn.close()
    logger.debug(
        "turbo_cache: applied incremental refresh to volume %s (%d upserts, %d deletes)",
        volume_serial, len(upserts), len(deletes),
    )


def load_all_records(volume_serial):
    """Every cached record for `volume_serial`, deserialized back into
    ParsedRecord objects -- a drop-in replacement for the `records` list a
    full Turbo Scan builds by looping parse_base_record over the whole MFT."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT record_json FROM cached_records WHERE volume_serial = ?",
        (volume_serial,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_record_from_json(row[0]) for row in rows]


def invalidate_volume(volume_serial):
    """Drop everything cached for `volume_serial` (cascades to
    cached_records), forcing the next scan of it to do a full rebuild.
    Called whenever a cache/journal invalidation trigger fires -- a
    mismatched USN journal ID, a wrapped journal, or any other refresh
    failure."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM cached_volumes WHERE volume_serial = ?", (volume_serial,))
    conn.commit()
    conn.close()
    logger.debug("turbo_cache: invalidated cache for volume %s", volume_serial)
