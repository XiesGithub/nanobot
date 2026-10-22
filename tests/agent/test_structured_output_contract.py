"""Exact-output instructions and untouched delivery, without model calls."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse


@pytest.mark.parametrize("channel", [None, "telegram", "whatsapp", "cli", "email", "feishu"])
def test_requested_format_overrides_channel_presentation(tmp_path, channel):
    builder = ContextBuilder(tmp_path)
    request = 'Return only JSON with fields "city" and "temperature".'
    messages = builder.build_messages([], request, channel=channel)
    system = messages[0]["content"]
    assert "Follow the user's requested output format and field restrictions" in system
    assert "without Markdown fences, introductions, or commentary" in system
    assert "unless the user explicitly requests a code block" in system
    assert "Channel formatting hints apply only when compatible with the requested format" in system
    assert messages[-1]["content"].endswith(request)
    assert all(value not in system for value in ("A17", "T900", "shipped", "V047"))


@pytest.mark.parametrize("user_text,reply", [
    ("Return only JSON with city and temperature.", '{"city":"Oslo","temperature":7}'),
    ("Return only JSON with city and temperature.", '```json\n{"city":"Oslo","temperature":7}\n```'),
    ("Show the JSON in a Markdown code block.", '```json\n{"city":"Oslo","temperature":7}\n```'),
])
async def test_runner_preserves_provider_output_without_format_repair(tmp_path, user_text, reply):
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content=reply, tool_calls=[]))
    messages = ContextBuilder(tmp_path).build_messages([], user_text)
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=messages, tools=ToolRegistry(), model="offline-control",
        workspace=tmp_path, max_iterations=2, max_tool_result_chars=4000,
    ))
    assert result.final_content == reply
    assert result.messages[-1]["content"] == reply
    provider.chat_with_retry.assert_awaited_once()
