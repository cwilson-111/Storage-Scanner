"""Tests for storage_scanner.cleanup_cache -- pure SQLite round-tripping,
no scan/GUI/Tkinter involved (see cleanup_window.py for how this gets
populated and consulted from the actual Cleanup Recommendations window).
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner import cleanup_cache
from storage_scanner.cleanup_recommendations import (
    CATEGORY_DUPLICATE, CATEGORY_REVIEW, Recommendation,
)
from storage_scanner.models import Node

SCAN_PATH = "C:/Example"


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanup_cache.db"
    monkeypatch.setattr(cleanup_cache, "DB_NAME", db_path)
    cleanup_cache.init_cleanup_cache_db()
    return db_path


def _node(path, name, is_dir=False, size=100):
    n = Node(path, name, is_dir=is_dir)
    n.size = size
    return n


def _rec(node, category=CATEGORY_REVIEW, reason="old and large", risk="Medium",
         recoverable_bytes=None, action="Review, then delete"):
    return Recommendation(
        node=node, category=category, reason=reason, risk=risk,
        recoverable_bytes=node.size if recoverable_bytes is None else recoverable_bytes,
        action=action,
    )


def test_init_cleanup_cache_db_is_idempotent_and_creates_all_tables(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    cleanup_cache.init_cleanup_cache_db()  # second call must not raise

    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()
    assert {"cached_cleanup_runs", "cached_recommendations"} <= tables


def test_load_recommendations_is_empty_for_an_unseen_scan_path(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert cleanup_cache.load_recommendations(SCAN_PATH) == []


def test_save_and_load_round_trips_every_field(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    node = _node("C:/Example/big.mp4", "big.mp4", is_dir=False, size=500_000_000)
    original = _rec(
        node, category=CATEGORY_REVIEW,
        reason="Large file (477 MB) not modified in ~200 days.",
        risk="Medium — not verified safe, just a candidate to look at",
        recoverable_bytes=500_000_000, action="Review, then delete or archive if unneeded",
    )

    cleanup_cache.save_recommendations(SCAN_PATH, [original])
    loaded = cleanup_cache.load_recommendations(SCAN_PATH)

    assert len(loaded) == 1
    rec = loaded[0]
    assert rec.category == original.category
    assert rec.reason == original.reason
    assert rec.risk == original.risk
    assert rec.recoverable_bytes == original.recoverable_bytes
    assert rec.action == original.action
    assert rec.node.path == node.path
    assert rec.node.name == node.name
    assert rec.node.is_dir == node.is_dir
    assert rec.node.size == node.size


def test_save_recommendations_preserves_original_order(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    recs = [
        _rec(_node(f"C:/Example/{i}.bin", f"{i}.bin", size=i), category=CATEGORY_REVIEW)
        for i in (300, 100, 200)  # deliberately not size-sorted
    ]

    cleanup_cache.save_recommendations(SCAN_PATH, recs)
    loaded = cleanup_cache.load_recommendations(SCAN_PATH)

    assert [r.node.size for r in loaded] == [300, 100, 200]


def test_a_second_save_fully_replaces_the_first_for_the_same_path(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    first = [_rec(_node("C:/Example/old.bin", "old.bin"), category=CATEGORY_REVIEW)]
    second = [_rec(_node("C:/Example/new.bin", "new.bin"), category=CATEGORY_DUPLICATE)]

    cleanup_cache.save_recommendations(SCAN_PATH, first)
    cleanup_cache.save_recommendations(SCAN_PATH, second)
    loaded = cleanup_cache.load_recommendations(SCAN_PATH)

    assert len(loaded) == 1
    assert loaded[0].node.path == "C:/Example/new.bin"
    assert loaded[0].category == CATEGORY_DUPLICATE


def test_saving_to_one_scan_path_does_not_affect_another(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    cleanup_cache.save_recommendations(
        "C:/A", [_rec(_node("C:/A/x.bin", "x.bin"))]
    )
    cleanup_cache.save_recommendations(
        "C:/B", [_rec(_node("C:/B/y.bin", "y.bin"))]
    )

    assert len(cleanup_cache.load_recommendations("C:/A")) == 1
    assert len(cleanup_cache.load_recommendations("C:/B")) == 1
    assert cleanup_cache.load_recommendations("C:/A")[0].node.path == "C:/A/x.bin"


def test_get_computed_at_is_none_for_an_unseen_scan_path(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert cleanup_cache.get_computed_at(SCAN_PATH) is None


def test_get_computed_at_returns_a_timestamp_after_a_save(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    cleanup_cache.save_recommendations(SCAN_PATH, [_rec(_node("C:/Example/a", "a"))])

    computed_at = cleanup_cache.get_computed_at(SCAN_PATH)

    assert computed_at is not None
    assert "T" in computed_at  # ISO 8601


def test_get_most_recently_cached_scan_path_is_none_when_nothing_is_cached(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert cleanup_cache.get_most_recently_cached_scan_path() is None


def test_get_most_recently_cached_scan_path_picks_the_newest_run(tmp_path, monkeypatch):
    db_path = _init_db(tmp_path, monkeypatch)
    # Insert directly with explicit, controlled timestamps rather than
    # relying on real wall-clock gaps between two save_recommendations()
    # calls (computed_at only has second precision).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cached_cleanup_runs (scan_path, computed_at) VALUES (?, ?)",
        ("C:/Older", "2024-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO cached_cleanup_runs (scan_path, computed_at) VALUES (?, ?)",
        ("C:/Newer", "2024-06-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    assert cleanup_cache.get_most_recently_cached_scan_path() == "C:/Newer"


def test_recommendations_survive_an_empty_list_save(tmp_path, monkeypatch):
    # A scan with zero recommendations is a legitimate result (nothing
    # flagged) -- saving it must not raise, and must still register the
    # run as "computed" (so get_computed_at reflects a real, recent scan
    # rather than looking like nothing was ever checked).
    _init_db(tmp_path, monkeypatch)
    cleanup_cache.save_recommendations(SCAN_PATH, [])

    assert cleanup_cache.load_recommendations(SCAN_PATH) == []
    assert cleanup_cache.get_computed_at(SCAN_PATH) is not None


def test_cached_node_has_no_children_or_error_attribute():
    # CachedNode is deliberately not a full storage_scanner.models.Node --
    # it must never accidentally be treated as one that could have a
    # live-tree subtree walked into it.
    node = cleanup_cache.CachedNode("C:/x", "x", False, 100)
    assert not hasattr(node, "children")
    assert not hasattr(node, "error")
