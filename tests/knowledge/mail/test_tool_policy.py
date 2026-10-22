import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.tool_policy import MailToolView
from nanobot.providers.base import ToolCallRequest
from tests.knowledge.mail.test_access import make_access


class CounterTool(Tool):
    def __init__(self, name, counter, capability=None):
        self._name, self.counter = name, counter
        self.private_capability = capability

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "test tool"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    @property
    def read_only(self):
        return True  # Intentionally allow concurrent batching in tests.

    async def execute(self, **kwargs):
        self.counter[self.name] = self.counter.get(self.name, 0) + 1
        return "safe test result"


@pytest.mark.parametrize("names", [
    ["mail_search", "message"], ["message", "mail_search"], ["mail_read_source", "exec"],
    ["mail_knowledge_status", "spawn"], ["alias_private", "cron"],
])
@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.asyncio
async def test_complete_batch_preflight_has_zero_forbidden_side_effects(tmp_path, names, concurrent):
    _, auth, _, access = make_access(tmp_path)
    counters, saved = {}, []
    registry = ToolRegistry()
    for name in names:
        capability = "mail_read" if name.startswith("mail_") or name == "alias_private" else None
        registry.register(CounterTool(name, counters, capability))
    view = MailToolView(registry, auth, access)

    async def checkpoint(payload):
        saved.append(payload)

    spec = AgentRunSpec(initial_messages=[], tools=view, model="deepseek-v4-flash", max_iterations=2,
                        max_tool_result_chars=10000, concurrent_tools=concurrent, checkpoint_callback=checkpoint)
    runner = AgentRunner(None)
    calls = [ToolCallRequest(str(i), name, {}) for i, name in enumerate(names)]
    results, _, _ = await runner._execute_tools(spec, calls, {}, {})
    assert len(counters) == 1
    assert any("tool_blocked_by_mail_policy" in str(r) for r in results)
    assert saved[0]["tool_policy"]["restricted"]
    assert len(registry.get_definitions()) == 2  # Shared registry was not mutated.
    result, _, _ = await runner._run_tool(spec, ToolCallRequest("next", names[-1] if not names[-1].startswith("mail") else names[0], {}), {}, {})
    assert "tool_blocked_by_mail_policy" in str(result)


@pytest.mark.asyncio
async def test_failed_checkpoint_prevents_even_private_reads(tmp_path):
    _, auth, _, access = make_access(tmp_path)
    counter = {}
    registry = ToolRegistry()
    registry.register(CounterTool("mail_search", counter, "mail_read"))
    view = MailToolView(registry, auth, access)

    async def checkpoint(payload):
        raise OSError("simulated disk full")

    with pytest.raises(OSError):
        await view.preflight([ToolCallRequest("1", "mail_search", {})], checkpoint)
    assert counter == {}
    assert "tool_blocked" in await view.execute("mail_search", {})


@pytest.mark.asyncio
async def test_restored_private_turn_blocks_remaining_message_and_old_state(tmp_path):
    _, auth, _, access = make_access(tmp_path)
    registry, counter = ToolRegistry(), {}
    registry.register(CounterTool("message", counter))
    view = MailToolView(registry, auth, access, restored={"version": 1, "private_entered": True,
                                                       "binding_id": access.binding_id,
                                                       "policy_revision": access.policy_revision})
    assert "tool_blocked" in await view.execute("message", {})
    assert counter == {}
    with pytest.raises(MailError, match="checkpoint_reauthorization_required"):
        MailToolView(registry, auth, access, restored={})
    auth.revoke(access.binding_id)
    with pytest.raises(MailError, match="authorization_revoked"):
        MailToolView(registry, auth, access, restored={"version": 1, "private_entered": True})


@pytest.mark.asyncio
async def test_external_view_denies_all_unknown_and_private_calls(tmp_path):
    _, auth, _, _ = make_access(tmp_path)
    registry, counter = ToolRegistry(), {}
    for name in ["exec", "read_file", "grep", "glob", "my", "spawn", "cron", "message", "mcp_unknown"]:
        registry.register(CounterTool(name, counter))
    view = MailToolView(registry, auth)
    assert view.get_definitions() == []
    for name in registry.tool_names:
        assert "tool_blocked" in await view.execute(name, {})
    assert counter == {}
