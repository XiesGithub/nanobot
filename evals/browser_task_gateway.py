"""Loopback-only WebUI evaluation gateway backed by the real agent and WS channel.

The harness deliberately keeps browser traffic, Agent events, model accounting and
filesystem state in one observation object. It never substitutes a UI receipt for
the underlying tool or file result.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest

MODEL = "deepseek-v4-flash"
V079_OWNER_LOGIN = "SYNTHETIC-V079-OWNER-LOGIN"


@tool_parameters(tool_parameters_schema(
    gate_id=StringSchema("The named evaluation synchronization gate"),
    required=["gate_id"],
))
class EvaluationGateTool(Tool):
    """A real cancellable tool call held until the loopback evaluator releases it."""

    def __init__(self, gateway):
        self.gateway = gateway

    @property
    def name(self) -> str:
        return "evaluation_gate"

    @property
    def description(self) -> str:
        return "Wait at the named evaluation synchronization gate until it is released."

    async def execute(self, gate_id: str, **_kwargs):
        gate = self.gateway.gates.setdefault(gate_id, {"event": asyncio.Event(), "state": "created",
                                                         "entries": 0, "cancellations": 0})
        gate["state"] = "waiting"
        gate["entries"] += 1
        event = {"gate_id": gate_id, "state": "waiting", "entry": gate["entries"]}
        self.gateway.observation["gates"].append(event)
        if self.gateway.emit:
            self.gateway.emit.event("browser_task_gate", **event)
        try:
            await gate["event"].wait()
        except asyncio.CancelledError:
            gate["state"] = "cancelled"
            gate["cancellations"] += 1
            self.gateway.observation["gates"].append({"gate_id": gate_id, "state": "cancelled"})
            if self.gateway.emit:
                self.gateway.emit.event("browser_task_gate", gate_id=gate_id, state="cancelled")
            raise
        gate["state"] = "released"
        if self.gateway.emit:
            self.gateway.emit.event("browser_task_gate", gate_id=gate_id, state="released")
        return f"Evaluation gate {gate_id} released."


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _emit_operation_event(emit: Any, kind: str, operation_phase: str, **fields: Any) -> None:
    """Emit a point event; tool operation phases are payload, never span boundaries."""
    if emit:
        emit.event(kind, operation_phase=operation_phase, **fields)


def _message_evidence(messages: Any) -> list[dict[str, Any]]:
    result = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        row = {key: message[key] for key in ("role", "tool_call_id", "name", "content") if key in message}
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            row["tool_calls"] = [
                {"id": call.get("id"),
                 "name": call.get("function", {}).get("name") if isinstance(call.get("function"), dict)
                 else call.get("name")}
                for call in calls if isinstance(call, dict)
            ]
        result.append(BrowserTaskGateway._redact_mapping(row))
    return result


def _instrument_standard_browser_provider(provider: LLMProvider, emit: Any,
                                          request_limit=8, token_limit=32000) -> None:
    """Record bounded non-streaming SDK attempts for protected mail turns."""
    from evals.worker import BudgetExceeded

    records: list[dict[str, Any]] = []
    ledger = {
        "requests": 0,
        "known_tokens": 0,
        "request_limit": request_limit,
        "token_limit": token_limit,
        "provider_closed": False,
    }
    provider._browser_request_records = records
    provider._browser_ledger = ledger
    provider._browser_ledger_kind = "sdk_attempts"
    provider._browser_model_events = []
    provider.observation_mode = "real_nonstream_protected_mail"

    original = provider._client.chat.completions.create

    async def counted_create(**kwargs):
        if ledger["requests"] >= ledger["request_limit"] or ledger["known_tokens"] >= ledger["token_limit"]:
            if emit:
                emit.event("budget", reason="browser_request_or_token_limit",
                           requests=ledger["requests"], known_tokens=ledger["known_tokens"])
            raise BudgetExceeded()

        ledger["requests"] += 1
        row = {
            "request_index": ledger["requests"],
            "purpose": "protected_mail_sdk_request",
            "requested_model": kwargs.get("model"),
            "streaming": kwargs.get("stream") is True,
            "messages": _message_evidence(kwargs.get("messages")),
            "status": "dispatched",
            "terminal": False,
        }
        records.append(row)
        try:
            result = await original(**kwargs)
        except BaseException as exc:
            row.update({"status": "error", "terminal": True,
                        "error_kind": type(exc).__name__})
            provider._browser_model_events.append({
                "request_index": row["request_index"], "requested_model": row["requested_model"],
                "streaming": row["streaming"], "status": "error",
                "error_kind": type(exc).__name__, "sdk_retries": 0,
            })
            raise

        usage = getattr(result, "usage", None)
        token_values = {
            name: getattr(usage, name, None) if usage is not None else None
            for name in ("prompt_tokens", "completion_tokens")
        }
        valid = all(type(value) is int and value >= 0 for value in token_values.values())
        row.update({
            "status": "complete",
            "terminal": True,
            "actual_model": getattr(result, "model", None),
            "usage": token_values,
            "usage_status": "known" if valid else "unknown",
        })
        if valid:
            ledger["known_tokens"] += sum(token_values.values())
            row["token_budget_overrun"] = ledger["known_tokens"] > ledger["token_limit"]
        provider._browser_model_events.append({
            "request_index": row["request_index"], "requested_model": row["requested_model"],
            "actual_model": row["actual_model"], "streaming": row["streaming"],
            "status": "complete", "sdk_retries": 0,
            "usage": token_values, "usage_status": row["usage_status"],
        })
        return result

    provider._client.chat.completions.create = counted_create


def _provider_ledger_summary(provider: LLMProvider) -> dict[str, Any] | None:
    if getattr(provider, "_browser_ledger_kind", None) != "sdk_attempts":
        return None
    records = provider._browser_request_records
    return {
        **provider._browser_ledger,
        "completed": sum(row["status"] == "complete" for row in records),
        "errors": sum(row["status"] == "error" for row in records),
        "pending": sum(not row["terminal"] for row in records),
        "usage_complete": bool(records)
        and not any(not row["terminal"] or row["status"] == "error" for row in records)
        and all(row["usage_status"] == "known" for row in records
                if row["status"] == "complete"),
        "token_budget_overrun": any(row.get("token_budget_overrun", False) for row in records),
    }


class ScriptedBrowserProvider(LLMProvider):
    """Deterministic D provider that still executes tools through AgentRunner."""

    observation_mode = "scripted"
    supports_progress_deltas = True

    def __init__(self, case_id: str, control: dict[str, Any] | None = None):
        super().__init__()
        self.case_id = case_id
        self.control = control or {}
        self.generation = GenerationSettings(temperature=0, max_tokens=256)
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, messages, **kwargs) -> LLMResponse:
        title_call = any("Generate a concise title for this chat" in str(message.get("content", ""))
                         for message in messages if isinstance(message, dict))
        self.calls.append({"request_index": len(self.calls) + 1,
                           "purpose": "session_title" if title_call else "agent_turn",
                           "model": kwargs.get("model") or MODEL,
                           "message_roles": [m.get("role") for m in messages if isinstance(m, dict)],
                           "messages": _message_evidence(messages)})
        if self.control.get("error"):
            raise RuntimeError("scripted_provider_error")
        tools = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if self.case_id == "V079":
            payload = None
            for message in reversed(messages):
                if message.get("role") != "user" or not isinstance(message.get("content"), str):
                    continue
                try:
                    candidate = json.loads(message["content"])
                except (TypeError, ValueError):
                    continue
                if isinstance(candidate, dict) and "schema" in candidate:
                    payload = candidate
                    break
                if isinstance(candidate, list) and all(isinstance(row, dict) for row in candidate):
                    payload = candidate
                    break
            schema_title = (payload or {}).get("schema", {}).get("title") if isinstance(payload, dict) else None
            if schema_title == "QueryPlan":
                data = {"intent": "mail_fact", "queries": [
                    {"query": "竹桥预算", "scope_action": "replace", "mode": "keyword", "limit": 8}
                ]}
            elif schema_title == "AnswerDraft":
                evidence = payload.get("evidence", [])
                match = next((item for item in evidence
                              if "120" in str(item.get("excerpt", ""))
                              and "竹桥" in str(item.get("excerpt", ""))), None)
                data = {"status": "answered", "claims": ([{
                    "text": "竹桥预算为120元。", "evidence_ids": [match["evidence_id"]]
                }] if match else [])}
            elif isinstance(payload, list) and all("claim" in row for row in payload):
                data = {"supported": [True for _ in payload]}
            else:
                data = {"status": "insufficient", "claims": []}
            return LLMResponse(content=json.dumps(data, ensure_ascii=False), finish_reason="stop")
        if self.case_id == "V075":
            asked = any(m.get("tool_call_id") == "browser-v075-ask-1" for m in tools)
            wrote = any(m.get("tool_call_id") == "browser-v075-write-1" for m in tools)
            if not asked:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    "browser-v075-ask-1", "ask_user",
                    {"question": "报告用表格还是JSON？", "options": ["表格", "JSON"]},
                )], finish_reason="tool_calls")
            if not wrote:
                answer = next((str(m.get("content", "")) for m in tools
                               if m.get("tool_call_id") == "browser-v075-ask-1"), "")
                if "JSON" not in answer:
                    return LLMResponse(content="尚未选择 JSON，未写入报告。", finish_reason="stop")
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    "browser-v075-write-1", "write_file",
                    {"path": "report.txt", "content": '{"total":150}'},
                )], finish_reason="tool_calls")
            return LLMResponse(content="JSON报告已写入。", finish_reason="stop")
        if self.case_id in {"V076", "V077"}:
            gate_result = any(m.get("tool_call_id", "").startswith("browser-gate-") for m in tools)
            if self.case_id == "V076":
                resumed = any(isinstance(m, dict) and m.get("role") == "user"
                              and "继续完成刚才的任务" in str(m.get("content", "")) for m in messages)
                read = next((m for m in tools if m.get("tool_call_id") == "browser-v076-read-1"), None)
                if resumed:
                    wrote = any(m.get("tool_call_id") == "browser-v076-write-1" for m in tools)
                    if not wrote:
                        return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                            "browser-v076-write-1", "write_file",
                            {"path": "result.txt", "content": "73"},
                        )], finish_reason="tool_calls")
                    return LLMResponse(content="已继续完成，result.txt为73。", finish_reason="stop")
                if not read:
                    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                        "browser-v076-read-1", "read_file", {"path": "fact.txt"},
                    )], finish_reason="tool_calls")
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    "browser-gate-v076", "evaluation_gate", {"gate_id": "v076-cancel-gate"},
                )], finish_reason="tool_calls")
            user = next((str(m.get("content", "")) for m in messages
                         if isinstance(m, dict) and m.get("role") == "user"), "")
            gate_id = "v077-A" if "A=17" in user else "v077-B"
            if not gate_result:
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    f"browser-gate-{gate_id}", "evaluation_gate", {"gate_id": gate_id},
                )], finish_reason="tool_calls")
            return LLMResponse(content=gate_id.replace("v077-", "") + "=" + ("17" if gate_id.endswith("A") else "23"),
                               finish_reason="stop")
        if self.case_id == "V074":
            edit_result = next((message for message in reversed(tools)
                                if message.get("tool_call_id") == "browser-v074-edit-1"
                                or "Successfully edited" in str(message.get("content", ""))), None)
            if edit_result is not None:
                content = str(edit_result.get("content", ""))
                if "Successfully edited" in content:
                    return LLMResponse(content="已将 timeout 改为 60，retries 保持 3。",
                                       finish_reason="stop")
                return LLMResponse(content="文件编辑没有成功，配置未确认修改。", finish_reason="stop")
            read_result_message = next((message for message in reversed(tools)
                                        if message.get("tool_call_id") == "browser-v074-read-1"
                                        or (message.get("name") == "read_file" and message is not None)), None)
            if read_result_message is None:
                if self.control.get("skip_edit"):
                    return LLMResponse(content="未执行文件修改。", finish_reason="stop")
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    "browser-v074-read-1", "read_file", {"path": "config.json"},
                )], finish_reason="tool_calls")
            if self.control.get("skip_edit"):
                return LLMResponse(content="未执行文件修改。", finish_reason="stop")
            read_result = next((str(message.get("content", "")) for message in reversed(messages)
                                if isinstance(message, dict) and message.get("role") == "tool"), "")
            match = re.search(r'("timeout"\s*:\s*)\d+', read_result)
            old = self.control.get("old_text", match.group(0) if match else '"timeout":30')
            new = self.control.get("new_text", re.sub(r"\d+$", "60", old))
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                "browser-v074-edit-1", "edit_file",
                {"path": "config.json", "old_text": old, "new_text": new},
            )], finish_reason="tool_calls")
        if self.case_id == "V078":
            content = "HISTORY17"
        elif self.case_id == "V080":
            content = "MOBILE17"
        else:
            content = str(self.control.get("answer", "5"))
        return LLMResponse(content=content, finish_reason="stop")

    async def chat_stream(self, messages, **kwargs) -> LLMResponse:
        response = await self.chat(messages, **kwargs)
        callback = kwargs.get("on_content_delta")
        if callback and response.content:
            await callback(response.content)
        return response


class BrowserTaskGateway:
    """Start a genuine isolated WebSocketChannel/AgentLoop test gateway.

    ``profile`` is ``B-scripted`` or ``B-real``. The caller owns the temporary
    runtime/workspace and must pass an isolated config path via runtime/config.json.
    """

    def __init__(self, profile: str, runtime: Path, workspace: Path,
                 provider: LLMProvider, emit: Any, control: dict[str, Any] | None = None):
        self.profile, self.runtime, self.workspace = profile, runtime.resolve(), workspace.resolve()
        self.provider, self.emit, self.control = provider, emit, control or {}
        self.observation: dict[str, Any] = {
            "schema": "browser-task-gateway/1.0", "profile": profile,
            "model": provider.get_default_model(), "observation_mode": getattr(provider, "observation_mode", "unknown"),
            "browser_submissions": [], "agent_events": [], "provider_requests": [],
            "provider_transports": [],
            "tool_events": [], "deliveries": [], "errors": [], "gates": [],
        }
        self.gates: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task] = []
        self._control_runner = None
        self._closed = False

    @classmethod
    async def create(cls, *, profile: str, runtime: str | Path, workspace: str | Path,
                     emit: Any = None, control: dict[str, Any] | None = None,
                     provider: LLMProvider | None = None, credentials: str | Path | None = None,
                     frontend_dir: str | Path | None = None):
        if profile not in {"B-scripted", "B-real"}:
            raise ValueError("invalid_browser_profile")
        import nanobot.agent.context as context_module
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus
        from nanobot.channels.manager import ChannelManager
        from nanobot.channels.websocket import WebSocketChannel
        from nanobot.config.loader import set_config_path
        from nanobot.config.schema import ToolsConfig
        from nanobot.session.manager import SessionManager

        # Keep task time fixed while the watchdog and browser timing stay real.
        context_module.current_time_str = lambda *a, **kw: "2026-10-01 09:00 (Asia/Shanghai)"

        runtime, workspace = Path(runtime).resolve(), Path(workspace).resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        case_id = (control or {}).get("case_id")
        if case_id == "V076":
            fact = workspace / "fact.txt"
            if not fact.exists():
                fact.write_text("73", encoding="utf-8")
        set_config_path(runtime / "config.json")
        sessions = SessionManager(runtime, allow_legacy=False)
        fixture_sessions = {}
        if case_id == "V077":
            for actor in ("A", "B"):
                chat_id = str(uuid.uuid4())
                session_key = f"websocket:{chat_id}"
                session = sessions.get_or_create(session_key)
                session.metadata.update({"webui": True, "title": f"V077-{actor}",
                                         "title_user_edited": True})
                sessions.save(session)
                fixture_sessions[actor.lower()] = {
                    "chat_id": chat_id,
                    "session_key": session_key,
                    "title": f"V077-{actor}",
                    "initial_messages": len(session.messages),
                    "messages": [],
                    "session_sha256": _sha(sessions._get_session_path(session_key).read_bytes()),
                    "initial_session_sha256": _sha(sessions._get_session_path(session_key).read_bytes()),
                }
        mail_service = None
        mail_binding = None
        mail_extension = None
        mail_bootstrap_secret = None
        if case_id == "V079":
            from evals.mail_task_worker import _fixture_service
            from nanobot.knowledge.mail.agent_bridge import MailTurnExtension

            fixture = {"mails": [{"uid": 79, "subject": "竹桥预算", "body": "竹桥预算120元",
                                  "attachments": [], "received_at": "2026-10-01T00:00:00+00:00"}]}
            mail_service, mail_binding, prepare_mail = _fixture_service(
                runtime, fixture, workspace=workspace, emit=emit,
            )
            mail_binding.channel = "websocket"
            await prepare_mail()
            mail_extension = MailTurnExtension(mail_service, timezone="Asia/Shanghai")
            mail_bootstrap_secret = V079_OWNER_LOGIN
        if provider is None:
            if profile == "B-real":
                if credentials is None:
                    raise ValueError("credentials_path_required")
                from evals.worker import real_provider
                class SafeEmitter:
                    def __init__(self, target, obs):
                        self.target, self.obs = target, obs
                    def event(self, kind, **fields):
                        if kind == "model_transport":
                            self.obs["provider_transports"].append(fields)
                        if self.target:
                            self.target.event(kind, **fields)
                    @property
                    def redactions(self):
                        return self.target.redactions if self.target else []
                observer = SafeEmitter(emit, {"provider_transports": []})
                if case_id == "V079":
                    # Protected mail answering is intentionally non-streaming;
                    # the standard audited provider records real SDK usage.
                    provider = real_provider(credentials, observer)
                    _instrument_standard_browser_provider(provider, emit,
                        request_limit=(control or {}).get("request_limit", 8),
                        token_limit=(control or {}).get("token_limit", 32000))
                else:
                    from evals.channel_task_worker import native_provider

                    provider = native_provider(credentials, observer,
                        request_limit=(control or {}).get("request_limit", 8),
                        token_limit=(control or {}).get("token_limit", 32000))
                    provider._browser_model_events = observer.obs["provider_transports"]
                    provider._browser_request_records = []
                    original_stream_call = provider.chat_stream_with_retry
                    async def observed_stream_call(*call_args, **call_kwargs):
                        messages = call_kwargs.get("messages")
                        if messages is None and call_args:
                            messages = call_args[0]
                        provider._browser_request_records.append({
                            "request_index": len(provider._browser_request_records) + 1,
                            "purpose": "agent_or_background",
                            "model": call_kwargs.get("model"),
                            "messages": _message_evidence(messages),
                        })
                        return await original_stream_call(*call_args, **call_kwargs)
                    provider.chat_stream_with_retry = observed_stream_call
                    # Wire SDK dispatch counts into the returned evidence.
                    original = provider._client.chat.completions.create
                    async def counted(**kwargs):
                        ledger = getattr(provider, "_browser_ledger", None)
                        if ledger is None:
                            ledger = provider._browser_ledger = {"requests": 0}
                        if ledger["requests"] >= (control or {}).get("request_limit", 8):
                            raise RuntimeError("browser_request_budget_exceeded")
                        ledger["requests"] += 1
                        return await original(**kwargs)
                    provider._client.chat.completions.create = counted
            else:
                provider = ScriptedBrowserProvider(str((control or {}).get("case_id", "V073")), control)

        bus = MessageBus()
        channel_cfg = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "path": "/", "token": "", "websocket_requires_token": False,
                       "streaming": True, "send_progress": True, "send_tool_hints": True,
                       "allow_from": ["*"]}
        if case_id == "V079":
            from nanobot.channels.websocket import WebSocketConfig
            channel_cfg = WebSocketConfig(
                enabled=True, host="127.0.0.1", port=0, path="/", token="",
                token_issue_secret=mail_bootstrap_secret, mail_owner_bootstrap=True,
                mail_owner_principal=mail_binding.principal_id, websocket_requires_token=True,
                streaming=True, allow_from=["*"],
            )
            channel = WebSocketChannel(channel_cfg, bus, mail_service=mail_service,
                                       session_manager=sessions,
                                       static_dist_path=Path(frontend_dir) if frontend_dir else None)
        else:
            channel = WebSocketChannel(channel_cfg, bus, session_manager=sessions,
                                       static_dist_path=Path(frontend_dir) if frontend_dir else None)
        channel.send_progress = channel.send_tool_hints = True
        # Test design requires a fixed port for browser capture; reserve loopback only.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        channel.config.port = port
        static_dist = Path(__file__).resolve().parents[1] / "webui" / "dist"
        if frontend_dir is None:
            channel._static_dist_path = static_dist.resolve() if static_dist.is_dir() else None
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace,
                         model=provider.get_default_model(), session_manager=sessions,
                         tools_config=ToolsConfig(restrict_to_workspace=True),
                         restrict_to_workspace=True, unified_session=False,
                         turn_extension=mail_extension, mail_service=mail_service)
        allowed_by_case = {
            "V073": [], "V074": ["read_file", "edit_file"],
            "V075": ["ask_user", "write_file"],
            "V076": ["read_file", "write_file", "evaluation_gate"],
            "V077": ["evaluation_gate"], "V078": [],
            "V079": [], "V080": [],
        }
        allowed_tools = allowed_by_case.get(case_id, [])
        instance = cls(profile, runtime, workspace, provider, emit, control)
        if case_id in {"V076", "V077"}:
            instance.loop = loop
            loop.tools.register(EvaluationGateTool(instance))
        for tool_name in list(loop.tools.tool_names):
            if tool_name not in allowed_tools:
                loop.tools.unregister(tool_name)
        instance_title_stub = bool((control or {}).get("disable_title", True))
        if instance_title_stub:
            from nanobot.agent import loop as agent_loop
            async def title_stub(**_kwargs):
                return False
            agent_loop.maybe_generate_webui_title_after_turn = title_stub
        manager = ChannelManager.__new__(ChannelManager)
        manager.bus, manager.channels = bus, {"websocket": channel}
        manager.config = type("EvalConfig", (), {"channels": type("EvalChannels", (), {"send_max_retries": 1})()})()
        manager.mail_service = mail_service
        manager._origin_reply_fingerprints = {}
        instance.port, instance.channel, instance.loop, instance.manager = port, channel, loop, manager
        instance.observation["workspace_before"] = instance._snapshot_tree(workspace)
        instance.observation["session_before"] = instance._snapshot_tree(runtime / "sessions")
        instance.observation["endpoint"] = f"ws://127.0.0.1:{port}/?client_id=browser-eval"
        instance.observation["allowed_tools"] = allowed_tools
        instance.observation["title_scope"] = "disabled_test_stub" if instance_title_stub else "real_title_generation"
        instance.observation["fixture_sessions"] = fixture_sessions
        instance.observation["fixture_model_requests"] = 0
        instance.mail_service = mail_service
        instance.mail_binding = mail_binding
        if mail_service is not None:
            instance.observation["mail_auth"] = {
                "bootstrap_enabled": True,
                "bootstrap_secret_scope": "synthetic_evaluation_only",
                "owner_binding": {"binding_id": mail_binding.binding_id,
                                  "principal_id": mail_binding.principal_id,
                                  "owner_id": mail_binding.owner_id,
                                  "channel": mail_binding.channel,
                                  "scopes": sorted(mail_binding.scopes)},
                "issued_tokens": [], "websocket_bindings": [], "http_authorizations": [],
                "source_reads": [], "http_requests": [], "revocations": [],
            }
            web_access = channel._mail_access
            original_issue = web_access.issue
            def observe_issue(token, ttl, *, owner_principal=None):
                security = original_issue(token, ttl, owner_principal=owner_principal)
                instance.observation["mail_auth"]["issued_tokens"].append({
                    "authenticated": security.authenticated,
                    "principal_id": security.principal_id,
                    "binding_id": security.binding_id,
                    "owner_id": security.owner_id,
                    "origin_kind": security.origin_kind,
                })
                return security
            web_access.issue = observe_issue
            original_bind = web_access.bind
            def observe_bind(connection, token):
                original_bind(connection, token)
                security = web_access.connections.get(connection)
                instance.observation["mail_auth"]["websocket_bindings"].append({
                    "authenticated": bool(security and security.authenticated),
                    "principal_id": security.principal_id if security else None,
                    "binding_id": security.binding_id if security else None,
                    "owner_id": security.owner_id if security else None,
                })
            web_access.bind = observe_bind
            original_http_access = web_access.http_access
            def observe_http_access(request, *, scope="read", chat_id="management"):
                header = request.headers.get("Authorization", "")
                token = header[7:] if header.startswith("Bearer ") else ""
                token_row = web_access.tokens.get(token)
                security = token_row[0] if token_row else None
                row = {"path": request.path.split("?", 1)[0], "chat_id": chat_id, "scope": scope,
                       "authenticated": bool(security and security.authenticated),
                       "principal_id": security.principal_id if security else None,
                       "binding_id": security.binding_id if security else None,
                       "owner_id": security.owner_id if security else None}
                try:
                    access = original_http_access(request, scope=scope, chat_id=chat_id)
                except Exception as exc:
                    row.update(status="denied", error_code=getattr(exc, "code", type(exc).__name__))
                    instance.observation["mail_auth"]["http_authorizations"].append(row)
                    raise
                row.update(status="allowed", principal_id=access.principal_id,
                           binding_id=access.binding_id, owner_id=access.owner_id)
                instance.observation["mail_auth"]["http_authorizations"].append(row)
                return access
            web_access.http_access = observe_http_access
            original_read_source = mail_service.retrieval.read_source
            def observe_source_read(access, request):
                row = {"request": request.model_dump(mode="json"),
                       "identity": {"principal_id": access.principal_id,
                                    "binding_id": access.binding_id,
                                    "owner_id": access.owner_id,
                                    "session_key_sha256": _sha(access.session_key.encode())}}
                try:
                    result = original_read_source(access, request)
                except Exception as exc:
                    row.update(status="denied", error_code=getattr(exc, "code", type(exc).__name__))
                    instance.observation["mail_auth"]["source_reads"].append(row)
                    raise
                row.update(status="allowed", sources=[source.model_dump(mode="json")
                                                       for source in result.sources])
                instance.observation["mail_auth"]["source_reads"].append(row)
                return result
            mail_service.retrieval.read_source = observe_source_read
        # Capture actual ingress and serialized egress at the WebSocket boundary.
        original_handle = channel._handle_message
        async def observe_handle(**kwargs):
            row = {"sender_id": kwargs.get("sender_id"), "chat_id": kwargs.get("chat_id"),
                   "content": kwargs.get("content"), "webui": bool((kwargs.get("metadata") or {}).get("webui")),
                   "received_monotonic_ns": time.perf_counter_ns(),
                   "accepted_clock": "python_perf_counter_ns"}
            instance.observation["browser_submissions"].append(row)
            result = await original_handle(**kwargs)
            row["accepted_monotonic_ns"] = time.perf_counter_ns()
            if emit:
                emit.event("browser_task_submission", **row)
            return result
        channel._handle_message = observe_handle
        original_send = channel.send
        async def observe_send(message):
            if message.metadata.get("_turn_end"):
                event = {"event": "turn_end", "chat_id": message.chat_id}
                if message.metadata.get("_turn_error") is True:
                    event["outcome"] = "error"
                if message.metadata.get("_turn_id") is not None:
                    event["turn_id"] = message.metadata["_turn_id"]
            elif message.metadata.get("_session_updated"):
                event = {"event": "session_updated", "chat_id": message.chat_id}
            else:
                event = {"event": "message", "chat_id": message.chat_id,
                         "text": message.content, "metadata": dict(message.metadata or {})}
                if message.metadata.get("_turn_id") is not None:
                    event["turn_id"] = message.metadata["_turn_id"]
            instance.observation["deliveries"].append(event)
            if emit:
                emit.event("browser_task_delivery", **event)
            await original_send(message)
            if instance.control.get("duplicate_delivery"):
                duplicate = {**event, "duplicate_control": True}
                instance.observation["deliveries"].append(duplicate)
                if emit:
                    emit.event("browser_task_delivery", **duplicate)
                await original_send(message)
        channel.send = observe_send
        original_send_delta = channel.send_delta
        async def observe_send_delta(chat_id, delta, metadata=None):
            metadata = metadata or {}
            row = {"event": "stream_end" if metadata.get("_stream_end") else "delta",
                   "chat_id": chat_id, "text": delta,
                   "stream_id": metadata.get("_stream_id"), "metadata": dict(metadata)}
            row["turn_id"] = metadata.get("_turn_id")
            if metadata.get("_stream_end"):
                row["resuming"] = metadata.get("_resuming") is True
            instance.observation["deliveries"].append(row)
            if emit:
                emit.event("browser_task_stream_delivery", **row)
            await original_send_delta(chat_id, delta, metadata)
            if instance.control.get("duplicate_delivery") and not metadata.get("_stream_end"):
                if emit:
                    emit.event("fault_injection", fault_id="duplicate_delivery",
                               boundary="WebSocketChannel.send_delta", triggered=True,
                               source_frame={"chat_id": chat_id, "text_sha256": _sha(delta.encode("utf-8")),
                                             "stream_id": metadata.get("_stream_id")})
                duplicate = {**row, "duplicate_control": True}
                instance.observation["deliveries"].append(duplicate)
                if emit:
                    emit.event("browser_task_stream_delivery", **duplicate)
                await original_send_delta(chat_id, delta, metadata)
        channel.send_delta = observe_send_delta
        original_http = channel._dispatch_http
        async def observe_http(connection, request):
            response = await original_http(connection, request)
            request_path = request.path.split("?", 1)[0]
            if instance.mail_service is not None and request_path.startswith("/api/mail-knowledge/"):
                body = getattr(response, "body", b"") or b""
                try:
                    payload = json.loads(body.decode("utf-8")) if body else None
                except (UnicodeDecodeError, ValueError):
                    payload = None
                row = {"path": request_path, "status": response.status_code,
                       "body_sha256": _sha(body)}
                if request_path.startswith("/api/mail-knowledge/sources/"):
                    row["body"] = payload
                instance.observation["mail_auth"]["http_requests"].append(row)
                if emit:
                    emit.event("browser_task_mail_http", path=request_path,
                               status=response.status_code, body_sha256=_sha(body))
            if response is not None and (request_path == "/api/sessions" or (
                request_path.startswith("/api/sessions/") and request_path.endswith("/messages")
            )):
                body = getattr(response, "body", b"") or b""
                instance.observation.setdefault("history_http_requests", []).append({
                    "path": request_path,
                    "status": response.status_code,
                    "body_sha256": _sha(body),
                    "body": body.decode("utf-8", errors="replace"),
                })
                if emit:
                    emit.event("browser_task_history_response", path=request_path,
                               status=response.status_code, body_sha256=_sha(body))
            return response
        channel._dispatch_http = observe_http
        # Observe the real edit tool invocation and resultant bytes.
        for tool_name in allowed_tools:
            tool = loop.tools.get(tool_name)
            if not tool:
                continue
            original_execute = tool.execute
            async def observed_tool(_tool_name=tool_name, _execute=original_execute, **arguments):
                item = {"name": _tool_name, "arguments": arguments}
                instance.observation["tool_events"].append({**item, "phase": "start"})
                _emit_operation_event(emit, "browser_task_tool", "start", **item)
                result = await _execute(**arguments)
                item["result"] = result
                target = (workspace / str(arguments.get("path", ""))).resolve()
                if target.is_relative_to(workspace) and target.is_file():
                    item["file_sha256"] = _sha(target.read_bytes())
                instance.observation["tool_events"].append({**item, "phase": "end"})
                _emit_operation_event(emit, "browser_task_tool", "end", **item)
                return result
            tool.execute = observed_tool
        instance._tasks = [asyncio.create_task(loop.run()), asyncio.create_task(manager._dispatch_outbound()),
                           asyncio.create_task(channel.start())]
        # Verify actual TCP listener before publishing ready to the browser worker.
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.03)
        else:
            await instance.close()
            raise RuntimeError("loopback_gateway_start_timeout")
        await instance._start_control_server()
        return instance

    @property
    def url(self) -> str:
        return self.observation["endpoint"]

    def record_submission(self, *, chat_id: str, text: str, browser_action: dict[str, Any] | None = None):
        """Called by the browser driver only after observing the actual WS send."""
        row = {"chat_id": chat_id, "text": text, "text_sha256": _sha(text.encode()),
               "browser_action": browser_action or {}}
        self.observation["browser_submissions"].append(row)
        if self.emit:
            self.emit.event("browser_task_submission", **row)

    def snapshot(self, files: list[str] | None = None) -> dict[str, Any]:
        selected = files or ["config.json"]
        out = {}
        for rel in selected:
            path = (self.workspace / rel).resolve()
            if path.is_relative_to(self.workspace) and path.is_file():
                data = path.read_bytes()
                out[rel] = {"sha256": _sha(data), "bytes": len(data),
                            "text": data.decode("utf-8", errors="replace")}
        self.observation["workspace_state"] = out
        if getattr(self.provider, "_browser_ledger_kind", None) == "sdk_attempts":
            self.observation["provider_ledger"] = self.provider._browser_request_records
            self.observation["provider_ledger_summary"] = _provider_ledger_summary(self.provider)
        else:
            self.observation["provider_ledger"] = (
                getattr(self.provider, "_browser_request_records", None)
                or getattr(self.provider, "calls", None)
                or getattr(self.provider, "_browser_ledger", None)
            )
        self.observation["provider_transports"] = getattr(self.provider, "_browser_model_events", None) or []
        self.observation["provider_requests"] = self.observation["provider_ledger"]
        return self.observation

    @staticmethod
    def _snapshot_tree(root: Path) -> dict[str, dict[str, Any]]:
        if not root.is_dir():
            return {}
        return {
            path.relative_to(root).as_posix(): {
                "sha256": _sha(path.read_bytes()), "bytes": path.stat().st_size,
            }
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    async def _start_control_server(self) -> None:
        from aiohttp import web
        app = web.Application()
        async def observe(_request):
            provider = self.provider
            ledger = getattr(provider, "_browser_ledger", None)
            probe_id = _request.query.get("probe_id")
            workspace_tree = self._workspace_evidence()
            session_files = self._session_evidence()
            active_tasks = {
                key: [{"done": task.done(), "cancelled": task.cancelled()} for task in tasks]
                for key, tasks in self.loop._active_tasks.items()
            }
            result = {
                "probe_id": probe_id,
                "monotonic_ns": time.monotonic_ns(),
                "case_id": self.control.get("case_id"),
                "inbound_submission_count": len(self.observation["browser_submissions"]),
                "provider_requests": self.snapshot().get("provider_ledger"),
                "provider_ledger_summary": self.observation.get("provider_ledger_summary"),
                "provider_transports": self.observation.get("provider_transports", []),
                "provider_request_count": (ledger or {}).get("requests") if ledger else (
                    len(getattr(provider, "calls", [])) if hasattr(provider, "calls") else None),
                "tool_events": self.observation["tool_events"],
                "deliveries": self.observation["deliveries"],
                "history_http_requests": self.observation.get("history_http_requests", []),
                "mail_auth": self.observation.get("mail_auth"),
                "gates": self._gate_state(),
                "workspace_state": workspace_tree,
                "session_files": session_files,
                "session_ids": [row["session_id"] for row in session_files],
                "checkpoint_ids": [row["checkpoint_id"] for row in session_files if row["checkpoint_id"]],
                "active_tasks": active_tasks,
                "cancellation_state": {
                    "active_task_count": sum(len(rows) for rows in active_tasks.values()),
                    "cancelled_task_count": sum(item["cancelled"] for rows in active_tasks.values() for item in rows),
                    "cancelled_gates": [row["gate_id"] for row in self._gate_state()
                                         if row["state"] == "cancelled"],
                },
            }
            binding = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            result["snapshot_sha256"] = _sha(binding.encode("utf-8"))
            if self.emit:
                self.emit.event("browser_task_probe", probe_id=probe_id,
                                snapshot_sha256=result["snapshot_sha256"],
                                session_ids=result["session_ids"], checkpoint_ids=result["checkpoint_ids"])
            return web.json_response(result)
        async def gate_state(_request):
            return web.json_response({"gates": self._gate_state()})
        async def release_gate(request):
            try:
                payload = await request.json()
            except (ValueError, json.JSONDecodeError):
                return web.json_response({"error": "invalid_json"}, status=400)
            gate_id = payload.get("gate_id") if isinstance(payload, dict) else None
            gate = self.gates.get(gate_id) if isinstance(gate_id, str) else None
            if gate is None or gate["state"] != "waiting":
                return web.json_response({"released": False, "reason": "gate_not_waiting"}, status=409)
            gate["event"].set()
            self.observation["gates"].append({"gate_id": gate_id, "state": "release_requested"})
            if self.emit:
                self.emit.event("browser_task_gate_control", gate_id=gate_id, action="release")
            return web.json_response({"released": True, "gate_id": gate_id})
        app.router.add_get("/observe", observe)
        app.router.add_get("/gate-state", gate_state)
        app.router.add_post("/gate/release", release_gate)
        if self.mail_service is not None:
            async def revoke_owner(_request):
                from aiohttp import web
                try:
                    revision = await asyncio.to_thread(
                        self.mail_service.access.revoke, self.mail_binding.binding_id,
                    )
                except Exception as exc:
                    return web.json_response({"revoked": False,
                                              "error_code": getattr(exc, "code", type(exc).__name__)}, status=500)
                row = {"binding_id": self.mail_binding.binding_id,
                       "principal_id": self.mail_binding.principal_id,
                       "owner_id": self.mail_binding.owner_id,
                       "policy_revision": revision, "status": "revoked"}
                self.observation["mail_auth"]["revocations"].append(row)
                if self.emit:
                    self.emit.event("browser_task_mail_permission", **row)
                return web.json_response({"revoked": True, "policy_revision": revision})
            app.router.add_post("/permission/revoke", revoke_owner)
        runner = web.AppRunner(app)
        await runner.setup()
        site_sock = socket.socket()
        site_sock.bind(("127.0.0.1", 0))
        control_port = site_sock.getsockname()[1]
        site_sock.close()
        site = web.TCPSite(runner, "127.0.0.1", control_port)
        await site.start()
        self._control_runner = runner
        self.observation["control_url"] = f"http://127.0.0.1:{control_port}/observe"
        self.observation["gate_state_url"] = f"http://127.0.0.1:{control_port}/gate-state"
        self.observation["gate_release_url"] = f"http://127.0.0.1:{control_port}/gate/release"
        if self.mail_service is not None:
            self.observation["permission_revoke_url"] = f"http://127.0.0.1:{control_port}/permission/revoke"

    def _gate_state(self) -> list[dict[str, Any]]:
        return [{"gate_id": gate_id, "state": gate["state"], "entries": gate["entries"],
                 "cancellations": gate["cancellations"]}
                for gate_id, gate in sorted(self.gates.items())]

    @staticmethod
    def _redact_mapping(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: "[redacted]" if re.search(r"api.?key|token|secret|password|credential", key, re.I)
                    else BrowserTaskGateway._redact_mapping(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [BrowserTaskGateway._redact_mapping(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[credential_redacted]", value)
            value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[credential_redacted]", value)
        return value

    def _workspace_evidence(self) -> dict[str, dict[str, Any]]:
        result = {}
        readable = {"fact.txt", "config.json", "report.txt", "result.txt"}
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.workspace).as_posix()
            raw = path.read_bytes()
            item = {"sha256": _sha(raw), "bytes": len(raw), "exists": True}
            if relative in readable:
                text = raw.decode("utf-8", errors="replace")
                if relative.endswith(".json"):
                    try:
                        text = json.dumps(self._redact_mapping(json.loads(text)), ensure_ascii=False,
                                          separators=(",", ":"))
                    except ValueError:
                        text = "[unparseable-json-redacted]"
                item["text"] = text
            result[relative] = item
        for relative in readable:
            result.setdefault(relative, {"exists": False, "sha256": None, "bytes": 0, "text": None})
        return result

    def _session_evidence(self) -> list[dict[str, Any]]:
        sessions_dir = self.runtime / "sessions"
        result = []
        if not sessions_dir.is_dir():
            return result
        for path in sorted(sessions_dir.glob("*.jsonl")):
            raw = path.read_bytes()
            lines = []
            metadata = {}
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    lines.append({"invalid_line_sha256": _sha(line.encode("utf-8"))})
                    continue
                lines.append(self._redact_mapping(item))
                if isinstance(item, dict) and item.get("_type") == "metadata":
                    metadata = item
            checkpoint = metadata.get("metadata", {}).get("runtime_checkpoint")
            checkpoint_id = _sha(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")) if checkpoint else None
            result.append({"session_id": metadata.get("key", path.stem),
                           "relative_path": path.relative_to(self.runtime).as_posix(),
                           "sha256": _sha(raw), "checkpoint_id": checkpoint_id,
                           "checkpoint_present": checkpoint is not None, "jsonl": lines})
        return result

    async def close(self) -> dict[str, Any]:
        if self._closed:
            return self.observation
        self._closed = True
        if self._control_runner is not None:
            await self._control_runner.cleanup()
        await self.channel.stop()
        self.loop.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        active = [task for tasks in self.loop._active_tasks.values() for task in tasks if not task.done()]
        if active:
            try:
                results = await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), timeout=20)
                self.observation["agent_tasks"] = [
                    {"status": "error", "kind": type(result).__name__}
                    if isinstance(result, BaseException) else {"status": "completed"}
                    for result in results
                ]
            except asyncio.TimeoutError:
                self.observation["errors"].append({"phase": "agent_task_drain", "kind": "TimeoutError"})
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
        await self.loop.close_mcp()
        try:
            from evals.worker import _close_provider
            await _close_provider(self.provider)
            if getattr(self.provider, "_browser_ledger_kind", None) == "sdk_attempts":
                self.provider._browser_ledger["provider_closed"] = True
        except Exception as exc:
            self.observation["errors"].append({"phase": "provider_close", "kind": type(exc).__name__})
        if self.mail_service is not None:
            try:
                await self.mail_service.close()
            except Exception as exc:
                self.observation["errors"].append({"phase": "mail_service_close", "kind": type(exc).__name__})
        self.snapshot()
        self.observation["workspace_after"] = self._snapshot_tree(self.workspace)
        self.observation["session_after"] = self._snapshot_tree(self.runtime / "sessions")
        return self.observation


class _EventConnection:
    """Collect events emitted by the shared evaluation Emitter."""
    def __init__(self, output: Path):
        self.output = output
        self.events: list[dict[str, Any]] = []
    def send(self, value: dict[str, Any]):
        self.events.append(value)


async def _serve_cli(args) -> None:
    from evals.worker import Emitter
    from nanobot.utils.observability import observation_context, span

    runtime, workspace, output, ready = map(Path, (args.runtime, args.workspace, args.output, args.ready))
    if args.control and Path(args.control).is_file():
        control = json.loads(Path(args.control).read_text(encoding="utf-8"))
    else:
        control = {"control": args.control} if args.control else {}
    control["case_id"] = args.case
    control["request_limit"] = args.request_limit
    control["token_limit"] = args.known_token_limit
    output.mkdir(parents=True, exist_ok=True)
    conn = _EventConnection(output)
    control_name = control.get("control", "positive")
    trial_id = f"{args.case}:{args.profile}:{control_name}:{output.name}"
    emitter = Emitter(conn, output.name, trial_id)
    with observation_context(emitter.event, trial_id) as state:
        with span("browser_gateway", case_id=args.case, profile=args.profile):
            gateway = await BrowserTaskGateway.create(profile=args.profile, runtime=runtime,
                                                      workspace=workspace, emit=emitter, control=control,
                                                      credentials=args.credentials,
                                                      frontend_dir=args.frontend_dir)
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text(json.dumps({"url": gateway.url, "port": gateway.port,
                                         "case_id": args.case, "profile": args.profile,
                                         "control_url": gateway.observation["control_url"],
                                         "gate_state_url": gateway.observation["gate_state_url"],
                                         "gate_release_url": gateway.observation["gate_release_url"],
                                         "mail_bootstrap": ({"url": f"http://127.0.0.1:{gateway.port}/webui/bootstrap",
                                                             "login_header": "X-Nanobot-Auth",
                                                             "login_secret": V079_OWNER_LOGIN,
                                                             "owner_principal": gateway.mail_binding.principal_id,
                                                             "permission_revoke_url": gateway.observation.get("permission_revoke_url")}
                                                            if gateway.mail_service is not None else None),
                                         "mail_fixture": ({"question": "竹桥预算是多少？给出邮件依据",
                                                           "subject": "竹桥预算",
                                                           "body": "竹桥预算120元",
                                                           "expected_source_excerpt": "竹桥预算120元"}
                                                          if gateway.mail_service is not None else None),
                                         "permission_revoke_url": gateway.observation.get("permission_revoke_url"),
                                         "fixture_sessions": gateway.observation.get("fixture_sessions", {})},
                             ensure_ascii=False),
                             encoding="utf-8")
            stop = runtime / "stop"
            try:
                while not stop.exists():
                    await asyncio.sleep(0.1)
            finally:
                await gateway.close()
        # The final browser_gateway span and observation context closure are part
        # of the receipt. Persist only after their end records have been emitted.
    gateway.observation["events"] = [row["event"] for row in conn.events
                                      if row.get("message") == "event"]
    gateway.observation["trace_summary"] = {
        "producer_id": emitter.producer, "last_seq": emitter.seq,
        "collector_errors": state.collector_errors,
    }
    (output / "gateway-evidence.json").write_text(
        json.dumps(gateway.observation, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["V073", "V074", "V075", "V076", "V077", "V078", "V079", "V080"], required=True)
    parser.add_argument("--profile", choices=["B-scripted", "B-real"], required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--control")
    parser.add_argument("--credentials")
    parser.add_argument("--frontend-dir")
    parser.add_argument("--request-limit", type=int, default=8)
    parser.add_argument("--known-token-limit", type=int, default=32000)
    asyncio.run(_serve_cli(parser.parse_args()))


async def start_gateway(**kwargs) -> BrowserTaskGateway:
    """Convenience entry point used by isolated browser profile drivers."""
    return await BrowserTaskGateway.create(**kwargs)


if __name__ == "__main__":
    main()
