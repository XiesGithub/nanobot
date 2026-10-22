from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.channel_task_stream import asyncrun_stream, fixed_stream_provider
from evals.trace import analyze_trace
from evals.worker import Emitter as WorkerEmitter
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.channels.websocket import WebSocketChannel
from nanobot.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from nanobot.utils.observability import observation_context, span


class _FixedStreamProvider(LLMProvider):
    """Deterministic provider for the real AgentLoop/Runner stream path."""

    observation_mode = "scripted"
    supports_progress_deltas = True

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings(temperature=0, max_tokens=256)
        self.model_inputs: list[dict[str, Any]] = []
        self.stream_requests = 0

    def get_default_model(self) -> str:
        return "deepseek-v4-flash"

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        tool_results = [str(item.get("content", "")) for item in messages if item.get("role") == "tool"]
        self.model_inputs.append({
            "model": kwargs.get("model") or self.get_default_model(),
            "streaming": True,
            "roles": [str(item.get("role", "")) for item in messages],
            "input_sha256": hashlib.sha256(
                json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        })
        if not tool_results:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("read-fact", "read_file", {"path": "fact.txt"})],
                finish_reason="tool_calls",
            )
        assert any("73" in result for result in tool_results)
        return LLMResponse(content="结果为73")

    async def chat_stream(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=256,
        temperature=0,
        reasoning_effort=None,
        tool_choice=None,
        on_content_delta=None,
    ) -> LLMResponse:
        self.stream_requests += 1
        response = await self.chat(messages, tools=tools, model=model)
        if response.content and on_content_delta:
            await on_content_delta("结果为")
            await on_content_delta("73")
        return response


class _CaptureConnection:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


@pytest.mark.asyncio
async def test_actual_tool_preface_is_preserved_separately_from_final_segment(tmp_path):
    class ToolPrefaceProvider(_FixedStreamProvider):
        async def chat_stream(self, *args, on_content_delta=None, **kwargs):
            response = await super().chat_stream(*args, on_content_delta=on_content_delta, **kwargs)
            if response.tool_calls:
                response.content = "I'll read the file."
                if on_content_delta:
                    await on_content_delta(response.content)
            return response

    class Pipe:
        def send(self, message):
            pass

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("值：73", encoding="utf-8")
    emitter = WorkerEmitter(Pipe(), "preface-D", "preface-D")
    with observation_context(emitter.event, "preface-D"):
        result = await asyncrun_stream(tmp_path / "runtime", workspace, ToolPrefaceProvider(),
                                      emitter, control={"run_ui": False})
    contract = result["render_contract"]
    assert contract["wire_delta_text"] == "I'll read the file.结果为73"
    assert contract["delta_matches_runner_final"] is True
    assert contract["stream_segments"]["final_text"] == "结果为73"
    assert [row["resuming"] for row in contract["stream_segments"]["segments"]] == [True, False]
    callback = "".join(delta["text"] for request in result["provider_stream_observations"]
                       for delta in request["deltas"])
    assert callback == contract["wire_delta_text"]


@pytest.mark.asyncio
async def test_agent_runner_tool_progress_and_websocket_stream_are_bound(tmp_path: Path, record_property) -> None:
    fact = tmp_path / "fact.txt"
    fact.write_text("值：73", encoding="utf-8")

    bus = MessageBus()
    provider = _FixedStreamProvider()
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="deepseek-v4-flash",
        restrict_to_workspace=True,
    )
    read_observations: list[dict[str, Any]] = []
    read_tool = loop.tools.get("read_file")
    assert read_tool is not None
    original_execute = read_tool.execute

    async def observe_execute(**arguments: Any) -> str:
        result = await original_execute(**arguments)
        read_observations.append({
            "tool": "read_file",
            "path": arguments.get("path"),
            "result": result,
            "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
            "result_contains_value": "73" in result,
        })
        return result

    read_tool.execute = observe_execute  # type: ignore[method-assign]

    channel = WebSocketChannel(
        {"enabled": True, "streaming": True, "websocket_requires_token": False},
        bus,
    )
    channel.send_progress = True
    channel.send_tool_hints = True
    connection = _CaptureConnection()
    channel._attach(connection, "chat-owner-A")
    writes: list[dict[str, Any]] = []
    original_send = channel.send
    original_send_delta = channel.send_delta

    async def observe_send(message: OutboundMessage) -> None:
        writes.append({"operation": "send", "chat_id": message.chat_id, "content": message.content})
        await original_send(message)

    async def observe_send_delta(
        chat_id: str, delta: str, metadata: dict[str, Any] | None = None,
    ) -> None:
        writes.append({
            "operation": "send_delta",
            "chat_id": chat_id,
            "stream_id": (metadata or {}).get("_stream_id"),
            "stream_end": bool((metadata or {}).get("_stream_end")),
            "content": delta,
        })
        await original_send_delta(chat_id, delta, metadata)

    channel.send = observe_send  # type: ignore[method-assign]
    channel.send_delta = observe_send_delta  # type: ignore[method-assign]

    manager = ChannelManager.__new__(ChannelManager)
    manager.bus = bus
    manager.channels = {"websocket": channel}
    manager.config = SimpleNamespace(channels=SimpleNamespace(send_max_retries=1))
    manager.mail_service = None
    manager._origin_reply_fingerprints = {}
    bus_writes: list[dict[str, Any]] = []
    original_guarded = manager._send_guarded

    async def observe_guarded(target: WebSocketChannel, message: OutboundMessage) -> None:
        metadata = message.metadata or {}
        events = metadata.get("_tool_events") or []
        bus_writes.append({
            "chat_id": message.chat_id,
            "content": message.content,
            "progress": bool(metadata.get("_progress")),
            "tool_hint": bool(metadata.get("_tool_hint")),
            "stream_delta": bool(metadata.get("_stream_delta")),
            "stream_end": bool(metadata.get("_stream_end")),
            "stream_id": metadata.get("_stream_id"),
            "streamed_final": bool(metadata.get("_streamed")),
            "tool_event_phases": [event.get("phase") for event in events],
            "tool_event_names": [event.get("name") for event in events],
        })
        await original_guarded(target, message)

    manager._send_guarded = observe_guarded  # type: ignore[method-assign]
    dispatch_task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await loop._dispatch(InboundMessage(
            channel="websocket",
            sender_id="owner-A",
            chat_id="chat-owner-A",
            content="读取fact.txt告诉我值。",
            metadata={"_wants_stream": True, "message_id": "task-v066-A"},
        ))
        async with asyncio.timeout(3):
            while not any(frame.get("event") == "turn_end" for frame in connection.frames):
                await asyncio.sleep(0.005)
    finally:
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass
        await loop.close_mcp()

    final_message = next(row for row in bus_writes if row["streamed_final"])
    delta_text = "".join(
        frame.get("text", "") for frame in connection.frames if frame.get("event") == "delta"
    )
    assistant_wire_messages = [
        frame for frame in connection.frames
        if frame.get("event") == "message" and frame.get("kind") not in {"tool_hint", "progress"}
    ]
    tool_events = [phase for row in bus_writes for phase in row["tool_event_phases"]]
    wire_text = [str(frame.get("text", "")) for frame in connection.frames]
    observation = {
        "schema": "channel-task-stream-worker-test/1.0",
        "task": {"task_id": "task-v066-A", "owner_id": "owner-A", "chat_id": "chat-owner-A"},
        "provider": {"model": "deepseek-v4-flash", "observation_mode": "scripted", "stream_requests": provider.stream_requests},
        "model_inputs": provider.model_inputs,
        "tool_reads": read_observations,
        "bus_writes": bus_writes,
        "adapter_writes": writes,
        "wire_frames": connection.frames,
        "runner_final": final_message["content"],
        "render_contract": {
            "delta_text": delta_text,
            "matches_runner_final": delta_text == final_message["content"],
            "assistant_full_messages": assistant_wire_messages,
            "internal_tool_result_on_wire": any("值：73" in text for text in wire_text),
            "tool_phases": tool_events,
            "turn_end_count": sum(frame.get("event") == "turn_end" for frame in connection.frames),
        },
    }
    artifact = tmp_path / "channel-task-stream-worker-observation.json"
    artifact.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    record_property("channel_task_stream_observation", json.dumps(observation, ensure_ascii=False))
    record_property("channel_task_stream_artifact", str(artifact))

    assert provider.stream_requests == 2
    assert len(read_observations) == 1
    assert read_observations[0]["tool"] == "read_file"
    assert read_observations[0]["path"] == "fact.txt"
    assert read_observations[0]["result_contains_value"] is True
    assert len(read_observations[0]["result_sha256"]) == 64
    assert read_observations[0]["result_sha256"] != hashlib.sha256(
        "值：73".encode("utf-8")
    ).hexdigest()
    assert tool_events == ["start", "end"]
    assert final_message["content"] == "结果为73"
    assert delta_text == final_message["content"]
    assert not assistant_wire_messages
    assert not observation["render_contract"]["internal_tool_result_on_wire"]
    assert observation["render_contract"]["turn_end_count"] == 1
    assert any(frame.get("event") == "stream_end" for frame in connection.frames)


@pytest.mark.asyncio
async def test_channel_task_stream_adapter_returns_traceable_evidence(tmp_path: Path) -> None:
    class CapturePipe:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        def send(self, message: dict[str, Any]) -> None:
            self.messages.append(message)

    pipe = CapturePipe()
    emitter = WorkerEmitter(pipe, "v066-run", "v066-trial")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = workspace / "fact.txt"
    fixture.write_text("值：73", encoding="utf-8")
    fixture_stat = fixture.stat()
    fixture_bytes = fixture.read_bytes()

    with observation_context(emitter.event, "v066-trial") as observed:
        with span("task", evaluation="V066"):
            result = await asyncrun_stream(
                tmp_path / "runtime",
                workspace,
                fixed_stream_provider(),
                emitter,
                control={"run_ui": True},
            )

    events = [message["event"] for message in pipe.messages if message.get("message") == "event"]
    trace = analyze_trace(events, collector_errors=observed.collector_errors)

    assert result["runner_final"] == "结果为73"
    assert result["model_inputs"] == result["model_requests"]
    assert result["model_calls"] == len(result["model_inputs"])
    assert result["native_stream"] is result["provider"]["native_stream_proven"]
    assert result["bus"] == result["channel_manager_outbound"]
    assert result["writes"] == result["adapter_writes"]
    assert result["actual_writes"] == result["writes"]
    assert result["actual_reads"] == result["tool_reads"]
    assert len(result["tool_reads"]) == 1
    read = result["tool_reads"][0]
    assert read["name"] == "read_file"
    assert read["call_id"] == result["tool_requests"][0]["call_id"]
    assert read["path"] == "fact.txt"
    assert "1| 值：73" in read["result"]
    assert read["source_sha256"] == hashlib.sha256("值：73".encode("utf-8")).hexdigest()
    assert read["result_contains_fixture_value"] is True
    assert len(read["result_sha256"]) == 64
    assert read["result_sha256"] != read["source_sha256"]
    assert result["render_contract"]["wire_delta_text"] == "结果为73"
    assert result["render_contract"]["delta_matches_runner_final"] is True
    assert result["render_contract"]["internal_tool_json_on_wire"] is False
    assert result["render_contract"]["ui_replay"]["status"] == "passed"
    replay = result["render_contract"]["ui_replay"]["captured_worker_render"]
    assert replay["contract_pass"] is True
    assert replay["visible_answers"] == ["结果为73"]
    ui = result["render_contract"]["ui_replay"]
    assert ui["input_sha256"] == hashlib.sha256(ui["input_text"].encode("utf-8")).hexdigest()
    assert ui["evidence_sha256"] == hashlib.sha256(ui["evidence_text"].encode("utf-8")).hexdigest()
    assert len(ui["frontend_source_binding_sha256"]) == 64
    assert {row["path"] for row in ui["frontend_source_hashes"].values()} >= {
        "webui/src/hooks/useNanobotStream.ts",
        "webui/src/lib/nanobot-client.ts",
    }
    assert ui["evidence"]["cases"]
    assert ui["input_object"]["wire_frames"] == result["wire_frames"]
    replay_case = next(case for case in ui["evidence"]["cases"]
                       if case.get("case") == "captured_worker_wire_frames")
    assert replay_case["wire_frames"] == result["wire_frames"]
    assert result["provider"]["observation_mode"] == "scripted"
    assert result["provider"]["native_stream_proven"] is False
    assert len(result["model_requests"]) == 2
    assert result["model_requests"][0]["tool_count"] == 1
    assert "tool" in result["model_requests"][1]["roles"]
    assert all("messages_sha256" in row and "messages" not in row
               for row in result["model_requests"])
    assert result["provider_stream_observations"][-1]["deltas"] == [
        {"seq": 1, "text": "结果为", "text_sha256": hashlib.sha256("结果为".encode("utf-8")).hexdigest()},
        {"seq": 2, "text": "73", "text_sha256": hashlib.sha256("73".encode("utf-8")).hexdigest()},
    ]
    assert any(event["type"] == "channel_task_stream_observation" for event in events)
    assert fixture.read_bytes() == fixture_bytes
    assert fixture.stat().st_mtime_ns == fixture_stat.st_mtime_ns
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 0
    assert observed.collector_errors == []
    assert all(event["task_id"] == "v066-trial" for event in events)
    run_span = next(event for event in events
                    if event["type"] == "channel_task_stream" and event["phase"] == "start")
    assert run_span["payload"]["experiment_task_id"] == result["task"]["task_id"]
    assert all(event["phase"] == "point" for event in events
               if event["type"] == "channel_stream_response")
    assert all("operation_phase" in event["payload"] for event in events
               if event["type"] == "channel_stream_response")
