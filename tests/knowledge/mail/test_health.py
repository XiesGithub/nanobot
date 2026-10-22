from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_store import envelope


def test_health_separates_unaccepted_sources_from_durable_jobs_and_ack_retries(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail, waiting = envelope(), envelope()
    store.accept(mail)
    with store.connect(write=True) as db:
        for source in (mail.source, waiting.source):
            db.execute("INSERT INTO admission_waits(source_key,source_json,first_at,next_attempt_at,error_code,attempt) VALUES(?,?,?,?,?,?)", (
                source.key, source.model_dump_json(), 1, 2, "capacity_backpressure", 1))
        db.execute("""INSERT INTO admission_waits VALUES(
            'unavailable','{}',1,2,'source_no_longer_unseen',1,'source_unavailable')""")
    service = MailService(store.root, store.config, readonly=True)
    service.access = auth
    health = service.status_sync(access)
    assert health.admission_waiting_count == 1
    assert health.admission_waiting_reasons == {"capacity_backpressure": 1}
    assert health.ingest_counts == {"accepted": 1}
    assert health.counts == {"queued": 1}
    assert health.pending_bytes > 0
    # Inputs still awaiting extraction have no text version to encode. They
    # belong to durable input stages, not the independent vector backlog.
    assert health.index_pending_documents == 0
    with store.connect(write=True) as db:
        db.execute("UPDATE documents SET active_generation=1 WHERE kind='body'")
    assert service.status_sync(access).index_pending_documents == 1
