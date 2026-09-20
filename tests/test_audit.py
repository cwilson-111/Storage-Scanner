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


def test_recycle_and_log_refuses_a_file_whose_size_changed_since_review(tmp_path, monkeypatch):
    """TOCTOU guard: node.size was captured at scan time; if something
    else (an installer, a sync client) modifies the file at node.path
    before the user clicks Delete, recycle_and_log must refuse rather
    than recycle a file that was never actually reviewed and log a
    stale size against it."""
    target = tmp_path / "reviewed.bin"
    target.write_bytes(b"x" * 100)  # scanned at 100 bytes...

    node = Node(str(target), "reviewed.bin", is_dir=False)
    node.size = 100

    target.write_bytes(b"y" * 250)  # ...but changed before the delete click

    called = []
    monkeypatch.setattr(audit, "recycle", lambda path: called.append(path) or True)
    recorded = {}
    monkeypatch.setattr(audit, "record_audit_entry", lambda **kwargs: recorded.update(kwargs))

    assert audit.recycle_and_log(node, source="Cleanup Recommendations") is False
    assert called == []  # the real file must never be sent to the Recycle Bin/Trash
    assert recorded["success"] is False
    assert "changed since it was reviewed" in recorded["error_message"]
    assert target.exists()  # untouched
    assert target.read_bytes() == b"y" * 250


def test_recycle_and_log_proceeds_when_file_is_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "reviewed.bin"
    target.write_bytes(b"x" * 100)

    node = Node(str(target), "reviewed.bin", is_dir=False)
    node.size = 100

    monkeypatch.setattr(audit, "recycle", lambda path: True)
    recorded = {}
    monkeypatch.setattr(audit, "record_audit_entry", lambda **kwargs: recorded.update(kwargs))

    assert audit.recycle_and_log(node, source="Main tree") is True
    assert recorded["success"] is True
    assert recorded["error_message"] is None


def test_recycle_and_log_does_not_check_staleness_for_directories(tmp_path, monkeypatch):
    """A directory's own os.path.getsize() isn't the recursive total
    node.size holds, and folder contents naturally drift during a long
    review session -- the staleness check must never apply to dirs."""
    target = tmp_path / "a_folder"
    target.mkdir()

    node = Node(str(target), "a_folder", is_dir=True)
    node.size = 999_999  # deliberately not the real (irrelevant) dir-entry size

    monkeypatch.setattr(audit, "recycle", lambda path: True)
    recorded = {}
    monkeypatch.setattr(audit, "record_audit_entry", lambda **kwargs: recorded.update(kwargs))

    assert audit.recycle_and_log(node, source="Main tree") is True
    assert recorded["success"] is True


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
