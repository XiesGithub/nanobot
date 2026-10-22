"""V066 full-task stream adapter: real AgentLoop, channel writes and UI replay."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest

CHAT_ID = "v066-owner-chat"
OWNER_ID = "v066-owner"
TASK_ID = "v066-task"
INPUT = "读取fact.txt告诉我值。"
EXPECTED_FIXTURE = "值：73"


class _FixedStreamProvider(LLMProvider):
    """D-only provider for a fixed tool request and fixed streamed response."""

    observation_mode = "scripted"
    supports_progress_deltas = True

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings(temperature=0, max_tokens=256)
        self.calls = 0

    def get_default_model(self) -> str:
        return "deepseek-v4-flash"

    async def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        tool_results = [str(message.get("content", "")) for message in messages
                        if message.get("role") == "tool"]
        if not tool_results:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("v066-read-1", "read_file", {"path": "fact.txt"})],
                finish_reason="tool_calls",
            )
        if not any("73" in result for result in tool_results):
            return LLMResponse(content="无法读取文件。")
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
        response = await self.chat(messages, model=model)
        if response.content and on_content_delta:
            await on_content_delta("结果为")
            await on_content_delta("73")
        return response


def fixed_stream_provider() -> LLMProvider:
    """Create a scripted provider for deterministic D evaluations and tests."""
    return _FixedStreamProvider()


class _CaptureConnection:
    """Capture actual WebSocketChannel JSON serialization without a network peer."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _event(emit: Any, kind: str, **fields: Any) -> None:
    fields = dict(fields)
    if "phase" in fields:
        fields["operation_phase"] = fields.pop("phase")
    if "task_id" in fields:
        fields["experiment_task_id"] = fields.pop("task_id")
    if hasattr(emit, "event"):
        emit.event(kind, **fields)
    elif callable(emit):
        emit(kind, **fields)


def _provider_contract(provider: LLMProvider, transport_requests: list[dict[str, Any]]) -> dict[str, Any]:
    mode = getattr(provider, "observation_mode", "unknown")
    native_override = type(provider).chat_stream is not LLMProvider.chat_stream
    sdk_stream = bool(transport_requests) and all(row.get("stream") is True for row in transport_requests)
    return {
        "class": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "model": provider.get_default_model(),
        "observation_mode": mode,
        "chat_stream_overrides_base_fallback": native_override,
        "sdk_transport_requests": transport_requests,
        "sdk_stream_true_for_every_request": sdk_stream,
        "native_stream_proven": mode != "scripted" and native_override and sdk_stream,
    }


def _observe_provider(provider: LLMProvider, emit: Any, stream_observations: list[dict[str, Any]],
                      transport_requests: list[dict[str, Any]],
                      model_requests: list[dict[str, Any]]):
    """Wrap only observation edges; delegate each real provider call unchanged."""
    original_stream = provider.chat_stream_with_retry
    transport_restores: list[tuple[Any, Any]] = []
    client = getattr(provider, "_client", None)
    for endpoint_name, endpoint in (
        ("chat.completions", getattr(getattr(client, "chat", None), "completions", None)),
        ("responses", getattr(client, "responses", None)),
    ):
        original_create = getattr(endpoint, "create", None)
        if not callable(original_create):
            continue

        async def observed_create(*args, _original=original_create, _endpoint=endpoint_name, **kwargs):
            row = {
                "endpoint": _endpoint,
                "model": kwargs.get("model"),
                "stream": kwargs.get("stream") is True,
            }
            transport_requests.append(row)
            _event(emit, "channel_stream_sdk_request", endpoint=row["endpoint"],
                   model=row["model"], stream=row["stream"],
                   observation_mode=getattr(provider, "observation_mode", "unknown"))
            result = _original(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        endpoint.create = observed_create
        transport_restores.append((endpoint, original_create))

    async def observed_stream(*args, **kwargs):
        original_callback = kwargs.get("on_content_delta")
        call_index = len(stream_observations) + 1
        deltas: list[dict[str, Any]] = []
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        tools = kwargs.get("tools")
        if tools is None and len(args) > 1:
            tools = args[1]
        serialized_messages = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
        )
        input_row = {
            "request_index": call_index,
            "model": kwargs.get("model"),
            "roles": [str(message.get("role", "")) for message in messages
                      if isinstance(message, dict)] if isinstance(messages, list) else [],
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "messages_sha256": _sha256(serialized_messages.encode("utf-8")),
            "tool_count": len(tools) if isinstance(tools, list) else 0,
        }
        model_requests.append(input_row)
        _event(emit, "channel_stream_model_request", **input_row)

        async def capture_delta(text: str) -> None:
            if isinstance(text, str) and text:
                row = {"seq": len(deltas) + 1, "text": text,
                       "text_sha256": _sha256(text.encode("utf-8"))}
                deltas.append(row)
                _event(emit, "channel_stream_delta", request_index=call_index,
                       delta_index=row["seq"], text=text, text_sha256=row["text_sha256"])
            if original_callback is not None:
                await original_callback(text)

        kwargs["on_content_delta"] = capture_delta
        try:
            response = await original_stream(*args, **kwargs)
        except BaseException as exc:
            stream_observations.append({
                "request_index": call_index,
                "delta_count": len(deltas),
                "error_kind": type(exc).__name__,
                "phase": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
            })
            _event(emit, "channel_stream_response", request_index=call_index,
                   phase=stream_observations[-1]["phase"], error_kind=type(exc).__name__)
            raise
        usage = response.usage if isinstance(response.usage, dict) else {}
        observation = {
            "request_index": call_index,
            "delta_count": len(deltas),
            "deltas": deltas,
            "finish_reason": response.finish_reason,
            "usage": {key: value for key, value in usage.items()
                      if key in {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"}
                      and type(value) is int and value >= 0},
            "usage_present": response.usage is not None,
        }
        stream_observations.append(observation)
        _event(emit, "channel_stream_response", request_index=call_index,
               phase="end", finish_reason=response.finish_reason,
               usage_present=observation["usage_present"], usage=observation["usage"])
        return response

    provider.chat_stream_with_retry = observed_stream  # type: ignore[method-assign]
    return original_stream, transport_restores


def _safe_runtime_file(runtime: Path, name: str) -> Path:
    runtime_root = runtime.resolve()
    target = (runtime_root / name).resolve()
    if not target.is_relative_to(runtime_root):
        raise ValueError("runtime_path_escape")
    return target


def _run_webui_replay(runtime: Path, *, task_id: str, owner_id: str, chat_id: str,
                      runner_final: str, wire_frames: list[dict[str, Any]], timeout_s: float = 60.0
                      ) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    webui = repo_root / "webui"
    node = shutil.which("node") or shutil.which("node.exe")
    vitest = webui / "node_modules" / "vitest" / "vitest.mjs"
    frontend_sources = {
        "use_nanobot_stream": webui / "src" / "hooks" / "useNanobotStream.ts",
        "nanobot_client": webui / "src" / "lib" / "nanobot-client.ts",
        "types": webui / "src" / "lib" / "types.ts",
        "client_provider": webui / "src" / "providers" / "ClientProvider.tsx",
        "eval_test": webui / "src" / "tests" / "channel-task-stream-eval.test.tsx",
    }
    source_hashes = {
        name: {
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": _sha256(path.read_bytes()),
        }
        for name, path in frontend_sources.items()
    }
    source_binding = json.dumps(source_hashes, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    if not node or not vitest.is_file():
        return {
            "status": "unavailable",
            "reason": "node_or_vitest_missing",
            "frontend_source_hashes": source_hashes,
            "frontend_source_binding_sha256": _sha256(source_binding),
        }

    input_path = _safe_runtime_file(runtime, "channel-task-stream-ui-input.json")
    evidence_path = _safe_runtime_file(runtime, "channel-task-stream-ui-observation.json")
    input_object = {
        "task_id": task_id,
        "owner_id": owner_id,
        "chat_id": chat_id,
        "runner_final": runner_final,
        "wire_frames": wire_frames,
    }
    input_text = json.dumps(input_object, ensure_ascii=False, separators=(",", ":"))
    input_path.write_bytes(input_text.encode("utf-8"))
    input_sha256 = _sha256(input_path.read_bytes())
    env = os.environ.copy()
    env["NANOBOT_CHANNEL_STREAM_UI_INPUT"] = str(input_path)
    env["NANOBOT_CHANNEL_STREAM_EVIDENCE"] = str(evidence_path)
    try:
        completed = subprocess.run(
            [node, str(vitest), "run", "src/tests/channel-task-stream-eval.test.tsx"],
            cwd=webui,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "reason": type(exc).__name__}

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    result = {
        "status": "passed" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "log_sha256": _sha256((stdout + stderr).encode("utf-8")),
        "input_path": input_path.relative_to(runtime).as_posix(),
        "input_text": input_text,
        "input_sha256": input_sha256,
        "input_object": input_object,
        "evidence_path": evidence_path.relative_to(runtime).as_posix(),
        "evidence_text": None,
        "evidence_sha256": None,
        "evidence": None,
        "frontend_source_hashes": source_hashes,
        "frontend_source_binding_sha256": _sha256(source_binding),
    }
    if completed.returncode != 0:
        result["diagnostic"] = (stdout + "\n" + stderr)[-3000:]
    if evidence_path.is_file():
        try:
            evidence_bytes = evidence_path.read_bytes()
            evidence_text = evidence_bytes.decode("utf-8")
            ui_evidence = json.loads(evidence_text)
            result["evidence_text"] = evidence_text
            result["evidence_sha256"] = _sha256(evidence_bytes)
            result["evidence"] = ui_evidence
            cases = ui_evidence.get("cases", [])
            captured = next((row for row in cases
                             if row.get("case") == "captured_worker_wire_frames"), None)
            result["captured_worker_render"] = captured
        except (OSError, ValueError, TypeError):
            result["evidence_parse_error"] = True
    return result


async def asyncrun_stream(runtime, workspace, provider, emit, control=None) -> dict[str, Any]:
    """Run V066 over the actual AgentLoop, AgentRunner, channel manager and WebSocket serializer.

    ``provider`` is an injected provider. Pass :func:`fixed_stream_provider` for D;
    R must pass the externally assembled native streaming provider.
    """
    from nanobot.agent.hook import AgentHook
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.bus.events import InboundMessage, OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.channels.websocket import WebSocketChannel
    from nanobot.config.schema import ToolsConfig
    from nanobot.session.manager import SessionManager
    from nanobot.utils.observability import point, span

    control = dict(control or {})
    runtime = Path(runtime).resolve()
    workspace = Path(workspace).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    fixture = workspace / "fact.txt"
    if fixture.exists():
        if fixture.read_text(encoding="utf-8") != EXPECTED_FIXTURE:
            raise ValueError("v066_fixture_mismatch")
    else:
        with fixture.open("x", encoding="utf-8", newline="") as stream:
            stream.write(EXPECTED_FIXTURE)
    fixture_sha256 = _sha256(fixture.read_bytes())

    class IsolatedLoop(AgentLoop):
        def _register_default_tools(self) -> None:
            # This profile exposes exactly the real read_file tool, with no shell or network tools.
            return

    class ToolBudgetHook(AgentHook):
        def __init__(self):
            super().__init__(reraise=True)
            self.requests: list[dict[str, Any]] = []

        async def before_execute_tools(self, context):
            for call in context.tool_calls:
                self.requests.append({"name": call.name, "call_id": call.id,
                                      "arguments_sha256": _sha256(json.dumps(
                                          call.arguments, sort_keys=True, ensure_ascii=False,
                                      ).encode("utf-8"))})
                _event(emit, "tool_request", name=call.name, call_id=call.id,
                       argument_sha256=self.requests[-1]["arguments_sha256"])
                if len(self.requests) > 8:
                    raise RuntimeError("v066_tool_request_limit")

    tool_hook = ToolBudgetHook()
    bus = MessageBus()
    sessions = SessionManager(runtime, allow_legacy=False)
    loop = IsolatedLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model=provider.get_default_model(),
        max_iterations=8,
        max_tool_result_chars=4096,
        hooks=[tool_hook],
        session_manager=sessions,
        tools_config=ToolsConfig(restrict_to_workspace=True),
        restrict_to_workspace=True,
        disabled_skills=["*"] if control.get("disable_skills", True) else [],
        unified_session=False,
    )
    read_tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    loop.tools.register(read_tool)

    tool_reads: list[dict[str, Any]] = []
    original_execute = read_tool.execute

    async def observe_execute(**arguments: Any) -> str:
        result = await original_execute(**arguments)
        path = Path(str(arguments.get("path", "")))
        target = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if not target.is_relative_to(workspace):
            raise ValueError("v066_read_path_escape")
        tool_reads.append({
            "name": "read_file",
            "call_id": next((request["call_id"] for request in tool_hook.requests
                             if request["name"] == "read_file"
                             and request["call_id"] not in {item["call_id"] for item in tool_reads}), None),
            "path": target.relative_to(workspace).as_posix(),
            "source_sha256": _sha256(target.read_bytes()) if target.is_file() else None,
            "result": result,
            "result_sha256": _sha256(result.encode("utf-8")),
            "result_contains_fixture_value": "73" in result,
        })
        point("channel_task_source_read", call_id=tool_reads[-1]["call_id"],
              path=tool_reads[-1]["path"],
              source_sha256=tool_reads[-1]["source_sha256"],
              result_sha256=tool_reads[-1]["result_sha256"])
        return result

    read_tool.execute = observe_execute  # type: ignore[method-assign]

    stream_observations: list[dict[str, Any]] = []
    transport_requests: list[dict[str, Any]] = []
    model_requests: list[dict[str, Any]] = []
    original_stream, transport_restores = _observe_provider(
        provider, emit, stream_observations, transport_requests, model_requests,
    )
    channel = WebSocketChannel({
        "enabled": True,
        "streaming": True,
        "websocket_requires_token": False,
        "send_progress": True,
        "send_tool_hints": True,
    }, bus)
    channel.send_progress = True
    channel.send_tool_hints = True
    connection = _CaptureConnection()
    channel._attach(connection, CHAT_ID)

    adapter_writes: list[dict[str, Any]] = []
    original_send = channel.send
    original_send_delta = channel.send_delta

    async def observe_send(message: OutboundMessage) -> None:
        adapter_writes.append({"operation": "send", "chat_id": message.chat_id,
                               "content_sha256": _sha256(message.content.encode("utf-8")),
                               "progress": bool(message.metadata.get("_progress")),
                               "streamed_final": bool(message.metadata.get("_streamed"))})
        await original_send(message)

    async def observe_send_delta(chat_id: str, delta: str, metadata=None) -> None:
        metadata = metadata or {}
        adapter_writes.append({"operation": "send_delta", "chat_id": chat_id,
                               "stream_id": metadata.get("_stream_id"),
                               "turn_id": metadata.get("_turn_id"),
                               "resuming": metadata.get("_resuming"),
                               "stream_end": bool(metadata.get("_stream_end")),
                               "delta": delta})
        await original_send_delta(chat_id, delta, metadata)

    channel.send = observe_send  # type: ignore[method-assign]
    channel.send_delta = observe_send_delta  # type: ignore[method-assign]

    manager = ChannelManager.__new__(ChannelManager)
    manager.bus = bus
    manager.channels = {"websocket": channel}
    manager.config = type("EvalConfig", (), {
        "channels": type("EvalChannels", (), {"send_max_retries": 1})(),
    })()
    manager.mail_service = None
    manager._origin_reply_fingerprints = {}
    bus_writes: list[dict[str, Any]] = []
    original_guarded = manager._send_guarded

    async def observe_guarded(target, message):
        metadata = message.metadata or {}
        tool_events = metadata.get("_tool_events") or []
        bus_writes.append({
            "chat_id": message.chat_id,
            "content": message.content,
            "progress": bool(metadata.get("_progress")),
            "tool_hint": bool(metadata.get("_tool_hint")),
            "stream_delta": bool(metadata.get("_stream_delta")),
            "stream_end": bool(metadata.get("_stream_end")),
            "stream_id": metadata.get("_stream_id"),
            "turn_id": metadata.get("_turn_id"),
            "resuming": metadata.get("_resuming"),
            "streamed_final": bool(metadata.get("_streamed")),
            "tool_event_phases": [event.get("phase") for event in tool_events],
            "tool_event_names": [event.get("name") for event in tool_events],
        })
        await original_guarded(target, message)

    manager._send_guarded = observe_guarded

    task_id = str(control.get("task_id") or TASK_ID)
    owner_id = str(control.get("owner_id") or OWNER_ID)
    chat_id = str(control.get("chat_id") or CHAT_ID)
    task_input = str(control.get("input") or INPUT)
    dispatch_task = asyncio.create_task(manager._dispatch_outbound())
    primary_error = None
    try:
        with span("channel_task_stream", experiment_task_id=task_id, owner_id=owner_id,
                  chat_id=chat_id, fixture_sha256=fixture_sha256):
            await loop._dispatch(InboundMessage(
                channel="websocket",
                sender_id=owner_id,
                chat_id=chat_id,
                content=task_input,
                metadata={"_wants_stream": True, "message_id": task_id},
            ))
        deadline = time.monotonic() + float(control.get("delivery_timeout_s", 10))
        while time.monotonic() < deadline:
            if any(frame.get("event") == "turn_end" for frame in connection.frames):
                break
            await asyncio.sleep(0.005)
        else:
            raise TimeoutError("channel_task_stream_delivery_timeout")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            if primary_error is None:
                primary_error = asyncio.CancelledError()
        try:
            await loop.close_mcp()
        finally:
            provider.chat_stream_with_retry = original_stream  # type: ignore[method-assign]
            for endpoint, original_create in transport_restores:
                endpoint.create = original_create

    final_messages = [row for row in bus_writes if row["streamed_final"]]
    if len(final_messages) != 1:
        runner_final = final_messages[-1]["content"] if final_messages else None
    else:
        runner_final = final_messages[0]["content"]
    deltas = [frame.get("text", "") for frame in connection.frames if frame.get("event") == "delta"]
    wire_delta_text = "".join(str(delta) for delta in deltas)
    from evals.channel_task_evidence import reconstruct_final_stream
    stream_contract = reconstruct_final_stream(connection.frames)
    assistant_full_messages = [
        frame for frame in connection.frames
        if frame.get("event") == "message" and frame.get("kind") not in {"tool_hint", "progress"}
    ]
    internal_json_on_wire = any(
        frame.get("event") == "message"
        and ("\"tool_call_id\"" in str(frame.get("text", ""))
             or "\"arguments\"" in str(frame.get("text", "")))
        for frame in connection.frames
    )
    provider_contract = _provider_contract(provider, transport_requests)
    ui_result = None
    if control.get("run_ui", True) and runner_final is not None:
        ui_result = _run_webui_replay(
            runtime,
            task_id=task_id,
            owner_id=owner_id,
            chat_id=chat_id,
            runner_final=str(runner_final),
            wire_frames=connection.frames,
        )
    observations = {
        "schema": "channel-task-stream/1.0",
        "task": {"task_id": task_id, "owner_id": owner_id, "chat_id": chat_id},
        "input": task_input,
        "fixture": {"path": "fact.txt", "text": EXPECTED_FIXTURE, "sha256": fixture_sha256},
        "provider": provider_contract,
        "native_stream": provider_contract["native_stream_proven"],
        "model_calls": len(model_requests),
        "model_inputs": model_requests,
        "model_requests": model_requests,
        "provider_stream_observations": stream_observations,
        "tool_requests": tool_hook.requests,
        "actual_reads": tool_reads,
        "tool_reads": tool_reads,
        "bus": bus_writes,
        "channel_manager_outbound": bus_writes,
        "writes": adapter_writes,
        "actual_writes": adapter_writes,
        "adapter_writes": adapter_writes,
        "wire_frames": connection.frames,
        "runner_final": runner_final,
        "render_contract": {
            "wire_delta_text": wire_delta_text,
            "delta_matches_runner_final": (runner_final is not None and stream_contract["valid"]
                                           and stream_contract["final_text"] == runner_final),
            "stream_segments": stream_contract,
            "assistant_full_wire_messages": assistant_full_messages,
            "internal_tool_json_on_wire": internal_json_on_wire,
            "turn_end_count": sum(frame.get("event") == "turn_end" for frame in connection.frames),
            "ui_replay": ui_result,
        },
    }
    _event(emit, "channel_task_stream_observation", mode=provider_contract["observation_mode"],
           fixture_sha256=fixture_sha256, runner_final_sha256=(
               _sha256(str(runner_final).encode("utf-8")) if runner_final is not None else None),
           tool_read_count=len(tool_reads), wire_frame_count=len(connection.frames),
           ui_status=ui_result.get("status") if ui_result else "not_run")
    return observations
