from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.contracts import ArchiveEnvelope, ArchivePart, MailError, SourceIdentity
from nanobot.knowledge.mail.store import MailStore


@pytest.fixture
def store(tmp_path):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig())
    store.initialize()
    return store


def envelope(**updates):
    return ArchiveEnvelope(
        source=SourceIdentity(account_id=uuid4(), mailbox="INBOX", uidvalidity=7, uid=1),
        source_fingerprint="fictional-server-account", sender_address="hr@example.test",
        subject="Fictional onboarding", body="Please arrive on September 28.", **updates,
    )


def count(store, table):
    with store.connect() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_concurrent_accept_and_restart_deduplicate(store):
    mail = envelope()
    second = MailStore(store.root, store.config)
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(lambda s: s.accept(mail), [store, second]))
    assert sorted(r.status for r in receipts) == ["accepted", "duplicate"]
    assert receipts[0].mail_id == receipts[1].mail_id
    second.recover()
    assert second.accept(mail).status == "duplicate"
    assert count(store, "mail_items") == 1
    assert count(store, "ingest_jobs") == 1


def test_manifest_published_before_database_recovers_once(store, monkeypatch):
    mail = envelope(interaction_input={"content": "fictional task"})
    register = store._register_manifest
    monkeypatch.setattr(store, "_register_manifest", lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.accept(mail)
    assert count(store, "mail_items") == 0
    assert count(store, "reservations") == 1
    monkeypatch.setattr(store, "_register_manifest", register)
    store.recover()
    store.recover()
    assert count(store, "mail_items") == count(store, "interaction_dispatches") == 1
    assert count(store, "reservations") == 0
    assert store.accept(mail).status == "duplicate"


def test_incomplete_publication_is_not_admitted(store, monkeypatch):
    import nanobot.knowledge.mail.store as module
    original = module.atomic_write

    def fail_manifest(path, data):
        if path.name == "manifest.json":
            raise OSError("simulated disk failure")
        original(path, data)

    monkeypatch.setattr(module, "atomic_write", fail_manifest)
    with pytest.raises(OSError):
        store.accept(envelope())
    store.recover()
    assert count(store, "mail_items") == count(store, "reservations") == 0
    assert not list((store.root / "spool").iterdir())


def test_capacity_is_atomic_and_released_only_at_completion(store):
    store.config.max_message_bytes = 100
    store.config.max_pending_bytes = 100
    with ThreadPoolExecutor(2) as pool:
        def reserve(key):
            try:
                return store.reserve(key, 60)
            except MailError as error:
                return error.code
        results = list(pool.map(reserve, ["a", "b"]))
    assert results.count("capacity_backpressure") == 1
    assert count(store, "reservations") == 1
    token = next(x for x in results if x != "capacity_backpressure")
    store.release_reservation(token)
    receipt = store.accept(envelope())
    with store.connect() as db:
        before = store.pending_bytes(db)
    assert before > 0
    job = store.claim()
    assert job["mail_id"] == receipt.mail_id
    with store.connect() as db:
        assert store.pending_bytes(db) == before
    store.finish(job)
    with store.connect() as db:
        assert store.pending_bytes(db) == 0


def test_identity_change_is_not_silently_merged(store):
    first = envelope()
    store.accept(first)
    other = first.model_copy(update={
        "source": first.source.model_copy(update={"uid": 2}),
        "source_fingerprint": "different-account",
    })
    with pytest.raises(MailError, match="source_identity_changed"):
        store.accept(other)
    assert count(store, "mail_items") == 1


def test_worker_fence_blocks_stale_and_deleted_commits(store):
    receipt = store.accept(envelope())
    stale = store.claim()
    store.recover()
    current = store.claim()
    with pytest.raises(MailError, match="revision_conflict"):
        store.finish(stale)
    store.remove(receipt.mail_id, 1)
    with pytest.raises(MailError, match="revision_conflict"):
        store.finish(current)


def test_delete_retains_only_minimal_locator_and_blocks_readmission(store):
    mail = envelope(parts=[ArchivePart(
        part_id="1", filename="private.txt", content=b"private attachment", size=18,
    )])
    receipt = store.accept(mail)
    store.remove(receipt.mail_id, 1)
    assert store.accept(mail).status == "removed"
    assert count(store, "documents") == 0
    with store.connect() as db:
        tombstone = dict(db.execute("SELECT * FROM source_tombstones").fetchone())
        assert tombstone["old_mail_id"] == receipt.mail_id
        assert tombstone["uid"] == 1
        assert tombstone["mailbox_server_name"] == "INBOX"
        assert tombstone["cleanup_pending"] == 0
        item = dict(db.execute("SELECT * FROM mail_items").fetchone())
        assert item["subject"] == item["sender_address"] == ""
    assert not list((store.root / "spool").iterdir())


def test_uncertain_dispatches_never_recover_as_pending(store):
    store.accept(envelope(interaction_input={"content": "fake task"}))
    with store.connect(write=True) as db:
        db.execute("UPDATE interaction_dispatches SET state='dispatching'")
    store.recover()
    with store.connect() as db:
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "uncertain"


def test_query_connection_really_readonly_and_missing_store_not_created(store, tmp_path):
    reader = MailStore(store.root, store.config, readonly=True)
    reader.initialize()
    with pytest.raises(MailError, match="knowledge_readonly"):
        with reader.connect(write=True):
            pass
    import sqlite3
    with reader.connect() as db, pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM mail_items")
    missing = MailStore(tmp_path / "missing", store.config, readonly=True)
    with pytest.raises(MailError, match="index_unavailable"):
        missing.initialize()
    assert not missing.root.exists()


def test_newer_schema_rejected_and_path_escape_blocked(store):
    with pytest.raises(MailError, match="invalid_object_path"):
        store.safe_path("../outside")
    with store.connect(write=True) as db:
        db.execute("UPDATE schema_meta SET schema_version=999")
    with pytest.raises(MailError, match="schema_version_unsupported"):
        store.initialize()
