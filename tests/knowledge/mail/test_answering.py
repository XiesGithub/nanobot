import json
from types import SimpleNamespace

import pytest

from nanobot.knowledge.mail.answering import MailAnswerService
from nanobot.knowledge.mail.contracts import MailError, MailQuery
from nanobot.knowledge.mail.service import MailService
from nanobot.providers.base import LLMResponse
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


class FakeProvider:
    def __init__(self, draft, effect=None):
        self.draft = draft
        self.effect = effect
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["tools"] is None
        self.calls.append(kwargs)
        if self.effect:
            self.effect()
            self.effect = None
        if "Check each claim" in kwargs["messages"][0]["content"]:
            data = json.loads(kwargs["messages"][1]["content"])
            return LLMResponse(json.dumps({"supported": [True] * len(data)}))
        data = json.loads(kwargs["messages"][1]["content"])
        return LLMResponse(json.dumps(self.draft(data), ensure_ascii=False))


def service_for(store, auth):
    service = MailService(store.root, store.config, readonly=True)
    service.access = auth
    return service


@pytest.mark.asyncio
async def test_server_rendered_sources_and_no_tools(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope()
    mail.body = "报到日期9月28日，地点星河楼。"
    ingest(store, mail)
    provider = FakeProvider(lambda data: {"status": "answered", "claims": [{
        "text": "报到日期为9月28日。", "evidence_ids": [data["evidence"][0]["evidence_id"]],
    }]})
    answer = await MailAnswerService(service_for(store, auth), provider).answer(access, "哪天报到", [MailQuery(query="报到")])
    assert "hr@example.test" in answer.text
    assert answer.citations
    assert answer.claims[0].text == "报到日期为9月28日。"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_fabricated_source_and_wrong_number_rejected_twice(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope()
    mail.body = "金额123元。"
    ingest(store, mail)
    for draft in [
        lambda data: {"status": "answered", "claims": [{"text": "金额123元", "evidence_ids": ["ev_fabricated"]}]},
        lambda data: {"status": "answered", "claims": [{"text": "金额12元", "evidence_ids": [data["evidence"][0]["evidence_id"]]}]},
    ]:
        answer = await MailAnswerService(service_for(store, auth), FakeProvider(draft)).answer(access, "金额", [MailQuery()])
        assert answer.status == "unverified_draft_rejected"
        assert "金额123元" in answer.text


@pytest.mark.asyncio
async def test_quoted_old_price_not_rendered_as_new_confirmation(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    mail = envelope()
    mail.body = "> 旧报价100元。"
    ingest(store, mail)
    provider = FakeProvider(lambda data: {"status": "answered", "claims": [{
        "text": "最新确认报价100元。", "asserts_current": True,
        "evidence_ids": [data["evidence"][0]["evidence_id"]],
    }]})
    answer = await MailAnswerService(service_for(store, auth), provider).answer(access, "报价", [MailQuery()])
    assert answer.status == "unverified_draft_rejected"
    assert "最新确认报价" not in answer.text
    assert "历史引用" in answer.text


@pytest.mark.asyncio
async def test_generation_time_removal_and_revocation_prevent_delivery(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    receipt = ingest(store, envelope())
    provider = FakeProvider(lambda data: {"status": "answered", "claims": [{
        "text": "Please arrive on September 28.", "evidence_ids": [data["evidence"][0]["evidence_id"]],
    }]}, effect=lambda: store.remove(receipt.mail_id, 1))
    with pytest.raises(MailError, match="source_removed"):
        await MailAnswerService(service_for(store, auth), provider).answer(access, "when", [MailQuery()])
    ingest(store, envelope())
    provider.effect = lambda: auth.revoke(access.binding_id)
    with pytest.raises(MailError, match="authorization_revoked"):
        await MailAnswerService(service_for(store, auth), provider).answer(access, "when", [MailQuery()])


@pytest.mark.asyncio
async def test_empty_result_does_not_call_llm(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    provider = SimpleNamespace(chat_with_retry=lambda **kwargs: pytest.fail("No model on empty result"))
    answer = await MailAnswerService(service_for(store, auth), provider).answer(access, "mail", [MailQuery()])
    assert "已归档范围" in answer.text
    assert not answer.citations


@pytest.mark.asyncio
async def test_model_timeout_falls_back_to_reread_sources(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())

    async def timeout(**kwargs):
        raise TimeoutError()

    answer = await MailAnswerService(service_for(store, auth), SimpleNamespace(
        chat_with_retry=timeout)).answer(access, "when", [MailQuery()])
    assert answer.status == "unverified_draft_rejected"
    assert answer.citations
    assert answer.claims == []


@pytest.mark.asyncio
async def test_correction_receives_safe_specific_validation_failure(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    provider = FakeProvider(lambda data: {"status": "answered", "claims": [{
        "text": "日期是12345日。", "evidence_ids": [data["evidence"][0]["evidence_id"]],
    }]})
    await MailAnswerService(service_for(store, auth), provider).answer(access, "when", [MailQuery()])
    assert "unsupported_numeric_claim" in provider.calls[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_comparison_reserves_evidence_budget_for_each_query_group(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    first = envelope()
    first.subject = "订单 PO-001"
    first.body = "\n".join("付款条款" + "背景" * 55 + str(i) for i in range(12))
    ingest(store, first)
    second = envelope()
    second.subject = "订单 PO-002"
    second.body = "付款金额999元。"
    second_receipt = ingest(store, second)
    seen = []

    def draft(data):
        seen.extend(data["evidence"])
        return {"status": "insufficient"}

    await MailAnswerService(service_for(store, auth), FakeProvider(draft)).answer(access,
        "比较两个订单", [MailQuery(exact_terms=["PO-001"]), MailQuery(exact_terms=["PO-002"])])
    assert any(item["mail_id"] == second_receipt.mail_id for item in seen)
    assert len({item["mail_id"] for item in seen}) == 2
