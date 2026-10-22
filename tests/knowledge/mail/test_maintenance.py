import pytest

from nanobot.knowledge.mail.contracts import (
    ArchivePart,
    MailError,
    MailQuery,
    ManageAction,
    SourceRead,
)
from nanobot.knowledge.mail.ingestion import MailWorker
from nanobot.knowledge.mail.maintenance import MailMaintenance
from nanobot.knowledge.mail.management import MailManagement
from nanobot.knowledge.mail.retrieval import MailRetrieval
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def prepare(store, auth, access, action, targets):
    return MailManagement(store, auth).manage(access, ManageAction(
        action=action, target_ids=targets, request_id="test-" + action,
    ))


@pytest.mark.asyncio
async def test_local_reparse_waits_for_result_and_keeps_old_snapshot(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    retrieval = MailRetrieval(store, auth)
    old = retrieval.search(access, MailQuery()).matches[0]
    with store.connect() as db:
        doc = db.execute("SELECT * FROM documents").fetchone()
    store.safe_path(doc["object_ref"]).write_text("Reparsed retained original", encoding="utf-8")
    receipt = prepare(store, auth, access, "reparse", [doc["document_id"]])
    maintenance = MailMaintenance(store)
    assert maintenance.process_one()
    with store.connect() as db:
        assert db.execute("SELECT status FROM management_actions").fetchone()[0] == "waiting_jobs"
    await MailWorker(store).process(store.claim())
    maintenance.process_one()
    with store.connect() as db:
        assert db.execute("SELECT status FROM management_actions WHERE action_id=?", (receipt.action_id,)).fetchone()[0] == "completed"
    assert retrieval.search(access, MailQuery()).matches[0].excerpt == "Reparsed retained original"
    assert retrieval.read_source(access, SourceRead(evidence_id=old.evidence_id)).sources[0].excerpt == old.excerpt


def test_reindex_uses_retained_text_with_no_original(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    with store.connect() as db:
        original = db.execute("SELECT object_ref FROM documents").fetchone()[0]
    store.safe_path(original).unlink()
    prepare(store, auth, access, "reindex", ["all"])
    assert MailMaintenance(store).process_one()
    with store.connect() as db:
        assert db.execute("SELECT status FROM management_actions").fetchone()[0] == "completed"
        assert db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] != "text-v1"
    assert MailRetrieval(store, auth).search(access, MailQuery()).matches


def test_revocation_before_consumption_prevents_reparse(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    with store.connect() as db:
        document_id = db.execute("SELECT document_id FROM documents").fetchone()[0]
    prepare(store, auth, access, "reparse", [document_id])
    auth.revoke(access.binding_id)
    MailMaintenance(store).process_one()
    with store.connect() as db:
        assert db.execute("SELECT stage FROM ingest_jobs").fetchone()[0] == "finished"
        row = db.execute("SELECT status,receipt_json FROM management_actions").fetchone()
        assert row[0] == "failed"
        assert "authorization_revoked" in row[1]


def test_retry_does_not_fetch_missing_original(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    with store.connect() as db:
        doc = db.execute("SELECT * FROM documents").fetchone()
    prepare(store, auth, access, "retry", [doc["document_id"]])
    store.safe_path(doc["object_ref"]).unlink()
    called = []
    MailMaintenance(store, source_fetcher=lambda *args: called.append(args)).process_one()
    assert called == []
    with store.connect() as db:
        assert "local_input_missing" in db.execute("SELECT receipt_json FROM management_actions").fetchone()[0]


@pytest.mark.asyncio
async def test_two_documents_same_mail_reparse_fences_claim_and_finishes_both_actions(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope(parts=[ArchivePart(part_id='1', filename='terms.txt',
        mime='text/plain', content=b'original terms')]))
    with store.connect() as db:
        docs = list(db.execute('SELECT * FROM documents ORDER BY kind'))
        dispatches = [tuple(r) for r in db.execute('SELECT * FROM interaction_dispatches')]
    for doc in docs:
        store.safe_path(doc['object_ref']).write_text('reparsed ' + doc['kind'], encoding='utf-8')
    manager, maintenance = MailManagement(store, auth), MailMaintenance(store)
    first = manager.manage(access, ManageAction(action='reparse', target_ids=[docs[0]['document_id']], request_id='first'))
    maintenance.process_one()
    stale_job = store.claim()
    second = manager.manage(access, ManageAction(action='reparse', target_ids=[docs[1]['document_id']], request_id='second'))
    maintenance.process_one()
    with store.connect() as db:
        with pytest.raises(MailError, match='revision_conflict'):
            store.check_fence(db, stale_job)
    await MailWorker(store).process(store.claim())
    maintenance.process_one()
    with store.connect() as db:
        assert dict(db.execute('SELECT action_id,status FROM management_actions')) == {
            first.action_id: 'completed', second.action_id: 'completed'}
        assert all(r[0] == 2 for r in db.execute('SELECT active_generation FROM documents'))
        assert [tuple(r) for r in db.execute('SELECT * FROM interaction_dispatches')] == dispatches
