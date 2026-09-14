import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import update_check


def test_parse_version_handles_leading_v():
    assert update_check.parse_version("v1.2.3") == (1, 2, 3)
    assert update_check.parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_rejects_non_semver_strings():
    assert update_check.parse_version("0.0.0-dev") is None
    assert update_check.parse_version("main") is None
    assert update_check.parse_version("") is None
    assert update_check.parse_version(None) is None


def test_is_newer_true_when_candidate_is_greater():
    assert update_check.is_newer("v1.2.0", "v1.1.4") is True
    assert update_check.is_newer("v2.0.0", "v1.9.9") is True


def test_is_newer_false_when_equal_or_older():
    assert update_check.is_newer("v1.1.4", "v1.1.4") is False
    assert update_check.is_newer("v1.0.0", "v1.1.4") is False


def test_is_newer_false_when_either_side_is_unparseable():
    # A dev build ("0.0.0-dev") must never be told it's "outdated" —
    # there's nothing meaningful to compare it against.
    assert update_check.is_newer("v1.2.3", "0.0.0-dev") is False
    assert update_check.is_newer("not-a-version", "v1.0.0") is False


def test_check_for_update_skips_network_call_within_min_interval(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    history.set_app_metadata(update_check._LAST_CHECK_KEY, recent)

    called = []
    monkeypatch.setattr(
        update_check, "_fetch_latest_release_tag",
        lambda: called.append(True) or "v99.0.0",
    )

    result = update_check.check_for_update(current_version="v1.0.0")

    assert result is None
    assert called == []  # network fetch must not have been attempted


def test_check_for_update_fetches_when_due_and_reports_newer_version(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    monkeypatch.setattr(update_check, "_fetch_latest_release_tag", lambda: "v2.0.0")

    result = update_check.check_for_update(current_version="v1.0.0")

    assert result == "v2.0.0"
    # The "last checked" timestamp must have been recorded so the next
    # call within 24h skips the network call.
    assert history.get_app_metadata(update_check._LAST_CHECK_KEY) is not None


def test_check_for_update_returns_none_when_already_current(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    monkeypatch.setattr(update_check, "_fetch_latest_release_tag", lambda: "v1.0.0")

    assert update_check.check_for_update(current_version="v1.0.0") is None


def test_check_for_update_never_raises_when_fetch_explodes(tmp_path, monkeypatch):
    db_path = tmp_path / "storage_history.db"
    monkeypatch.setattr(history, "DB_NAME", str(db_path))
    history.init_history_db()

    def boom():
        raise RuntimeError("network is on fire")

    monkeypatch.setattr(update_check, "_fetch_latest_release_tag", boom)

    # Must not propagate — an update check can never be allowed to crash startup.
    assert update_check.check_for_update(current_version="v1.0.0") is None
