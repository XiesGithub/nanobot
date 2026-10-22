import pytest

from nanobot.config.schema import Config, MailKnowledgeConfig
from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.factory import create_mail_service
from nanobot.knowledge.mail.store import MailStore


def config():
    value = Config()
    value.agents.defaults.model = "deepseek-v4-flash"
    return value


def test_never_enabled_is_inert_and_uses_explicit_instance_path(tmp_path):
    value = config()
    path = tmp_path / "instance-b" / "config.json"
    assert create_mail_service(value, config_path=path) is None
    assert not path.parent.exists()
    value.mail_knowledge.enabled = True
    service = create_mail_service(value, config_path=path)
    assert service.store.root == path.parent / "knowledge" / "mail"
    assert service.worker is None
    assert service.store.readonly
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_independent_query_cannot_initialize_or_write_knowledge(tmp_path):
    value = config()
    value.mail_knowledge.enabled = True
    value.mail_knowledge.semantic_enabled = False
    path = tmp_path / "config.json"
    reader = create_mail_service(value, config_path=path)
    with pytest.raises(MailError, match="index_unavailable"):
        await reader.start()
    writer = create_mail_service(value, config_path=path, role="management")
    await writer.start()
    assert writer.worker is None
    await reader.start()
    with pytest.raises(MailError, match="knowledge_readonly"):
        with reader.store.connect(write=True):
            pass
    await reader.close()
    await writer.close()


def test_disabled_existing_private_state_keeps_guard_and_rejects_unified_history(tmp_path):
    root = tmp_path / "knowledge" / "mail"
    MailStore(root, MailKnowledgeConfig()).initialize()
    value = config()
    assert not value.mail_knowledge.enabled
    assert create_mail_service(value, config_path=tmp_path / "config.json") is not None
    value.agents.defaults.unified_session = True
    with pytest.raises(MailError, match="unified_session_incompatible"):
        create_mail_service(value, config_path=tmp_path / "config.json")


def test_disabled_private_state_still_enforces_generation_model(tmp_path):
    MailStore(tmp_path / "knowledge" / "mail", MailKnowledgeConfig()).initialize()
    value = Config()
    value.agents.defaults.model = "another-model"
    with pytest.raises(MailError, match="model_not_allowed"):
        create_mail_service(value, config_path=tmp_path / "config.json")


@pytest.mark.asyncio
async def test_reader_explicit_management_queues_without_worker_or_changing_query_handle(tmp_path):
    from nanobot.config.schema import MailOwnerBinding
    from nanobot.knowledge.mail.contracts import AccessContext, MailQuery, ManageAction
    from tests.knowledge.mail.test_retrieval import ingest
    from tests.knowledge.mail.test_store import envelope

    value = config()
    value.mail_knowledge.enabled = True
    value.mail_knowledge.semantic_enabled = False
    value.mail_knowledge.owner_bindings = [MailOwnerBinding(binding_id="owner", principal_id="alice",
        channel="sdk", scopes={"read", "manage"})]
    path = tmp_path / "config.json"
    writer = create_mail_service(value, config_path=path, role="management")
    await writer.start()
    ingest(writer.store, envelope())
    await writer.close()
    reader = create_mail_service(value, config_path=path)
    await reader.start()
    access = AccessContext.from_security(reader.access.authenticate("sdk", "alice", private_chat=True), "sdk:private")
    try:
        page = await reader.search(access, MailQuery())
        receipt = await reader.manage(access, ManageAction(action="reindex", target_ids=["all"], request_id="offline-index"))
        assert receipt.status == "accepted_waiting_service"
        assert reader.worker is None and reader.store.readonly
        with pytest.raises(MailError, match="knowledge_readonly"):
            with reader.store.connect(write=True):
                pass
        with reader.store.connect() as db:
            assert db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] == "text-v1"
        removal = await reader.manage(access, ManageAction(action="prepare_remove", target_ids=[page.matches[0].mail_id], request_id="offline-remove"))
        assert removal.status == "needs_confirmation"
        confirmed = await reader.confirm(access, removal.confirmation_token)
        assert confirmed.status == "completed"
        assert not (await reader.search(access, MailQuery())).matches
    finally:
        await reader.close()
