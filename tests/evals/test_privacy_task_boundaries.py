import json

import pytest

from evals.privacy_task_boundaries import probe_boundaries
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from nanobot.knowledge.mail.contracts import AccessContext, ArchivePart, MailQuery
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


class CaptureConnection:
    def __init__(self):
        self.sent = []
        self.remote_address = ("127.0.0.1", 1)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_real_http_websocket_domain_and_cache_boundaries(tmp_path):
    config = MailKnowledgeConfig(enabled=True, semantic_enabled=False)
    service = MailService(tmp_path / "mail", config, worker_enabled=False)
    service.store.initialize()
    service.access.initialize_bindings([MailOwnerBinding(
        binding_id="privacy-owner", principal_id="fictional-principal-A",
        channel="websocket", scopes={"read", "manage"},
    )])
    extension = MailTurnExtension(service)
    channel = WebSocketChannel(WebSocketConfig(
        enabled=True, token_issue_secret="fictional-login-only", websocket_requires_token=True,
    ), MessageBus(), mail_service=service)
    web = channel._mail_access

    owner_token, visitor_token = "privacy-owner-token", "privacy-visitor-token"
    security_a = web.issue(owner_token, 300, owner_principal="fictional-principal-A")
    security_b = web.issue(visitor_token, 300)
    chat_a, chat_b = "privacy-A", "privacy-B"
    access_a = AccessContext.from_security(security_a, web.session_key(security_a, chat_a))

    attachment_text_a = "PRIVATE-A17附件正文：费用为120元。"
    attachment_text_b = "PRIVATE-FLOW-X8附件正文：会议时间为周五15:00。"
    receipt_a = ingest(service.store, envelope(parts=[ArchivePart(
        part_id="1", filename="PRIVATE-A17.txt", mime="text/plain",
        content=attachment_text_a.encode("utf-8"))]).model_copy(update={
            "subject": "PRIVATE-A17 合成项目材料", "body": "PRIVATE-A17 私有正文：费用为120元。"}))
    receipt_b = ingest(service.store, envelope(parts=[ArchivePart(
        part_id="1", filename="PRIVATE-FLOW-X8.txt", mime="text/plain",
        content=attachment_text_b.encode("utf-8"))]).model_copy(update={
            "subject": "PRIVATE-FLOW-X8 合成会议材料", "body": "PRIVATE-FLOW-X8 私有正文：会议时间为周五15:00。"}))
    page_a = await service.search(access_a, MailQuery(
        query="PRIVATE-A17 费用", exact_terms=["PRIVATE-A17"], mode="keyword", limit=8,
    ))
    page_b = await service.search(access_a, MailQuery(
        query="PRIVATE-FLOW-X8 会议", exact_terms=["PRIVATE-FLOW-X8"], mode="keyword", limit=8,
    ))
    assert page_a.matches and page_b.matches
    evidence_a, evidence_b = page_a.matches[0], page_b.matches[0]
    with service.store.connect() as db:
        attachment_a = db.execute(
            "SELECT document_id,sha256 FROM documents WHERE mail_id=? AND kind='attachment'",
            (receipt_a.mail_id,),
        ).fetchone()
        attachment_b = db.execute(
            "SELECT document_id,sha256 FROM documents WHERE mail_id=? AND kind='attachment'",
            (receipt_b.mail_id,),
        ).fetchone()
    assert attachment_a and attachment_b

    owner_connection = CaptureConnection()
    visitor_connection = CaptureConnection()
    web.bind(owner_connection, owner_token)
    web.bind(visitor_connection, visitor_token)
    web.attach(owner_connection, chat_a)
    owner_session = extension.sessions.get_or_create(access_a.session_key)
    owner_session.metadata.update(binding_id=access_a.binding_id, title="synthetic private session")
    owner_session.add_message("assistant", "PRIVATE-FLOW-X8 session history")
    extension.sessions.save(owner_session)

    actor_a = {
        "role": "A", "security": security_a, "access": access_a, "token": owner_token,
        "connection": owner_connection, "chat_id": chat_a,
    }
    actor_b = {
        "role": "B", "security": security_b, "access": None, "token": visitor_token,
        "connection": visitor_connection, "chat_id": chat_b,
    }
    source_state = [
        {"artifact_id": "V095.body", "kind": "body", "uid": 1, "mail_id": receipt_a.mail_id,
         "document_id": evidence_a.document_id, "query_id": page_a.query_id,
         "evidence_id": evidence_a.evidence_id, "text": "PRIVATE-A17 私有正文：费用为120元。"},
        {"artifact_id": "V095.attachment", "kind": "attachment", "uid": 1,
         "mail_id": receipt_a.mail_id, "document_id": attachment_a["document_id"],
         "part_id": "1", "text": attachment_text_a, "sha256": attachment_a["sha256"]},
        {"artifact_id": "V107.body", "kind": "body", "uid": 1, "mail_id": receipt_b.mail_id,
         "document_id": evidence_b.document_id, "query_id": page_b.query_id,
         "evidence_id": evidence_b.evidence_id, "text": "PRIVATE-FLOW-X8 私有正文：会议时间为周五15:00。"},
        {"artifact_id": "V107.attachment", "kind": "attachment", "uid": 1,
         "mail_id": receipt_b.mail_id, "document_id": attachment_b["document_id"],
         "part_id": "1", "text": attachment_text_b, "sha256": attachment_b["sha256"]},
    ]
    refs = {
        "V095": {"evidence_id": evidence_a.evidence_id, "query_id": page_a.query_id,
                 "mail_id": receipt_a.mail_id, "document_id": evidence_a.document_id,
                 "cursor": f"{page_a.query_id}:0", "attachment_document_id": attachment_a["document_id"]},
        "V107": {"evidence_id": evidence_b.evidence_id, "query_id": page_b.query_id,
                 "mail_id": receipt_b.mail_id, "document_id": evidence_b.document_id,
                 "cursor": f"{page_b.query_id}:0", "attachment_document_id": attachment_b["document_id"]},
    }

    result = await probe_boundaries(
        service, web, channel, actor_a, actor_b, source_state, refs, tmp_path,
    )
    by_name = {}
    for boundary in result["boundaries"]:
        by_name.setdefault(boundary["name"], []).append(boundary)

    assert by_name["auth_B"][0]["denied"] is True
    assert by_name["search_B"][0]["denied"] is True
    assert by_name["cache_B"][0]["denied"] is True
    cache = by_name["cache_B"][0]["result"]
    assert cache["B_identity_rows_absent"] is True
    assert cache["A_original_query_preserved"] is True
    for case_cache in cache["cases"].values():
        assert case_cache["actor_session_keys"]["distinct"] is True
        assert case_cache["rows_unchanged_by_B_domain_attempts"] is True
        assert len(case_cache["owner_session_keys"]) == 1

    for name in ("read_evidence_B", "read_document_B", "read_mail_B", "expand_http_B",
                 "attachment_http_B", "history_http_B", "envelope_source_B"):
        assert by_name[name][0]["denied"] is True, name
    assert by_name["source_fetch_B"][0]["stage"] == "SourceFetcher._one.require_manage"
    for case_result in by_name["source_fetch_B"][0]["result"]["cases"].values():
        assert case_result["result"]["tested"] is True
        assert case_result["result"]["imap_constructor_calls"] == 0

    http_a = [item for item in result["visible"] if item["surface"] == "http" and item["actor"] == "A"]
    http_b = [item for item in result["visible"] if item["surface"] == "http" and item["actor"] == "B"]
    assert any(item["status"] == 200 and "PRIVATE-A17" in item.get("body_text", "") for item in http_a)
    assert any(item["status"] == 200 and "PRIVATE-FLOW-X8" in item.get("body_text", "") for item in http_a)
    downloads = [item for item in http_a if item["status"] == 206]
    assert len(downloads) == 2
    assert {item["body_sha256"] for item in downloads} == {
        __import__("hashlib").sha256(text.encode()).hexdigest()
        for text in (attachment_text_a, attachment_text_b)
    }
    assert all(item["headers"]["cache-control"] == "no-store" for item in downloads)
    assert http_b and all(item["status"] == 403 for item in http_b)
    assert all("PRIVATE-A17" not in item.get("body_text", "") for item in http_b)
    assert all("PRIVATE-FLOW-X8" not in item.get("body_text", "") for item in http_b)

    ws_a = [item for item in result["visible"] if item["surface"] == "websocket" and item["actor"] == "A"]
    ws_b = [item for item in result["visible"] if item["surface"] == "websocket" and item["actor"] == "B"]
    assert len(ws_a) == len(ws_b) == 2
    assert all(json.loads(item["body_text"])["status"] == "completed" for item in ws_a)
    assert all(json.loads(item["body_text"])["data"]["sources"] for item in ws_a)
    assert all(json.loads(item["body_text"])["status"] == "failed" for item in ws_b)
    assert all(json.loads(item["body_text"])["error"] == "unauthorized" for item in ws_b)
    assert all("PRIVATE-A17" not in item["body_text"] for item in ws_b)
    assert all("PRIVATE-FLOW-X8" not in item["body_text"] for item in ws_b)
    assert "actor_owner_read_A" in result["coverage"]
    assert "envelope_source_B" in result["coverage"]
