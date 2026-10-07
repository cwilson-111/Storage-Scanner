"""The Turbo Scan cache's table layout, and how a ParsedRecord maps onto
its rows. turbo_cache owns the connection and every read and write; this is
what both of those have to agree on.

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
"""

from storage_scanner.mft_parser import _FRN_RECORD_NUMBER_MASK, ParsedRecord

_RECORD_FIELDS = (
    "frn",
    "is_directory",
    "file_attributes",
    "is_link",
    "is_cloud_placeholder",
    "mtime",
    "atime",
    "logical_size",
    "alloc_size",
)
_RECORD_COLUMNS = ", ".join(_RECORD_FIELDS)
# The same, from the cached_records table aliased as `r` in a join.
_R_RECORD_COLUMNS = ", ".join(f"r.{field}" for field in _RECORD_FIELDS)

# PRAGMA user_version of a cache whose rows today's parser would write the
# same. Raise it whenever mft_parser changes what a record parses to: an
# incremental scan re-reads only changed records, so older rows would stay
# wrong. 1 (2026-10-06): a second hard link in the same folder kept, and a
# WOF-compressed file not a link (both found by turbo_checklist.py).
_LAYOUT_VERSION = 1


def _create_tables_on(conn):
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(cached_records)")
    existing_columns = {row[1] for row in cur.fetchall()}
    version = cur.execute("PRAGMA user_version").fetchone()[0]
    if existing_columns and ("is_link" not in existing_columns or version < _LAYOUT_VERSION):
        # An older layout: one opaque JSON (record_json, before 2026-09-17)
        # or pickle (record_blob, before 2026-09-24) value per record,
        # columns with an is_reparse_point flag (before 2026-09-29) that
        # can't tell a OneDrive folder from a junction, or rows from a
        # parser older than _LAYOUT_VERSION. None of it is worth keeping.
        # Wiping all three tables forces exactly one full rescan on the
        # next call: correct, just not cached yet. Dropping only
        # cached_records would leave a cached_volumes row that still
        # passes the record_size check, pointing at an empty record set.
        cur.execute("DROP TABLE IF EXISTS cached_names")
        cur.execute("DROP TABLE cached_records")
        cur.execute("DROP TABLE IF EXISTS cached_volumes")
        conn.commit()
    cur.execute(f"PRAGMA user_version = {_LAYOUT_VERSION}")

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
            is_link               INTEGER NOT NULL,
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


def _record_row(volume_serial, record):
    return (
        volume_serial,
        record.frn & _FRN_RECORD_NUMBER_MASK,
        record.frn,
        int(record.is_directory),
        record.file_attributes,
        int(record.is_link),
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
    frn, is_dir, attrs, is_link, is_cloud, mtime, atime, size, alloc = row
    return ParsedRecord(
        frn=frn,
        is_directory=bool(is_dir),
        file_attributes=attrs,
        is_link=bool(is_link),
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
         is_link, is_cloud_placeholder, mtime, atime, logical_size, alloc_size)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_NAME = """
    INSERT OR REPLACE INTO cached_names
        (volume_serial, parent_frn, name, record_number, namespace)
    VALUES (?, ?, ?, ?, ?)
"""
