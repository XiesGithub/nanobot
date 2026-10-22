"""Length-recovery accumulation and turn-boundary regression tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _split_delta(text: str) -> list[str]:
    if len(text) < 2:
        return [text]
    midpoint = len(text) // 2
    return [text[:midpoint], text[midpoint:]]


class _StreamingHook(AgentHook):
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.stream_ends: list[bool] = []

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        self.deltas.append(delta)

    async def on_stream_end(
        self,
        context: AgentHookContext,
        *,
        resuming: bool,
    ) -> None:
        self.stream_ends.append(resuming)


_LENGTH_CASES = [
    pytest.param(
        "single", [("费用为", "length"), ("120元", "stop")], "费用为120元", 2,
        id="single-continuation-reproduces-v115",
    ),
    pytest.param(
        "multiple", [("ha ", "length"), ("ha ", "length"), ("ha!", "stop")], "ha ha ha!", 3,
        id="multiple-continuations-preserve-spaces-and-legitimate-repetition",
    ),
    pytest.param(
        "limit",
        [(f"片段{i} ", "length") for i in range(1, 5)],
        "片段1 片段2 片段3 片段4 ",
        4,
        id="three-recoveries-then-return-fourth-response",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["non-streaming", "streaming"])
@pytest.mark.parametrize("case_name,responses,expected,expected_calls", _LENGTH_CASES)
async def test_length_recovery_accumulates_once_in_return_and_history(
    case_name: str,
    responses: list[tuple[str, str]],
    expected: str,
    expected_calls: int,
    streaming: bool,
    record_property,
) -> None:
    """Returned text and one-copy assistant history match every response fragment."""
    call_count = 0
    emitted_deltas: list[str] = []

    async def next_response() -> LLMResponse:
        nonlocal call_count
        call_count += 1
        content, finish_reason = responses[call_count - 1]
        return LLMResponse(content=content, finish_reason=finish_reason, usage={})

    async def chat_with_retry(*, messages, **kwargs) -> LLMResponse:
        return await next_response()

    async def chat_stream_with_retry(*, messages, on_content_delta, **kwargs) -> LLMResponse:
        response = await next_response()
        for delta in _split_delta(response.content or ""):
            emitted_deltas.append(delta)
            await on_content_delta(delta)
        return response

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    provider.chat_stream_with_retry = chat_stream_with_retry
    hook = _StreamingHook() if streaming else None
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "write"}],
        tools=_tools(),
        model="test-model",
        max_iterations=20,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        hook=hook,
    ))
    assistant_history = [
        str(message["content"])
        for message in result.messages
        if message.get("role") == "assistant" and isinstance(message.get("content"), str)
    ]
    history_text = "".join(assistant_history)
    stream_ends = hook.stream_ends if hook else []

    record_property("v115_observation", json.dumps({
        "case": case_name,
        "streaming": streaming,
        "call_count": call_count,
        "delta_fragments": emitted_deltas,
        "final": result.final_content,
        "assistant_history": assistant_history,
        "stream_end_resuming": stream_ends,
    }, ensure_ascii=False))

    assert result.stop_reason == "completed"
    assert call_count == expected_calls
    assert result.final_content == expected
    assert history_text == expected
    assert assistant_history == [content for content, _ in responses]
    if streaming:
        assert "".join(emitted_deltas) == expected
        assert hook is not None
        assert hook.deltas == emitted_deltas
        assert hook.stream_ends == [True] * (expected_calls - 1) + [False]
    else:
        assert emitted_deltas == []
        assert stream_ends == []


@pytest.mark.asyncio
async def test_cancelled_continuation_does_not_create_a_final_answer() -> None:
    call_count = 0

    async def chat_with_retry(*, messages, **kwargs) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResponse(content="incomplete ", finish_reason="length", usage={})
        raise asyncio.CancelledError

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    with pytest.raises(asyncio.CancelledError):
        await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "write"}],
            tools=_tools(),
            model="test-model",
            max_iterations=10,
            max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        ))

    assert call_count == 2


@pytest.mark.asyncio
async def test_user_injection_starts_a_new_answer_without_length_prefix() -> None:
    responses = [
        LLMResponse(content="old prefix ", finish_reason="length", usage={}),
        LLMResponse(content="intermediate", finish_reason="stop", usage={}),
        LLMResponse(content="new answer", finish_reason="stop", usage={}),
    ]
    request_messages: list[list[dict]] = []
    injected = False

    async def chat_with_retry(*, messages, **kwargs) -> LLMResponse:
        request_messages.append([dict(message) for message in messages])
        return responses[len(request_messages) - 1]

    async def drain_injection() -> list[dict[str, str]]:
        nonlocal injected
        if injected:
            return []
        injected = True
        return [{"role": "user", "content": "follow-up question"}]

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "first question"}],
        tools=_tools(),
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        injection_callback=drain_injection,
    ))

    assert result.had_injections is True
    assert result.final_content == "new answer"
    assert request_messages[-1][-1] == {"role": "user", "content": "follow-up question"}
    assert [
        message["content"]
        for message in result.messages
        if message.get("role") == "assistant" and message.get("content")
    ] == ["old prefix ", "intermediate", "new answer"]


@pytest.mark.asyncio
async def test_tool_response_starts_a_new_answer_without_length_prefix() -> None:
    responses = [
        LLMResponse(content="old prefix ", finish_reason="length", usage={}),
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="call-1", name="lookup", arguments={})],
            finish_reason="tool_calls",
            usage={},
        ),
        LLMResponse(content="fresh answer", finish_reason="stop", usage={}),
    ]
    call_count = 0

    async def chat_with_retry(*, messages, **kwargs) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return responses[call_count - 1]

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = _tools()
    tools.prepare_call.return_value = (None, {}, None)
    tools.execute = AsyncMock(return_value="lookup complete")
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "first question"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
    ))

    assert result.final_content == "fresh answer"
    assert result.tools_used == ["lookup"]
    assert [
        message["content"]
        for message in result.messages
        if message.get("role") == "assistant" and message.get("content")
    ] == ["old prefix ", "fresh answer"]
    tools.execute.assert_awaited_once_with("lookup", {})


@pytest.mark.asyncio
async def test_length_prefix_is_not_added_to_safe_error_reply() -> None:
    responses = [
        LLMResponse(content="partial private answer", finish_reason="length", usage={}),
        LLMResponse(content="provider diagnostic", finish_reason="error", usage={}),
    ]
    call_count = 0

    async def chat_with_retry(*, messages, **kwargs) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        return responses[call_count - 1]

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "write"}],
        tools=_tools(),
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
    ))

    assert result.stop_reason == "error"
    assert result.final_content == "Sorry, I encountered an error calling the AI model."
    assert result.final_content not in "".join(
        str(message.get("content", ""))
        for message in result.messages
        if message.get("role") == "assistant"
    )
    assert "provider diagnostic" not in str(result.final_content)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["non-streaming", "streaming"])
async def test_process_message_delivers_and_persists_streamed_length_answer_once(
    tmp_path,
    record_property,
    streaming: bool,
) -> None:
    responses = [
        ("费用为", "length"),
        ("120 元", "stop"),
    ]
    call_count = 0
    emitted_deltas: list[str] = []
    stream_ends: list[bool] = []

    async def next_response() -> LLMResponse:
        nonlocal call_count
        call_count += 1
        content, finish_reason = responses[call_count - 1]
        return LLMResponse(content=content, finish_reason=finish_reason, usage={})

    async def chat_with_retry(**kwargs) -> LLMResponse:
        return await next_response()

    async def chat_stream_with_retry(*, on_content_delta, **kwargs) -> LLMResponse:
        response = await next_response()
        for delta in _split_delta(response.content or ""):
            emitted_deltas.append(delta)
            await on_content_delta(delta)
        return response

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = chat_with_retry
    provider.chat_stream_with_retry = chat_stream_with_retry
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    async def on_stream(delta: str) -> None:
        # _process_message's stream callback is the user-visible delta path.
        delivered_deltas.append(delta)

    async def on_stream_end(*, resuming: bool = False) -> None:
        stream_ends.append(resuming)

    delivered_deltas: list[str] = []
    expected = "费用为120 元"
    outbound = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="v115", content="询问费用"),
        on_stream=on_stream if streaming else None,
        on_stream_end=on_stream_end if streaming else None,
    )

    assert outbound is not None
    key = "cli:v115"
    loop.sessions.invalidate(key)
    reloaded = loop.sessions.get_or_create(key)
    assistant_history = [
        str(message["content"])
        for message in reloaded.messages
        if message.get("role") == "assistant" and isinstance(message.get("content"), str)
    ]
    history_text = "".join(assistant_history)

    record_property("v115_loop_observation", json.dumps({
        "streaming": streaming,
        "call_count": call_count,
        "delta_fragments": emitted_deltas,
        "delivered_deltas": delivered_deltas,
        "final": outbound.content,
        "assistant_history": assistant_history,
        "stream_end_resuming": stream_ends,
    }, ensure_ascii=False))

    assert call_count == 2
    assert outbound.content == expected
    assert history_text == expected
    assert assistant_history == [content for content, _ in responses]
    if streaming:
        assert "".join(emitted_deltas) == expected
        assert "".join(delivered_deltas) == expected
        assert stream_ends == [True, False]
    else:
        assert emitted_deltas == []
        assert delivered_deltas == []
        assert stream_ends == []
