"""Provider retry notices stay internal while ordinary progress remains visible."""

from __future__ import annotations

import asyncio
import copy
import json
from collections import deque
from contextlib import suppress
from typing import Any

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.observability import observation_context


class ScriptedProvider(LLMProvider):
    """Exercise the real provider retry wrapper without a network request."""

    observation_mode = "scripted"

    def __init__(self, responses: list[LLMResponse], *, retry_delay: float = 0.001) -> None:
        super().__init__()
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []
        self._CHAT_RETRY_DELAYS = (retry_delay,)

    def get_default_model(self) -> str:
        return "deepseek-v4-flash"

    async def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls.append({
            "model": kwargs.get("model"),
            "request_index": len(self.calls) + 1,
            "messages_count": len(messages),
        })
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingChannel:
    send_progress = True
    send_tool_hints = False

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.deltas: list[tuple[str, str, dict[str, Any]]] = []

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None,
    ) -> None:
        self.deltas.append((chat_id, delta, dict(metadata or {})))


def _capture(events: list[dict[str, Any]]):
    def sink(kind: str, **event: Any) -> None:
        events.append(copy.deepcopy({"type": kind, **event}))

    return sink


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = {"model_request", "scripted_response", "retry_wait"}
    fields = (
        "type", "phase", "purpose", "cause", "attempt_index", "finish_reason",
        "error_status_code", "usage", "usage_status", "delay_seconds",
    )
    return {
        key: [
            {field: event[field] for field in fields if field in event}
            for event in events if event["type"] == key
        ]
        for key in sorted(kinds)
    }


def _make_loop(bus: MessageBus, provider: ScriptedProvider, tmp_path) -> AgentLoop:
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="deepseek-v4-flash")
    loop.tools.get_definitions = lambda: []
    loop.consolidator.maybe_consolidate_by_tokens = _no_consolidation
    return loop


async def _no_consolidation(*_args, **_kwargs) -> bool:
    return False


def _make_manager(bus: MessageBus) -> tuple[ChannelManager, RecordingChannel]:
    manager = ChannelManager(Config(), bus)
    channel = RecordingChannel()
    manager.channels["mock"] = channel  # type: ignore[assignment]
    return manager, channel


async def _stop_dispatcher(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("outbound dispatcher did not finish the expected deliveries")
        await asyncio.sleep(0.005)
    # Allow the dispatcher to consume any following stream-end/final markers.
    await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_retry_callback_marks_internal_progress_without_mutating_input_metadata(
    tmp_path, record_property,
) -> None:
    bus = MessageBus()
    provider = ScriptedProvider([])
    loop = _make_loop(bus, provider, tmp_path)
    original_metadata = {
        "origin_message_id": "source-7",
        "_progress": False,
        "_retry_wait": False,
        "custom": "preserved",
    }
    expected_metadata = copy.deepcopy(original_metadata)
    msg = InboundMessage(
        channel="mock", sender_id="u1", chat_id="chat-1", content="hello",
        metadata=original_metadata,
    )

    retry_callback = await loop._build_retry_wait_callback(msg)
    progress_callback = await loop._build_bus_progress_callback(msg)
    await retry_callback("Model request failed, retry in 1s (attempt 1).")
    await progress_callback("ordinary progress")
    retry_notice = await bus.consume_outbound()
    ordinary_progress = await bus.consume_outbound()

    assert retry_notice.metadata["_progress"] is True
    assert retry_notice.metadata["_retry_wait"] is True
    assert retry_notice.metadata["origin_message_id"] == "source-7"
    assert retry_notice.metadata["custom"] == "preserved"
    assert ordinary_progress.metadata["_progress"] is True
    assert ordinary_progress.metadata.get("_retry_wait") is not True
    assert msg.metadata == expected_metadata
    record_property("retry_callback_metadata", json.dumps({
        "retry": retry_notice.metadata,
        "ordinary_progress": ordinary_progress.metadata,
        "input_after_callbacks": msg.metadata,
    }, ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("streaming", [False, True], ids=["ordinary", "stream"])
@pytest.mark.asyncio
async def test_503_retry_is_internal_and_answer_is_delivered_once(
    tmp_path, record_property, streaming: bool,
) -> None:
    answer = "The answer is 42."
    provider = ScriptedProvider([
        LLMResponse(
            content="503 service unavailable", finish_reason="error", error_status_code=503,
            usage={"prompt_tokens": 9, "completion_tokens": 0},
        ),
        LLMResponse(
            content=answer, usage={"prompt_tokens": 13, "completion_tokens": 5},
        ),
    ])
    bus = MessageBus()
    loop = _make_loop(bus, provider, tmp_path)
    source_metadata: dict[str, Any] = {
        "origin_message_id": "source-503",
        "_progress": False,
        "_retry_wait": False,
    }
    if streaming:
        source_metadata["_wants_stream"] = True
    expected_metadata = copy.deepcopy(source_metadata)
    msg = InboundMessage(
        channel="mock", sender_id="u1", chat_id="chat-1", content="What is the answer?",
        metadata=source_metadata,
    )
    events: list[dict[str, Any]] = []

    with observation_context(_capture(events), "V001-provider_503_once-test"):
        await loop._dispatch(msg)
        # Use the production callback to prove ordinary progress still has its own subtype.
        progress_callback = await loop._build_bus_progress_callback(msg)
        await progress_callback("ordinary progress")
        if loop._background_tasks:
            await asyncio.gather(*loop._background_tasks)

    outbox: list[OutboundMessage] = []
    while bus.outbound_size:
        outbox.append(await bus.consume_outbound())
    retry_notices = [item for item in outbox if item.metadata.get("_retry_wait") is True]
    progress_messages = [
        item for item in outbox
        if item.metadata.get("_progress") is True and item.metadata.get("_retry_wait") is not True
    ]
    final_messages = [
        item for item in outbox
        if item.content == answer and not item.metadata.get("_stream_delta")
    ]

    assert len(provider.calls) == 2
    assert len(retry_notices) == 1
    assert retry_notices[0].metadata.get("_progress") is True
    assert len(progress_messages) == 1
    assert progress_messages[0].content == "ordinary progress"
    assert len(final_messages) == 1
    assert bool(final_messages[0].metadata.get("_streamed")) is streaming
    assert msg.metadata == expected_metadata

    attempts = [event for event in events if event["type"] == "scripted_response"]
    assert [event["phase"] for event in attempts if event["phase"] != "start"] == ["error", "end"]
    assert all(event.get("usage_status") == "known" for event in attempts if event["phase"] != "start")
    request_events = [event for event in events if event["type"] == "model_request"]
    assert len([event for event in request_events if event["phase"] == "start"]) == 1
    assert len([event for event in events if event["type"] == "retry_wait" and event["phase"] == "start"]) == 1

    for item in outbox:
        await bus.publish_outbound(item)
    manager, channel = _make_manager(bus)
    dispatcher = asyncio.create_task(manager._dispatch_outbound())
    try:
        await _wait_until(lambda: len(channel.sent) >= 2 if not streaming else (
            len(channel.sent) >= 1 and len(channel.deltas) >= 2
        ))
    finally:
        await _stop_dispatcher(dispatcher)

    assert "ordinary progress" in [item.content for item in channel.sent]
    delivered_retry_text = [
        item.content for item in channel.sent if item.metadata.get("_retry_wait")
    ]
    assert delivered_retry_text == []
    if streaming:
        streamed_answer = "".join(
            delta for _, delta, metadata in channel.deltas if metadata.get("_stream_delta")
        )
        assert streamed_answer == answer
        assert answer not in [item.content for item in channel.sent]
    else:
        assert [item.content for item in channel.sent].count(answer) == 1

    record_property("recovery_trace", json.dumps({
        "streaming": streaming,
        "provider_calls": provider.calls,
        "source_metadata_after_turn": msg.metadata,
        "outbox": [{"content": item.content, "metadata": item.metadata} for item in outbox],
        "channel_sent": [{"content": item.content, "metadata": item.metadata} for item in channel.sent],
        "channel_deltas": [
            {"chat_id": chat_id, "delta": delta, "metadata": metadata}
            for chat_id, delta, metadata in channel.deltas
        ],
        "events": _event_summary(events),
    }, ensure_ascii=False, sort_keys=True))


@pytest.mark.asyncio
async def test_permanent_400_has_no_retry_notice_or_success_reply(tmp_path, record_property) -> None:
    provider = ScriptedProvider([LLMResponse(
        content="400 invalid request", finish_reason="error", error_status_code=400,
        error_should_retry=False, usage={"prompt_tokens": 6, "completion_tokens": 0},
    )])
    bus = MessageBus()
    loop = _make_loop(bus, provider, tmp_path)
    msg = InboundMessage(
        channel="mock", sender_id="u1", chat_id="chat-1", content="run request",
        metadata={"origin_message_id": "source-400"},
    )
    events: list[dict[str, Any]] = []

    with observation_context(_capture(events), "V001-permanent-400-test"):
        await loop._dispatch(msg)
        if loop._background_tasks:
            await asyncio.gather(*loop._background_tasks)

    outbox: list[OutboundMessage] = []
    while bus.outbound_size:
        outbox.append(await bus.consume_outbound())
    assert len(provider.calls) == 1
    assert not any(item.metadata.get("_retry_wait") for item in outbox)
    assert [item.content for item in outbox] == [
        "Sorry, I encountered an error calling the AI model."
    ]
    assert not any(event["type"] == "retry_wait" for event in events)
    record_property("permanent_400_trace", json.dumps({
        "provider_calls": provider.calls,
        "outbox": [{"content": item.content, "metadata": item.metadata} for item in outbox],
        "events": _event_summary(events),
    }, ensure_ascii=False, sort_keys=True))


@pytest.mark.asyncio
async def test_cancelling_retry_wait_keeps_attempt_cost_and_suppresses_notice(
    tmp_path, record_property,
) -> None:
    provider = ScriptedProvider([
        LLMResponse(
            content="503 service unavailable", finish_reason="error", error_status_code=503,
            usage={"prompt_tokens": 8, "completion_tokens": 0},
        ),
        LLMResponse(content="must not be requested"),
    ], retry_delay=0.5)
    bus = MessageBus()
    loop = _make_loop(bus, provider, tmp_path)
    wait_started = asyncio.Event()
    original_builder = loop._build_retry_wait_callback

    async def build_signalling_callback(msg):
        callback = await original_builder(msg)

        async def signal_after_publish(content: str) -> None:
            await callback(content)
            wait_started.set()

        return signal_after_publish

    loop._build_retry_wait_callback = build_signalling_callback  # type: ignore[method-assign]
    manager, channel = _make_manager(bus)
    dispatcher = asyncio.create_task(manager._dispatch_outbound())
    msg = InboundMessage(
        channel="mock", sender_id="u1", chat_id="chat-1", content="cancel this request",
        metadata={"origin_message_id": "source-cancel"},
    )
    events: list[dict[str, Any]] = []

    with observation_context(_capture(events), "V001-cancelled-retry-test"):
        dispatch = asyncio.create_task(loop._dispatch(msg))
        try:
            await asyncio.wait_for(wait_started.wait(), timeout=2)
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
            await asyncio.sleep(0.02)
        finally:
            if not dispatch.done():
                dispatch.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch
            await _stop_dispatcher(dispatcher)

    assert len(provider.calls) == 1
    assert channel.sent == []
    assert channel.deltas == []
    retry_waits = [event for event in events if event["type"] == "retry_wait"]
    assert [event["phase"] for event in retry_waits] == ["start", "cancelled"]
    failed_attempts = [
        event for event in events
        if event["type"] == "scripted_response" and event["phase"] == "error"
    ]
    assert len(failed_attempts) == 1
    assert failed_attempts[0]["error_status_code"] == 503
    assert failed_attempts[0]["usage_status"] == "known"
    record_property("cancelled_retry_trace", json.dumps({
        "provider_calls": provider.calls,
        "channel_sent": [item.content for item in channel.sent],
        "channel_deltas": channel.deltas,
        "events": _event_summary(events),
    }, ensure_ascii=False, sort_keys=True))
