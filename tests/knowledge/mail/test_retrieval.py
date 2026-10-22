from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nanobot.knowledge.mail.contracts import ArchivePart, MailError, MailQuery, SourceRead
from nanobot.knowledge.mail.extractors import extract
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import MailStore
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_store import envelope


def ingest(store, mail):
    receipt = store.accept(mail)
    job = store.claim()
    with store.connect() as db:
        documents = list(db.execute("SELECT * FROM documents WHERE mail_id=?", (receipt.mail_id,)))
    for doc in documents:
        if doc["object_ref"]:
            result = extract(store.safe_path(doc["object_ref"]), doc["mime"],
                             body=doc["kind"] == "body", max_chars=2_000_000)
            store.publish_document(job, doc["document_id"], result)
    store.finish(job)
    return receipt


def test_ranked_results_diversify_mails_and_deduplicate_but_source_read_keeps_all(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    first = envelope()
    first.body = "\n".join([f"供应商甲独立条款{i}。" for i in range(6)] + ["重复页脚。", "重复页脚。"])
    one = ingest(store, first)
    second = envelope()
    second.body = "供应商乙独立条款。"
    two = ingest(store, second)
    with store.connect() as db:
        original = list(db.execute("SELECT c.chunk_id,c.text,m.mail_id FROM chunks c JOIN documents d USING(document_id) JOIN mail_items m USING(mail_id) ORDER BY c.ordinal"))
    ranked = [r[0] for mid in [one.mail_id, two.mail_id] for r in original if r[2] == mid]
    encoder = SimpleNamespace(revision="fixture-space", search=lambda *args, **kwargs: ranked)
    retrieval = MailRetrieval(store, auth, encoder)
    page = retrieval.search(access, MailQuery(query="semanticfixture", limit=4))
    assert page.matches[2].mail_id == two.mail_id
    assert page.semantic_status == "semantic_partial"
    all_matches = list(page.matches)
    while page.next_cursor:
        page = retrieval.search(access, MailQuery(cursor=page.next_cursor, limit=4))
        all_matches.extend(page.matches)
    assert len(all_matches) == len(original) - 1
    assert len([m for m in all_matches if m.excerpt == "重复页脚。"]) == 1
    source = retrieval.read_source(access, SourceRead(mail_id=one.mail_id, limit=10000))
    source_matches = list(source.sources)
    while source.next_cursor:
        source = retrieval.read_source(access, SourceRead(mail_id=one.mail_id, cursor=source.next_cursor, limit=10000))
        source_matches.extend(source.sources)
    assert len([m for m in source_matches if m.excerpt == "重复页脚。"]) == 2


def test_exact_mail_match_can_retrieve_other_attachment_tail(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope(parts=[ArchivePart(part_id="1", filename="terms.txt", mime="text/plain",
                                      content=("背景说明\n" * 300 + "付款尾款为123元").encode())])
    mail.subject = "订单 PO-001"
    receipt = ingest(store, mail)
    other = envelope()
    other.subject = "订单 PO-0010"
    other.body = "付款尾款为999元"
    ingest(store, other)
    retrieval = MailRetrieval(store, auth)
    page = retrieval.search(access, MailQuery(query="付款尾款", exact_terms=["po-001"]))
    assert page.matching_mail_count == 1
    assert all(e.mail_id == receipt.mail_id for e in page.matches)
    assert any("123元" in e.excerpt for e in page.matches)
    assert all("999元" not in e.excerpt for e in page.matches)
    assert page.semantic_status == "semantic_unavailable"


def test_all_across_documents_any_and_explicit_empty_scope(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope(parts=[ArchivePart(part_id="1", filename="same.txt", content=b"PO-001")])
    mail.body = "REF-002"
    ingest(store, mail)
    retrieval = MailRetrieval(store, auth)
    page = retrieval.search(access, MailQuery(exact_terms=["PO-001", "REF-002"]))
    assert page.matching_mail_count == 1
    assert retrieval.search(access, MailQuery(exact_terms=["PO-001", "missing"])).matching_mail_count == 0
    assert retrieval.search(access, MailQuery(exact_terms=["PO-001", "missing"], exact_operator="any")).matching_mail_count == 1
    assert retrieval.search(access, MailQuery(mail_ids=[])).matching_mail_count == 0
    body_id = next(e.document_id for e in page.matches if "REF-002" in e.excerpt)
    assert retrieval.search(access, MailQuery(document_ids=[body_id], exact_terms=["PO-001"])).matching_mail_count == 0


def test_strict_received_range_excludes_unknown_and_upper_bound(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    for received in [datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), None]:
        ingest(store, envelope(received_at=received, received_time_basis="internaldate" if received else "unknown"))
    page = MailRetrieval(store, auth).search(access, MailQuery(
        received_from=datetime(2026, 8, 1, tzinfo=UTC), received_before=datetime(2026, 9, 1, tzinfo=UTC),
    ))
    assert page.matching_mail_count == 1
    assert page.excluded_unknown_time == 1


def test_snapshot_keeps_original_generation_and_fixed_expiry(tmp_path, monkeypatch):
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    retrieval = MailRetrieval(store, auth)
    page = retrieval.search(access, MailQuery())
    evidence = page.matches[0]
    with store.connect(write=True) as db:
        db.execute("UPDATE ingest_jobs SET stage='accepted'")
    job = store.claim()
    with store.connect() as db:
        ref = db.execute("SELECT object_ref FROM documents").fetchone()[0]
    store.safe_path(ref).write_text("Updated new version", encoding="utf-8")
    result = extract(store.safe_path(ref), "text/plain", body=True, max_chars=10000)
    store.publish_document(job, evidence.document_id, result)
    store.finish(job)
    assert retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id)).sources[0].excerpt == evidence.excerpt
    assert retrieval.search(access, MailQuery()).matches[0].excerpt == "Updated new version"
    with store.connect(query_state=True) as state:
        expires = state.execute("SELECT expires_at FROM query_runs WHERE query_id=?", (page.query_id,)).fetchone()[0]
    monkeypatch.setattr("nanobot.knowledge.mail.retrieval.time.time", lambda: expires)
    with pytest.raises(MailError, match="source_version_expired"):
        retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))
    with pytest.raises(MailError, match="query_expired"):
        retrieval.page(access, page.query_id + ":0")
    assert receipt.mail_id == evidence.mail_id


def test_removed_source_overrides_query_retention_and_reader_is_independent(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    reader = MailStore(store.root, store.config, readonly=True)
    reader.initialize()
    retrieval = MailRetrieval(reader, auth)
    page = retrieval.search(access, MailQuery())
    evidence = page.matches[0]
    store.remove(receipt.mail_id, 1)
    with pytest.raises(MailError, match="source_removed"):
        retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))
    assert not retrieval.page(access, page.query_id + ":0").matches


def test_query_and_evidence_are_binding_and_session_scoped(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    retrieval = MailRetrieval(store, auth)
    page = retrieval.search(access, MailQuery(query='" OR * NOT private'))
    with pytest.raises(MailError, match="source_not_found"):
        retrieval.page(access.model_copy(update={"session_key": "other"}), page.query_id + ":0")
    auth.revoke(access.binding_id)
    with pytest.raises(MailError, match="authorization_revoked"):
        retrieval.page(access, page.query_id + ":0")
