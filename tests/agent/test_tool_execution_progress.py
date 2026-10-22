"""Progress events follow the actual tool execution boundary."""

import asyncio

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.ask import AskUserTool
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.observability import observation_context


@tool_parameters({
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
})
class _RecordingTool(Tool):
    def __init__(self):
        self.calls = []

    @property
    def name(self):
        return "record"

    @property
    def description(self):
        return "record a value"

    async def execute(self, *, value):
        self.calls.append(value)
        return {"private_result": value}


class _BlockingTool(_RecordingTool):
    async def execute(self, *, value):
        self.calls.append(value)
        await asyncio.Event().wait()


def _spec(registry, callback=None):
    return AgentRunSpec(
        initial_messages=[],
        tools=registry,
        model="test",
        max_iterations=1,
        max_tool_result_chars=1000,
        tool_event_callback=callback,
    )


def _call(name="record", arguments=None, call_id="call-1"):
    return ToolCallRequest(id=call_id, name=name, arguments=arguments or {"value": "safe"})


async def _run(runner, spec, call):
    return await runner._run_tool(spec, call, {}, {})


@pytest.mark.asyncio
async def test_execution_events_have_real_boundary_and_minimal_payload():
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    events = []

    await _run(AgentRunner(None), _spec(registry, events.append), _call())

    assert tool.calls == ["safe"]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert [event["execution_started"] for event in events] == [True, True]
    assert all(event["call_id"] == "call-1" for event in events)
    assert all(event["name"] == "record" and event["version"] == 1 for event in events)
    assert all("arguments" not in event and "result" not in event for event in events)


@pytest.mark.asyncio
async def test_prepare_rejection_emits_rejected_without_counting_execution():
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    events = []

    await _run(
        AgentRunner(None),
        _spec(registry, events.append),
        _call(arguments={"unexpected": "private"}),
    )

    assert tool.calls == []
    assert [event["phase"] for event in events] == ["rejected"]
    assert events[0]["execution_started"] is False


@pytest.mark.asyncio
async def test_repeated_lookup_rejection_emits_no_execution_start(monkeypatch):
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    events = []
    monkeypatch.setattr(
        "nanobot.agent.runner.repeated_external_lookup_error",
        lambda *args: "repeated lookup blocked",
    )

    await _run(AgentRunner(None), _spec(registry, events.append), _call())

    assert tool.calls == []
    assert [event["phase"] for event in events] == ["rejected"]
    assert events[0]["execution_started"] is False


@pytest.mark.asyncio
async def test_ask_user_reports_waiting_after_real_execution_started():
    registry = ToolRegistry()
    registry.register(AskUserTool())
    events = []

    await _run(
        AgentRunner(None),
        _spec(registry, events.append),
        _call("ask_user", {"question": "Continue?"}),
    )

    assert [event["phase"] for event in events] == ["start", "waiting_confirmation"]
    assert all(event["execution_started"] is True for event in events)


@pytest.mark.asyncio
async def test_cancelled_execution_has_one_cancelled_terminal():
    registry = ToolRegistry()
    tool = _BlockingTool()
    registry.register(tool)
    events = []

    task = asyncio.create_task(_run(AgentRunner(None), _spec(registry, events.append), _call()))
    while not tool.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event["phase"] for event in events] == ["start", "cancelled"]
    assert all(event["execution_started"] is True for event in events)
    assert len([event for event in events if event["phase"] in {
        "end", "error", "cancelled", "waiting_confirmation",
    }]) == 1


@pytest.mark.asyncio
async def test_cancellation_during_start_delivery_does_not_count_or_run_tool():
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    events = []
    delivery_entered = asyncio.Event()
    observations = []

    async def callback(event):
        events.append(event)
        if event["phase"] == "start":
            delivery_entered.set()
            await asyncio.Event().wait()

    def sink(kind, **payload):
        observations.append((kind, payload))

    with observation_context(sink, "task-start-cancel"):
        task = asyncio.create_task(_run(AgentRunner(None), _spec(registry, callback), _call()))
        await delivery_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert tool.calls == []
    assert [event["phase"] for event in events] == ["start", "cancelled"]
    assert events[-1]["execution_started"] is False
    assert not any(
        kind == "tool_execution" and payload.get("stage") == "execution_started"
        for kind, payload in observations
    )


@pytest.mark.asyncio
async def test_callback_owned_cancelled_error_is_delivery_failure_only():
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    deliveries = []

    async def callback(_event):
        raise asyncio.CancelledError

    def sink(kind, **payload):
        if kind == "tool_progress_delivery":
            deliveries.append(payload)

    with observation_context(sink, "task-callback-cancel"):
        await _run(AgentRunner(None), _spec(registry, callback), _call())

    assert tool.calls == ["safe"]
    assert [item["delivery"] for item in deliveries] == ["error", "error"]
    assert all(item["error_kind"] == "CancelledError" for item in deliveries)


@pytest.mark.asyncio
async def test_terminal_delivery_cancellation_preserves_completed_execution():
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    events = []
    terminal_entered = asyncio.Event()
    observations = []

    async def callback(event):
        events.append(event)
        if event["phase"] == "end":
            terminal_entered.set()
            await asyncio.Event().wait()

    def sink(kind, **payload):
        observations.append((kind, payload))

    with observation_context(sink, "task-end-cancel"):
        task = asyncio.create_task(_run(AgentRunner(None), _spec(registry, callback), _call()))
        await terminal_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert tool.calls == ["safe"]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert any(
        kind == "tool" and payload.get("phase") == "end"
        for kind, payload in observations
    )
    assert any(
        kind == "tool_progress_delivery" and payload.get("delivery") == "cancelled"
        and payload.get("event_phase") == "end"
        for kind, payload in observations
    )


@pytest.mark.asyncio
async def test_callback_failure_is_observed_and_does_not_block_tool(caplog):
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    deliveries = []

    async def broken_callback(event):
        raise RuntimeError("progress transport unavailable")

    def sink(kind, **payload):
        if kind == "tool_progress_delivery":
            deliveries.append(payload)

    with observation_context(sink, "task-1"):
        await _run(AgentRunner(None), _spec(registry, broken_callback), _call())

    assert tool.calls == ["safe"]
    assert [item["delivery"] for item in deliveries] == ["error", "error"]
    assert [item["event_phase"] for item in deliveries] == ["start", "end"]


@pytest.mark.asyncio
async def test_absent_callback_is_explicit_in_observation():
    registry = ToolRegistry()
    registry.register(_RecordingTool())
    deliveries = []

    def sink(kind, **payload):
        if kind == "tool_progress_delivery":
            deliveries.append(payload)

    with observation_context(sink, "task-2"):
        await _run(AgentRunner(None), _spec(registry), _call())

    assert [item["delivery"] for item in deliveries] == ["missing", "missing"]
