import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage_scanner.audit as audit
from storage_scanner.models import Node


def test_recycle_and_log_records_success(monkeypatch):
    monkeypatch.setattr(audit, "recycle", lambda path: True)
    recorded = {}
    monkeypatch.setattr(
        audit, "record_audit_entry",
        lambda **kwargs: recorded.update(kwargs),
    )

    node = Node("/Users/me/file.bin", "file.bin", is_dir=False)
    node.size = 4096

    assert audit.recycle_and_log(node, source="Duplicate Files") is True
    assert recorded["source"] == "Duplicate Files"
    assert recorded["path"] == "/Users/me/file.bin"
    assert recorded["is_dir"] is False
    assert recorded["size_bytes"] == 4096
    assert recorded["success"] is True
    assert recorded["error_message"] is None


def test_recycle_and_log_records_failure(monkeypatch):
    monkeypatch.setattr(audit, "recycle", lambda path: False)
    recorded = {}
    monkeypatch.setattr(
        audit, "record_audit_entry",
        lambda **kwargs: recorded.update(kwargs),
    )

    node = Node("/Users/me/locked", "locked", is_dir=True)
    node.size = 999

    assert audit.recycle_and_log(node, source="Main tree") is False
    assert recorded["success"] is False
    assert recorded["error_message"] is not None


def test_recycle_and_log_never_raises_if_logging_itself_fails(monkeypatch):
    """A broken audit log must never take down the actual delete flow."""
    monkeypatch.setattr(audit, "recycle", lambda path: True)

    def boom(**kwargs):
        raise sqlite_error

    sqlite_error = RuntimeError("disk full")
    monkeypatch.setattr(audit, "record_audit_entry", boom)

    node = Node("/Users/me/file.bin", "file.bin", is_dir=False)
    node.size = 10

    # Must still report the real recycle() outcome, not raise.
    assert audit.recycle_and_log(node, source="Cleanup Recommendations") is True
