import json
import sqlite3
from contextlib import closing

import pytest
from filelock import FileLock

from nanobot.knowledge.mail.backup import verify_backup
from nanobot.knowledge.mail.contracts import MailError, MailQuery
from nanobot.knowledge.mail.retrieval import MailRetrieval
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def test_consistent_backup_includes_maintenance_inputs_and_tombstones_not_query_history(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    first = ingest(store, envelope())
    removed = ingest(store, envelope())
    store.remove(removed.mail_id, 1)
    MailRetrieval(store, auth).search(access, MailQuery())
    (store.root / "private").mkdir()
    (store.root / "private" / "history.json").write_text("fictional-private-history")
    extra = store.safe_path("maintenance/input_fixture")
    extra.mkdir()
    (extra / "body.bin").write_bytes(b"fictional staged input")
    target = store.root / "backups" / "backup_fixture"
    manifest = store.backup(target)
    assert verify_backup(store, target) == manifest
    assert "maintenance/input_fixture/body.bin" in manifest["files"]
    assert "query_state.sqlite3" not in manifest["files"]
    assert not (target / "private").exists()
    with closing(sqlite3.connect(target / "knowledge.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 1
        assert db.execute("SELECT status FROM mail_items WHERE mail_id=?", (first.mail_id,)).fetchone()[0] == "ready"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_backup_rejects_running_worker_and_paths_outside_private_backups(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    target = store.root / "backups" / "backup_fixture"
    with FileLock(str(store.root / "worker.lock"), timeout=0):
        with pytest.raises(MailError, match="offline_maintenance_required"):
            store.backup(target)
    assert not target.exists()
    with pytest.raises(MailError, match="invalid_backup_path"):
        store.backup(tmp_path / "public")
    with pytest.raises(MailError, match="invalid_backup_path"):
        store.backup(store.root / "objects" / "backup")


def test_backup_integrity_detects_tampered_bytes_and_traversal_manifest(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    ingest(store, envelope())
    target = store.root / "backups" / "backup_fixture"
    manifest = store.backup(target)
    original = (target / "manifest.json").read_text()
    manifest["files"]["../knowledge.sqlite3"] = manifest["files"]["knowledge.sqlite3"]
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(MailError, match="backup_integrity_failed"):
        verify_backup(store, target)
    (target / "manifest.json").write_text(original)
    with (target / "knowledge.sqlite3").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(MailError, match="backup_integrity_failed"):
        verify_backup(store, target)


def test_incomplete_backup_never_publishes_successful_target(tmp_path, monkeypatch):
    store, _, _, _ = make_access(tmp_path)
    ingest(store, envelope())
    target = store.root / "backups" / "backup_fixture"

    def failed_hash(_):
        raise OSError("fictional disk failure")

    monkeypatch.setattr("nanobot.knowledge.mail.backup.file_digest", failed_hash)
    with pytest.raises(OSError):
        store.backup(target)
    assert not target.exists()
    assert not list((store.root / "backups").glob(".building_*"))
