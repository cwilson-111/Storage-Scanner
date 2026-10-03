"""Reading saved scans back from the history database: the scans of a
path, two scans compared folder by folder, the series the usage chart,
forecast and anomaly detection read, and one scan's folder sizes for the
main tree's Change column. Nothing here writes.
"""

import os

from storage_scanner.history_db import connect


def get_previous_scan_id(scan_path, current_scan_id):
    conn = connect()
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
    conn = connect()
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
    conn = connect()
    row = conn.execute("SELECT scan_path FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def list_scans_for_path(scan_path, limit=200):
    """All saved scans of `scan_path`, most recent first.

    Feeds the snapshot-comparison picker: `get_folder_growth` and
    `get_growth_summary` already accept any two scan ids (not just
    "latest"/"previous"), so the UI just needs a list to choose from.
    """
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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


def get_folder_sizes(scan_id):
    """{normalized folder path: size} for one saved scan: the folders of
    MIN_FOLDER_SIZE_FOR_HISTORY or more it kept. The main tree's Change
    column compares with these."""
    conn = connect()
    rows = conn.execute(
        """
        SELECT p.path, s.size_bytes
        FROM folder_snapshots s
        JOIN folder_paths p ON p.id = s.path_id
        WHERE s.scan_id = ?
    """,
        (scan_id,),
    ).fetchall()
    conn.close()
    return {os.path.normcase(os.path.normpath(path)): size for path, size in rows}


def get_latest_scan_snapshot(scan_path):
    """(created_at, total_size, file_count, folder_count) for the most
    recent scan of `scan_path`, or None if it's never been scanned.

    Distinct from get_scan_history() (a window of scans, oldest first, for
    charting a trend) — this is the single latest data point, for budget
    checks.
    """
    conn = connect()
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
