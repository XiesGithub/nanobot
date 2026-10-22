"""Runner observations describe execution without changing agent decisions."""

import asyncio
import copy
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.utils.observability import observation_context, span


class Probe(Tool):
    name = "probe"
    description = "A deterministic observation probe."
    parameters = {"type": "object", "properties": {"value": {"type": "integer"}},
                  "required": ["value"]}
    concurrency_safe = True

    def __init__(self, action=None):
        self.action = action or AsyncMock(side_effect=lambda **kwargs: str(kwargs["value"]))

    async def execute(self, **kwargs):
        return await self.action(**kwargs)


def make_spec(**kwargs):
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "private task prompt"}],
        model="deepseek-v4-flash", max_iterations=3, max_tool_result_chars=1000,
        **kwargs,
    )


def capture(events):
    def sink(kind, **event):
        events.append(copy.deepcopy({"type": kind, **event}))
    return sink


def tool_terminals(events):
    return [e for e in events if e["type"] == "tool" and e["phase"] != "start"]


async def test_finalization_is_distinct_request_and_preserves_calls():
    async def run(observe):
        events, calls = [], []
        responses = iter([LLMResponse(content=""), LLMResponse(content=""),
                          LLMResponse(content="finished", usage={"prompt_tokens": 2,
                                                                 "completion_tokens": 1})])

        async def chat(**kwargs):
            calls.append(copy.deepcopy(kwargs))
            return next(responses)

        provider = MagicMock(chat_with_retry=chat)
        with observation_context(capture(events), "case-1") if observe else nullcontext():
            result = await AgentRunner(provider).run(make_spec(tools=ToolRegistry()))
        return result, calls, events

    plain, plain_calls, _ = await run(False)
    observed, calls, events = await run(True)
    assert observed == plain
    assert calls == plain_calls
    assert len(calls) == 3 and calls[-1]["tools"] is None
    requests = [e for e in events if e["type"] == "model_request"]
    assert [e["purpose"] for e in requests if e["phase"] == "start"] == [
        "iteration", "iteration", "empty_finalization",
    ]
    terminals = [e for e in requests if e["phase"] == "end"]
    assert [e["usage_status"] for e in terminals] == ["unknown", "unknown", "known"]
    assert "private task prompt" not in repr(events)
    assert len({e["span"] for e in requests}) == 3


async def test_real_provider_attempt_is_child_of_single_model_request():
    class OneShotProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def chat(self, **_kwargs):
            self.calls += 1
            return LLMResponse(content="finished")

        def get_default_model(self):
            return "deepseek-v4-flash"

    provider = OneShotProvider()
    events = []
    with observation_context(capture(events), "provider-parentage"):
        result = await AgentRunner(provider).run(make_spec(tools=ToolRegistry()))

    requests = [event for event in events if event["type"] == "model_request"]
    attempts = [event for event in events if event["type"] == "model_attempt"]
    assert result.final_content == "finished"
    assert provider.calls == 1
    assert len([event for event in requests if event["phase"] == "start"]) == 1
    assert len([event for event in attempts if event["phase"] == "start"]) == 1
    request = next(event for event in requests if event["phase"] == "start")
    attempt = next(event for event in attempts if event["phase"] == "start")
    terminal = next(event for event in attempts if event["span"] == attempt["span"]
                    and event["phase"] != "start")
    assert attempt["parent_span_id"] == request["span"]
    assert terminal["usage"] is None
    assert terminal["usage_status"] == "unknown"


@pytest.mark.parametrize("arguments", [{}, {"value": 4}])
async def test_tool_validation_and_direct_execute_are_counted_once(arguments):
    registry = ToolRegistry()
    tool = Probe()
    registry.register(tool)
    events = []
    with observation_context(capture(events), "case-2"):
        with span("task") as task:
            result = await AgentRunner(MagicMock())._execute_tools(
                make_spec(tools=registry), [ToolCallRequest("call-1", "probe", arguments)], {}, {},
            )
    starts = [e for e in events if e["type"] == "tool" and e["phase"] == "start"]
    terminals = tool_terminals(events)
    assert len(starts) == len(terminals) == 1
    terminal = terminals[0]
    assert terminal["parent_span_id"] == task.span_id
    assert terminal["span"] == starts[0]["span"]
    assert terminal["execution_started"] == bool(arguments)
    assert terminal["rejected"] == (not arguments)
    assert tool.action.await_count == int(bool(arguments))
    assert result[1][0]["status"] == ("ok" if arguments else "error")
    execution = [e for e in events if e["type"] == "tool_execution"]
    assert len(execution) == (1 if arguments else 0)
    assert all(e["parent_span_id"] == terminal["span"] for e in execution)


@pytest.mark.parametrize("cancel", [False, True])
async def test_preflight_failure_keeps_requested_tools(cancel):
    events = []

    class RejectedRegistry(ToolRegistry):
        async def preflight(self, calls, callback):
            assert len([e for e in events if e["type"] == "tool"]) == 2
            raise asyncio.CancelledError() if cancel else RuntimeError("preflight denied")

    provider = MagicMock(chat_with_retry=AsyncMock(return_value=LLMResponse(
        content=None, tool_calls=[ToolCallRequest(str(i), "probe", {"value": i}) for i in range(2)],
    )))
    with observation_context(capture(events), "case-3"):
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await AgentRunner(provider).run(make_spec(tools=RejectedRegistry()))
    terminals = tool_terminals(events)
    assert len(terminals) == 2
    assert all(e["phase"] == ("cancelled" if cancel else "error") for e in terminals)
    assert all(e["execution_started"] is False for e in terminals)
    assert not any(e["type"] == "tool_execution" for e in events)


@pytest.mark.parametrize("cancel", [False, True])
async def test_parallel_tools_have_independent_spans_and_terminal_events(cancel):
    events, entered = [], []
    both_entered = asyncio.Event()

    async def action(value):
        entered.append(value)
        if len(entered) == 2:
            both_entered.set()
        await both_entered.wait()
        if cancel:
            await asyncio.Event().wait()
        if value == 1:
            raise ValueError("tool exploded")
        return str(value)

    registry = ToolRegistry()
    registry.register(Probe(action))
    with observation_context(capture(events), "case-4"):
        with span("task") as task_span:
            task = asyncio.create_task(AgentRunner(MagicMock())._execute_tools(
                make_spec(tools=registry, concurrent_tools=True),
                [ToolCallRequest(str(i), "probe", {"value": i}) for i in range(2)], {}, {},
            ))
            await asyncio.wait_for(both_entered.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result[1][1]["status"] == "error"
    terminals = tool_terminals(events)
    assert len(terminals) == 2
    assert len({e["span"] for e in terminals}) == 2
    assert all(e["parent_span_id"] == task_span.span_id for e in terminals)
    assert all(e["execution_started"] for e in terminals)
    assert all(e["batch_id"] == 0 for e in terminals)
    assert sorted(e["phase"] for e in terminals) == (
        ["cancelled", "cancelled"] if cancel else ["end", "error"]
    )
    if not cancel:
        assert next(e for e in terminals if e["phase"] == "error")["error_kind"] == "ValueError"


@pytest.mark.parametrize("failure", ["exception", "cancel", "timeout"])
async def test_model_failures_close_request_and_keep_original_behavior(failure):
    async def chat(**kwargs):
        if failure == "exception":
            raise ValueError("provider failure")
        if failure == "cancel":
            raise asyncio.CancelledError()
        await asyncio.Event().wait()

    provider = MagicMock(chat_with_retry=chat)
    events = []
    with observation_context(capture(events), "case-5"):
        spec = make_spec(tools=ToolRegistry(), llm_timeout_s=0.01)
        if failure == "timeout":
            result = await AgentRunner(provider).run(spec)
            assert result.stop_reason == "error"
        else:
            with pytest.raises(ValueError if failure == "exception" else asyncio.CancelledError):
                await AgentRunner(provider).run(spec)
    requests = [e for e in events if e["type"] == "model_request"]
    assert len(requests) == 2
    assert requests[-1]["phase"] == ("cancelled" if failure == "cancel" else "error")


async def test_streaming_and_broken_collector_do_not_change_behavior():
    class StreamingHook(AgentHook):
        def wants_streaming(self):
            return True

        async def on_stream(self, context, delta):
            received.append(delta)

    received = []
    calls = []

    async def streaming(**kwargs):
        calls.append(kwargs)
        await kwargs["on_content_delta"]("done")
        return LLMResponse(content="done")

    def failing_sink(*args, **kwargs):
        raise ValueError("sink unavailable")

    provider = MagicMock(chat_stream_with_retry=streaming, chat_with_retry=AsyncMock())
    with observation_context(failing_sink, "case-6") as observation:
        result = await AgentRunner(provider).run(make_spec(tools=ToolRegistry(), hook=StreamingHook()))
    assert result.final_content == "done"
    assert received == ["done"]
    assert len(calls) == 1 and calls[0]["model"] == "deepseek-v4-flash"
    assert provider.chat_with_retry.await_count == 0
    assert observation.collector_errors


async def test_self_cancel_does_not_falsely_close_a_running_sibling():
    events = []
    sibling_started, release_sibling, sibling_finished = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )

    async def action(value):
        if value == 0:
            await sibling_started.wait()
            raise asyncio.CancelledError()
        sibling_started.set()
        await release_sibling.wait()
        sibling_finished.set()
        return "sibling survived"

    registry = ToolRegistry()
    registry.register(Probe(action))
    with observation_context(capture(events), "self-cancel"):
        with span("task"):
            with pytest.raises(asyncio.CancelledError):
                await AgentRunner(MagicMock())._execute_tools(
                    make_spec(tools=registry, concurrent_tools=True),
                    [ToolCallRequest(str(i), "probe", {"value": i}) for i in range(2)], {}, {},
                )
            assert [e["call_id"] for e in tool_terminals(events)] == ["0"]
            release_sibling.set()
            await asyncio.wait_for(sibling_finished.wait(), 2)
            # Let the sibling's wrapper record its result after execute returns.
            await asyncio.sleep(0)
    terminals = tool_terminals(events)
    assert [(e["call_id"], e["phase"]) for e in terminals] == [("0", "cancelled"), ("1", "end")]
    assert terminals[1]["status"] == "ok"


@pytest.mark.parametrize("concurrent", [False, True])
async def test_tool_observation_preserves_results_preflight_and_call_order(concurrent):
    async def run(observe):
        events, order = [], []

        class RecordingRegistry(ToolRegistry):
            async def preflight(self, calls, callback):
                order.append(("preflight", [call.id for call in calls]))

        async def action(value):
            order.append(("tool", value))
            return str(value)

        registry = RecordingRegistry()
        registry.register(Probe(action))
        responses = iter([
            LLMResponse(content="execute", tool_calls=[
                ToolCallRequest(str(i), "probe", {"value": i}) for i in range(2)
            ]),
            LLMResponse(content="done"),
        ])

        async def chat(**kwargs):
            order.append(("model", copy.deepcopy(kwargs)))
            return next(responses)

        with observation_context(capture(events), "parity") if observe else nullcontext():
            result = await AgentRunner(MagicMock(chat_with_retry=chat)).run(
                make_spec(tools=registry, concurrent_tools=concurrent),
            )
        return result, order, events

    plain, plain_order, _ = await run(False)
    observed, observed_order, events = await run(True)
    assert observed == plain
    assert observed_order == plain_order
    assert [entry[0] for entry in observed_order] == [
        "model", "preflight", "preflight", "tool", "tool", "model",
    ]
    assert len(tool_terminals(events)) == 2
