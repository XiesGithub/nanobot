import json

import pytest

from nanobot.knowledge.mail.chat_management import manage_chat
from nanobot.knowledge.mail.contracts import MailError, MailQuery
from nanobot.knowledge.mail.routing import MailRouter, ManagementPlan, PlannedQuery, QueryPlan
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def setup(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    service = MailService(store.root, store.config, readonly=True)
    return store, service, access, MailRouter(service, None)


@pytest.mark.asyncio
async def test_natural_range_prepares_exact_complete_scope_without_model_confirmation(tmp_path):
    store, service, access, router = setup(tmp_path)
    selected = ingest(store, envelope())
    other = envelope()
    other.sender_address = "other@example.test"
    ingest(store, other)
    plan = QueryPlan(intent="mail_manage", management=ManagementPlan(action="prepare_remove",
        selection=PlannedQuery(sender_addresses=["hr@example.test"], mode="enumerate")))
    text = await manage_chat(service, router, access, "移除 hr@example.test 的邮件", plan)
    preview = json.loads(text.split("\n", 1)[0])
    assert preview["target_ids"] == [selected.mail_id]
    assert preview["status"] == "needs_confirmation"
    assert "/mail-confirm " in text and "服务器邮件不变" in text
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items WHERE deleted_at IS NULL").fetchone()[0] == 2
    await service.confirm(access, preview["confirmation_token"])
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items WHERE deleted_at IS NULL").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_ambiguous_semantic_scope_and_invented_ids_never_mutate(tmp_path):
    store, service, access, router = setup(tmp_path)
    receipt = ingest(store, envelope())
    plan = QueryPlan(intent="mail_manage", management=ManagementPlan(action="prepare_remove",
        selection=PlannedQuery(query="没用的邮件")))
    assert "限定管理范围" in await manage_chat(service, router, access, "删掉没用的邮件", plan)
    plan.management = ManagementPlan(action="prepare_remove", target_ids=[receipt.mail_id])
    with pytest.raises(MailError, match="target_selection_required"):
        await manage_chat(service, router, access, "删掉邮件", plan)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM management_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_focus_cannot_turn_multi_mail_query_into_implicit_bulk_removal(tmp_path):
    store, service, access, router = setup(tmp_path)
    ingest(store, envelope())
    ingest(store, envelope())
    await service.search(access, MailQuery())
    plan = QueryPlan(intent="mail_manage", management=ManagementPlan(action="prepare_remove", use_focus=True))
    assert "多封邮件" in await manage_chat(service, router, access, "移除这封", plan)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM management_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_mode_change_uses_explicit_writer_without_starting_query_worker(tmp_path):
    store, service, access, router = setup(tmp_path)
    plan = QueryPlan(intent="mail_manage", management=ManagementPlan(action="set_mode", trigger_agent=False))
    result = json.loads(await manage_chat(service, router, access, "关闭来信交互", plan))
    assert result["status"] == "applied"
    assert service.store.readonly and service.worker is None and not service._online
    with store.connect() as db:
        assert db.execute("SELECT archive_enabled,trigger_agent FROM schema_meta").fetchone()[1] == 0


@pytest.mark.asyncio
async def test_router_followup_preserves_filters_not_explicitly_overridden(tmp_path):
    store, service, access, router = setup(tmp_path)
    selected = ingest(store, envelope())
    other = envelope()
    other.sender_address = "other@example.test"
    ingest(store, other)
    await service.search(access, MailQuery(sender_addresses=["hr@example.test"]))
    plan = QueryPlan(intent="mail_fact", queries=[PlannedQuery(query="地点", scope_action="inherit")])
    query = router.queries(plan)[0]
    assert "sender_addresses" not in query.model_fields_set
    page = await service.search(access, query)
    assert page.applied_filters["sender_addresses"] == ["hr@example.test"]
    assert page.applied_filters["mail_ids"] == [selected.mail_id]
