"""Trusted child adapter: real AgentLoop, restricted real tools, pipe-only evidence."""

import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from evals.common import MODEL, SCHEMA, TOOLS, digest, now
from nanobot.utils.observability import (
    current_observation,
    current_span_id,
    observation_context,
    point,
    span,
    usage_payload,
)


class BudgetExceeded(BaseException):
    """Must escape product retry and tool error handling."""


_DIAGNOSTIC_FAULTS = frozenset({
    "provider_503_once", "tool_once", "tool_not_triggered",
    "save_open", "save_replace", "save_replace_cleanup",
})


def _validate_fault_mode(mode, fault):
    if fault is None:
        return
    if mode != "D":
        raise ValueError("fault_injection_requires_deterministic_mode")
    if fault not in _DIAGNOSTIC_FAULTS:
        raise ValueError("unsupported_diagnostic_fault")


@contextmanager
def _save_fault(sessions, emit, fault):
    """D-only faults at disk operations; the real SAVE stage still runs."""
    if fault not in {"save_open", "save_replace", "save_replace_cleanup"}:
        yield
        return
    import errno
    from contextlib import ExitStack

    error = PermissionError(errno.EACCES, "synthetic save failure")
    error.winerror = 5

    def fail(*args, **kwargs):
        emit.event("fault_injection", fault_id=fault, boundary="SessionManager.save",
                   recoverable=False, triggered=True)
        raise error

    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if (path.parent == sessions.sessions_dir and path.name.startswith(".session-save-")
                and path.name.endswith(".tmp")):
            cleanup_error = PermissionError(errno.EACCES, "synthetic cleanup failure")
            cleanup_error.winerror = 32
            raise cleanup_error
        return original_unlink(path, *args, **kwargs)

    with ExitStack() as stack:
        target = "nanobot.session.manager.tempfile.mkstemp" if fault == "save_open" else "nanobot.session.manager.os.replace"
        stack.enter_context(patch(target, fail, create=True))
        if fault == "save_replace_cleanup":
            stack.enter_context(patch.object(Path, "unlink", unlink))
        yield


async def _close_provider(provider, primary_error=None):
    if not hasattr(provider, "_client"):
        return
    try:
        await provider._client.close()
    except BaseException as exc:
        point("cleanup_error", operation="provider_close", relative_path=None,
              error_kind=type(exc).__name__, errno=getattr(exc, "errno", None),
              winerror=getattr(exc, "winerror", None), location="evals.worker._close_provider")
        if primary_error is not None:
            raise primary_error from exc
        raise


class Emitter:
    def __init__(self, connection, run_id, trial_id):
        self.connection = connection
        self.run_id = run_id
        self.trial_id = trial_id
        self.producer = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.seq = 0
        self.redactions = []

    def event(self, kind, phase="point", span=None, parent_span_id=None,
              task_id=None, parent_task_id=None, **payload):
        self.seq += 1
        event_id = f"{self.producer}:{self.seq}"
        def redact(value):
            if isinstance(value, str):
                for secret in self.redactions:
                    value = value.replace(secret, "[credential_redacted]")
                return value
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            return value

        self.connection.send({"message": "event", "event": {
            "schema_version": SCHEMA, "event_id": event_id, "run_id": self.run_id,
            "trial_id": self.trial_id, "task_id": task_id or self.trial_id,
            "parent_task_id": parent_task_id,
            "span_id": span or event_id,
            "parent_span_id": parent_span_id if span else parent_span_id or current_span_id(),
            "producer_id": self.producer, "seq": self.seq, "clock_id": self.producer,
            "monotonic_ns": time.monotonic_ns(), "wall_time": now(), "type": kind,
            "phase": phase, "payload": redact(payload),
        }})
        return span or event_id


def scripted_provider(script, emit, fault=None):
    from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest

    class ScriptedProvider(LLMProvider):
        observation_mode = "scripted"

        def __init__(self):
            super().__init__()
            self.index = 0
            self.generation = GenerationSettings(temperature=0, max_tokens=2048)
            self.fault_triggered = False

        def get_default_model(self):
            return MODEL

        async def chat(self, messages, **kwargs):
            if fault == "provider_503_once" and not self.fault_triggered:
                self.fault_triggered = True
                emit.event("fault_injection", fault_id=fault, boundary="provider.chat",
                           recoverable=True, triggered=True)
                # The provider retry contract consumes structured error responses. Return
                # this before reading/incrementing the scripted response index so the
                # retry replays the exact pending row.
                return LLMResponse(content="503 unavailable", finish_reason="error",
                                   error_status_code=503, error_should_retry=True,
                                   retry_after=0.001)
            if self.index >= 16:
                emit.event("budget", reason="scripted_response_limit")
                raise BudgetExceeded()
            row = script[min(self.index, len(script) - 1)]
            self.index += 1
            if row.get("sleep"):
                await asyncio.sleep(row["sleep"])
            calls = [ToolCallRequest(f"call-{self.index}-{i}", x["name"], x["arguments"])
                     for i, x in enumerate(row.get("tool_calls", []))]
            return LLMResponse(content=row.get("content"), tool_calls=calls,
                               finish_reason="tool_calls" if calls else "stop")

    return ScriptedProvider()


def real_provider(credentials, emit):
    from dataclasses import replace

    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.registry import find_by_name

    key = Path(credentials).read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]+", key):
        raise ValueError("unsupported_credential_format")
    emit.redactions.append(key)
    # Avoid the provider's optional environment export of credentials.
    spec = replace(find_by_name("deepseek"), env_key="", env_extras=())
    provider = OpenAICompatProvider(api_key=key, default_model=MODEL, spec=spec,
                                    extra_body={"thinking": {"type": "disabled"}})
    provider.generation = GenerationSettings(temperature=0, max_tokens=2048)
    provider.observation_sdk_retries = 0
    original = provider._client.chat.completions.create
    attempts = 0

    async def observed_create(**kwargs):
        nonlocal attempts
        if kwargs.get("model") != MODEL or kwargs.get("stream"):
            raise ValueError("unsupported_model_or_stream")
        if attempts >= 8:
            emit.event("budget", reason="model_attempt_limit")
            raise BudgetExceeded()
        attempts += 1
        # No raw messages, credentials, HTTP errors or hidden reasoning are persisted.
        try:
            result = await original(**kwargs)
        except BaseException as exc:
            point("model_transport", status="error", error_kind=type(exc).__name__,
                  requested_model=kwargs.get("model"), actual_model=None,
                  native_stream=False, wire_stream=False, complete=False,
                  sdk_retries=0, counting_role="diagnostic_only",
                  **usage_payload({}, source="sdk_response_usage"))
            raise
        usage = result.usage
        # Keep the raw SDK field presence. Product parsers may normalize missing
        # token fields to zero; that is not evidence of a known zero bill.
        raw_usage = {name: getattr(usage, name, None) for name in
                     ("prompt_tokens", "completion_tokens")} if usage is not None else {}
        point("model_transport", actual_model=result.model, requested_model=kwargs.get("model"),
              native_stream=False, wire_stream=False, complete=True, sdk_retries=0,
              fingerprint=getattr(result, "system_fingerprint", None),
              counting_role="diagnostic_only", transport_usage_present=usage is not None,
              **usage_payload(raw_usage, source="sdk_response_usage"))
        return result

    provider._client.chat.completions.create = observed_create
    return provider


def audit_tools(loop, emit, workspace, fault=None, full_task_semantic=False):
    """Observe actual execute even when Runner bypasses registry.execute."""
    from nanobot.agent.tools.filesystem import (
        EditFileTool,
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )

    for tool_type in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
        tool = tool_type(workspace=workspace, allowed_dir=workspace)
        original_resolve = tool._resolve
        last_resolved = {"path": None}

        def resolve(path, _resolve=original_resolve, _name=tool.name,
                    _last_resolved=last_resolved, _workspace=workspace,
                    _full_task_semantic=full_task_semantic):
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = _workspace / target
            if not target.resolve().is_relative_to(_workspace.resolve()):
                emit.event("safety", name=_name, action="path_denied", attempted=True,
                           actual=False)
                raise PermissionError("outside allowed directory")
            point("file_access", name=_name,
                  path=target.resolve().relative_to(_workspace.resolve()).as_posix())
            resolved = _resolve(path)
            if _full_task_semantic and _name == "read_file":
                _last_resolved["path"] = resolved
            return resolved

        tool._resolve = resolve
        if tool_type is ReadFileTool and full_task_semantic:
            original_execute = tool.execute

            async def observe_source_read(*args, _execute=original_execute,
                                          _last_resolved=last_resolved,
                                          _workspace=workspace, **kwargs):
                _last_resolved["path"] = None
                result = await _execute(*args, **kwargs)
                source_path = _last_resolved["path"]
                path = (source_path.relative_to(_workspace.resolve()).as_posix()
                        if source_path is not None else None)
                result_text = result if isinstance(result, str) else None
                status = ("ok" if result_text is not None
                          and not result_text.lstrip().startswith("Error") else "error")
                source_hash = None
                if source_path is not None:
                    try:
                        source_hash = digest(source_path.read_bytes())
                    except OSError:
                        status = "error"
                observation = current_observation()
                active = (observation.active_spans.get(current_span_id())
                          if observation and current_span_id() else None)
                point("source_read", path=path, source_sha256=source_hash,
                      result_text=result_text,
                      result_sha256=(digest(result_text.encode("utf-8"))
                                     if result_text is not None else None),
                      offset=kwargs.get("offset", 1), limit=kwargs.get("limit"),
                      call_id=(active.payload.get("call_id") if active else None), status=status)
                return result

            tool.execute = observe_source_read
        if tool_type is EditFileTool and fault == "tool_once":
            original_execute = tool.execute
            triggered = False

            async def fail_first_edit(*args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    emit.event("fault_injection", fault_id=fault,
                               boundary="edit_file.execute", recoverable=True,
                               triggered=True)
                    return "Error: injected transient evaluation fault"
                return await original_execute(*args, **kwargs)

            tool.execute = fail_first_edit
        loop.tools.register(tool)


async def execute_task(connection, options, emit):
    from evals.trace import EvalHook
    from nanobot.agent.hook import AgentHook
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path
    from nanobot.config.schema import ToolsConfig
    from nanobot.security.context import SecurityContext
    from nanobot.session.manager import SessionManager

    class IsolatedLoop(AgentLoop):
        def _register_default_tools(self):
            # Profile assembly only: no plugin constructors, shell or external services.
            # The inherited process_direct/runner/context/session implementation is unchanged.
            pass

        async def _state_save(self, ctx):
            with _save_fault(self.sessions, emit, fault):
                return await super()._state_save(ctx)

    class BudgetHook(AgentHook):
        def __init__(self):
            super().__init__(reraise=True)
            self.requested = 0

        async def before_execute_tools(self, context):
            for call in context.tool_calls:
                self.requested += 1
                emit.event("tool_request", name=call.name, call_id=call.id,
                           arguments=call.arguments, stage="requested")
                if call.name not in TOOLS:
                    emit.event("safety", action="unavailable_tool_attempt", name=call.name,
                               attempted=True, actual=False)
                if self.requested > 16:
                    emit.event("budget", reason="tool_request_limit")
                    raise BudgetExceeded()

    fault = options.get("fault")
    _validate_fault_mode(options["mode"], fault)
    runtime = Path(options["runtime"])
    workspace = Path(options["workspace"])
    set_config_path(runtime / "config.json")
    provider = (scripted_provider(options["script"], emit, fault=fault)
                if options["mode"] == "D"
                else real_provider(options["credentials"], emit))
    sessions = SessionManager(runtime, allow_legacy=False)
    loop = IsolatedLoop(bus=MessageBus(), provider=provider, workspace=workspace, model=MODEL,
                     max_iterations=8, hooks=[EvalHook(), BudgetHook()], session_manager=sessions,
                     tools_config=ToolsConfig(restrict_to_workspace=True),
                     restrict_to_workspace=True, timezone="Asia/Shanghai",
                     disabled_skills=[p.name for p in BUILTIN_SKILLS_DIR.iterdir() if p.is_dir()])
    audit_tools(loop, emit, workspace,
                fault=fault if options["mode"] == "D" else None,
                full_task_semantic=options.get("full_task_semantic") is True)
    if sorted(loop.tool_names) != sorted(TOOLS):
        raise ValueError("tool_isolation_failed")
    # Freeze only the task's business clock, not watchdog monotonic time.
    import nanobot.agent.context as context_module
    context_module.current_time_str = lambda *a, **kw: "2026-10-01 09:00 (Asia/Shanghai)"
    connection.send({"message": "ready", "tools": loop.tool_names,
                     "session_file": sessions._get_session_path(options["trial_id"]).name})
    if connection.recv() != "go":
        return
    entry_span = span("entry", entry="AgentLoop.process_direct", sender_id="user")
    entry_span.__enter__()
    entry_error = None
    try:
        result = await loop.process_direct(
            content=options["input"], session_key=options["trial_id"], channel="cli", chat_id="eval",
            media=[], security=SecurityContext(principal_id="eval", authenticated=True,
                                               origin_kind="evaluation"))
        pending = [t for t in loop._background_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending)
        outbox = []
        while not loop.bus.outbound.empty():
            message = loop.bus.outbound.get_nowait()
            internal = (message.channel == "cli" and message.chat_id == "eval"
                        and message.metadata.get("_progress") is True and not message.media)
            outbox.append({"channel": message.channel, "chat_id": message.chat_id,
                           "internal_progress": internal,
                           "content_sha256": digest(message.content.encode("utf-8"))})
        point("delivery", response=result.content if result else None,
              outbox_count=sum(not m["internal_progress"] for m in outbox),
              outbox=outbox, background_pending=0)
        entry_span.close()
    except BaseException as exc:
        entry_error = exc
        entry_span.__exit__(type(exc), exc, exc.__traceback__)
        if not isinstance(exc, (BudgetExceeded, Exception)):
            raise
    finally:
        await _close_provider(provider, entry_error)


def worker_main(connection, options):
    # These changes apply only to the disposable child process.
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()
    emit = Emitter(connection, options["run_id"], options["trial_id"])
    state = None
    try:
        with observation_context(emit.event, options["trial_id"],
                                 parent_span_id=options.get("parent_span_id")) as state:
            asyncio.run(execute_task(connection, options, emit))
    except BaseException as exc:
        connection.send({"message": "worker_error", "error_kind": type(exc).__name__})
    finally:
        if state is not None:
            connection.send({"message": "observation_summary", "producer_id": emit.producer,
                             "last_seq": emit.seq, "collector_errors": state.collector_errors})
        connection.close()
