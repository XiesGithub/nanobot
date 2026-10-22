"""Real loopback V065 media ingress and separately labelled D boundary probes.

No media authorization is added here. The faithful text upload records the
product's MIME rejection before any Agent/model call. Accepted image controls
use actual saved bytes, the real filesystem tool and the real WS/HTTP routes.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import socket
from pathlib import Path

import httpx
import websockets

from evals.common import digest
from evals.worker import scripted_provider
from nanobot.utils.observability import point, span

NOTICE = "会议日期2026-10-12"
QUESTIONS = {"A": "我上传文件中的会议日期是什么？", "B": "我刚上传文件的会议日期是什么？"}
CONTROLS = frozenset({"image_ingress", "borrow_path", "borrow_signed_url", "borrow_chat", "api_text_control", "raw_post"})
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII="
)


def _files(root):
    return {path.relative_to(root).as_posix(): {"sha256": digest(path.read_bytes()), "size": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file()} if root.exists() else {}


async def _wait_server(channel, task):
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
    raise TimeoutError("media_loopback_server_not_ready")


async def _recv(connection):
    return json.loads(await asyncio.wait_for(connection.recv(), 5))


async def _ingress(connection, bus, actor, mime, body, name, content):
    envelope = {"type": "message", "chat_id": actor["chat_id"], "content": content,
                "media": [{"name": name, "data_url": "data:" + mime + ";base64," + base64.b64encode(body).decode()}]}
    await connection.send(json.dumps(envelope, ensure_ascii=False))
    wire = asyncio.create_task(_recv(connection))
    inbound = asyncio.create_task(bus.consume_inbound())
    try:
        done, _ = await asyncio.wait((wire, inbound), timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError("media_ingress_not_observed")
        message = inbound.result() if inbound in done else None
        response = wire.result() if wire in done else None
        row = {"actor": "A", "request": envelope, "response": response, "mime": mime,
               "desired_filename": name, "decoded_sha256": digest(body),
               "stored_paths": list(message.media) if message else [],
               "stored_filenames": [Path(path).name for path in message.media] if message else [],
               "inbound_count": int(message is not None),
               "actual_inbound": {"channel": message.channel, "chat_id": message.chat_id,
                                  "sender_id": message.sender_id, "session_key": message.session_key,
                                  "content": message.content, "media": message.media} if message else None}
        point("channel_media_ingress", **row)
        return row, message
    finally:
        for pending in (wire, inbound):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(wire, inbound, return_exceptions=True)


def _observe_provider(provider, actor, model_inputs, attempts):
    original = provider.chat

    async def chat(messages, **kwargs):
        attempts["model_requests"] += 1
        if attempts["model_requests"] > 4:
            raise RuntimeError("media_diagnostic_request_budget_exhausted")
        row = {"actor": actor, "messages": copy.deepcopy(messages), "model": kwargs.get("model")}
        model_inputs.append(row)
        point("channel_media_model_input", **row)
        return await original(messages, **kwargs)

    provider.chat = chat
    return original


def _loop(bus, provider, workspace, runtime, actual_read, actor):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.config.schema import ToolsConfig
    from nanobot.session.manager import SessionManager

    class ReadOnlyLoop(AgentLoop):
        def _register_default_tools(self):
            tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
            original = tool.execute

            async def execute(*args, **kwargs):
                path = kwargs.get("path", args[0] if args else None)
                result = await original(*args, **kwargs)
                resolved = Path(path).resolve() if path else None
                row = {"actor": actor, "path": str(resolved) if resolved else None,
                       "status": "error" if isinstance(result, str) and result.startswith("Error") else "ok",
                       "file_sha256": digest(resolved.read_bytes()) if resolved and resolved.is_file() else None,
                       "result": result}
                actual_read.append(row)
                point("channel_media_actual_read", **row)
                return result

            tool.execute = execute
            self.tools.register(tool)

    return ReadOnlyLoop(bus, provider, workspace, model="deepseek-v4-flash", max_iterations=4,
                        session_manager=SessionManager(runtime, allow_legacy=False),
                        tools_config=ToolsConfig(restrict_to_workspace=True), restrict_to_workspace=True,
                        unified_session=False,
                        disabled_skills=[path.name for path in BUILTIN_SKILLS_DIR.iterdir() if path.is_dir()])


async def _agent_read(channel, connection, bus, actor, path, workspace, runtime, emit,
                      provider, model_inputs, actual_read, attempts):
    if provider is None:
        provider = scripted_provider([
            {"tool_calls": [{"name": "read_file", "arguments": {"path": path}}]},
            {"content": "已读取隔离测试媒体。"},
        ], emit)
    if getattr(provider, "observation_mode", None) != "scripted":
        raise ValueError("media_diagnostic_requires_scripted_provider")
    original = _observe_provider(provider, "B", model_inputs, attempts)
    try:
        loop = _loop(bus, provider, workspace, runtime, actual_read, "B")
        request = {"type": "message", "chat_id": actor["chat_id"],
                   "content": "请读取这个隔离测试媒体路径：" + path}
        await connection.send(json.dumps(request, ensure_ascii=False))
        inbound = await asyncio.wait_for(bus.consume_inbound(), 5)
        point("channel_media_task_input", actor="B", request=request, media=inbound.media,
              session_key=inbound.session_key)
        with span("channel_media_agent_entry", actor="B", session_key=inbound.session_key):
            await loop._dispatch(inbound)
        frames = []
        while not bus.outbound.empty():
            outbound = bus.outbound.get_nowait()
            await channel.send(outbound)
            frames.append(await _recv(connection))
        return {"actor": "B", "chat_id": actor["chat_id"], "session_key": inbound.session_key,
                "frames": frames, "request": request, "input_media": inbound.media}
    finally:
        provider.chat = original


async def _api_control(workspace, runtime, bus, emit, model_inputs, actual_read, attempts):
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    from nanobot.api.server import create_app
    provider = scripted_provider([{"content": "会议日期是2026-10-12。"}], emit)
    _observe_provider(provider, "A", model_inputs, attempts)
    loop = _loop(bus, provider, workspace, runtime, actual_read, "A")
    original = loop.process_direct
    observed = []

    async def process_direct(content, **kwargs):
        observed.append({"content": content, **kwargs})
        return await original(content, **kwargs)

    loop.process_direct = process_direct
    client = TestClient(TestServer(create_app(loop, model_name="deepseek-v4-flash")))
    await client.start_server()
    try:
        data = FormData()
        data.add_field("message", QUESTIONS["A"])
        data.add_field("session_id", "V065-API-control")
        data.add_field("files", NOTICE.encode(), filename="notice.txt", content_type="text/plain")
        with span("channel_media_api_control", route="POST /v1/chat/completions"):
            response = await client.post("/v1/chat/completions", data=data)
            raw = await response.text()
        call = observed[0] if len(observed) == 1 else {}
        path = Path(call["media"][0]) if call.get("media") else None
        return {"name": "api_multipart_text", "route": "POST /v1/chat/completions", "status": response.status,
                "channel": call.get("channel"), "session_key": call.get("session_key"),
                "actual_agent_dispatch": bool(observed), "file_path": str(path) if path else None,
                "file_sha256": digest(path.read_bytes()) if path and path.is_file() else None,
                "response_wire": raw, "source_provenance": "api_only_control"}
    finally:
        await client.close()


async def _raw_post(port):
    writer = None
    body = NOTICE.encode()
    row = {"name": "raw_post_media", "route": "POST /api/media", "actual_request_sent": False,
           "stored_paths": [], "status": None, "error_kind": None, "response_wire": None}
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        request = (f"POST /api/media HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
        writer.write(request)
        await writer.drain()
        row["actual_request_sent"] = True
        raw = await asyncio.wait_for(reader.read(65536), 5)
        row["response_wire"] = raw.decode("utf-8", errors="replace")
        if raw.startswith(b"HTTP/"):
            row["status"] = int(raw.split(b" ", 2)[1])
        if not raw:
            row["error_kind"] = "EOFWithoutResponse"
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        row["error_kind"] = type(error).__name__
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
    return row


async def run_media(runtime, workspace, provider, emit, control=None):
    """Observe real product results; no side control satisfies the original task."""
    if control is not None and control not in CONTROLS:
        raise ValueError("unsupported_media_control")
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket import WebSocketChannel
    from nanobot.config.loader import get_config_path, set_config_path

    runtime, workspace = Path(runtime).resolve(), Path(workspace).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    fixture = workspace / "notice.txt"
    if fixture.exists():
        if fixture.read_bytes() != NOTICE.encode():
            raise ValueError("media_fixture_content_mismatch")
    else:
        with fixture.open("xb") as stream:
            stream.write(NOTICE.encode())
    fixture_before = {"path": "notice.txt", "sha256": digest(fixture.read_bytes()), "size": fixture.stat().st_size}
    prior_config = get_config_path()
    set_config_path(runtime / "config.json")
    bus = MessageBus()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    channel = WebSocketChannel({"enabled": True, "allow_from": ["V065-A", "V065-B"],
        "host": "127.0.0.1", "port": port, "path": "/ws", "websocket_requires_token": False}, bus)
    server = asyncio.create_task(channel.start())
    media = runtime / "media"
    result = {"case_id": "V065", "control": control, "questions": dict(QUESTIONS), "upload_wire": [],
              "actors": {}, "model_inputs": [], "actual_read": [], "deliveries": [], "boundary_results": [],
              "files_before": {}, "files_after": {}, "source_fixture_before": fixture_before,
              "attempts": {"model_requests": 0, "paid_requests": 0}, "capability_gap": None,
              "coverage_scope": "api_only_control" if control == "api_text_control" else
                  "original_task" if control is None else "diagnostic_boundary",
              "precondition_ready": False, "capability_status": "diagnostic_only"}
    try:
        await _wait_server(channel, server)
        url = f"ws://127.0.0.1:{port}/ws?client_id="
        async with websockets.connect(url + "V065-A") as a, websockets.connect(url + "V065-B") as b:
            ready_a, ready_b = await _recv(a), await _recv(b)
            for role, ready in (("A", ready_a), ("B", ready_b)):
                result["actors"][role] = {"client_id": ready["client_id"], "chat_id": ready["chat_id"],
                    "session_key": "websocket:" + ready["chat_id"], "instance_id": digest(str(runtime).encode()),
                    "server_generated_chat": ready.get("event") == "ready"}
            point("channel_media_actors", actors=result["actors"])
            if control == "raw_post":
                result["boundary_results"].append(await _raw_post(port))
            elif control == "api_text_control":
                row = await _api_control(workspace, runtime, bus, emit, result["model_inputs"],
                                         result["actual_read"], result["attempts"])
                result["boundary_results"].append(row)
            else:
                faithful = control is None
                mime, body, name = ("text/plain", fixture.read_bytes(), "notice.txt") if faithful else ("image/png", PNG, "control.png")
                upload, inbound = await _ingress(a, bus, result["actors"]["A"], mime, body, name, QUESTIONS["A"])
                result["upload_wire"].append(upload)
                result["files_before"] = _files(media)
                result["precondition_ready"] = inbound is not None
                if faithful:
                    if inbound is not None:
                        result["capability_status"] = "faithful_task_not_executed"
                        result["capability_gap"] = {"kind": "adapter_execution_missing", "entry": "AgentLoop._dispatch"}
                    else:
                        result["capability_status"] = "product_unsupported_mime"
                        result["capability_gap"] = {"kind": "product_capability_failure", "entry": "WebSocketChannel._save_envelope_media"}
                    point("channel_media_precondition", ready=result["precondition_ready"],
                          capability_status=result["capability_status"], upload_wire=upload, planned_paid_requests=0)
                elif inbound is None:
                    result["capability_gap"] = {"kind": "image_ingress_control_failed", "entry": "WebSocketChannel._save_envelope_media"}
                elif control == "borrow_path":
                    path = inbound.media[0]
                    delivery = await _agent_read(channel, b, bus, result["actors"]["B"], path, workspace,
                        runtime, emit, provider, result["model_inputs"], result["actual_read"], result["attempts"])
                    result["deliveries"].append(delivery)
                    allowed = any(row["actor"] == "B" and row["status"] == "ok" for row in result["actual_read"])
                    result["boundary_results"].append({"name": "borrow_path_B", "actor": "B", "attempted": True,
                        "allowed": allowed, "denied": not allowed, "source_provenance": "image_ingress_control", "path": path})
                    result["capability_status"] = "product_cross_session_media_access" if allowed else "diagnostic_boundary_denied"
                elif control == "borrow_signed_url":
                    signed = channel._sign_media_path(Path(inbound.media[0]))
                    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                        response = await client.get(f"http://127.0.0.1:{port}" + signed)
                    allowed = response.status_code == 200
                    result["boundary_results"].append({"name": "borrow_signed_url_B", "actor": "B", "attempted": True,
                        "allowed": allowed, "denied": not allowed, "status": response.status_code,
                        "source_provenance": "image_ingress_control", "request_ref": signed,
                        "wire_body_base64": base64.b64encode(response.content).decode(), "body_sha256": digest(response.content),
                        "headers": dict(response.headers)})
                    result["capability_status"] = "product_cross_session_media_access" if allowed else "diagnostic_boundary_denied"
                elif control == "borrow_chat":
                    chat = result["actors"]["A"]["chat_id"]
                    await b.send(json.dumps({"type": "attach", "chat_id": chat}))
                    attached = await _recv(b)
                    await channel.send(OutboundMessage("websocket", chat, "CHANNEL-A-CONTROL"))
                    frame_a, frame_b = await _recv(a), await _recv(b)
                    allowed = frame_b.get("chat_id") == chat and frame_b.get("text") == "CHANNEL-A-CONTROL"
                    result["boundary_results"].append({"name": "borrow_chat_B", "actor": "B", "attempted": True,
                        "allowed": allowed, "denied": not allowed, "requested_chat_id": chat,
                        "attached_response": attached, "a_received_frames": [frame_a], "b_received_a_frames": [frame_b]})
                    result["capability_status"] = "product_cross_session_chat_attach" if allowed else "diagnostic_boundary_denied"
                else:
                    result["capability_status"] = "image_ingress_observed"
            result["files_after"] = _files(media)
            result["source_fixture_after"] = {"path": "notice.txt", "sha256": digest(fixture.read_bytes()), "size": fixture.stat().st_size}
            for row in result["boundary_results"]:
                point("channel_media_boundary", **row)
            point("channel_media_result", **result)
            return result
    finally:
        await channel.stop()
        try:
            await server
        finally:
            set_config_path(prior_config)
