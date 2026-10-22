"""V063 real Slack SDK envelopes and Agent turns over an isolated local API."""

from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import json
import re
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

import aiohttp
from aiohttp import web
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from evals.common import digest
from evals.security_task_worker import _limit_provider_usage
from evals.trace import EvalHook
from evals.worker import BudgetExceeded, scripted_provider
from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.bus.queue import MessageBus
from nanobot.channels.slack import SlackChannel, SlackConfig
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.utils.observability import point, span

CONTROLS = frozenset({"wrong_answer", "cross_session", "wrong_reply_thread", "illegal_event"})
_turn = ContextVar("slack_eval_turn", default=None)
_session = ContextVar("slack_eval_session", default=None)

CHANNEL = "C063LOCAL"
USER = "U063LOCAL"
THREADS = {"A": "1700000000.000100", "B": "1700000000.000200"}
TURNS = (
    ("seed_A", "A", "1700000000.000110", "本线程代号A17"),
    ("seed_B", "B", "1700000000.000210", "本线程代号B23"),
    ("ask_B", "B", "1700000000.000220", "本线程代号是什么？"),
    ("ask_A", "A", "1700000000.000120", "本线程代号是什么？"),
)


def _history_provider(emit):
    provider = scripted_provider([{"content": "已记录。"}], emit)

    async def chat(messages, **kwargs):
        users = [message.get("content", "") for message in messages if message.get("role") == "user"]
        latest = json.dumps(users[-1], ensure_ascii=False) if users else ""
        if "本线程代号是什么？" not in latest:
            return LLMResponse(content="已记录。", finish_reason="stop")
        codes = re.findall(r"本线程代号(A17|B23)", json.dumps(users, ensure_ascii=False))
        return LLMResponse(content=codes[-1] if codes else "未获得代号。", finish_reason="stop")

    provider.chat = chat
    return provider


async def run_slack(runtime, workspace, provider, emit, control=None):
    """Run four real turns; D faults are explicit and never applied to R providers.

    The local service only supplies protocol fixtures and captures API requests.
    It never chooses an answer or repairs a channel/session/Agent result.
    """
    if control not in CONTROLS | {None}:
        raise ValueError("unknown_slack_control")
    runtime, workspace = Path(runtime), Path(workspace)
    runtime.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    if provider is None:
        provider = _history_provider(emit)
    if control is not None and getattr(provider, "observation_mode", None) != "scripted":
        raise ValueError("slack_fault_controls_require_scripted_provider")

    events, model_inputs, deliveries = [], [], []
    actual_sessions = {}
    local_threads = {key: [{"type": "message", "user": USER, "ts": timestamp, "text": ""}]
                     for key, timestamp in THREADS.items()}
    by_timestamp = {timestamp: key for key, timestamp in THREADS.items()}

    def record(kind, **payload):
        row = {"kind": kind, **payload}
        events.append(row)
        point("slack_" + kind, **payload)
        return row

    async def local_api(request):
        method = request.match_info["method"]
        if request.content_type == "application/json":
            parameters = await request.json()
        elif request.method == "GET":
            parameters = dict(request.query)
        else:
            parameters = dict(await request.post())
        current = _turn.get()
        # The local HTTP server is a separate task. Correlation comes from the
        # adapter's one active turn, never an untrusted platform field.
        if current is None:
            current = active_turn[0]
        turn_id, thread = current if current else (None, None)
        outbound_metadata = copy.deepcopy(active_outbound[0]) if active_outbound[0] is not None else None
        record("sdk_http", turn_id=turn_id, thread=thread, method=method,
               parameters=parameters, metadata=outbound_metadata,
               transport="loopback", sdk_class="slack_sdk.web.async_client.AsyncWebClient")
        if method == "conversations.replies":
            thread_key = by_timestamp.get(parameters.get("ts"))
            if parameters.get("channel") != CHANNEL or thread_key is None:
                return web.json_response({"ok": False, "error": "channel_not_found"})
            return web.json_response({"ok": True, "messages": copy.deepcopy(local_threads[thread_key]),
                                      "has_more": False})
        if method == "chat.postMessage":
            delivery = {"turn_id": turn_id, "thread": thread,
                        "metadata": outbound_metadata,
                        **{key: parameters.get(key) for key in ("channel", "thread_ts", "text")}}
            deliveries.append(delivery)
            timestamp = f"1700000010.{len(deliveries):06d}"
            destination = by_timestamp.get(parameters.get("thread_ts"))
            if destination:
                local_threads[destination].append({"type": "message", "user": "UBOT063",
                                                  "ts": timestamp, "text": parameters.get("text", "")})
            return web.json_response({"ok": True, "channel": parameters.get("channel"), "ts": timestamp,
                                      "message": {"text": parameters.get("text", "")}})
        if method in {"reactions.add", "reactions.remove"}:
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "unexpected_eval_api"})

    active_turn = [None]
    active_outbound = [None]
    app = web.Application()
    app.router.add_route("*", "/api/{method}", local_api)
    server = web.AppRunner(app, access_log=None)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    sdk_session = aiohttp.ClientSession(trust_env=False)
    sdk = AsyncWebClient(token="xoxb-SYNTHETIC-LOCAL-ONLY", base_url=f"http://127.0.0.1:{port}/api/",
                         session=sdk_session, proxy="", retry_handlers=[])
    bus = MessageBus()
    channel = SlackChannel(SlackConfig(enabled=True, group_policy="open", reply_in_thread=True,
                                      include_thread_context=True), bus)
    channel._web_client = sdk
    channel._bot_user_id = "UBOT063"

    class AckTransport:
        async def send_socket_mode_response(self, response):
            if not isinstance(response, SocketModeResponse):
                raise TypeError("expected_real_sdk_socket_response")
            record("socket_ack", response=response.to_dict(), turn_id=_turn.get()[0])

    class ThreadLoop(AgentLoop):
        def _register_default_tools(self):
            # V063 measures thread conversation routing, not tool authorization.
            pass

    sessions = SessionManager(runtime, allow_legacy=False)
    loop = ThreadLoop(bus, provider, workspace, model="deepseek-v4-flash", max_iterations=4,
                      context_window_tokens=128000, session_manager=sessions, unified_session=False,
                      tools_config=ToolsConfig(restrict_to_workspace=True), restrict_to_workspace=True,
                      hooks=[EvalHook()], disabled_skills=[path.name for path in BUILTIN_SKILLS_DIR.iterdir()
                                                          if path.is_dir()])
    original_chat, original_retry = provider.chat, provider.chat_with_retry
    _limit_provider_usage(provider)
    limited_chat = provider.chat
    requests = 0

    async def observed_chat(messages, **kwargs):
        nonlocal requests
        if requests >= 8:
            point("budget", reason="slack_model_attempt_limit")
            raise BudgetExceeded()
        requests += 1
        current = _turn.get()
        row = {"turn_id": current[0], "thread": current[1], "session_key": _session.get(),
               "messages": copy.deepcopy(messages)}
        model_inputs.append(row)
        point("slack_model_input", **row)
        response = await limited_chat(messages, **kwargs)
        if control == "wrong_answer" and current[0].startswith("ask_"):
            point("fault_injection", boundary="provider_response", fault_id=control, triggered=True)
            return replace(response, content="A17")
        return response

    provider.chat = observed_chat

    def memory_snapshot():
        result = []
        for path in (loop.context.memory.memory_file, loop.context.memory.history_file,
                     loop.context.memory.legacy_history_file):
            raw = path.read_bytes() if path.exists() else None
            result.append({"relative_path": path.relative_to(workspace).as_posix(), "exists": raw is not None,
                           "byte_length": len(raw) if raw is not None else 0,
                           "sha256": digest(raw) if raw is not None else None,
                           "empty": raw is None or not raw.strip()})
        return result

    try:
        memory_before = memory_snapshot()
        point("slack_memory_state", moment="before", files=memory_before)
        point("tool_inventory", tools=[], scope="slack_thread_conversation")
        for turn_id, thread, timestamp, text in TURNS:
            turn_token = _turn.set((turn_id, thread))
            active_turn[0] = (turn_id, thread)
            session_token = None
            try:
                with span("slack_turn", turn_id=turn_id, thread=thread, entry="SlackChannel._on_socket_request"):
                    event = {"type": "message", "user": USER, "channel": CHANNEL, "channel_type": "channel",
                             "text": text, "ts": timestamp, "event_ts": timestamp, "thread_ts": THREADS[thread]}
                    if control == "illegal_event":
                        event["subtype"] = "message_changed"
                    local_threads[thread].append(copy.deepcopy(event))
                    payload = {"type": "event_callback", "team_id": "T063LOCAL", "event_id": "E063" + turn_id,
                               "event": event}
                    envelope = {"type": "events_api", "envelope_id": "env-063-" + turn_id, "payload": payload}
                    request = SocketModeRequest(**envelope)
                    record("socket_request", turn_id=turn_id, thread=thread, envelope=copy.deepcopy(envelope),
                           sdk_class="slack_sdk.socket_mode.request.SocketModeRequest")
                    await channel._on_socket_request(AckTransport(), request)
                    if bus.inbound.empty():
                        continue
                    inbound = bus.inbound.get_nowait()
                    if control == "cross_session":
                        point("fault_injection", boundary="inbound_session_override", fault_id=control, triggered=True)
                        inbound = replace(inbound, session_key_override=None)
                    actual_sessions[thread] = inbound.session_key
                    record("inbound", turn_id=turn_id, thread=thread, content=inbound.content,
                           session_key=inbound.session_key, metadata=copy.deepcopy(inbound.metadata),
                           channel=inbound.channel, sender_id=inbound.sender_id)
                    session_token = _session.set(inbound.session_key)
                    await loop._dispatch(inbound)
                    while not bus.outbound.empty():
                        outbound = bus.outbound.get_nowait()
                        if control == "wrong_reply_thread" and turn_id.startswith("ask_"):
                            metadata = copy.deepcopy(outbound.metadata)
                            metadata["slack"]["thread_ts"] = THREADS["B" if thread == "A" else "A"]
                            point("fault_injection", boundary="product_outbound_thread", fault_id=control, triggered=True)
                            outbound = replace(outbound, metadata=metadata)
                        active_outbound[0] = copy.deepcopy(outbound.metadata)
                        try:
                            await channel.send(outbound)
                        finally:
                            active_outbound[0] = None
                    pending = [task for task in loop._background_tasks if not task.done()]
                    if pending:
                        await asyncio.gather(*pending)
            finally:
                if session_token is not None:
                    _session.reset(session_token)
                _turn.reset(turn_token)
                active_turn[0] = None
        actors, session_state = {}, {}
        for thread in THREADS:
            key = actual_sessions.get(thread)
            stored = sessions.read_session_file(key) if key else None
            path = sessions._get_session_path(key) if key else None
            actors[thread] = {"channel": CHANNEL, "user": USER, "thread_ts": THREADS[thread], "session_key": key}
            session_state[thread] = {"session_key": key, "messages": stored["messages"] if stored else [],
                                     "metadata": stored["metadata"] if stored else {},
                                     "relative_path": path.relative_to(runtime).as_posix() if path else None,
                                     "sha256": digest(path.read_bytes()) if path and path.exists() else None}
        memory_after = memory_snapshot()
        point("slack_memory_state", moment="after", files=memory_after)
        point("settlement", status="settled", coverage_complete=True,
              background_pending=sum(not task.done() for task in loop._background_tasks),
              running_subtasks=loop.subagents.get_running_count(), incoming_queue=bus.inbound.qsize(),
              outgoing_queue=bus.outbound.qsize())
        return {"events": events, "actors": actors, "model_inputs": model_inputs,
                "session_state": session_state, "deliveries": deliveries,
                "memory_state": {"before": memory_before, "after": memory_after},
                "SDK_version": importlib.metadata.version("slack-sdk"),
                "slackify_version": importlib.metadata.version("slackify-markdown"),
                "control": control, "evidence_scope": "deterministic_fault_control" if control else "full_task",
                "platform_scope": {"real_platform_connected": False, "sdk_http_transport": "loopback", "tools": [],
                                   "socket_handshake": "not_covered", "real_platform_delivery": "not_covered"}}
    finally:
        provider.chat, provider.chat_with_retry = original_chat, original_retry
        await sdk_session.close()
        await server.cleanup()
