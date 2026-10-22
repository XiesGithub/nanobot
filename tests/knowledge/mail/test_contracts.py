import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nanobot.bus.events import InboundMessage
from nanobot.config.paths import get_mail_knowledge_dir
from nanobot.config.schema import Config, MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.contracts import (
    AcceptReceipt,
    AccessContext,
    MailError,
    MailQuery,
    SourceIdentity,
    SourceRead,
)
from nanobot.knowledge.mail.environment import capabilities


def enabled_config(**mail):
    return Config.model_validate({
        "agents": {"defaults": {"model": "deepseek-v4-flash"}},
        "mailKnowledge": {"enabled": True, **mail},
    })


def test_defaults_are_inert(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config.json")
    assert not Config().mail_knowledge.enabled
    assert get_mail_knowledge_dir() == tmp_path / "knowledge" / "mail"
    assert not get_mail_knowledge_dir().exists()
    assert "sentence_transformers" not in sys.modules


def test_configuration_aliases_and_constraints():
    assert enabled_config().model_dump(by_alias=True)["mailKnowledge"]["archiveEnabled"] is False
    with pytest.raises(ValidationError, match="maxPendingBytes"):
        MailKnowledgeConfig(maxPendingBytes=1)
    with pytest.raises(ValidationError, match="consentGranted"):
        enabled_config(archiveEnabled=True)
    with pytest.raises(ValidationError, match="deepseek-v4-flash"):
        Config(mailKnowledge={"enabled": True})
    with pytest.raises(ValidationError, match="separate sessions"):
        Config(agents={"defaults": {"unifiedSession": True}}, mailKnowledge={"enabled": True})
    with pytest.raises(ValidationError):
        MailKnowledgeConfig(externalInteractionPolicy="unrestricted")


@pytest.mark.parametrize("field,value", [
    ("channel", "email"), ("principal_id", "*"), ("binding_id", ""),
    ("private_chat", False), ("scopes", ["manage"]),
])
def test_invalid_bindings_rejected(field, value):
    values = dict(binding_id="binding-1", principal_id="alice", channel="websocket")
    values[field] = value
    with pytest.raises(ValidationError):
        MailOwnerBinding(**values)


def test_metadata_cannot_supply_identity():
    message = InboundMessage("websocket", "alice", "chat", "query", metadata={
        "webui": True, "owner_id": "local-owner", "authenticated": True,
    })
    with pytest.raises(MailError, match="unauthorized"):
        AccessContext.from_security(message.security, message.session_key)


def test_source_namespaces_and_durability():
    identity = SourceIdentity(account_id=uuid4(), mailbox="INBOX", uidvalidity=1, uid=1)
    for update in ({"account_id": uuid4()}, {"mailbox": "Other"}, {"uidvalidity": 2}):
        assert identity.key != identity.model_copy(update=update).key
    assert identity.key == SourceIdentity.model_validate_json(identity.model_dump_json()).key
    with pytest.raises(ValidationError):
        AcceptReceipt(status="accepted", durable=False)
    with pytest.raises(ValidationError):
        AcceptReceipt(status="rejected", error_code="capacity_backpressure")


def test_query_bounds_and_scope_distinction():
    assert MailQuery().mail_ids is None
    assert MailQuery(mail_ids=[]).mail_ids == []
    with pytest.raises(ValidationError):
        MailQuery(limit=21)
    with pytest.raises(ValidationError):
        MailQuery(received_from="2026-01-01T00:00:00")
    with pytest.raises(ValidationError):
        SourceRead(evidence_id="ev", document_id="doc")
    with pytest.raises(ValidationError):
        MailQuery(owner_id="intruder")


def test_environment_reports_semantic_degradation():
    result = capabilities()
    assert result["fts5"]
    assert result["semantic_status"] == "semantic_unavailable"
