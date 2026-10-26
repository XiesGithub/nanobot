"""V065 isolated D control: real loopback upload, dispatch, and chat ownership checks."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import copy
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

VERSION = "V065-attachment-D/3.0"
SCHEMA = "V065-attachment-D-evidence/1"
MODEL = "deepseek-v4-flash"
CONTRACT_SHA256 = "c973607f5ace357b25166c4ec116e42816003d460f527fa492aa9519979bf7fb"
CONTRACT_REL = Path("docs/agent-evaluation-v065-contract-20261026-r1/contract.json")
CASE_REL = Path("docs/validation-cases/08-transport.md")
NOTICE = "会议日期2026-10-12"
QUESTION_A = "我上传文件中的会议日期是什么？"
QUESTION_B = "我刚上传文件的会议日期是什么？"
DATE = "2026-10-12"
FINAL_A = "文件中的会议日期是 2026-10-12。"
FINAL_B = "当前会话没有收到附件，因此无法确定会议日期。"

_ROOT = Path(__file__).parents[1]
_CURRENT_ACTOR: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "v065_current_actor", default=None
)
_OPEN_WATCH: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "v065_open_watch", default=None
)
_AUDIT_HOOK_INSTALLED = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(value: str) -> str:
    return _sha256(value.encode("utf-8"))


def _normalized_path(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _install_open_audit_hook() -> None:
    """Observe only matching open events; never block or alter file operations."""
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return

    def observe(event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or not args:
            return
        state = _OPEN_WATCH.get()
        path = args[0]
        if state is None or not isinstance(path, (str, bytes, os.PathLike)):
            return
        try:
            normalized = _normalized_path(os.fsdecode(path))
        except (OSError, TypeError, ValueError):
            return
        if normalized in state["targets"]:
            state["events"].append({"actor": state["actor"], "path": normalized})

    sys.addaudithook(observe)
    _AUDIT_HOOK_INSTALLED = True


@contextmanager
def _watch_opens(actor: str, targets: list[Path]):
    state = {
        "actor": actor,
        "targets": {_normalized_path(path) for path in targets},
        "events": [],
    }
    token = _OPEN_WATCH.set(state)
    try:
        yield state
    finally:
        _OPEN_WATCH.reset(token)


@contextmanager
def _actor_context(**fields: Any):
    token = _CURRENT_ACTOR.set(fields)
    try:
        yield
    finally:
        _CURRENT_ACTOR.reset(token)


def _audit_result_text(result: Any) -> str | None:
    return result if isinstance(result, str) else None


def _record_read(evidence: dict[str, Any], path: str, result: Any) -> None:
    current = _CURRENT_ACTOR.get() or {}
    text = _audit_result_text(result)
    lower = text.lower() if text else ""
    if text is not None and text.startswith("Error:") and (
        "outside the current chat's allowed media directory" in lower
        or "outside allowed directory" in lower
    ):
        status = "denied"
    elif text is not None and text.startswith("Error:"):
        status = "error"
    else:
        status = "ok"
    evidence["reads"].append({
        "actor": current.get("actor"),
        "chat_id": current.get("chat_id"),
        "session_key": current.get("session_key"),
        "request_id": current.get("request_id"),
        "kind": current.get("kind"),
        "requested_path": path,
        "status": status,
        "return_text": text,
        "return_text_sha256": _text_sha256(text) if text is not None else None,
        "contains_notice": bool(text and NOTICE in text),
    })


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _redact_frame(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "chat_token":
                raw = item if isinstance(item, str) else ""
                result[key] = {
                    "present": bool(raw),
                    "sha256": _text_sha256(raw) if raw else None,
                }
            else:
                result[key] = _redact_frame(item)
        return result
    if isinstance(value, list):
        return [_redact_frame(item) for item in value]
    return value


def _flatten_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten_content(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        return "\n".join(_flatten_content(item) for item in value.values())
    return ""


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _flatten_content(message.get("content"))
    return ""


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(_flatten_content(message.get("content")) for message in messages)


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    return [
        _flatten_content(message.get("content"))
        for message in messages
        if message.get("role") == "tool" and message.get("name") == "read_file"
    ]


def _contract_and_sources() -> dict[str, Any]:
    root = _ROOT.resolve()
    contract_path = root / CONTRACT_REL
    case_path = root / CASE_REL
    contract_bytes = contract_path.read_bytes()
    case_bytes = case_path.read_bytes()
    contract = json.loads(contract_bytes.decode("utf-8"))
    fixture = contract["fixture"]
    questions = contract["questions"]
    if (
        contract.get("version") != VERSION
        or contract.get("allowed_modes") != ["D"]
        or fixture.get("name") != "notice.txt"
        or fixture.get("mime") != "text/plain"
        or fixture.get("text") != NOTICE
        or questions.get("A") != QUESTION_A
        or questions.get("B") != QUESTION_B
    ):
        raise ValueError("contract_mismatch")
    if _sha256(contract_bytes) != CONTRACT_SHA256:
        raise ValueError("frozen_contract_hash_mismatch")
    driver_path = Path(__file__).resolve()
    return {
        "contract_path": CONTRACT_REL.as_posix(),
        "contract_sha256": _sha256(contract_bytes),
        "original_case_path": CASE_REL.as_posix(),
        "original_case_sha256": _sha256(case_bytes),
        "driver_path": driver_path.relative_to(root).as_posix(),
        "driver_sha256": _sha256(driver_path.read_bytes()),
    }


def _snapshot_files(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            result[path.relative_to(root).as_posix()] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return result


class _GroundingError(RuntimeError):
    pass


def _make_grounded_provider(evidence: dict[str, Any], saved: Path):
    """Create the scripted provider only after run() has accepted D mode."""
    from nanobot.providers.base import (
        GenerationSettings,
        LLMProvider,
        LLMResponse,
        ToolCallRequest,
    )

    class GroundedScriptedProvider(LLMProvider):
        """A scripted provider whose every response depends on actual loop input."""

        observation_mode = "scripted"

        def __init__(self):
            super().__init__()
            self.generation = GenerationSettings(temperature=0, max_tokens=512)
            self.evidence = evidence
            self.saved = saved
            self.call_count = 0
            self.closed = False

        def get_default_model(self) -> str:
            return MODEL

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any):
            current = _CURRENT_ACTOR.get() or {}
            actor = current.get("actor")
            model = kwargs.get("model")
            self.call_count += 1
            record: dict[str, Any] = {
                "index": self.call_count,
                "actor": actor,
                "chat_id": current.get("chat_id"),
                "session_key": current.get("session_key"),
                "request_id": current.get("request_id"),
                "model": model,
                "messages": _json_safe(copy.deepcopy(messages)),
            }
            self.evidence["provider"]["calls"].append(record)
            self.evidence["provider"]["chat_calls"] = self.call_count
            if model != MODEL:
                raise _GroundingError("unexpected_model")

            whole = _all_message_text(messages)
            latest = _latest_user_text(messages)
            if actor == "A":
                if self.call_count == 1:
                    if QUESTION_A not in latest or NOTICE not in whole:
                        raise _GroundingError("A_input_not_grounded_in_uploaded_text")
                    tool_calls = [ToolCallRequest(
                        id="v065-a-read-1",
                        name="read_file",
                        arguments={"path": str(self.saved)},
                    )]
                    response = LLMResponse(content=None, tool_calls=tool_calls, finish_reason="tool_calls")
                    record["response"] = {"content": None, "tool_calls": [
                        {"name": "read_file", "arguments": {"path": str(self.saved)}}
                    ]}
                    record["grounded"] = True
                else:
                    tool_results = _tool_results(messages)
                    if not tool_results or NOTICE not in tool_results[-1]:
                        raise _GroundingError("A_read_result_missing_from_next_provider_input")
                    response = LLMResponse(content=FINAL_A)
                    record["response"] = {"content": FINAL_A, "tool_calls": []}
                    record["grounded"] = True
            elif actor == "B":
                if QUESTION_B not in latest:
                    raise _GroundingError("B_original_question_missing")
                if NOTICE in whole or DATE in whole or str(self.saved) in whole:
                    raise _GroundingError("B_input_contains_A_attachment_evidence")
                response = LLMResponse(content=FINAL_B)
                record["response"] = {"content": FINAL_B, "tool_calls": []}
                record["grounded"] = True
            else:
                raise _GroundingError("provider_call_without_actor_identity")

            return response

        async def aclose(self) -> None:
            self.closed = True

    return GroundedScriptedProvider()


class _ReadOnlyLoop:
    """Dynamically becomes an AgentLoop subclass while registering one real tool."""

    def __new__(cls, bus: Any, provider: Any, workspace: Path, runtime: Path,
                evidence: dict[str, Any]):
        from nanobot.agent.loop import AgentLoop
        from nanobot.agent.tools.filesystem import ReadFileTool
        from nanobot.config.schema import ToolsConfig
        from nanobot.session.manager import SessionManager

        class ReadOnlyAgentLoop(AgentLoop):
            def _register_default_tools(self) -> None:
                tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
                original = tool.execute

                async def observe_execute(*args: Any, **kwargs: Any) -> Any:
                    path = kwargs.get("path", args[0] if args else "")
                    result = await original(*args, **kwargs)
                    _record_read(evidence, str(path), result)
                    return result

                tool.execute = observe_execute
                self.tools.register(tool)

        disabled = []
        return ReadOnlyAgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            model=MODEL,
            max_iterations=4,
            restrict_to_workspace=True,
            session_manager=SessionManager(runtime, allow_legacy=False),
            tools_config=ToolsConfig(restrict_to_workspace=True),
            unified_session=False,
            disabled_skills=disabled,
        )


async def _wait_server(channel: Any, task: asyncio.Task[Any]) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if task.done():
            task.result()
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", channel.config.port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.025)
    raise TimeoutError("websocket_server_not_ready")


def _wire_event(evidence: dict[str, Any], actor: str, direction: str, purpose: str,
                raw: str, frame: Any) -> None:
    prefix = purpose.partition(":")[0]
    request_id = prefix if len(prefix) == 32 and all(
        ch in "0123456789abcdef" for ch in prefix
    ) else None
    redacted = _redact_frame(frame)
    canonical = json.dumps(redacted, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    evidence["wire"].append({
        "actor": actor,
        "direction": direction,
        "purpose": purpose,
        "request_id": request_id,
        "raw_sha256": _text_sha256(raw),
        "frame_sha256": _sha256(canonical),
        "frame": redacted,
    })


async def _send_frame(connection: Any, evidence: dict[str, Any], actor: str,
                      purpose: str, frame: dict[str, Any]) -> str:
    raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    _wire_event(evidence, actor, "client_to_server", purpose, raw, frame)
    await connection.send(raw)
    return raw


async def _recv_frame(connection: Any, evidence: dict[str, Any], actor: str,
                      purpose: str, timeout: float = 5) -> dict[str, Any]:
    raw = await asyncio.wait_for(connection.recv(), timeout)
    if not isinstance(raw, str):
        raise ValueError("non_text_protocol_frame")
    frame = json.loads(raw)
    if not isinstance(frame, dict):
        raise ValueError("non_object_protocol_frame")
    _wire_event(evidence, actor, "server_to_client", purpose, raw, frame)
    return frame


def _turn_id_from(frame: dict[str, Any]) -> str | None:
    value = frame.get("turn_id")
    return value if isinstance(value, str) else None


async def _dispatch_turn(loop: Any, channel: Any, bus: Any, connection: Any,
                         inbound: Any, evidence: dict[str, Any], actor: str,
                         request_id: str, open_targets: list[Path]) -> dict[str, Any]:
    from nanobot.security.media import bind_media_scope

    record: dict[str, Any] = {
        "actor": actor,
        "request_id": request_id,
        "dispatch_started": True,
        "dispatch_completed": False,
        "chat_id": inbound.chat_id,
        "channel": inbound.channel,
        "sender_id": inbound.sender_id,
        "session_key": inbound.session_key,
        "content": inbound.content,
        "media": list(inbound.media),
        "outbound": [],
        "frames": [],
        "final_count": 0,
        "turn_end_count": 0,
        "turn_id": None,
        "turn_end_turn_id": None,
        "final_text": None,
        "wire_complete": False,
        "terminal_observation": {
            "complete": False,
            "quiet": False,
            "quiet_window_ms": 0,
            "unexpected_frames": [],
        },
        "agent_loop_class": type(loop).__name__,
        "agent_runner_class": type(loop.runner).__name__,
    }
    evidence["turns"].append(record)
    actor_info = {
        "actor": actor,
        "chat_id": inbound.chat_id,
        "session_key": inbound.session_key,
        "request_id": request_id,
        "kind": "agent_dispatch",
    }
    with _actor_context(**actor_info), _watch_opens(actor, open_targets) as watcher:
        try:
            with bind_media_scope(inbound.channel, inbound.chat_id):
                await asyncio.wait_for(loop._dispatch(inbound), timeout=45)
        finally:
            record["audit_open_events"] = list(watcher["events"])
        record["dispatch_completed"] = True
        while not bus.outbound.empty():
            outbound = bus.outbound.get_nowait()
            outbound_record = {
                "channel": outbound.channel,
                "chat_id": outbound.chat_id,
                "content": outbound.content,
                "metadata": _json_safe(dict(outbound.metadata or {})),
            }
            record["outbound"].append(outbound_record)
            if outbound.chat_id != inbound.chat_id or outbound.channel != "websocket":
                raise ValueError("outbound_actor_mismatch")
            await channel.send(outbound)
            frame = await _recv_frame(connection, evidence, actor,
                                      f"{request_id}:agent_output", timeout=5)
            record["frames"].append(frame)
            if frame.get("event") == "message" and isinstance(frame.get("text"), str):
                message_id = frame.get("message_id")
                if isinstance(message_id, str) and message_id.endswith(":final"):
                    record["final_count"] += 1
                    record["final_text"] = frame["text"]
                    record["turn_id"] = _turn_id_from(frame)
            if frame.get("event") == "turn_end":
                record["turn_end_count"] += 1
                record["turn_end_turn_id"] = _turn_id_from(frame)
        quiet_start = time.monotonic()
        deadline = quiet_start + 0.2
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                extra_raw = await asyncio.wait_for(connection.recv(), timeout=remaining)
            except TimeoutError:
                break
            if not isinstance(extra_raw, str):
                raise ValueError("non_text_frame_in_terminal_window")
            extra_frame = json.loads(extra_raw)
            if not isinstance(extra_frame, dict):
                raise ValueError("non_object_frame_in_terminal_window")
            _wire_event(evidence, actor, "server_to_client", f"{request_id}:terminal_window",
                        extra_raw, extra_frame)
            record["frames"].append(extra_frame)
            record["terminal_observation"]["unexpected_frames"].append(extra_frame)
        record["terminal_observation"].update({
            "complete": True,
            "quiet": not record["terminal_observation"]["unexpected_frames"],
            "quiet_window_ms": round((time.monotonic() - quiet_start) * 1000),
        })
        final_frames = [
            frame for frame in record["frames"]
            if frame.get("event") == "message"
            and isinstance(frame.get("message_id"), str)
            and frame["message_id"].endswith(":final")
        ]
        end_frames = [frame for frame in record["frames"] if frame.get("event") == "turn_end"]
        record["final_count"] = len(final_frames)
        record["turn_end_count"] = len(end_frames)
        record["final_text"] = final_frames[-1].get("text") if final_frames else None
        record["turn_id"] = final_frames[-1].get("turn_id") if final_frames else None
        record["turn_end_turn_id"] = end_frames[-1].get("turn_id") if end_frames else None
        record["wire_complete"] = True
        record["outbound_queue_empty"] = bus.outbound.empty()
    return record


async def _run_d(output: Path) -> dict[str, Any]:
    import websockets

    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket import WebSocketChannel
    from nanobot.config import loader as config_loader
    from nanobot.config.loader import set_config_path
    from nanobot.utils.observability import observation_context, span

    output = output.absolute()
    output.mkdir(exist_ok=False)
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "mode": "D",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact_root": str(output),
        "source": {},
        "fixture": {
            "name": "notice.txt",
            "mime": "text/plain",
            "text": NOTICE,
            "bytes_sha256": _sha256(NOTICE.encode("utf-8")),
            "bytes_length": len(NOTICE.encode("utf-8")),
        },
        "actors": {},
        "wire": [],
        "workspace_path": str(output / "workspace"),
        "workspace_before_files": None,
        "workspace_after_files": None,
        "upload": {},
        "provider": {
            "implementation": "grounded_scripted",
            "model_requested": MODEL,
            "chat_calls": 0,
            "llm_calls": 0,
            "sdk_calls": 0,
            "calls": [],
        },
        "turns": [],
        "reads": [],
        "file_integrity": {},
        "isolation": {},
        "cleanup": {
            "provider_closed": False,
            "channel_stopped": False,
            "server_task_done": False,
            "connections_closed": False,
            "pending_tasks_settled": False,
            "config_pointer_restored": False,
            "driver_errors": [],
        },
        "observation": {"complete": False, "errors": []},
        "errors": [],
    }
    previous_config_pointer = config_loader._current_config_path
    runtime = output / "runtime"
    workspace = output / "workspace"
    config_path = runtime / "config.json"
    channel = None
    server_task: asyncio.Task[Any] | None = None
    server_inner_task: asyncio.Task[Any] | None = None
    provider = None
    loop = None
    connections: list[Any] = []
    primary_error: BaseException | None = None
    media_root = runtime / "media"
    saved: Path | None = None
    try:
        evidence["source"] = _contract_and_sources()
        runtime.mkdir()
        workspace.mkdir()
        evidence["workspace_before_files"] = _snapshot_files(workspace)
        config_path.write_text(
            json.dumps({"agents": {"defaults": {"model": MODEL}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        set_config_path(config_path)
        _install_open_audit_hook()

        bus = MessageBus()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        channel = WebSocketChannel(
            {
                "enabled": True,
                "allow_from": ["*"],
                "host": "127.0.0.1",
                "port": port,
                "path": "/ws",
                "websocket_requires_token": False,
                "streaming": False,
            },
            bus,
        )
        server_task = asyncio.create_task(channel.start(), name="v065-websocket-server")
        await _wait_server(channel, server_task)
        server_inner_task = channel._server_task
        url = f"ws://127.0.0.1:{port}/ws?client_id="

        async with websockets.connect(url + "v065-A") as a, websockets.connect(url + "v065-B") as b:
            connections.extend([a, b])
            ready_a = await _recv_frame(a, evidence, "A", "ready")
            ready_b = await _recv_frame(b, evidence, "B", "ready")
            if ready_a.get("event") != "ready" or ready_b.get("event") != "ready":
                raise ValueError("missing_ready_event")
            token_a = ready_a.get("chat_token")
            token_b = ready_b.get("chat_token")
            if not isinstance(token_a, str) or not isinstance(token_b, str):
                raise ValueError("missing_server_chat_capability")
            chat_a, chat_b = ready_a.get("chat_id"), ready_b.get("chat_id")
            evidence["actors"] = {
                "A": {
                    "client_id_requested": "v065-A",
                    "ready": _redact_frame(ready_a),
                    "chat_id": chat_a,
                    "chat_token_present": True,
                    "chat_token_sha256": _text_sha256(token_a),
                },
                "B": {
                    "client_id_requested": "v065-B",
                    "ready": _redact_frame(ready_b),
                    "chat_id": chat_b,
                    "chat_token_present": True,
                    "chat_token_sha256": _text_sha256(token_b),
                },
            }
            if not isinstance(chat_a, str) or not isinstance(chat_b, str):
                raise ValueError("missing_server_chat_id")

            body = NOTICE.encode("utf-8")
            request_a = uuid.uuid4().hex
            upload_frame = {
                "type": "message",
                "chat_id": chat_a,
                "content": QUESTION_A,
                "media": [{
                    "name": "notice.txt",
                    "data_url": "data:text/plain;base64," + base64.b64encode(body).decode("ascii"),
                }],
            }
            await _send_frame(a, evidence, "A", f"{request_a}:legal_text_upload", upload_frame)
            inbound_a = await asyncio.wait_for(bus.consume_inbound(), timeout=5)
            if inbound_a.chat_id != chat_a or inbound_a.content != QUESTION_A or len(inbound_a.media) != 1:
                raise ValueError("upload_inbound_mismatch")
            saved = Path(inbound_a.media[0]).resolve()
            expected_chat_dir = (media_root / "websocket" / hashlib.sha256(chat_a.encode("utf-8")).hexdigest()).resolve()
            evidence["upload"] = {
                "request_id": request_a,
                "request_frame": _redact_frame(upload_frame),
                "mime": "text/plain",
                "filename": "notice.txt",
                "decoded_bytes_sha256": _sha256(body),
                "decoded_bytes_length": len(body),
                "inbound": {
                    "channel": inbound_a.channel,
                    "chat_id": inbound_a.chat_id,
                    "sender_id": inbound_a.sender_id,
                    "session_key": inbound_a.session_key,
                    "content": inbound_a.content,
                    "media": list(inbound_a.media),
                },
                "saved_path": str(saved),
                "saved_under_A_chat": saved.is_relative_to(expected_chat_dir),
            }
            before_bytes = saved.read_bytes()
            evidence["file_integrity"] = {
                "input_sha256": _sha256(body),
                "stored_sha256_before": _sha256(before_bytes),
                "stored_size_before": len(before_bytes),
                "stored_sha256_after": None,
                "stored_size_after": None,
                "unchanged": None,
            }

            provider = _make_grounded_provider(evidence, saved)
            loop = _ReadOnlyLoop(bus, provider, workspace, runtime, evidence)
            with observation_context(lambda *_args, **_kwargs: None, output.name, "V065"), span(
                "task", case_id="V065", mode="D", artifact_id=output.name
            ):
                await _dispatch_turn(
                    loop, channel, bus, a, inbound_a, evidence, "A", request_a,
                    [saved, saved.parent, saved.parent.parent, saved.parent.parent.parent],
                )

                request_b = uuid.uuid4().hex
                question_frame = {"type": "message", "chat_id": chat_b, "content": QUESTION_B}
                await _send_frame(b, evidence, "B", f"{request_b}:original_question", question_frame)
                inbound_b = await asyncio.wait_for(bus.consume_inbound(), timeout=5)
                if inbound_b.chat_id != chat_b or inbound_b.content != QUESTION_B or inbound_b.media:
                    raise ValueError("B_original_input_mismatch")
                await _dispatch_turn(
                    loop, channel, bus, b, inbound_b, evidence, "B", request_b,
                    [saved, saved.parent, saved.parent.parent, saved.parent.parent.parent],
                )

                read_tool = loop.tools.get("read_file")
                if read_tool is None:
                    raise ValueError("actual_read_file_tool_missing")
                from nanobot.security.media import bind_media_scope
                relative_saved = os.path.relpath(saved, workspace)
                parent_path = saved.parent.parent
                probe_specs = [
                    ("absolute_file", str(saved)),
                    ("relative_file", relative_saved),
                    ("chat_parent_directory", str(parent_path)),
                ]
                probe_baseline = _snapshot_files(media_root)
                for label, path in probe_specs:
                    probe_id = uuid.uuid4().hex
                    actor_info = {
                        "actor": "B",
                        "chat_id": chat_b,
                        "session_key": f"websocket:{chat_b}",
                        "request_id": probe_id,
                        "kind": "boundary_probe:" + label,
                    }
                    with _actor_context(**actor_info), _watch_opens(
                        "B", [saved, saved.parent, saved.parent.parent, saved.parent.parent.parent]
                    ) as watcher, bind_media_scope("websocket", chat_b):
                        await asyncio.wait_for(read_tool.execute(path=path), timeout=5)
                    probe_record = evidence["reads"][-1]
                    probe_record["probe"] = label
                    probe_record["audit_open_events"] = list(watcher["events"])
                probe_after = _snapshot_files(media_root)

                # Unauthorized operations must fail before media persistence or bus publication.
                isolation: dict[str, Any] = {"probes": [], "A_only_delivery": {}}
                evidence["isolation"] = isolation
                attach_media_before = _snapshot_files(media_root)
                for label, token in (
                    ("B_attach_A", None),
                    ("B_wrong_token_attach_A", "wrong-v065-capability"),
                ):
                    await _send_frame(b, evidence, "B", f"negative:{label}", {
                        "type": "attach", "chat_id": chat_a, "chat_token": token,
                    })
                    reply = await _recv_frame(b, evidence, "B", f"negative:{label}:reply")
                    isolation["probes"].append({
                        "name": label,
                        "reply": reply,
                        "inbound_queue_empty": bus.inbound.empty(),
                        "media_files_unchanged": _snapshot_files(media_root) == attach_media_before,
                    })
                media_before = _snapshot_files(media_root)
                inbound_count_before = bus.inbound.qsize()
                for label, token in (
                    ("B_foreign_message_upload", None),
                    ("B_wrong_token_message_upload", "wrong-v065-capability"),
                ):
                    foreign = {
                        "type": "message",
                        "chat_id": chat_a,
                        "content": "foreign chat attachment probe",
                        "media": [{
                            "name": "borrow.txt",
                            "data_url": "data:text/plain;base64," + base64.b64encode(b"foreign").decode("ascii"),
                        }],
                    }
                    if token is not None:
                        foreign["chat_token"] = token
                    await _send_frame(b, evidence, "B", f"negative:{label}", foreign)
                    reply = await _recv_frame(b, evidence, "B", f"negative:{label}:reply")
                    isolation["probes"].append({
                        "name": label,
                        "reply": reply,
                        "inbound_queue_size": bus.inbound.qsize(),
                        "media_files_unchanged": _snapshot_files(media_root) == media_before,
                    })
                isolation["unauthorized_message_bus_unchanged"] = bus.inbound.qsize() == inbound_count_before

                # A delivery must remain exclusive after every rejected B request.
                await channel.send(OutboundMessage(
                    channel="websocket", chat_id=chat_a, content="V065_A_ONLY_SENTINEL"
                ))
                sentinel_a = await _recv_frame(a, evidence, "A", "A_only_sentinel")
                quiet_started = time.monotonic()
                try:
                    b_unexpected = await asyncio.wait_for(b.recv(), timeout=0.25)
                    b_unexpected_parsed = json.loads(b_unexpected)
                    _wire_event(evidence, "B", "server_to_client", "unexpected_after_A_sentinel",
                                b_unexpected, b_unexpected_parsed)
                except TimeoutError:
                    b_unexpected_parsed = None
                b_quiet_ms = round((time.monotonic() - quiet_started) * 1000)
                isolation["A_only_delivery"] = {
                    "A_received": sentinel_a,
                    "B_received": b_unexpected_parsed,
                    "B_quiet_for_ms": b_quiet_ms,
                    "B_receive_observation": {
                        "complete": True,
                        "timed_out": b_unexpected_parsed is None,
                        "window_ms": b_quiet_ms,
                    },
                }

                # A client_id collision receives a fresh server chat and no authority over A.
                async with websockets.connect(url + "v065-A") as spoof:
                    connections.append(spoof)
                    spoof_ready = await _recv_frame(spoof, evidence, "spoof", "spoof_ready")
                    await _send_frame(spoof, evidence, "spoof", "spoof_attach_A", {
                        "type": "attach", "chat_id": chat_a,
                    })
                    spoof_reply = await _recv_frame(spoof, evidence, "spoof", "spoof_attach_A_reply")
                    isolation["spoof_client_id"] = {
                        "requested_client_id": "v065-A",
                        "fresh_chat_id": spoof_ready.get("chat_id"),
                        "same_chat_as_A": spoof_ready.get("chat_id") == chat_a,
                        "attach_reply": spoof_reply,
                        "inbound_queue_empty": bus.inbound.empty(),
                    }

                # A capability is intentionally sufficient for explicit resumption.
                async with websockets.connect(url + "v065-A-resume") as resume:
                    connections.append(resume)
                    resume_ready = await _recv_frame(resume, evidence, "A_resume", "resume_ready")
                    await _send_frame(resume, evidence, "A_resume", "valid_capability_attach", {
                        "type": "attach", "chat_id": chat_a, "chat_token": token_a,
                    })
                    attached = await _recv_frame(resume, evidence, "A_resume", "valid_capability_attach_reply")
                    resume_request = uuid.uuid4().hex
                    await _send_frame(resume, evidence, "A_resume", f"{resume_request}:valid_capability_message", {
                        "type": "message", "chat_id": chat_a,
                        "content": "V065_AUTHORIZED_CAPABILITY_CONTROL",
                    })
                    resumed_inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=5)
                    isolation["valid_capability"] = {
                        "ready_chat_id": resume_ready.get("chat_id"),
                        "attached_reply": attached,
                        "inbound_request_id": resume_request,
                        "inbound": {
                            "chat_id": resumed_inbound.chat_id,
                            "session_key": resumed_inbound.session_key,
                            "content": resumed_inbound.content,
                            "media": list(resumed_inbound.media),
                        },
                        "token_present": True,
                        "token_sha256": _text_sha256(token_a),
                    }
                    await channel.send(OutboundMessage(
                        channel="websocket", chat_id=chat_a, content="V065_CAPABILITY_DELIVERY_CONTROL"
                    ))
                    isolation["valid_capability"]["delivery"] = await _recv_frame(
                        resume, evidence, "A_resume", "valid_capability_delivery"
                    )
                    await _recv_frame(a, evidence, "A", "capability_delivery_copy")

                if not bus.inbound.empty():
                    raise ValueError("unexpected_pending_inbound")
                evidence["isolation"]["boundary_probe_media_unchanged"] = probe_baseline == probe_after

        evidence["workspace_after_files"] = _snapshot_files(workspace)
        after_bytes = saved.read_bytes()
        evidence["file_integrity"].update({
            "stored_sha256_after": _sha256(after_bytes),
            "stored_size_after": len(after_bytes),
            "unchanged": _sha256(before_bytes) == _sha256(after_bytes),
        })
        evidence["observation"]["complete"] = True
    except BaseException as exc:
        primary_error = exc
        evidence["observation"]["errors"].append({
            "phase": "run",
            "kind": type(exc).__name__,
            "message": str(exc),
        })
        evidence["errors"].append({
            "phase": "run",
            "kind": type(exc).__name__,
            "message": str(exc),
        })
    finally:
        # WebSocket contexts close their sockets before channel shutdown.
        evidence["cleanup"]["connections_closed"] = all(
            getattr(getattr(connection, "state", None), "name", None) == "CLOSED"
            for connection in connections
        )
        if loop is not None:
            try:
                await asyncio.wait_for(loop.close_mcp(), timeout=5)
                pending = [
                    task for task in loop._background_tasks
                    if isinstance(task, asyncio.Task) and not task.done()
                ]
                pending.extend(
                    task for tasks in loop._active_tasks.values() for task in tasks
                    if isinstance(task, asyncio.Task) and not task.done()
                )
                if pending:
                    done, still_pending = await asyncio.wait(pending, timeout=5)
                    for task in still_pending:
                        task.cancel()
                    if still_pending:
                        await asyncio.gather(*still_pending, return_exceptions=True)
                    evidence["cleanup"]["pending_tasks_settled"] = not still_pending
                else:
                    evidence["cleanup"]["pending_tasks_settled"] = True
            except BaseException as exc:
                evidence["cleanup"]["driver_errors"].append({
                    "operation": "loop_cleanup", "kind": type(exc).__name__, "message": str(exc),
                })
        else:
            evidence["cleanup"]["pending_tasks_settled"] = True

        if provider is not None:
            try:
                await provider.aclose()
                evidence["cleanup"]["provider_closed"] = provider.closed
            except BaseException as exc:
                evidence["cleanup"]["driver_errors"].append({
                    "operation": "provider_close", "kind": type(exc).__name__, "message": str(exc),
                })
        else:
            evidence["cleanup"]["provider_closed"] = True

        if channel is not None:
            try:
                await asyncio.wait_for(channel.stop(), timeout=10)
                evidence["cleanup"]["channel_stopped"] = not channel._running
                evidence["cleanup"]["server_task_done"] = bool(
                    server_inner_task is None or server_inner_task.done()
                )
            except BaseException as exc:
                evidence["cleanup"]["driver_errors"].append({
                    "operation": "channel_stop", "kind": type(exc).__name__, "message": str(exc),
                })
        else:
            evidence["cleanup"]["channel_stopped"] = True
            evidence["cleanup"]["server_task_done"] = True

        if server_task is not None:
            try:
                if not server_task.done():
                    await asyncio.wait_for(server_task, timeout=5)
                else:
                    server_task.result()
                evidence["cleanup"]["server_task_done"] = server_task.done() and (
                    server_inner_task is None or server_inner_task.done()
                )
            except BaseException as exc:
                evidence["cleanup"]["driver_errors"].append({
                    "operation": "server_task", "kind": type(exc).__name__, "message": str(exc),
                })

        try:
            config_loader._current_config_path = previous_config_pointer
            evidence["cleanup"]["config_pointer_restored"] = (
                config_loader._current_config_path == previous_config_pointer
            )
        except BaseException as exc:
            evidence["cleanup"]["driver_errors"].append({
                "operation": "config_pointer_restore", "kind": type(exc).__name__, "message": str(exc),
            })

        if (saved is not None and saved.exists()
                and evidence["file_integrity"].get("stored_sha256_after") is None):
            try:
                final_bytes = saved.read_bytes()
                evidence["file_integrity"].update({
                    "stored_sha256_after": _sha256(final_bytes),
                    "stored_size_after": len(final_bytes),
                    "unchanged": evidence["file_integrity"].get("stored_sha256_before") == _sha256(final_bytes),
                })
            except BaseException as exc:
                evidence["cleanup"]["driver_errors"].append({
                    "operation": "file_integrity_finalize", "kind": type(exc).__name__, "message": str(exc),
                })

        evidence["provider"]["chat_calls"] = getattr(provider, "call_count", 0)
        verdict = score(evidence)
        evidence["status"] = verdict["status"]
        evidence["checks"] = verdict["checks"]
        evidence["reasons"] = verdict["reasons"]
        try:
            (output / "evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            (output / "verdict.json").write_text(
                json.dumps(verdict, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
            evidence["cleanup"]["driver_errors"].append({
                "operation": "write_evidence", "kind": type(exc).__name__, "message": str(exc),
            })

    if primary_error is not None and not evidence.get("status"):
        evidence["status"] = "invalid"
    return evidence


async def run(output: Path, *, mode: str = "D") -> dict[str, Any]:
    """Run the V065 D control into a new output directory and persist evidence."""
    if mode != "D":
        raise ValueError("unsupported_mode")
    return await _run_d(Path(output))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _trace_invalid_reasons(evidence: dict[str, Any]) -> list[str]:
    """Reject missing, inconsistent, or detached instrumentation before scoring behavior."""
    invalid: list[str] = []
    source = evidence.get("source")
    if not isinstance(source, dict):
        return ["source_identity_missing"]
    try:
        root = _ROOT.resolve()
        current = {
            "contract_path": CONTRACT_REL.as_posix(),
            "contract_sha256": _sha256((root / CONTRACT_REL).read_bytes()),
            "original_case_path": CASE_REL.as_posix(),
            "original_case_sha256": _sha256((root / CASE_REL).read_bytes()),
            "driver_path": Path(__file__).resolve().relative_to(root).as_posix(),
            "driver_sha256": _sha256(Path(__file__).read_bytes()),
        }
    except (OSError, ValueError):
        return ["source_identity_unavailable"]
    if current["contract_sha256"] != CONTRACT_SHA256 or source != current:
        invalid.append("source_identity_mismatch")

    observation = evidence.get("observation")
    if not isinstance(observation, dict) or observation.get("complete") is not True:
        invalid.append("observation_incomplete")
    elif observation.get("errors"):
        invalid.append("collector_error_present")
    if evidence.get("errors"):
        invalid.append("run_error_present")
    cleanup = evidence.get("cleanup")
    if not isinstance(cleanup, dict):
        invalid.append("cleanup_evidence_missing")
    elif cleanup.get("driver_errors"):
        invalid.append("driver_cleanup_error_present")
    elif any(cleanup.get(key) is not True for key in (
        "provider_closed", "connections_closed", "config_pointer_restored",
    )):
        invalid.append("driver_resource_cleanup_incomplete")

    actors = evidence.get("actors")
    wire = evidence.get("wire")
    provider = evidence.get("provider")
    turns = evidence.get("turns")
    reads = evidence.get("reads")
    upload = evidence.get("upload")
    isolation = evidence.get("isolation")
    file_integrity = evidence.get("file_integrity")
    if not isinstance(actors, dict) or not isinstance(wire, list) or not wire:
        invalid.append("raw_wire_missing")
        wire = []
    if not isinstance(provider, dict) or not isinstance(provider.get("calls"), list):
        invalid.append("provider_actual_results_missing")
        provider = {"calls": []}
    if not isinstance(turns, list) or not turns:
        invalid.append("turn_evidence_missing")
        turns = []
    if not isinstance(reads, list):
        invalid.append("read_evidence_missing")
        reads = []
    if not isinstance(upload, dict) or not isinstance(isolation, dict):
        invalid.append("upload_or_isolation_evidence_missing")
        upload = upload if isinstance(upload, dict) else {}
        isolation = isolation if isinstance(isolation, dict) else {}
    if not isinstance(file_integrity, dict):
        invalid.append("file_integrity_evidence_missing")
        file_integrity = {}

    for entry in wire:
        if not isinstance(entry, dict):
            invalid.append("wire_entry_malformed")
            continue
        frame = entry.get("frame")
        if (
            not isinstance(frame, dict)
            or not _is_sha256(entry.get("raw_sha256"))
            or not _is_sha256(entry.get("frame_sha256"))
        ):
            invalid.append("wire_digest_or_frame_missing")
            continue
        canonical = json.dumps(
            frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if _sha256(canonical) != entry["frame_sha256"]:
            invalid.append("wire_frame_digest_mismatch")
        purpose = entry.get("purpose")
        prefix = purpose.partition(":")[0] if isinstance(purpose, str) else ""
        expected_request_id = prefix if len(prefix) == 32 and all(
            ch in "0123456789abcdef" for ch in prefix
        ) else None
        if entry.get("request_id") != expected_request_id:
            invalid.append("wire_request_correlation_mismatch")

    if isinstance(actors, dict):
        for actor_name, client_id in (("A", "v065-A"), ("B", "v065-B")):
            actor = actors.get(actor_name)
            if not isinstance(actor, dict):
                invalid.append("actor_identity_missing:" + actor_name)
                continue
            ready_rows = [
                row for row in wire
                if isinstance(row, dict) and row.get("actor") == actor_name
                and row.get("direction") == "server_to_client" and row.get("purpose") == "ready"
            ]
            if len(ready_rows) != 1 or actor.get("ready") != (
                ready_rows[0].get("frame") if ready_rows else None
            ):
                invalid.append("ready_wire_binding_mismatch:" + actor_name)
            if (
                actor.get("client_id_requested") != client_id
                or actor.get("chat_id") != actor.get("ready", {}).get("chat_id")
                or actor.get("chat_token_present") is not True
                or not _is_sha256(actor.get("chat_token_sha256"))
                or actor.get("ready", {}).get("client_id") != client_id
                or actor.get("ready", {}).get("chat_token", {}).get("sha256")
                != actor.get("chat_token_sha256")
            ):
                invalid.append("actor_ready_identity_invalid:" + actor_name)

    if isinstance(upload, dict):
        request_id = upload.get("request_id")
        upload_rows = [
            row for row in wire
            if isinstance(row, dict) and row.get("actor") == "A"
            and row.get("direction") == "client_to_server"
            and row.get("purpose") == f"{request_id}:legal_text_upload"
        ]
        if len(upload_rows) != 1 or upload.get("request_frame") != (
            upload_rows[0].get("frame") if upload_rows else None
        ):
            invalid.append("upload_wire_binding_mismatch")
        frame = upload.get("request_frame")
        media = frame.get("media") if isinstance(frame, dict) else None
        data_url = media[0].get("data_url") if isinstance(media, list) and media and isinstance(media[0], dict) else None
        prefix = "data:text/plain;base64,"
        try:
            decoded = base64.b64decode(data_url[len(prefix):], validate=True) if (
                isinstance(data_url, str) and data_url.startswith(prefix)
            ) else None
        except (ValueError, TypeError):
            decoded = None
        if (
            decoded != NOTICE.encode("utf-8")
            or upload.get("decoded_bytes_sha256") != _sha256(NOTICE.encode("utf-8"))
            or upload.get("decoded_bytes_length") != len(NOTICE.encode("utf-8"))
            or file_integrity.get("input_sha256") != _sha256(NOTICE.encode("utf-8"))
        ):
            invalid.append("uploaded_notice_bytes_unbound")

    turn_by_actor = {
        turn.get("actor"): turn for turn in turns if isinstance(turn, dict)
    }
    if set(turn_by_actor) != {"A", "B"} or len(turns) != 2:
        invalid.append("original_turn_pair_missing_or_duplicate")
    for actor_name in ("A", "B"):
        turn = turn_by_actor.get(actor_name)
        actor = actors.get(actor_name, {}) if isinstance(actors, dict) else {}
        expected_content = QUESTION_A if actor_name == "A" else QUESTION_B
        expected_request_id = turn.get("request_id") if isinstance(turn, dict) else None
        expected_purpose = "legal_text_upload" if actor_name == "A" else "original_question"
        input_rows = [
            row for row in wire
            if isinstance(row, dict) and row.get("actor") == actor_name
            and row.get("direction") == "client_to_server"
            and row.get("purpose") == f"{expected_request_id}:{expected_purpose}"
        ]
        output_rows = [
            row for row in wire
            if isinstance(row, dict) and row.get("actor") == actor_name
            and row.get("direction") == "server_to_client"
            and row.get("request_id") == expected_request_id
            and row.get("purpose") in (
                f"{expected_request_id}:agent_output",
                f"{expected_request_id}:terminal_window",
            )
        ]
        if not isinstance(turn, dict):
            invalid.append("turn_identity_missing:" + actor_name)
            continue
        if (
            turn.get("request_id") != expected_request_id
            or not isinstance(expected_request_id, str)
            or len(expected_request_id) != 32
            or turn.get("chat_id") != actor.get("chat_id")
            or turn.get("session_key") != f"websocket:{actor.get('chat_id')}"
            or turn.get("channel") != "websocket"
            or turn.get("content") != expected_content
            or not isinstance(turn.get("audit_open_events"), list)
            or turn.get("dispatch_started") is not True
            or turn.get("dispatch_completed") is not True
            or turn.get("wire_complete") is not True
            or turn.get("outbound_queue_empty") is not True
            or not isinstance(turn.get("outbound"), list)
            or not isinstance(turn.get("frames"), list)
            or not isinstance(turn.get("terminal_observation"), dict)
            or turn["terminal_observation"].get("complete") is not True
            or turn["terminal_observation"].get("quiet_window_ms", 0) < 150
            or len(input_rows) != 1
        ):
            invalid.append("turn_wire_identity_or_observation_mismatch:" + actor_name)
            continue
        main_output_rows = [row for row in output_rows
                            if row.get("purpose") == f"{expected_request_id}:agent_output"]
        terminal_rows = [row for row in output_rows
                         if row.get("purpose") == f"{expected_request_id}:terminal_window"]
        expected_frames = [row.get("frame") for row in main_output_rows + terminal_rows]
        if (
            len(main_output_rows) != len(turn.get("outbound", []))
            or expected_frames != turn.get("frames")
            or turn["terminal_observation"].get("unexpected_frames")
            != [row.get("frame") for row in terminal_rows]
            or turn["terminal_observation"].get("quiet") is not (not terminal_rows)
        ):
            invalid.append("turn_frame_wire_binding_mismatch:" + actor_name)
        input_frame = input_rows[0].get("frame")
        if (
            input_frame.get("chat_id") != turn.get("chat_id")
            or input_frame.get("content") != expected_content
            or (actor_name == "A" and input_frame != upload.get("request_frame"))
            or (actor_name == "B" and input_frame.get("media"))
        ):
            invalid.append("turn_input_wire_content_mismatch:" + actor_name)
        final_frames = [
            frame for frame in turn["frames"]
            if frame.get("event") == "message"
            and isinstance(frame.get("message_id"), str)
            and frame["message_id"].endswith(":final")
        ]
        end_frames = [frame for frame in turn["frames"] if frame.get("event") == "turn_end"]
        if (
            turn.get("final_count") != len(final_frames)
            or turn.get("turn_end_count") != len(end_frames)
            or turn.get("final_text") != (final_frames[-1].get("text") if final_frames else None)
            or turn.get("turn_id") != (final_frames[-1].get("turn_id") if final_frames else None)
            or turn.get("turn_end_turn_id") != (end_frames[-1].get("turn_id") if end_frames else None)
        ):
            invalid.append("turn_summary_not_derived_from_wire:" + actor_name)
        outbound = turn.get("outbound", [])
        if len(outbound) != len(main_output_rows):
            invalid.append("outbound_wire_cardinality_mismatch:" + actor_name)
        else:
            for sent, received in zip(outbound, [row.get("frame") for row in main_output_rows], strict=True):
                metadata = sent.get("metadata", {})
                if (
                    sent.get("channel") != "websocket"
                    or sent.get("chat_id") != turn.get("chat_id")
                    or received.get("chat_id") != turn.get("chat_id")
                    or metadata.get("_turn_id") != received.get("turn_id")
                    or (metadata.get("_turn_end") is True) != (received.get("event") == "turn_end")
                    or (received.get("event") == "message" and sent.get("content") != received.get("text"))
                ):
                    invalid.append("outbound_frame_binding_mismatch:" + actor_name)
                    break

    calls = provider.get("calls", [])
    if len(calls) != 3 or provider.get("chat_calls") != len(calls):
        invalid.append("provider_actual_call_results_incomplete")
    expected_call_actors = ["A", "A", "B"]
    for index, (call, actor_name) in enumerate(zip(calls, expected_call_actors)):
        turn = turn_by_actor.get(actor_name, {})
        if (
            not isinstance(call, dict)
            or call.get("actor") != actor_name
            or call.get("chat_id") != turn.get("chat_id")
            or call.get("session_key") != turn.get("session_key")
            or call.get("request_id") != turn.get("request_id")
            or call.get("index") != index + 1
            or call.get("model") != MODEL
            or not isinstance(call.get("messages"), list)
            or not isinstance(call.get("response"), dict)
        ):
            invalid.append("provider_call_identity_or_result_mismatch")
            break

    for read in reads:
        if not isinstance(read, dict):
            invalid.append("read_result_malformed")
            continue
        return_text = read.get("return_text")
        if (
            not isinstance(return_text, str)
            or not _is_sha256(read.get("return_text_sha256"))
            or _text_sha256(return_text) != read.get("return_text_sha256")
        ):
            invalid.append("read_return_hash_missing_or_mismatched")
            continue
        actor_name = read.get("actor")
        actor = actors.get(actor_name, {}) if isinstance(actors, dict) else {}
        if (
            read.get("chat_id") != actor.get("chat_id")
            or read.get("session_key") != f"websocket:{actor.get('chat_id')}"
            or (actor_name == "A" and read.get("request_id") != turn_by_actor.get("A", {}).get("request_id"))
            or (actor_name == "B" and not any(
                read.get("request_id") == item.get("request_id")
                for item in reads if isinstance(item, dict)
                and str(item.get("kind", "")).startswith("boundary_probe:")
            ) and not str(read.get("kind", "")).startswith("boundary_probe:"))
        ):
            invalid.append("read_result_identity_mismatch")
    a_reads = [
        read for read in reads if isinstance(read, dict)
        and read.get("actor") == "A" and read.get("kind") == "agent_dispatch"
    ]
    named_probes = [
        read for read in reads if isinstance(read, dict) and read.get("probe") is not None
    ]
    probe_names = [read.get("probe") for read in named_probes]
    if len(a_reads) != 1 or sorted(probe_names) != sorted(
        ["absolute_file", "relative_file", "chat_parent_directory"]
    ):
        invalid.append("read_probe_set_missing_or_duplicate")
    elif any(
        read.get("actor") != "B"
        or read.get("chat_id") != actors.get("B", {}).get("chat_id")
        or read.get("session_key") != f"websocket:{actors.get('B', {}).get('chat_id')}"
        or not isinstance(read.get("audit_open_events"), list)
        for read in named_probes
    ):
        invalid.append("boundary_read_observation_incomplete")
    if len(a_reads) == 1:
        a_read = a_reads[0]
        call_results = [
            _tool_results(call.get("messages", []))
            for call in calls if isinstance(call, dict) and call.get("actor") == "A"
        ]
        if (
            a_read.get("requested_path") != upload.get("saved_path")
            or not call_results or not call_results[-1]
            or a_read.get("return_text") not in call_results[-1]
        ):
            invalid.append("positive_read_result_provider_binding_mismatch")
        saved_path = upload.get("saved_path")
        workspace_path = evidence.get("workspace_path")
        if not isinstance(saved_path, str) or not isinstance(workspace_path, str):
            invalid.append("saved_media_or_workspace_path_missing")
        else:
            try:
                saved_resolved = Path(saved_path).resolve()
                chat_dir = (
                    Path(evidence.get("artifact_root", "")) / "runtime" / "media" / "websocket"
                    / hashlib.sha256(actors["A"]["chat_id"].encode("utf-8")).hexdigest()
                ).resolve()
                saved_resolved.relative_to(chat_dir)
                Path(saved_path).relative_to(chat_dir)
                if saved_resolved.name != "notice.txt" or saved_resolved.is_relative_to(Path(workspace_path).resolve()):
                    invalid.append("saved_media_path_scope_mismatch")
            except (KeyError, OSError, ValueError, TypeError):
                invalid.append("saved_media_path_scope_mismatch")
            if evidence.get("workspace_before_files") != {}:
                invalid.append("workspace_not_isolated_before_dispatch")
            expected_probe_paths = {
                "absolute_file": str(saved_resolved),
                "relative_file": os.path.relpath(saved_resolved, Path(workspace_path).resolve()),
                "chat_parent_directory": str(saved_resolved.parent.parent),
            }
            for read in named_probes:
                expected_path = expected_probe_paths.get(read.get("probe"))
                actual_path = read.get("requested_path")
                if read.get("probe") == "relative_file":
                    path_matches = isinstance(actual_path, str) and os.path.normcase(actual_path) == os.path.normcase(expected_path or "")
                else:
                    path_matches = isinstance(actual_path, str) and _normalized_path(actual_path) == _normalized_path(expected_path or "")
                if not path_matches:
                    invalid.append("boundary_probe_path_mismatch:" + str(read.get("probe")))
    turn_a = turn_by_actor.get("A", {})
    a_open_events = turn_a.get("audit_open_events")
    if (
        not isinstance(a_open_events, list)
        or not any(
            isinstance(event, dict)
            and event.get("actor") == "A"
            and event.get("path") == _normalized_path(upload.get("saved_path", ""))
            for event in a_open_events
        )
    ):
        invalid.append("positive_file_open_audit_missing")
    probes = isolation.get("probes")
    names = [item.get("name") for item in probes if isinstance(item, dict)] if isinstance(probes, list) else []
    required_names = [
        "B_attach_A", "B_wrong_token_attach_A", "B_foreign_message_upload",
        "B_wrong_token_message_upload",
    ]
    if not isinstance(probes, list) or sorted(names) != sorted(required_names):
        invalid.append("negative_auth_probe_set_missing_or_duplicate")
    else:
        for item in probes:
            name = item["name"]
            requests = [row for row in wire if isinstance(row, dict)
                        and row.get("purpose") == f"negative:{name}"
                        and row.get("direction") == "client_to_server"]
            replies = [row for row in wire if isinstance(row, dict)
                       and row.get("purpose") == f"negative:{name}:reply"
                       and row.get("direction") == "server_to_client"]
            if (
                len(requests) != 1 or len(replies) != 1
                or requests[0].get("actor") != "B" or replies[0].get("actor") != "B"
                or item.get("reply") != replies[0].get("frame")
            ):
                invalid.append("negative_auth_wire_binding_missing:" + name)
            elif (
                requests[0]["frame"].get("chat_id") != actors.get("A", {}).get("chat_id")
                or requests[0]["frame"].get("type") != (
                    "attach" if "attach" in name else "message"
                )
                or ("upload" in name and not requests[0]["frame"].get("media"))
                or ("wrong_token" in name and not requests[0]["frame"].get("chat_token", {}).get("present"))
                or ("wrong_token" not in name and requests[0]["frame"].get("chat_token", {}).get("present"))
            ):
                invalid.append("negative_auth_request_shape_mismatch:" + name)
    delivery = isolation.get("A_only_delivery")
    if not isinstance(delivery, dict):
        invalid.append("bounded_A_only_delivery_trace_missing")
    else:
        sentinel_rows = [row for row in wire if isinstance(row, dict)
                         and row.get("actor") == "A" and row.get("purpose") == "A_only_sentinel"
                         and row.get("direction") == "server_to_client"]
        receive_observation = delivery.get("B_receive_observation")
        b_leak_rows = [row for row in wire if isinstance(row, dict)
                       and row.get("actor") == "B"
                       and row.get("purpose") == "unexpected_after_A_sentinel"
                       and row.get("direction") == "server_to_client"]
        received_frame = delivery.get("B_received")
        if (
            len(sentinel_rows) != 1
            or delivery.get("A_received") != sentinel_rows[0].get("frame")
            or not isinstance(receive_observation, dict)
            or receive_observation.get("complete") is not True
            or (received_frame is None and (
                receive_observation.get("timed_out") is not True
                or receive_observation.get("window_ms", 0) < 200
                or b_leak_rows
            ))
            or (received_frame is not None and (
                receive_observation.get("timed_out") is not False
                or len(b_leak_rows) != 1
                or b_leak_rows[0].get("frame") != received_frame
            ))
        ):
            invalid.append("bounded_A_only_delivery_observation_incomplete")
    spoof = isolation.get("spoof_client_id")
    if not isinstance(spoof, dict):
        invalid.append("client_id_spoof_trace_missing")
    else:
        spoof_ready = [row for row in wire if isinstance(row, dict)
                       and row.get("actor") == "spoof" and row.get("purpose") == "spoof_ready"]
        spoof_request = [row for row in wire if isinstance(row, dict)
                         and row.get("actor") == "spoof" and row.get("purpose") == "spoof_attach_A"]
        spoof_reply = [row for row in wire if isinstance(row, dict)
                       and row.get("actor") == "spoof" and row.get("purpose") == "spoof_attach_A_reply"]
        if (
            len(spoof_ready) != 1 or len(spoof_request) != 1 or len(spoof_reply) != 1
            or spoof.get("fresh_chat_id") != spoof_ready[0].get("frame", {}).get("chat_id")
            or spoof_ready[0].get("frame", {}).get("client_id") != "v065-A"
            or spoof.get("attach_reply") != spoof_reply[0].get("frame")
            or spoof_request[0].get("frame", {}).get("chat_id") != actors.get("A", {}).get("chat_id")
        ):
            invalid.append("client_id_spoof_wire_binding_missing")
    capability = isolation.get("valid_capability")
    if not isinstance(capability, dict):
        invalid.append("valid_capability_trace_missing")
    else:
        capability_request = [row for row in wire if isinstance(row, dict)
                              and row.get("actor") == "A_resume"
                              and row.get("purpose") == "valid_capability_attach"]
        capability_reply = [row for row in wire if isinstance(row, dict)
                            and row.get("actor") == "A_resume"
                            and row.get("purpose") == "valid_capability_attach_reply"]
        capability_messages = [row for row in wire if isinstance(row, dict)
                               and row.get("actor") == "A_resume"
                               and isinstance(row.get("purpose"), str)
                               and row.get("purpose", "").endswith(":valid_capability_message")]
        capability_deliveries = [row for row in wire if isinstance(row, dict)
                                  and row.get("actor") == "A_resume"
                                  and row.get("purpose") == "valid_capability_delivery"]
        capability_copies = [row for row in wire if isinstance(row, dict)
                             and row.get("actor") == "A"
                             and row.get("purpose") == "capability_delivery_copy"]
        if (
            len(capability_request) != 1 or len(capability_reply) != 1
            or len(capability_messages) != 1 or len(capability_deliveries) != 1 or len(capability_copies) != 1
            or capability.get("attached_reply") != capability_reply[0].get("frame")
            or capability_request[0].get("frame", {}).get("chat_id") != actors.get("A", {}).get("chat_id")
            or capability_request[0].get("frame", {}).get("chat_token", {}).get("sha256")
            != actors.get("A", {}).get("chat_token_sha256")
            or capability.get("token_sha256") != actors.get("A", {}).get("chat_token_sha256")
            or capability_messages[0].get("request_id") != capability.get("inbound_request_id")
            or capability_messages[0].get("frame", {}).get("chat_id") != actors.get("A", {}).get("chat_id")
            or capability.get("inbound", {}).get("chat_id") != capability_messages[0].get("frame", {}).get("chat_id")
            or capability.get("inbound", {}).get("content") != capability_messages[0].get("frame", {}).get("content")
            or capability.get("delivery") != capability_deliveries[0].get("frame")
            or capability_deliveries[0].get("frame") != capability_copies[0].get("frame")
        ):
            invalid.append("valid_capability_wire_binding_missing")
    if "absolute_file" not in [read.get("probe") for read in reads if isinstance(read, dict)]:
        invalid.append("positive_and_negative_read_probe_trace_missing")
    return sorted(set(invalid))


def score(evidence: dict[str, Any]) -> dict[str, Any]:
    """Score captured evidence; incomplete instrumentation is always invalid."""
    invalid: list[str] = []
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    if not isinstance(evidence, dict):
        return {"status": "invalid", "checks": {}, "reasons": ["evidence_not_object"]}

    if evidence.get("schema") != SCHEMA or evidence.get("version") != VERSION or evidence.get("mode") != "D":
        invalid.append("schema_version_or_mode_mismatch")
    invalid.extend(_trace_invalid_reasons(evidence))

    actors = evidence.get("actors")
    wire = evidence.get("wire")
    upload = evidence.get("upload")
    provider = evidence.get("provider")
    turns = evidence.get("turns")
    reads = evidence.get("reads")
    file_integrity = evidence.get("file_integrity")
    isolation = evidence.get("isolation")
    cleanup = evidence.get("cleanup")
    if not isinstance(actors, dict) or not isinstance(wire, list) or not isinstance(upload, dict):
        invalid.append("wire_actor_or_upload_evidence_missing")
    if not isinstance(provider, dict) or not isinstance(provider.get("calls"), list):
        invalid.append("provider_evidence_missing")
    if not isinstance(turns, list) or not isinstance(reads, list):
        invalid.append("turn_or_read_evidence_missing")
    if not isinstance(file_integrity, dict) or not isinstance(isolation, dict) or not isinstance(cleanup, dict):
        invalid.append("boundary_or_cleanup_evidence_missing")
    if invalid:
        return {"status": "invalid", "checks": checks, "reasons": invalid}

    # From here on, field structure is complete. Failed product observations are product_fail.
    a_actor, b_actor = actors.get("A", {}), actors.get("B", {})
    checks["distinct_server_generated_chats"] = bool(
        isinstance(a_actor.get("chat_id"), str)
        and isinstance(b_actor.get("chat_id"), str)
        and a_actor.get("chat_id") != b_actor.get("chat_id")
        and a_actor.get("ready", {}).get("event") == "ready"
        and b_actor.get("ready", {}).get("event") == "ready"
    )
    checks["server_capabilities_digest_only"] = all(
        actor.get("chat_token_present") is True and _is_sha256(actor.get("chat_token_sha256"))
        and isinstance(actor.get("ready", {}).get("chat_token"), dict)
        and actor["ready"]["chat_token"].get("present") is True
        for actor in (a_actor, b_actor)
    )
    checks["legal_text_upload_and_actual_inbound"] = bool(
        upload.get("mime") == "text/plain"
        and upload.get("filename") == "notice.txt"
        and upload.get("request_frame", {}).get("type") == "message"
        and upload.get("request_frame", {}).get("media", [{}])[0].get("data_url", "").startswith("data:text/plain;base64,")
        and upload.get("inbound", {}).get("chat_id") == a_actor.get("chat_id")
        and upload.get("inbound", {}).get("content") == QUESTION_A
        and len(upload.get("inbound", {}).get("media", [])) == 1
        and upload.get("saved_under_A_chat") is True
    )
    checks["input_and_stored_bytes_match_and_remain_unchanged"] = bool(
        _is_sha256(file_integrity.get("input_sha256"))
        and file_integrity.get("input_sha256") == evidence.get("fixture", {}).get("bytes_sha256")
        and file_integrity.get("stored_sha256_before") == file_integrity.get("input_sha256")
        and file_integrity.get("stored_sha256_after") == file_integrity.get("input_sha256")
        and file_integrity.get("unchanged") is True
        and file_integrity.get("stored_size_before") == file_integrity.get("stored_size_after")
    )

    a_calls = [call for call in provider["calls"] if call.get("actor") == "A"]
    b_calls = [call for call in provider["calls"] if call.get("actor") == "B"]
    a_call_inputs = [_all_message_text(call.get("messages", [])) for call in a_calls]
    b_call_inputs = [_all_message_text(call.get("messages", [])) for call in b_calls]
    a_read = next((read for read in reads if read.get("actor") == "A" and read.get("status") == "ok"), None)
    checks["A_actual_document_extraction"] = bool(
        QUESTION_A in (a_call_inputs[0] if a_call_inputs else "")
        and NOTICE in (a_call_inputs[0] if a_call_inputs else "")
    )
    checks["A_actual_read_result_reaches_next_provider_input"] = bool(
        a_read is not None
        and a_read.get("contains_notice") is True
        and _is_sha256(a_read.get("return_text_sha256"))
        and len(a_call_inputs) >= 2
        and any(NOTICE in text for text in _tool_results(a_calls[1].get("messages", [])))
    )

    turn_by_actor = {turn.get("actor"): turn for turn in turns if isinstance(turn, dict)}
    turn_structure_complete = all(
        actor in turn_by_actor
        and turn_by_actor[actor].get("dispatch_started") is True
        and turn_by_actor[actor].get("dispatch_completed") is True
        and turn_by_actor[actor].get("wire_complete") is True
        and turn_by_actor[actor].get("outbound_queue_empty") is True
        and isinstance(turn_by_actor[actor].get("outbound"), list)
        and isinstance(turn_by_actor[actor].get("frames"), list)
        for actor in ("A", "B")
    )
    if not turn_structure_complete:
        return {
            "status": "invalid",
            "checks": checks,
            "reasons": ["turn_wire_observation_incomplete"],
        }

    checks["A_final_answers_uploaded_date"] = bool(
        turn_by_actor["A"].get("final_count") == 1
        and DATE in (turn_by_actor["A"].get("final_text") or "")
    )
    checks["B_original_input_has_no_A_evidence"] = bool(
        turn_by_actor["B"].get("content") == QUESTION_B
        and not turn_by_actor["B"].get("media")
        and b_call_inputs
        and all(NOTICE not in text and DATE not in text and upload.get("saved_path", "") not in text
                for text in b_call_inputs)
    )
    b_final = turn_by_actor["B"].get("final_text") or ""
    checks["B_final_disclaims_attachment_without_borrowing"] = bool(
        turn_by_actor["B"].get("final_count") == 1
        and ("没有收到附件" in b_final or "未收到附件" in b_final or "没有附件" in b_final)
        and DATE not in b_final
        and NOTICE not in b_final
    )
    checks["both_original_turns_have_matching_final_and_turn_end"] = all(
        turn_by_actor[actor].get("final_count") == 1
        and turn_by_actor[actor].get("turn_end_count") == 1
        and isinstance(turn_by_actor[actor].get("turn_id"), str)
        and turn_by_actor[actor].get("turn_id") == turn_by_actor[actor].get("turn_end_turn_id")
        for actor in ("A", "B")
    )
    checks["terminal_windows_complete_and_quiet"] = all(
        turn_by_actor[actor].get("terminal_observation", {}).get("complete") is True
        and turn_by_actor[actor].get("terminal_observation", {}).get("quiet") is True
        and not turn_by_actor[actor].get("terminal_observation", {}).get("unexpected_frames")
        for actor in ("A", "B")
    )

    probe_by_name = {read.get("probe"): read for read in reads if read.get("probe")}
    expected_probes = {"absolute_file", "relative_file", "chat_parent_directory"}
    checks["B_direct_paths_are_denied_without_open"] = bool(
        expected_probes.issubset(probe_by_name)
        and all(
            probe_by_name[name].get("actor") == "B"
            and probe_by_name[name].get("status") == "denied"
            and isinstance(probe_by_name[name].get("return_text"), str)
            and _is_sha256(probe_by_name[name].get("return_text_sha256"))
            and not probe_by_name[name].get("audit_open_events")
            for name in expected_probes
        )
    )
    checks["boundary_probe_media_unchanged"] = isolation.get("boundary_probe_media_unchanged") is True
    probe_rows = isolation.get("probes", [])
    checks["foreign_chat_attach_and_upload_are_denied_without_side_effects"] = bool(
        len(probe_rows) >= 4
        and all(row.get("reply", {}).get("event") == "error"
                and row.get("reply", {}).get("detail") == "chat_unauthorized"
                for row in probe_rows[:4])
        and all(row.get("inbound_queue_empty") is True for row in probe_rows[:2])
        and all(row.get("media_files_unchanged") is True for row in probe_rows[2:4])
        and isolation.get("unauthorized_message_bus_unchanged") is True
    )
    spoof = isolation.get("spoof_client_id", {})
    checks["client_id_spoof_has_no_authority"] = bool(
        spoof.get("same_chat_as_A") is False
        and spoof.get("attach_reply", {}).get("event") == "error"
        and spoof.get("attach_reply", {}).get("detail") == "chat_unauthorized"
        and spoof.get("inbound_queue_empty") is True
    )
    delivery = isolation.get("A_only_delivery", {})
    checks["A_only_delivery_does_not_reach_B"] = bool(
        delivery.get("A_received", {}).get("text") == "V065_A_ONLY_SENTINEL"
        and delivery.get("B_received") is None
        and delivery.get("B_quiet_for_ms", 0) >= 200
        and delivery.get("B_receive_observation", {}).get("complete") is True
        and delivery.get("B_receive_observation", {}).get("timed_out") is True
        and not any(row.get("purpose") == "unexpected_after_A_sentinel" for row in wire)
    )
    capability = isolation.get("valid_capability", {})
    checks["valid_chat_capability_resumes_owner"] = bool(
        capability.get("attached_reply", {}).get("event") == "attached"
        and capability.get("attached_reply", {}).get("chat_id") == a_actor.get("chat_id")
        and capability.get("inbound", {}).get("chat_id") == a_actor.get("chat_id")
        and capability.get("inbound", {}).get("content") == "V065_AUTHORIZED_CAPABILITY_CONTROL"
        and capability.get("token_present") is True
        and _is_sha256(capability.get("token_sha256"))
        and capability.get("delivery", {}).get("text") == "V065_CAPABILITY_DELIVERY_CONTROL"
    )
    b_open_events = [
        event
        for turn in turns
        for event in turn.get("audit_open_events", [])
        if event.get("actor") == "B"
    ]
    b_open_events.extend(
        event
        for read in reads
        for event in read.get("audit_open_events", [])
        if read.get("actor") == "B"
    )
    checks["no_A_media_open_under_B_scope"] = not b_open_events
    checks["scripted_provider_grounded_and_no_external_llm_sdk"] = bool(
        provider.get("implementation") == "grounded_scripted"
        and provider.get("model_requested") == MODEL
        and provider.get("chat_calls") == len(provider["calls"])
        and provider.get("chat_calls") == 3
        and provider.get("llm_calls") == 0
        and provider.get("sdk_calls") == 0
        and all(call.get("grounded") is True for call in provider["calls"])
        and len(b_call_inputs) == 1
    )
    checks["cleanup_converged"] = bool(
        cleanup.get("channel_stopped") is True
        and cleanup.get("server_task_done") is True
        and cleanup.get("pending_tasks_settled") is True
    )

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        reasons.extend("check_failed:" + name for name in failed)
        return {"status": "product_fail", "checks": checks, "reasons": reasons}
    return {"status": "pass", "checks": checks, "reasons": []}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("D", "R"), default="D")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode != "D":
        raise ValueError("unsupported_mode")
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    target = args.output.absolute()
    try:
        target.relative_to(_ROOT)
    except ValueError as exc:
        raise ValueError("output_outside_workspace") from exc
    evidence = asyncio.run(run(target, mode=args.mode))
    print(json.dumps({"status": evidence["status"], "output": str(target)}, ensure_ascii=False))
    return 0 if evidence["status"] == "pass" else 2 if evidence["status"] == "product_fail" else 3


if __name__ == "__main__":
    raise SystemExit(main())
