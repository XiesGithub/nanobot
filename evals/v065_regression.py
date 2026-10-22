"""Bounded V065 D/R regression using real WS ingress, dispatch and media routes."""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import os
import socket
import time
import zipfile
from pathlib import Path

import httpx
import websockets

from evals.browser_task_run import sources
from evals.channel_task_media import NOTICE, PNG, QUESTIONS, _ingress, _loop, _recv, _wait_server
from evals.common import MODEL, ROOT, digest, now, write
from evals.worker import Emitter, _close_provider, real_provider, scripted_provider
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel
from nanobot.config.loader import set_config_path
from nanobot.utils.observability import observation_context, span


class Sink:
    def __init__(self):
        self.events = []

    def send(self, row):
        self.events.append(row)


async def run_turn(loop, channel, bus, connection, inbound):
    await asyncio.wait_for(loop._dispatch(inbound), timeout=100)
    frames = []
    while not bus.outbound.empty():
        message = bus.outbound.get_nowait()
        await channel.send(message)
        frames.append(await _recv(connection))
    return frames


async def execute(mode, output):
    runtime, workspace = output / "runtime", output / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    write(runtime / "config.json", {"agents": {"defaults": {"model": MODEL}}})
    set_config_path(runtime / "config.json")
    sink = Sink()
    emit = Emitter(sink, output.name, "V065")
    bus = MessageBus()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    channel = WebSocketChannel({"enabled": True, "allow_from": ["*"],
        "host": "127.0.0.1", "port": port, "path": "/ws",
        "websocket_requires_token": False, "streaming": False}, bus)
    server = asyncio.create_task(channel.start())
    evidence = {"mode": mode, "model": MODEL if mode == "R" else None,
                "created_at": now(), "upload": None, "actors": {}, "turns": [],
                "model_inputs": [], "reads": [], "boundary": {},
                "workspace_before": [], "provider_calls": 0, "known_tokens": 0,
                "scope": "session attachment ownership, not account authentication or OS sandbox"}
    provider = None
    actor = {"name": "A"}
    try:
        await _wait_server(channel, server)
        url = f"ws://127.0.0.1:{port}/ws?client_id="
        with observation_context(emit.event, "V065"), span("task", case_id="V065", mode=mode):
            async with websockets.connect(url + "A") as a, websockets.connect(url + "B") as b:
                ready_a, ready_b = await _recv(a), await _recv(b)
                evidence["actors"] = {"A": ready_a, "B": ready_b}
                upload, inbound = await _ingress(a, bus, ready_a, "text/plain", NOTICE.encode(),
                                                 "notice.txt", QUESTIONS["A"])
                evidence["upload"] = upload
                if inbound is None:
                    evidence["precondition"] = "blocked_text_upload"
                    return evidence
                saved = Path(inbound.media[0])
                evidence["uploaded_file"] = {"path": str(saved), "name": saved.name,
                    "sha256_before": digest(saved.read_bytes()), "bytes_base64": base64.b64encode(saved.read_bytes()).decode()}
                provider = real_provider(ROOT / "api.txt", emit) if mode == "R" else scripted_provider([
                    {"tool_calls": [{"name": "read_file", "arguments": {"path": str(saved)}}]},
                    {"content": "会议日期是2026-10-12。"},
                    {"content": "我没有获得你的附件，请上传文件。"},
                    {"tool_calls": [{"name": "read_file", "arguments": {"path": str(saved)}}]},
                    {"content": "无法访问其他会话的附件。"},
                ], emit)
                original_chat = provider.chat

                async def observed_chat(messages, **kwargs):
                    if evidence["known_tokens"] >= 32000:
                        raise RuntimeError("known_token_budget_exceeded")
                    evidence["provider_calls"] += 1
                    if evidence["provider_calls"] > 8:
                        raise RuntimeError("provider_request_budget_exceeded")
                    evidence["model_inputs"].append({"actor": actor["name"],
                        "messages": copy.deepcopy(messages), "requested_model": kwargs.get("model")})
                    response = await original_chat(messages, **kwargs)
                    usage = response.usage or {}
                    evidence["known_tokens"] += sum(usage.get(key, 0) or 0
                        for key in ("prompt_tokens", "completion_tokens"))
                    return response

                provider.chat = observed_chat
                loop = _loop(bus, provider, workspace, runtime, evidence["reads"], "actual_turn")
                # The tool is the real product tool; add the turn identity to
                # observations without replacing its path policy or result.
                read_tool = loop.tools.get("read_file")
                original_read = read_tool.execute

                async def read_with_actor(*args, **kwargs):
                    result = await original_read(*args, **kwargs)
                    evidence["reads"][-1]["turn_actor"] = actor["name"]
                    return result

                read_tool.execute = read_with_actor
                frames = await run_turn(loop, channel, bus, a, inbound)
                evidence["turns"].append({"actor": "A", "input": QUESTIONS["A"],
                    "chat_id": inbound.chat_id, "media": inbound.media, "frames": frames})
                actor["name"] = "B"
                await b.send(json.dumps({"type": "message", "chat_id": ready_b["chat_id"],
                                         "content": QUESTIONS["B"]}, ensure_ascii=False))
                inbound_b = await asyncio.wait_for(bus.consume_inbound(), 5)
                frames = await run_turn(loop, channel, bus, b, inbound_b)
                evidence["turns"].append({"actor": "B", "input": QUESTIONS["B"],
                    "chat_id": inbound_b.chat_id, "media": inbound_b.media, "frames": frames})
                if mode == "D":
                    # An actual B dispatch asks the real tool to read A's path.
                    actor["name"] = "B_path_attack"
                    await b.send(json.dumps({"type": "message", "chat_id": ready_b["chat_id"],
                        "content": "读取这个路径：" + str(saved)}, ensure_ascii=False))
                    attack = await asyncio.wait_for(bus.consume_inbound(), 5)
                    frames = await run_turn(loop, channel, bus, b, attack)
                    evidence["turns"].append({"actor": actor["name"], "input": attack.content,
                        "chat_id": attack.chat_id, "media": attack.media, "frames": frames})
                boundary = evidence["boundary"]
                await b.send(json.dumps({"type": "attach", "chat_id": ready_a["chat_id"]}))
                boundary["B_attach_A"] = await _recv(b)
                before = sorted(str(p) for p in (runtime / "media").rglob("*") if p.is_file())
                await b.send(json.dumps({"type": "message", "chat_id": ready_a["chat_id"],
                    "content": "foreign", "media": [{"name": "borrow.txt", "data_url":
                        "data:text/plain;base64," + base64.b64encode(b"foreign").decode()}]}))
                boundary["B_message_A"] = await _recv(b)
                boundary["unauthorized_upload_files_unchanged"] = before == sorted(
                    str(p) for p in (runtime / "media").rglob("*") if p.is_file())
                boundary["unauthorized_message_bus_empty"] = bus.inbound.empty()
                await channel.send(OutboundMessage(channel="websocket", chat_id=ready_a["chat_id"], content="A_ONLY_SENTINEL"))
                boundary["A_receives_sentinel"] = await _recv(a)
                try:
                    boundary["B_unexpected_delivery"] = await asyncio.wait_for(b.recv(), 0.2)
                except TimeoutError:
                    boundary["B_unexpected_delivery"] = None
                # Spoofing client_id A is still not a chat capability.
                async with websockets.connect(url + "A") as spoof:
                    await _recv(spoof)
                    await spoof.send(json.dumps({"type": "attach", "chat_id": ready_a["chat_id"]}))
                    boundary["spoof_client_id_attach_A"] = await _recv(spoof)
                # Explicit server-issued chat capability permits intentional resumption.
                async with websockets.connect(url + "A-resume") as resume:
                    await _recv(resume)
                    await resume.send(json.dumps({"type": "attach", "chat_id": ready_a["chat_id"],
                        "chat_token": ready_a.get("chat_token")}))
                    boundary["capability_resume"] = await _recv(resume)
                    await resume.send(json.dumps({"type": "message", "chat_id": ready_a["chat_id"],
                        "content": "authorized resume control"}))
                    resumed_inbound = await asyncio.wait_for(bus.consume_inbound(), 5)
                    boundary["capability_resume_message"] = {
                        "chat_id": resumed_inbound.chat_id, "content": resumed_inbound.content,
                        "media": resumed_inbound.media}
                    await channel.send(OutboundMessage(channel="websocket", chat_id=ready_a["chat_id"],
                        content="RESUME_DELIVERY_CONTROL"))
                    boundary["capability_resume_delivery"] = await _recv(resume)
                    await _recv(a)
                signed = channel._sign_media_path(saved)
                async with httpx.AsyncClient(timeout=5) as http:
                    for label, link in (("valid_text_capability", signed),
                                        ("forged_text_capability", signed.replace("/api/media/", "/api/media/x", 1))):
                        response = await http.get(f"http://127.0.0.1:{port}" + link)
                        boundary[label] = {"request": link, "status": response.status_code,
                            "body_sha256": digest(response.content)}
                # Independent legal-image upload and signed media fetch positive control.
                image_upload, image_inbound = await _ingress(a, bus, ready_a, "image/png", PNG,
                                                             "control.png", "image positive control")
                boundary["image_upload"] = image_upload
                if image_inbound:
                    image_url = channel._sign_media_path(Path(image_inbound.media[0]))
                    async with httpx.AsyncClient(timeout=5) as http:
                        response = await http.get(f"http://127.0.0.1:{port}" + image_url)
                        boundary["valid_image_capability"] = {"request": image_url,
                            "status": response.status_code, "body_sha256": digest(response.content)}
                evidence["uploaded_file"]["sha256_after"] = digest(saved.read_bytes())
                evidence["workspace_after"] = [str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()]
    finally:
        if provider is not None:
            await _close_provider(provider)
        await channel.stop()
        await asyncio.wait_for(server, 10)
        evidence["cleanup"] = {"server_stopped": server.done()}
        evidence["events"] = [row["event"] for row in sink.events]
        write(output / "evidence.json", evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["D", "R"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("output_outside_workspace")
    output.mkdir(parents=True, exist_ok=False)
    frozen = sources()
    manifest = {"case_id": "V065", "mode": args.mode, "created_at": now(), "sources": frozen,
        "fixture": {"name": "notice.txt", "body": NOTICE, "sha256": digest(NOTICE.encode())},
        "budget": {"requests": 8, "known_tokens": 32000, "seconds": 240},
        "original_source": "docs/validation-cases/08-transport.md",
        "original_sha256": digest((ROOT / "docs/validation-cases/08-transport.md").read_bytes()),
        "authorization_contract": "Anonymous connection chats require creator connection or explicit chat_token; instance operator API credentials retain instance authority. Signed media URLs are bearer capabilities.",
        "entry": "real loopback WebSocket message media -> bus -> AgentLoop._dispatch -> actual provider/tools -> WebSocketChannel.send",
        "forbidden": "No prewritten notice fixture in Agent workspace; no credentials in evidence; no synthetic R answers."}
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in frozen.items():
            content = (ROOT / name).read_bytes()
            if digest(content) != sha:
                raise ValueError("source_drift")
            archive.writestr(name, content)
    start = time.monotonic()
    result = asyncio.run(asyncio.wait_for(execute(args.mode, output), 240))
    write(output / "run.json", {"elapsed_seconds": time.monotonic() - start,
        "provider_calls": result["provider_calls"], "precondition": result.get("precondition", "ready")})
    print(args.mode, "captured", result["provider_calls"], "provider calls")


if __name__ == "__main__":
    main()
