
import sqlite3
from datetime import datetime

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - optional runtime dependency
    plt = None

import os


DB_NAME = os.path.join(os.path.dirname(__file__), "storage_history.db")

def init_history_db():
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

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

def estimate_days_until_full(scan_path, drive_capacity_bytes):
    history = get_scan_history(scan_path)

    if len(history) < 2:
        return None

    first_date = datetime.fromisoformat(history[0][0])
    first_size = history[0][1]

    last_date = datetime.fromisoformat(history[-1][0])
    last_size = history[-1][1]

    days_elapsed = (last_date - first_date).days

    if days_elapsed <= 0:
        return None

    growth_bytes = last_size - first_size
    daily_growth = growth_bytes / days_elapsed

    if daily_growth <= 0:
        return None

    remaining_bytes = drive_capacity_bytes - last_size

    if remaining_bytes <= 0:
        return 0

    days_until_full = remaining_bytes / daily_growth

    return round(days_until_full)

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
    percent_text = f"{growth_percent:.2f}%"

    print("\nFolder Growth Report")
    print("-" * 80)

    for folder_path, previous_size, current_size, growth_bytes, growth_percent, growth_type, file_count in growth_rows:
        print(f"{folder_path}")
        print(f"  Previous: {format_bytes(previous_size)}")
        print(f"  Current:  {format_bytes(current_size)}")
        print(f"  Growth:   {format_bytes(growth_bytes)}")
        print(f"  Percent:   {percent_text}")
        print(f"  Status:   {growth_type}")
        print(f"  Files:    {file_count}")
        print()