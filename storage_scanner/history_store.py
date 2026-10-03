"""Saving scans to the history database and removing them: one scan's
totals and folder rows, written with history retention (history_retention)
applied to that path's older scans in the same transaction, and Growth
History's "Remove this scan".
"""

from datetime import datetime

from storage_scanner import history_retention
from storage_scanner.history_db import connect_for_write


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

    conn = connect_for_write()
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
    conn = connect_for_write()
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
