import json

import pytest

from nanobot.knowledge.mail.contracts import MailError, MailQuery, SourceRead
from nanobot.knowledge.mail.restore import restore_backup
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import identifier
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_process_recovery import child
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def backup(store):
    name = identifier("backup_")
    store.backup(store.root / "backups" / name)
    return name


def test_restore_preserves_new_deletion_revocation_and_invalidates_old_snapshots(tmp_path):
    store, auth, binding, access = make_access(tmp_path)
    removed = ingest(store, envelope())
    kept = ingest(store, envelope())
    page = MailRetrieval(store, auth).search(access, MailQuery(mail_ids=[kept.mail_id]))
    name = backup(store)
    new_removed = ingest(store, envelope())
    store.remove(removed.mail_id, 1)
    store.remove(new_removed.mail_id, 1)
    auth.revoke(binding.binding_id)
    result = restore_backup(store, name, principal_id="fixture-local-admin")
    assert result["query_snapshots_invalidated"]
    assert not auth.authenticate("cli", "alice", private_chat=True).authenticated
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM mail_items WHERE deleted_at IS NULL").fetchone()[0] == 1
        assert db.execute("SELECT mail_id FROM mail_items WHERE deleted_at IS NULL").fetchone()[0] == kept.mail_id
        assert db.execute("SELECT COUNT(*) FROM documents WHERE mail_id=?", (removed.mail_id,)).fetchone()[0] == 0
    auth.grant(binding)
    refreshed = access.model_copy(update={"policy_revision": auth.authenticate("cli", "alice", private_chat=True).policy_revision})
    with pytest.raises(MailError, match="source_not_found"):
        MailRetrieval(store, auth).read_source(refreshed, SourceRead(evidence_id=page.matches[0].evidence_id))
    assert MailRetrieval(store, auth).search(refreshed, MailQuery()).matches


def test_interrupted_restore_blocks_queries_and_only_original_local_principal_can_resume(tmp_path, monkeypatch):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    name = backup(store)
    original = __import__("nanobot.knowledge.mail.restore", fromlist=["_restore_database"])._restore_database

    def fail(*_):
        raise OSError("fictional disk outage")

    monkeypatch.setattr("nanobot.knowledge.mail.restore._restore_database", fail)
    with pytest.raises(MailError, match="restore_recovery_required"):
        restore_backup(store, name, principal_id="fixture-local-admin")
    assert (store.root / "restore_pending.json").is_file()
    with pytest.raises(MailError, match="restore_recovery_required"):
        store.initialize()
    with pytest.raises(MailError):
        MailRetrieval(store, auth).search(access, MailQuery())
    with pytest.raises(MailError, match="unauthorized"):
        restore_backup(store, "", principal_id="someone-else", resume=True)
    monkeypatch.setattr("nanobot.knowledge.mail.restore._restore_database", original)
    assert restore_backup(store, "", principal_id="fixture-local-admin", resume=True)["status"] == "completed"
    assert not (store.root / "restore_pending.json").exists()
    with store.connect(write=True, query_state=True) as db:
        assert db.execute("SELECT COUNT(*) FROM query_runs").fetchone()[0] == 0


def test_restore_cancels_old_confirmations_and_never_replays_pending_interaction(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    ingest(store, envelope(interaction_input={"content": "fictional interaction"}))
    name = backup(store)
    with store.connect(write=True) as db:
        db.execute("UPDATE schema_meta SET trigger_agent=0,mode_revision=7")
    restore_backup(store, name, principal_id="fixture-local-admin")
    with store.connect() as db:
        mode = db.execute("SELECT trigger_agent,mode_revision FROM schema_meta").fetchone()
        assert tuple(mode) == (0, 8)
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "uncertain"
    assert not list(store.root.glob("restore_*"))


def test_restore_marker_rejects_directory_escape(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    (store.root / "restore_pending.json").write_text(json.dumps({"stage": "../outside", "principal_id": "fixture"}))
    with pytest.raises(MailError, match="restore_integrity_failed"):
        restore_backup(store, "", principal_id="fixture", resume=True)


def test_real_process_exit_during_restore_remains_closed_until_explicit_resume(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    receipt = ingest(store, envelope())
    name = backup(store)
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from nanobot.knowledge.mail import restore
        store = MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        restore._restore_database = lambda *args: os._exit(46)
        restore.restore_backup(store, sys.argv[2], principal_id='fixture-local-admin')
    """, store.root, name)
    assert result.returncode == 46, result.stderr
    with pytest.raises(MailError, match="restore_recovery_required"):
        with store.connect():
            pytest.fail("No read while a restore is partially installed")
    restore_backup(store, "", principal_id="fixture-local-admin", resume=True)
    with store.connect() as db:
        assert db.execute("SELECT mail_id FROM mail_items WHERE deleted_at IS NULL").fetchone()[0] == receipt.mail_id
