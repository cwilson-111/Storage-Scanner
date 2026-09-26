"""The first-run guide is due until it's been closed once, that stays true
across launches (it's stored in the history database), and a database that
can't be read or written never hides the guide or breaks startup."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history
from storage_scanner import onboarding


@pytest.fixture
def history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path / "storage_history.db"))
    history.init_history_db()


def test_guide_is_due_on_first_run_and_not_after_it_was_closed(history_db):
    assert onboarding.should_show_onboarding()

    assert onboarding.mark_onboarding_seen()

    # A later launch reads the same database.
    assert not onboarding.should_show_onboarding()


def test_unusable_database_shows_the_guide_and_reports_the_failed_save(tmp_path, monkeypatch):
    # A folder where the database file should be: SQLite can't open it.
    monkeypatch.setattr(history, "DB_NAME", str(tmp_path))

    assert onboarding.should_show_onboarding()
    assert not onboarding.mark_onboarding_seen()
