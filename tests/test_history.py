import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history


def test_get_latest_scan_id_returns_most_recent_scan(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))

    history.init_history_db()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scans
            (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Example", 100, 1000, 10, 3, "2024-01-01T00:00:00"),
    )
    cur.execute(
        """
        INSERT INTO scans
            (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Example", 200, 1000, 20, 5, "2024-02-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    assert history.get_latest_scan_id("C:/Example") == 2


def test_list_scans_for_path_returns_all_scans_newest_first(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))

    history.init_history_db()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for total_size, created_at in [
        (100, "2024-01-01T00:00:00"),
        (200, "2024-02-01T00:00:00"),
        (150, "2024-03-01T00:00:00"),
    ]:
        cur.execute(
            """
            INSERT INTO scans
                (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("C:/Example", total_size, 1000, 10, 3, created_at),
        )
    # A scan of a different path must not show up in this path's picker.
    cur.execute(
        """
        INSERT INTO scans
            (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Other", 999, 1000, 10, 3, "2024-04-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    rows = history.list_scans_for_path("C:/Example")

    assert len(rows) == 3
    # Newest first, by created_at — lets the comparison picker default to
    # the two most recent without an extra sort step.
    assert [created_at for _id, created_at, _size, _files in rows] == [
        "2024-03-01T00:00:00",
        "2024-02-01T00:00:00",
        "2024-01-01T00:00:00",
    ]
    assert [size for _id, _created_at, size, _files in rows] == [150, 200, 100]


def test_get_scan_ids_by_created_at_maps_each_timestamp_to_its_scan_id(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))

    history.init_history_db()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for total_size, created_at in [
        (100, "2024-01-01T00:00:00"),
        (200, "2024-02-01T00:00:00"),
    ]:
        cur.execute(
            """
            INSERT INTO scans
                (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("C:/Example", total_size, 1000, 10, 3, created_at),
        )
    # A different path's scan must never leak into this path's mapping.
    cur.execute(
        """
        INSERT INTO scans
            (scan_path, total_size, drive_capacity, file_count, folder_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("C:/Other", 999, 1000, 10, 3, "2024-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    mapping = history.get_scan_ids_by_created_at("C:/Example")

    assert mapping == {"2024-01-01T00:00:00": 1, "2024-02-01T00:00:00": 2}


def test_a_limited_scan_history_is_the_newest_scans_oldest_first(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()
    conn = sqlite3.connect(db_path)
    for total_size, created_at in [
        (100, "2024-01-01T00:00:00"),
        (200, "2024-02-01T00:00:00"),
        (300, "2024-03-01T00:00:00"),
        (350, "2024-03-01T00:00:00"),  # saved in the same second as the one before
        (400, "2024-04-01T00:00:00"),
    ]:
        conn.execute(
            "INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, "
            "folder_count, created_at) VALUES ('C:/Example', ?, 1000, 10, 3, ?)",
            (total_size, created_at),
        )
    conn.execute(
        "INSERT INTO scans (scan_path, total_size, drive_capacity, file_count, "
        "folder_count, created_at) VALUES ('C:/Other', 999, 1000, 10, 3, '2024-05-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    rows = history.get_scan_history("C:/Example", limit=2)
    ids = history.get_scan_ids_by_created_at("C:/Example", limit=2)

    assert [(created_at, size) for created_at, size, _files, _folders in rows] == [
        ("2024-03-01T00:00:00", 350),
        ("2024-04-01T00:00:00", 400),
    ]
    assert ids == {"2024-03-01T00:00:00": 4, "2024-04-01T00:00:00": 5}
    assert [row[1] for row in history.get_scan_history("C:/Example", limit=10)] == [
        100,
        200,
        300,
        350,
        400,
    ]


def test_record_and_get_audit_entry_round_trips(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_audit_entry(
        source="Duplicate Files",
        action="recycle",
        path="/Users/me/dup.bin",
        is_dir=False,
        size_bytes=1234,
        success=True,
    )
    history.record_audit_entry(
        source="Main tree",
        action="recycle",
        path="/Users/me/locked",
        is_dir=True,
        size_bytes=999,
        success=False,
        error_message="It may be in use, protected, or require admin rights.",
    )

    rows = history.get_audit_log()

    assert len(rows) == 2
    # Most recent first.
    created_at, source, action, path, is_dir, size_bytes, success, error_message = rows[0]
    assert source == "Main tree"
    assert action == "recycle"
    assert path == "/Users/me/locked"
    assert is_dir == 1
    assert size_bytes == 999
    assert success == 0
    assert "protected" in error_message

    second = rows[1]
    assert second[1] == "Duplicate Files"
    assert second[5] == 1234
    assert second[6] == 1
    assert second[7] is None


def test_get_audit_log_respects_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    for i in range(5):
        history.record_audit_entry(
            source="Search & Filter",
            action="recycle",
            path=f"/tmp/f{i}.bin",
            is_dir=False,
            size_bytes=i,
            success=True,
        )

    assert len(history.get_audit_log(limit=3)) == 3
    assert len(history.get_audit_log(limit=100)) == 5


def test_fresh_snapshot_has_no_orphans(tmp_path, monkeypatch):
    """The very first snapshot ever taken can't find any orphans -- there's
    nothing to compare against yet, by design (see history.py's own
    known_install_locations docstring)."""
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_install_locations_snapshot(
        [
            ("An App", "C:/Program Files/An App"),
        ]
    )

    assert history.get_orphaned_install_locations() == []


def test_a_location_missing_from_the_next_snapshot_becomes_orphaned(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    # Snapshot 1: the app is installed.
    history.record_install_locations_snapshot(
        [
            ("An App", "C:/Program Files/An App"),
        ]
    )
    assert history.get_orphaned_install_locations() == []

    # Snapshot 2: the app is gone -- its location is now an orphan candidate.
    history.record_install_locations_snapshot([])

    orphans = history.get_orphaned_install_locations()
    assert len(orphans) == 1
    install_location, display_name, first_seen_at, last_seen_installed_at = orphans[0]
    assert install_location == os.path.normcase(os.path.normpath("C:/Program Files/An App"))
    assert display_name == "An App"
    assert first_seen_at == last_seen_installed_at  # only ever seen installed once


def test_a_location_still_present_in_the_next_snapshot_is_not_orphaned(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_install_locations_snapshot([("An App", "C:/Program Files/An App")])
    history.record_install_locations_snapshot([("An App", "C:/Program Files/An App")])

    assert history.get_orphaned_install_locations() == []


def test_an_orphan_that_gets_reinstalled_is_no_longer_orphaned(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_install_locations_snapshot([("An App", "C:/Program Files/An App")])
    history.record_install_locations_snapshot([])  # now orphaned
    assert len(history.get_orphaned_install_locations()) == 1

    history.record_install_locations_snapshot(
        [("An App", "C:/Program Files/An App")]
    )  # reinstalled
    assert history.get_orphaned_install_locations() == []


def test_an_app_never_seen_installed_never_appears_as_an_orphan(tmp_path, monkeypatch):
    """Guards against a same-session false positive: a location that has
    never once been observed as currently_installed = 1 has no row at all
    until it's actually seen installed, so it can never spontaneously
    appear as an orphan just because it's absent from a snapshot."""
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    # A location that was never in any prior snapshot, and isn't in this
    # one either -- there's no row for it at all, so nothing can flag it.
    history.record_install_locations_snapshot([("Other App", "C:/Program Files/Other App")])

    assert history.get_orphaned_install_locations() == []


def test_snapshot_upsert_updates_display_name_and_last_seen(tmp_path, monkeypatch):
    """A location can legitimately be reused by a different app over time
    (uninstall, then something else installs to the same path) -- the
    stored display_name and last_seen_installed_at should always reflect
    the most recent snapshot, not the first one ever seen."""
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    history.record_install_locations_snapshot([("Old App", "C:/Program Files/Shared Path")])
    history.record_install_locations_snapshot([])  # orphaned
    history.record_install_locations_snapshot([("New App", "C:/Program Files/Shared Path")])

    # Reinstalled (under a different app) -- no longer an orphan, and the
    # stored name reflects whichever app is there now.
    assert history.get_orphaned_install_locations() == []

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT display_name, currently_installed FROM known_install_locations "
        "WHERE install_location = ?",
        (os.path.normcase(os.path.normpath("C:/Program Files/Shared Path")),),
    )
    display_name, currently_installed = cur.fetchone()
    conn.close()
    assert display_name == "New App"
    assert currently_installed == 1


def _save(folders):
    return history.save_scan_snapshot(
        "C:/Example",
        sum(folders.values()),
        1000,
        len(folders),
        len(folders),
        {path: {"size": size, "file_count": 7} for path, size in folders.items()},
    )


def test_folder_growth_between_two_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()
    older = _save(
        {
            "C:/Example/grew": 100,
            "C:/Example/same": 200,
            "C:/Example/shrank": 400,
            "C:/Example/gone": 999,
        }
    )
    newer = _save(
        {
            "C:/Example/grew": 150,
            "C:/Example/same": 200,
            "C:/Example/shrank": 300,
            "C:/Example/new": 500,
        }
    )

    rows = history.get_folder_growth(newer, older)

    # Largest growth first; a folder only the older scan had isn't listed.
    assert rows == [
        ("C:/Example/new", 0, 500, 500, None, "Growing", 7),
        ("C:/Example/grew", 100, 150, 50, 50.0, "Growing", 7),
        ("C:/Example/same", 200, 200, 0, 0.0, "Unchanged", 7),
        ("C:/Example/shrank", 400, 300, -100, -25.0, "Shrinking", 7),
    ]
    summary = history.get_growth_summary(newer, older)
    assert (summary["tracked_folders"], summary["new_folders"]) == (4, 1)
    assert summary["largest_growth_folder"][0] == "C:/Example/new"
    assert summary["largest_shrink_folder"][0] == "C:/Example/shrank"


def test_folder_growth_limit_breaks_ties_by_path(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()
    folders = {f"C:/Example/{name}": 10 for name in ("d", "b", "a", "c")}
    older = _save(folders)
    newer = _save({**folders, "C:/Example/c": 11})

    rows = history.get_folder_growth(newer, older, limit=3)

    assert [row[0] for row in rows] == ["C:/Example/c", "C:/Example/a", "C:/Example/b"]
