import json

import pytest

from nanobot.knowledge.mail.answering import MailAnswerService
from nanobot.knowledge.mail.contracts import MailError, MailQuery
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_answering import FakeProvider, service_for
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def extractor(data):
    return {"status": "answered", "claims": [{"text": e["excerpt"],
        "evidence_ids": [e["evidence_id"]]} for e in data["evidence"][:30]]}


@pytest.mark.asyncio
async def test_full_range_continues_snapshot_without_top_k_or_new_mail(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    expected = set()
    for i in range(54):
        mail = envelope()
        mail.body = f"虚构项目第{i}项安排。"
        expected.add(ingest(store, mail).mail_id)
    provider = FakeProvider(extractor)
    answerer = MailAnswerService(service_for(store, auth), provider)
    result = await answerer.answer(access, "汇总所有安排", [MailQuery(mode="enumerate")])
    query_id = result.query_ids[0]
    assert result.status == "summary_partial"
    assert result.coverage["matched_mail_count"] == 54
    with store.connect(query_state=True) as db:
        expiry = db.execute("SELECT expires_at FROM query_runs WHERE query_id=?", (query_id,)).fetchone()[0]
    late = ingest(store, envelope()).mail_id
    seen, rounds = {e.mail_id for e in result.citations}, 1
    while result.status == "summary_partial":
        before = len(provider.calls)
        result = await answerer.answer(access, "继续上一批", [], continue_summary=True)
        assert len(provider.calls) - before <= 4
        assert result.query_ids == [query_id]
        assert result.coverage["inspected_this_turn"] <= 50
        seen.update(e.mail_id for e in result.citations)
        rounds += 1
        assert rounds < 15
    assert seen == expected and late not in seen
    assert result.coverage["processed_mail_count"] == 54
    with store.connect(query_state=True) as db:
        assert db.execute("SELECT expires_at FROM query_runs WHERE query_id=?", (query_id,)).fetchone()[0] == expiry
    before = len(provider.calls)
    result = await answerer.answer(access, "继续上一批", [], continue_summary=True)
    assert result.status == "summary_complete" and not result.citations
    assert len(provider.calls) == before


@pytest.mark.asyncio
async def test_long_mail_continuation_uses_every_chunk_once_and_fixed_expiry(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope()
    mail.body = "\n".join(f"第{i}条：" + "虚构说明" * 25 for i in range(45))
    ingest(store, mail)
    with store.connect() as db:
        expected = {row[0] for row in db.execute("SELECT chunk_id FROM chunks")}
    provider = FakeProvider(extractor)
    answerer = MailAnswerService(service_for(store, auth), provider)
    result = await answerer.answer(access, "汇总", [MailQuery(mode="enumerate")])
    assert result.status == "summary_partial" and result.coverage["processed_mail_count"] == 0
    seen = [e.chunk_id for e in result.citations]
    while result.status == "summary_partial":
        result = await answerer.answer(access, "继续上一批", [], continue_summary=True)
        seen.extend(e.chunk_id for e in result.citations)
    assert set(seen) == expected and len(seen) == len(expected)
    assert result.coverage["processed_mail_count"] == 1
    with store.connect(write=True, query_state=True) as db:
        db.execute("UPDATE query_runs SET expires_at=0 WHERE query_id=?", (result.query_ids[0],))
    with pytest.raises(MailError, match="query_expired"):
        await answerer.answer(access, "继续上一批", [], continue_summary=True)


@pytest.mark.asyncio
async def test_pending_and_failed_documents_are_counted_without_llm(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    receipt = store.accept(envelope())
    provider = FakeProvider(lambda _: pytest.fail("No usable evidence"))
    answer = await MailAnswerService(service_for(store, auth), provider).answer(
        access, "汇总全部", [MailQuery(mode="enumerate")])
    assert answer.coverage["matched_mail_count"] == 1
    assert answer.coverage["processed_mail_count"] == 0
    assert answer.coverage["pending_documents"] == 1
    assert receipt.mail_id and not provider.calls


async def test_summary_fallback_remains_visible_after_continuation(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    for _ in range(21):
        ingest(store, envelope())
    provider = FakeProvider(lambda data: {"not": "a valid draft"})
    answerer = MailAnswerService(service_for(store, auth), provider)
    first = await answerer.answer(access, "汇总全部", [MailQuery(mode="enumerate")])
    assert first.status == "summary_partial"
    assert first.coverage["unverified_batches"] == 2
    provider.draft = extractor
    last = await answerer.answer(access, "继续", [], continue_summary=True)
    assert last.status == "summary_complete_with_fallback"
    assert last.coverage["unverified_batches"] == 2
    assert last.claims and "不表示摘要验证通过" in last.text


@pytest.mark.asyncio
async def test_earlier_batch_deletion_during_later_generation_blocks_whole_answer(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    for _ in range(20):
        mail = envelope()
        mail.body = "虚构安排" * 40
        ingest(store, mail)
    first = []

    def draft(data):
        if first:
            store.remove(first[0], 1)
        else:
            first.append(data["evidence"][0]["mail_id"])
        return extractor(data)

    with pytest.raises(MailError, match="source_removed"):
        await MailAnswerService(service_for(store, auth), FakeProvider(draft)).answer(
            access, "汇总", [MailQuery(mode="enumerate")])
    with store.connect(query_state=True) as db:
        assert db.execute("SELECT COUNT(*) FROM summary_progress").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_summary_cannot_cross_session_or_use_ranked_query(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    answerer = MailAnswerService(service_for(store, auth), FakeProvider(extractor))
    await answerer.answer(access, "汇总", [MailQuery(mode="enumerate")])
    with pytest.raises(MailError, match="summary_not_started"):
        await answerer.answer(access.model_copy(update={"session_key": "another"}),
                              "继续", [], continue_summary=True)
    with store.connect(write=True, query_state=True) as db:
        db.execute("UPDATE query_runs SET filters_json=?", (json.dumps({"mode": "hybrid"}),))
    with pytest.raises(MailError, match="summary_requires_single_range"):
        await answerer.answer(access, "继续", [], continue_summary=True)
