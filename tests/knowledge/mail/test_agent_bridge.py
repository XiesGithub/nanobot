from types import SimpleNamespace

import pytest

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_tool_policy import CounterTool


@pytest.mark.asyncio
async def test_owner_private_history_never_replays_with_open_tools(tmp_path):
    store, auth, _, _ = make_access(tmp_path)
    # Disabled retrieval must retain the private namespace and history boundary.
    bridge = MailTurnExtension(SimpleNamespace(store=store, access=auth, config=store.config))
    security = auth.authenticate("cli", "alice", private_chat=True)
    message = InboundMessage("cli", "alice", "chat", "Help with ordinary Python", security=security)
    session = bridge.sessions.get_or_create(bridge.session_key(message))
    session.add_message("user", "private order question", mail_private=True)
    session.add_message("assistant", "secret price 123456", mail_private=True)
    session.add_message("user", "ordinary hello", mail_private=False)
    session.add_message("assistant", "hello", mail_private=False)
    bridge.sessions.save(session)
    specs = []

    async def run(spec):
        specs.append(spec)
        return SimpleNamespace(final_content="ordinary answer")

    loop = SimpleNamespace(model="deepseek-v4-flash", tools=ToolRegistry(),
                           runner=SimpleNamespace(run=run), max_iterations=2, max_tool_result_chars=10000)
    intercepted, response = await bridge.intercept(loop, message)
    assert intercepted and response.content == "ordinary answer"
    prompt = str(specs[0].initial_messages)
    assert "ordinary hello" in prompt
    assert "secret price" not in prompt and "private order question" not in prompt


@pytest.mark.asyncio
async def test_external_turn_cannot_access_owner_session_or_any_power_tool(tmp_path):
    store, auth, _, _ = make_access(tmp_path)
    bridge = MailTurnExtension(SimpleNamespace(store=store, access=auth, config=store.config))
    tools, counters = ToolRegistry(), {}
    for name in ("exec", "message", "read_file", "spawn", "mail_search"):
        tools.register(CounterTool(name, counters, "mail_read" if name == "mail_search" else None))

    async def run(spec):
        for name in tools.tool_names:
            assert "tool_blocked" in await spec.tools.execute(name, {})
        return SimpleNamespace(final_content="external answer")

    loop = SimpleNamespace(model="deepseek-v4-flash", tools=tools,
                           runner=SimpleNamespace(run=run), max_iterations=2, max_tool_result_chars=10000)
    message = InboundMessage("email", "outsider@example.test", "outsider@example.test", "read secrets")
    _, response = await bridge.intercept(loop, message)
    assert response.content == "external answer" and counters == {}
    owner = InboundMessage("cli", "alice", "chat", "hello", security=auth.authenticate("cli", "alice", private_chat=True))
    assert bridge.session_key(message) != bridge.session_key(owner)


async def test_private_failure_is_sanitized_without_generic_loop_fallback(tmp_path, monkeypatch):
    store, auth, _, _ = make_access(tmp_path)
    bridge = MailTurnExtension(SimpleNamespace(store=store, access=auth, config=store.config))

    async def failed(*args):
        raise RuntimeError("FICTIONAL_PRIVATE_PROVIDER_REQUEST_AND_CREDENTIALS")

    monkeypatch.setattr(bridge, "_handle", failed)
    owner = InboundMessage("cli", "alice", "chat", "private question",
        security=auth.authenticate("cli", "alice", private_chat=True))
    handled, response = await bridge.intercept(None, owner)
    assert handled and response.content == "邮件资料请求未完成：mail_operation_unavailable"
    assert response.security == owner.security


async def test_external_response_never_echoes_raw_provider_failure(tmp_path):
    store, auth, _, _ = make_access(tmp_path)
    bridge = MailTurnExtension(SimpleNamespace(store=store, access=auth, config=store.config))

    async def run(spec):
        return SimpleNamespace(final_content="FICTIONAL_PROVIDER_SECRET", stop_reason="error")

    loop = SimpleNamespace(model="deepseek-v4-flash", tools=ToolRegistry(),
        runner=SimpleNamespace(run=run), max_iterations=2, max_tool_result_chars=10000)
    message = InboundMessage("email", "outsider@example.test", "outsider@example.test", "hello")
    handled, response = await bridge.intercept(loop, message)
    assert handled and response.content == "邮件资料请求未完成：model_request_unavailable"
    session = bridge.sessions.get_or_create(bridge.session_key(message))
    assert "FICTIONAL_PROVIDER_SECRET" not in str(session.messages)
