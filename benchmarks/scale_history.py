"""benchmarks/scale.py's scan-history scenarios: repeat scans of one folder,
two years of daily scans under retention, and a 20,001-folder save and
comparison."""

import os
import sqlite3
import time
from datetime import datetime, timedelta

from scale_volume import FILES_PER_DIR, VOLUME_ROOT, build_node_tree, database_bytes, layout

SCHEDULED_SCANS = 30
# Two years and ten weeks of daily scans: every retention tier, down to
# one scan a year, has scans in it.
DAILY_SCANS = 800
HISTORY_FOLDERS = 20_000


class VmSteps:
    """Counts SQLite virtual-machine instructions run on every connection
    opened while active (history.py opens its own per call): the work a
    query does, repeatable where its timing isn't."""

    PER_TICK = 100

    def __init__(self):
        self.ticks = 0

    def _tick(self):
        self.ticks += 1  # returns None: carry on

    def __enter__(self):
        self._connect = sqlite3.connect

        def connect(*args, **kwargs):
            conn = self._connect(*args, **kwargs)
            conn.set_progress_handler(self._tick, self.PER_TICK)
            return conn

        sqlite3.connect = connect
        return self

    def __exit__(self, *_exc):
        sqlite3.connect = self._connect

    @property
    def steps(self):
        return self.ticks * self.PER_TICK


def scenario_history(n_files, workdir):
    import history
    from storage_scanner import scan_history

    history.DB_NAME = os.path.join(workdir, "storage_history.db")
    history.init_history_db()
    root = build_node_tree(n_files)
    folder_rows = len(scan_history.collect_folder_sizes(root)[0])

    timings = []
    for _ in range(SCHEDULED_SCANS):
        start = time.perf_counter()
        scan_history.record_scan(root)
        timings.append(time.perf_counter() - start)

    db_bytes = database_bytes(history.DB_NAME)
    return {
        "history_bytes_per_scan": round(db_bytes / SCHEDULED_SCANS),
        "history_rows_per_scan": folder_rows,
        "history_last_record_seconds": round(timings[-1], 3),
    }


def scenario_history_retention(n_files, workdir):
    """A scheduled scan's history save once a day for DAILY_SCANS days, on a
    simulated clock: how many scans retention keeps, and the database they
    fill. Saves the same folder sizes every day (walking the tree 800 times
    would only measure the walk; the `history` scenario covers that)."""
    import history
    from storage_scanner import scan_history

    class SimulatedClock(datetime):
        today = datetime(2024, 1, 1, 3, 0)

        @classmethod
        def now(cls, tz=None):
            return cls.today

    history.DB_NAME = os.path.join(workdir, "storage_history.db")
    history.datetime = SimulatedClock
    history.init_history_db()
    folder_sizes, folder_count = scan_history.collect_folder_sizes(build_node_tree(n_files))
    first_day = SimulatedClock.today
    start = time.perf_counter()
    for day in range(DAILY_SCANS):
        SimulatedClock.today = first_day + timedelta(days=day)
        history.save_scan_snapshot(VOLUME_ROOT, 1, 1, 1, folder_count, folder_sizes)
    elapsed = time.perf_counter() - start

    conn = sqlite3.connect(history.DB_NAME)
    (kept,) = conn.execute("SELECT COUNT(*) FROM scans").fetchone()
    conn.close()
    return {
        "history_daily_scans_kept": kept,
        "history_daily_scans_db_bytes": database_bytes(history.DB_NAME),
        "history_daily_save_seconds": round(elapsed / DAILY_SCANS, 4),
    }


def _history_20k_folder_sizes():
    """Two consecutive scans' folder_sizes for the 1M-file volume's
    HISTORY_FOLDERS + 1 folders: in the second, one folder in ten grew, one
    in a hundred is gone, and 1% more are new."""
    volume = layout(HISTORY_FOLDERS * FILES_PER_DIR)
    paths = [os.path.join(VOLUME_ROOT, *parts) for parts, _count in volume]
    first = {path: {"size": 60_000_000 + i * 7, "file_count": 50} for i, path in enumerate(paths)}
    second = {
        path: {"size": first[path]["size"] + (i * 1_000 if i % 10 == 0 else 0), "file_count": 50}
        for i, path in enumerate(paths)
        if i % 100 != 1
    }
    for i in range(len(paths) // 100):
        second[os.path.join(VOLUME_ROOT, "new", f"new_{i:05d}")] = {
            "size": 70_000_000,
            "file_count": 5,
        }
    return first, second


def scenario_history_20k(_n_files, workdir):
    """What scan_history.record_scan does with a 20,001-folder scan: save
    it, then compare it with the previous scan of the same path."""
    import history

    history.DB_NAME = os.path.join(workdir, "storage_history.db")
    history.init_history_db()
    first, second = _history_20k_folder_sizes()
    history.save_scan_snapshot(VOLUME_ROOT, 1, 1, 1, len(first), first)

    with VmSteps() as save_steps:
        start = time.perf_counter()
        scan_id = history.save_scan_snapshot(VOLUME_ROOT, 1, 1, 1, len(second), second)
        save_seconds = time.perf_counter() - start
    previous_id = history.get_previous_scan_id(VOLUME_ROOT, scan_id)
    with VmSteps() as growth_steps:
        start = time.perf_counter()
        rows = history.get_folder_growth(scan_id, previous_id, limit=50)
        growth_seconds = time.perf_counter() - start
    assert rows[0][0].startswith(os.path.join(VOLUME_ROOT, "new"))

    return {
        "history_20k_folder_rows": len(second),
        "history_20k_save_seconds": round(save_seconds, 3),
        "history_20k_growth_seconds": round(growth_seconds, 3),
        "history_20k_bytes_per_scan": round(database_bytes(history.DB_NAME) / 2),
        "history_20k_save_steps_per_row": round(save_steps.steps / len(second), 1),
        "history_20k_growth_steps_per_row": round(growth_steps.steps / len(second), 1),
    }
