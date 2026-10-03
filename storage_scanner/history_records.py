"""The history database's records other than scans: the delete audit
ledger, size budgets, and the install locations orphaned-install detection
compares each registry read against.
"""

import os
from datetime import datetime

from storage_scanner import history_schema
from storage_scanner.delete_outcome import is_removed
from storage_scanner.history_db import connect, connect_for_write


def record_audit_entry(source, action, path, is_dir, size_bytes, outcome, error_message=None):
    """Record one delete request's outcome to the audit ledger.

    This is the durable record of "what did this app remove, when, from
    where, and where did it go" — every delete in the app (main tree,
    Search & Filter, Duplicate Files, Cleanup Recommendations, the Cleanup
    Cart) writes here via storage_scanner.delete_service, including the
    ones it refused. `outcome` is a storage_scanner.delete_outcome value;
    `success` is kept as "the item is gone" for anything that reads it.
    """
    conn = connect_for_write()
    cur = conn.cursor()

    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute(
        """
        INSERT INTO audit_log
        (created_at, source, action, path, is_dir, size_bytes, success, error_message, outcome)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            created_at,
            source,
            action,
            path,
            int(bool(is_dir)),
            int(size_bytes),
            int(is_removed(outcome)),
            error_message,
            outcome,
        ),
    )

    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    return entry_id


def get_audit_log(limit=500):
    """Every recorded audit entry, most recent first: (created_at, source,
    action, path, is_dir, size_bytes, success, error_message, outcome)."""
    conn = connect()
    cur = conn.cursor()

    # A row with no outcome was written by an older version of the app into
    # an already-migrated database; read it the way the migration would.
    cur.execute(
        f"""
        SELECT created_at, source, action, path, is_dir, size_bytes, success, error_message,
               COALESCE(outcome, {history_schema.LEGACY_OUTCOME_SQL})
        FROM audit_log
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def set_budget(path, threshold_bytes):
    """Create or update the size budget for `path` (upsert, one per path)."""
    conn = connect_for_write()
    cur = conn.cursor()
    created_at = datetime.now().isoformat(timespec="seconds")

    cur.execute(
        """
        INSERT INTO budgets (path, threshold_bytes, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET threshold_bytes = excluded.threshold_bytes
    """,
        (path, threshold_bytes, created_at),
    )

    conn.commit()
    conn.close()


def list_budgets():
    """Every defined budget: [(id, path, threshold_bytes, created_at), ...]."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, path, threshold_bytes, created_at FROM budgets ORDER BY path")
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_budget(budget_id):
    conn = connect_for_write()
    cur = conn.cursor()
    cur.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()
    conn.close()


def record_install_locations_snapshot(locations):
    """Update known_install_locations from a fresh registry read.

    `locations`: [(display_name, install_location), ...] --
    storage_scanner.installed_apps.get_installed_apps()'s own return
    shape, already filtered to the candidate roots (Program Files/
    AppData) by the caller.

    Every install_location is normalized (os.path.normcase +
    os.path.normpath) before being used as the table's primary key --
    the same real folder can legitimately be reported with different
    case or a trailing separator across two different registry reads,
    and without normalizing, that would look like the old form
    "disappearing" and a new one "newly appearing" in the same snapshot
    instead of being recognized as the same, still-installed location.

    Every location present in this snapshot is upserted with
    currently_installed = 1 (first_seen_at set only on first insert,
    last_seen_installed_at bumped every time). Every previously-known
    location NOT present in this snapshot is marked
    currently_installed = 0 -- this is the moment a formerly-installed
    app's leftover folder becomes an orphan candidate. A location can
    only ever be marked 0 if it already existed as a row from an earlier
    snapshot; nothing inserted by this same call can also be zeroed out
    by it, since a location is either present (upserted to 1) or absent
    (only then eligible to be zeroed), never both.
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect_for_write()
    cur = conn.cursor()

    normalized_locations = [
        (display_name, os.path.normcase(os.path.normpath(install_location)))
        for display_name, install_location in locations
    ]
    present_locations = [loc for _name, loc in normalized_locations]

    for display_name, install_location in normalized_locations:
        cur.execute(
            """
            INSERT INTO known_install_locations
                (install_location, display_name, first_seen_at,
                 last_seen_installed_at, currently_installed)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(install_location) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen_installed_at = excluded.last_seen_installed_at,
                currently_installed = 1
        """,
            (install_location, display_name, now, now),
        )

    if present_locations:
        placeholders = ",".join("?" for _ in present_locations)
        cur.execute(
            f"UPDATE known_install_locations SET currently_installed = 0 "
            f"WHERE install_location NOT IN ({placeholders})",
            present_locations,
        )
    else:
        cur.execute("UPDATE known_install_locations SET currently_installed = 0")

    conn.commit()
    conn.close()


def get_orphaned_install_locations():
    """[(install_location, display_name, first_seen_at,
    last_seen_installed_at), ...] for every location whose owning app is
    no longer installed as of the most recent snapshot."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT install_location, display_name, first_seen_at, last_seen_installed_at
        FROM known_install_locations
        WHERE currently_installed = 0
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_installed_install_locations():
    """The normalized install locations the most recent snapshot saw
    installed."""
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT install_location FROM known_install_locations WHERE currently_installed = 1"
    )
    rows = [location for (location,) in cur.fetchall()]
    conn.close()
    return rows


def get_known_install_location_count():
    """Total rows in known_install_locations, regardless of
    currently_installed. 0 means record_install_locations_snapshot has
    never been called before -- orphaned-install detection needs at
    least a second snapshot to find anything (see that function's own
    docstring), so this is what the UI checks to show a "still learning"
    note on a first run rather than silently showing zero results with
    no explanation."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM known_install_locations")
    count = cur.fetchone()[0]
    conn.close()
    return count
