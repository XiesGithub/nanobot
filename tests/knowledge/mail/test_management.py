import pytest

from nanobot.knowledge.mail.contracts import MailError, ManageAction
from nanobot.knowledge.mail.management import MailManagement
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def test_remove_requires_owner_confirmation_and_is_replay_safe(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    manager = MailManagement(store, auth)
    action = ManageAction(action="prepare_remove", target_ids=[receipt.mail_id], request_id="remove-1")
    prepared = manager.manage(access, action)
    assert prepared.status == "needs_confirmation"
    assert manager.manage(access, action) == prepared
    with store.connect() as db:
        assert db.execute("SELECT deleted_at FROM mail_items").fetchone()[0] is None
    with pytest.raises(MailError, match="unauthorized"):
        manager.confirm(access.model_copy(update={"session_key": "other"}), prepared.confirmation_token)
    confirmed = manager.confirm(access, prepared.confirmation_token)
    assert confirmed.status == "completed"
    assert manager.manage(access, action).status == "completed"
    with pytest.raises(MailError, match="confirmation_expired"):
        manager.confirm(access, prepared.confirmation_token)


def test_confirmation_revocation_revision_and_policy_checks(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    manager = MailManagement(store, auth)
    prepared = manager.manage(access, ManageAction(action="prepare_remove", target_ids=[receipt.mail_id], request_id="r1"))
    store.config.max_attachments = 2
    with pytest.raises(MailError, match="revision_conflict"):
        manager.confirm(access, prepared.confirmation_token)
    auth.revoke(access.binding_id)
    with pytest.raises(MailError, match="authorization_revoked"):
        manager.confirm(access, prepared.confirmation_token)


def test_local_retry_missing_input_never_fetches_and_gateway_off_queues(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    manager = MailManagement(store, auth)
    with store.connect() as db:
        doc = dict(db.execute("SELECT * FROM documents").fetchone())
    receipt = manager.manage(access, ManageAction(action="reparse", target_ids=[doc["document_id"]], request_id="p1"))
    assert receipt.status == "accepted_waiting_service"
    store.safe_path(doc["object_ref"]).unlink()
    with pytest.raises(MailError, match="local_input_missing"):
        manager.manage(access, ManageAction(action="retry", target_ids=[doc["document_id"]], request_id="p2"))


def test_cleanup_failure_is_pending_and_restore_waits_for_cleanup(tmp_path, monkeypatch):
    from nanobot.knowledge.mail.maintenance import MailMaintenance
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    manager = MailManagement(store, auth)
    prepared = manager.manage(access, ManageAction(action="prepare_remove", target_ids=[receipt.mail_id], request_id="remove"))
    cleanup = store.cleanup_removed

    def fail_cleanup(*args):
        raise OSError("simulated sharing violation")

    monkeypatch.setattr(store, "cleanup_removed", fail_cleanup)
    assert manager.confirm(access, prepared.confirmation_token).status == "accepted"
    restore = manager.manage(access, ManageAction(action="request_restore", target_ids=[receipt.mail_id], request_id="restore"))
    manager.confirm(access, restore.confirmation_token)
    maintenance = MailMaintenance(store)
    assert not maintenance.process_one()
    monkeypatch.setattr(store, "cleanup_removed", cleanup)
    assert maintenance.cleanup_one()
    with store.connect() as db:
        # Cleanup must not cancel the explicitly queued recovery request.
        assert db.execute("SELECT status FROM management_actions WHERE action_id=?", (restore.action_id,)).fetchone()[0] == "accepted_waiting_service"
    maintenance.process_one()
    with store.connect() as db:
        assert db.execute("SELECT status FROM management_actions WHERE action_id=?", (prepared.action_id,)).fetchone()[0] == "completed"


def test_remove_scrubs_sensitive_previews_and_query_filters(tmp_path):
    from nanobot.knowledge.mail.contracts import MailQuery
    from nanobot.knowledge.mail.retrieval import MailRetrieval
    store, auth, _, access = make_access(tmp_path)
    mail = envelope()
    mail.subject = "SENSITIVE-SUBJECT-9832"
    receipt = ingest(store, mail)
    MailRetrieval(store, auth).search(access, MailQuery(subject_terms=[mail.subject]))
    manager = MailManagement(store, auth)
    prepared = manager.manage(access, ManageAction(action="prepare_remove", target_ids=[receipt.mail_id], request_id="remove"))
    manager.confirm(access, prepared.confirmation_token)
    with store.connect() as db:
        for table in ("mail_items", "documents", "confirmations", "management_actions", "source_tombstones"):
            assert mail.subject not in str([tuple(r) for r in db.execute("SELECT * FROM " + table)])
    with store.connect(query_state=True) as db:
        assert not db.execute("SELECT * FROM mail_focus").fetchall()
        assert mail.subject not in str([tuple(r) for r in db.execute("SELECT * FROM query_runs")])
