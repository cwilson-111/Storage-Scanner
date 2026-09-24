"""Persists the last computed Cleanup Recommendations result to SQLite, so
opening the app fresh and going straight to Tools > Clean Up > Cleanup
Recommendations shows last time's results immediately -- no scan needed
first. See cleanup_window.show_cleanup_recommendations for how this gets
populated and consulted.

Same %LOCALAPPDATA%\\NeuralStorageMatrix\\ directory, own DB file, and
per-call-connection/WAL/PRAGMA conventions as history.py/turbo_cache.py.

Stores the *computed* cleanup_recommendations.Recommendation rows
themselves, not the raw scanned file tree -- deliberately simpler than
turbo_cache.py's whole-volume record cache. This module's whole job is "show
what Cleanup Recommendations found the last time it actually ran," not
"reproduce exactly what a brand-new live scan would find right now" -- the
Rescan action already covers the latter. A save always fully replaces
whatever was cached for that scan_path in one transaction; a stale row from
three runs ago must never linger next to fresh ones.
"""

import sqlite3
from datetime import datetime

from history import APP_DATA_DIR
from storage_scanner.cleanup_recommendations import Recommendation
from storage_scanner.logging_setup import logger

DB_NAME = APP_DATA_DIR / "cleanup_cache.db"


def _connect():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_cleanup_cache_db():
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cached_cleanup_runs (
            scan_path    TEXT PRIMARY KEY,
            computed_at  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cached_recommendations (
            scan_path          TEXT NOT NULL,
            row_id             INTEGER NOT NULL,
            category           TEXT NOT NULL,
            node_path          TEXT NOT NULL,
            node_name          TEXT NOT NULL,
            node_is_dir        INTEGER NOT NULL,
            node_size          INTEGER NOT NULL,
            reason             TEXT NOT NULL,
            risk               TEXT NOT NULL,
            recoverable_bytes  INTEGER NOT NULL,
            action             TEXT NOT NULL,
            PRIMARY KEY (scan_path, row_id),
            FOREIGN KEY (scan_path) REFERENCES cached_cleanup_runs(scan_path) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


class CachedNode:
    """Enough of a real storage_scanner.models.Node for a cached
    recommendation row to be displayed, revealed in the file manager, and
    deleted -- see cleanup_window.py's use of rec.node.path/name/is_dir/
    size. Never a stand-in for a real scanned node otherwise: no
    .children, no .error, no .mtime -- this never participates in (or gets
    inserted into) a live scan tree.
    """

    __slots__ = ("path", "name", "is_dir", "size")

    def __init__(self, path, name, is_dir, size):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = size


def save_recommendations(scan_path, recommendations):
    """Replace whatever was cached for `scan_path` with `recommendations`
    (a list of cleanup_recommendations.Recommendation), in one
    transaction, so a concurrent reader never sees a mix of two different
    runs."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO cached_cleanup_runs (scan_path, computed_at)
        VALUES (?, ?)
        ON CONFLICT(scan_path) DO UPDATE SET computed_at = excluded.computed_at
    """,
        (scan_path, now),
    )

    cur.execute("DELETE FROM cached_recommendations WHERE scan_path = ?", (scan_path,))
    cur.executemany(
        "INSERT INTO cached_recommendations "
        "(scan_path, row_id, category, node_path, node_name, node_is_dir, "
        " node_size, reason, risk, recoverable_bytes, action) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                scan_path,
                row_id,
                rec.category,
                rec.node.path,
                rec.node.name,
                int(rec.node.is_dir),
                rec.node.size,
                rec.reason,
                rec.risk,
                rec.recoverable_bytes,
                rec.action,
            )
            for row_id, rec in enumerate(recommendations)
        ),
    )

    conn.commit()
    conn.close()
    logger.debug(
        "cleanup_cache: saved %d recommendation(s) for %r",
        len(recommendations),
        scan_path,
    )


def load_recommendations(scan_path):
    """[] if nothing's cached yet for this exact scan_path."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT category, node_path, node_name, node_is_dir, node_size,
               reason, risk, recoverable_bytes, action
        FROM cached_recommendations
        WHERE scan_path = ?
        ORDER BY row_id
    """,
        (scan_path,),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        Recommendation(
            node=CachedNode(node_path, node_name, bool(node_is_dir), node_size),
            category=category,
            reason=reason,
            risk=risk,
            recoverable_bytes=recoverable_bytes,
            action=action,
        )
        for (
            category,
            node_path,
            node_name,
            node_is_dir,
            node_size,
            reason,
            risk,
            recoverable_bytes,
            action,
        ) in rows
    ]


def get_computed_at(scan_path):
    """ISO timestamp the cached run for `scan_path` was computed at, or
    None if nothing's cached for it."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT computed_at FROM cached_cleanup_runs WHERE scan_path = ?", (scan_path,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_most_recently_cached_scan_path():
    """The scan_path with the newest cached run, across every path ever
    cached -- lets Cleanup Recommendations show *something* useful on a
    totally fresh app launch, before any scan has happened this session at
    all. None if nothing has ever been cached."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT scan_path FROM cached_cleanup_runs ORDER BY computed_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None
