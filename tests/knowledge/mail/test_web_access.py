import asyncio
import json
import socket

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from nanobot.knowledge.mail.contracts import AccessContext, MailError
from nanobot.knowledge.mail.service import MailService
from nanobot.knowledge.mail.web_api import private_envelope
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


class Connection:
    def __init__(self):
        self.sent = []
        self.remote_address = ("127.0.0.1", 1)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def channel(tmp_path):
    service = MailService(tmp_path / "mail", MailKnowledgeConfig(enabled=True, semantic_enabled=False))
    service.store.initialize()
    service.access.initialize_bindings([MailOwnerBinding(binding_id="web-owner", principal_id="webui-owner", channel="websocket", scopes={"read", "manage"})])
    MailTurnExtension(service)
    value = WebSocketChannel(WebSocketConfig(enabled=True, token_issue_secret="fictional-login-only",
        mail_owner_bootstrap=True), MessageBus(), mail_service=service)
    web = value._mail_access
    web.issue("owner-token", 300, owner_principal="webui-owner")
    web.issue("visitor-token", 300)
    owner, visitor = Connection(), Connection()
    web.bind(owner, "owner-token")
    web.bind(visitor, "visitor-token")
    return value, service, owner, visitor


def test_owner_bootstrap_requires_secret_and_binding(tmp_path):
    with pytest.raises(ValueError, match="requires a configured login secret"):
        WebSocketConfig(mail_owner_bootstrap=True)
    ch, service, owner, visitor = channel(tmp_path)
    assert ch._mail_access.security(owner).authenticated
    assert not ch._mail_access.security(visitor).authenticated
    with pytest.raises(MailError, match="unauthorized"):
        ch._mail_access.http_access(Request("/api/mail-knowledge/status?token=owner-token", Headers()))
    with pytest.raises(MailError, match="origin_denied"):
        ch._mail_access.http_access(Request("/api/mail-knowledge/status", Headers({
            "Authorization": "Bearer owner-token", "Host": "localhost:8765", "Origin": "https://attacker.test",
        })))


@pytest.mark.asyncio
async def test_guessed_chat_id_cannot_subscribe_or_receive_owner_answer(tmp_path):
    ch, service, owner, visitor = channel(tmp_path)
    ch._attach(owner, "private-chat")
    with pytest.raises(MailError, match="unauthorized"):
        ch._attach(visitor, "private-chat")
    security = ch._mail_access.security(owner)
    await ch.send(OutboundMessage("websocket", "private-chat", "private content", security=security))
    assert owner.sent[-1]["text"] == "private content"
    assert not visitor.sent
    service.access.revoke("web-owner")
    await ch.send(OutboundMessage("websocket", "private-chat", "must not arrive", security=security))
    assert len(owner.sent) == 1
    assert owner not in ch._mail_access.connections
    with pytest.raises(MailError, match="authorization_revoked"):
        ch._mail_access.bind(Connection(), "owner-token")


@pytest.mark.asyncio
async def test_mode_and_confirmation_request_ids_are_idempotent(tmp_path):
    ch, service, owner, visitor = channel(tmp_path)
    request = {"type": "mail_knowledge", "request_id": "mode-1", "action": "set_mode",
               "payload": {"archive_enabled": False, "trigger_agent": False, "expected_revision": 1}}
    await private_envelope(ch, owner, request)
    first = owner.sent[-1]
    await private_envelope(ch, owner, request)
    assert owner.sent[-1] == first and first["status"] == "applied"
    request["payload"]["trigger_agent"] = True
    await private_envelope(ch, owner, request)
    assert owner.sent[-1]["error"] == "request_id_conflict"
    receipt = ingest(service.store, envelope())
    prepare = {"type": "mail_knowledge", "request_id": "remove-1", "action": "prepare_remove", "payload": {"target_ids": [receipt.mail_id]}}
    await private_envelope(ch, owner, prepare)
    token = owner.sent[-1]["data"]["confirmation_token"]
    confirm = {"type": "mail_knowledge", "request_id": "confirm-1", "action": "confirm_remove", "payload": {"token": token}}
    await private_envelope(ch, owner, confirm)
    first = owner.sent[-1]
    await private_envelope(ch, owner, confirm)
    assert owner.sent[-1] == first and first["status"] == "completed"
    await private_envelope(ch, visitor, prepare)
    assert visitor.sent[-1]["error"] == "unauthorized"


def test_http_history_uses_private_binding_namespace(tmp_path):
    ch, service, owner, _ = channel(tmp_path)
    ch._attach(owner, "private-chat")
    security = ch._mail_access.security(owner)
    key = ch._mail_access.session_key(security, "private-chat")
    session = service.private_sessions.get_or_create(key)
    session.metadata.update(binding_id="web-owner", runtime_checkpoint={"secret": "not a public field"})
    session.add_message("assistant", "owner history", media=["C:/private/path"])
    service.private_sessions.save(session)
    access = AccessContext.from_security(security, key)
    assert ch._mail_access.session_list(access)["sessions"][0]["key"] == "websocket:private-chat"
    result = ch._mail_access.session(access, "private-chat")
    assert result["messages"][0]["content"] == "owner history"
    assert "C:/private" not in str(result) and "runtime_checkpoint" not in str(result)


@pytest.mark.asyncio
async def test_real_http_transport_reauthorizes_before_bytes_and_releases_gate(tmp_path):
    ch, service, _, _ = channel(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        ch.config.port = probe.getsockname()[1]
    task = asyncio.create_task(ch.start())
    for _ in range(100):
        if ch._server_task is not None:
            break
        await asyncio.sleep(.01)
    await asyncio.sleep(.05)
    base = f"http://127.0.0.1:{ch.config.port}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            bootstrap = await client.get(base + "/webui/bootstrap", headers={"Authorization": "Bearer fictional-login-only"})
            assert bootstrap.status_code == 200
            token = bootstrap.json()["token"]
            assert bootstrap.json()["mail_owner"]
            headers = {"Authorization": "Bearer " + token}
            response = await client.get(base + "/api/mail-knowledge/status", headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["Cache-Control"] == "no-store"
            inventory = await client.get(base + "/api/mail-knowledge/inventory?kind=actions", headers=headers)
            assert inventory.status_code == 200 and inventory.json()["items"] == []
            deliveries = await client.get(base + "/api/mail-knowledge/inventory?kind=deliveries", headers=headers)
            assert deliveries.status_code == 200 and deliveries.json()["items"] == []
            invalid = await client.get(base + "/api/mail-knowledge/inventory?kind=bindings", headers=headers)
            assert invalid.status_code == 400
            # Cross-thread revocation would block if the transport retained its lock.
            await asyncio.wait_for(asyncio.to_thread(service.access.revoke, "web-owner"), 2)
            response = await client.get(base + "/api/mail-knowledge/status", headers=headers)
            assert response.status_code == 403
    finally:
        await ch.stop()
        await task
