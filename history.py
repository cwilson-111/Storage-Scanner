
import sqlite3
from datetime import datetime

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - optional runtime dependency
    plt = None

import logging
import sys
import os
from pathlib import Path

APP_NAME = "NeuralStorageMatrix"

if sys.platform == "darwin":
    _APP_DATA_BASE = Path.home() / "Library" / "Application Support"
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

def init_history_db():
    
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")


    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cur.execute("""
            INSERT OR IGNORE INTO app_metadata
            (key, value)
            VALUES ('schema_version', '1')
            """)


    cur.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_path TEXT NOT NULL,
            total_size INTEGER NOT NULL,
            drive_capacity INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            folder_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS folder_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            folder_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(scan_id) REFERENCES scans(id)
        )
    """)


    

    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            path TEXT NOT NULL,
            is_dir INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error_message TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_scans_path
        ON scans(scan_path)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_folder_scan
        ON folder_snapshots(scan_id)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_folder_path
        ON folder_snapshots(folder_path)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_created_at
        ON audit_log(created_at)
    """)


    conn.commit()
    conn.close()

def save_scan_snapshot(scan_path, total_size, drive_capacity, file_count, folder_count, folder_sizes):
    """
    Saves one scan result into SQLite.

    folder_sizes example:
    {
        "C:\\Users\\Cole\\Downloads": {
            "size": 123456789,
            "file_count": 312
        }
    }
    """

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute("""
    INSERT INTO scans 
    (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
""", (scan_path, total_size, drive_capacity, file_count, folder_count, created_at))

    scan_id = cur.lastrowid

    rows = []
    for folder_path, data in folder_sizes.items():
        rows.append((
            scan_id,
            folder_path,
            int(data.get("size", 0)),
            int(data.get("file_count", 0)),
            created_at
        ))

    cur.executemany("""
        INSERT INTO folder_snapshots
        (scan_id, folder_path, size_bytes, file_count, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, rows)

    conn.commit()
    conn.close()

    return scan_id

def get_previous_scan_id(scan_path, current_scan_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT id
        FROM scans
        WHERE scan_path = ?
          AND id < ?
        ORDER BY id DESC
        LIMIT 1
    """, (scan_path, current_scan_id))

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None

def get_latest_scan_id(scan_path):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT id
        FROM scans
        WHERE scan_path = ?
        ORDER BY id DESC
        LIMIT 1
    """, (scan_path,))

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None

def list_scans_for_path(scan_path, limit=200):
    """All saved scans of `scan_path`, most recent first.

    Feeds the snapshot-comparison picker: `get_folder_growth` and
    `get_growth_summary` already accept any two scan ids (not just
    "latest"/"previous"), so the UI just needs a list to choose from.
    """
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, created_at, total_size, file_count
        FROM scans
        WHERE scan_path = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (scan_path, limit))

    rows = cur.fetchall()
    conn.close()

    return rows


def get_folder_growth(current_scan_id, previous_scan_id, limit=50):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            curr.folder_path,
            COALESCE(prev.size_bytes, 0) AS previous_size,
            curr.size_bytes AS current_size,
            curr.size_bytes - COALESCE(prev.size_bytes, 0) AS growth_bytes,
            curr.file_count
        FROM folder_snapshots curr
        LEFT JOIN folder_snapshots prev
            ON curr.folder_path = prev.folder_path
           AND prev.scan_id = ?
        WHERE curr.scan_id = ?
        ORDER BY growth_bytes DESC
        LIMIT ?
    """, (previous_scan_id, current_scan_id, limit))

    rows = cur.fetchall()
    results = []

    for row in rows:
        folder_path, previous_size, current_size, growth_bytes, file_count = row

        if previous_size > 0:
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
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT total_size, file_count, created_at
        FROM scans
        WHERE id = ?
    """, (current_scan_id,))
    current_row = cur.fetchone()

    cur.execute("""
        SELECT total_size, file_count, created_at
        FROM scans
        WHERE id = ?
    """, (previous_scan_id,))
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

    growth_rows = get_folder_growth(current_scan_id, previous_scan_id, limit=50)
    tracked_folders = len(growth_rows)
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
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT created_at, total_size, file_count, folder_count
        FROM scans
        WHERE scan_path = ?
        ORDER BY created_at ASC
        LIMIT ?
    """, (scan_path, limit))

    rows = cur.fetchall()
    conn.close()

    return rows


def record_audit_entry(source, action, path, is_dir, size_bytes, success, error_message=None):
    """Record one deletion/recycle action to the audit ledger.

    This is the durable record of "what did this app remove, when, and
    from where" — every deletion path in the app (main tree, Search &
    Filter, Duplicate Files, Cleanup Recommendations) writes here via
    storage_scanner.audit.recycle_and_log, regardless of which window
    triggered it.
    """
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute("""
        INSERT INTO audit_log
        (created_at, source, action, path, is_dir, size_bytes, success, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (created_at, source, action, path, int(bool(is_dir)), int(size_bytes),
          int(bool(success)), error_message))

    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    return entry_id


def get_audit_log(limit=500):
    """Every recorded audit entry, most recent first."""
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT created_at, source, action, path, is_dir, size_bytes, success, error_message
        FROM audit_log
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cur.fetchall()
    conn.close()

    return rows


def format_bytes(num):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"

def create_usage_history_chart(scan_path, output_file="usage_history.png"):
    history = get_scan_history(scan_path)

    if not history:
        print("No history found.")
        return None

    if plt is None:
        print("matplotlib is not installed; chart could not be generated.")
        return None

    dates = []
    sizes_gb = []

    for created_at, total_size, file_count, folder_count in history:
        dates.append(datetime.fromisoformat(created_at))
        sizes_gb.append(total_size / (1024 ** 3))

    plt.figure(figsize=(10, 5))
    plt.plot(dates, sizes_gb, marker="o")
    plt.title(f"Storage Usage History: {scan_path}")
    plt.xlabel("Scan Date")
    plt.ylabel("Used Space (GB)")
    plt.xticks(rotation=35)
    plt.tight_layout()
    plt.savefig(output_file)
    plt.close()

    return output_file

def print_growth_report(current_scan_id, previous_scan_id):
    growth_rows = get_folder_growth(current_scan_id, previous_scan_id)

    print("\nFolder Growth Report")
    print("-" * 80)

    for folder_path, previous_size, current_size, growth_bytes, growth_percent, growth_type, file_count in growth_rows:
        percent_text = (
            f"{growth_percent:.2f}%" if growth_percent is not None else "N/A (new folder)"
        )
        print(f"{folder_path}")
        print(f"  Previous: {format_bytes(previous_size)}")
        print(f"  Current:  {format_bytes(current_size)}")
        print(f"  Growth:   {format_bytes(growth_bytes)}")
        print(f"  Percent:   {percent_text}")
        print(f"  Status:   {growth_type}")
        print(f"  Files:    {file_count}")
        print()