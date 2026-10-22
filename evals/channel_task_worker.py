"""Trusted channel-profile worker; credentials stay outside Agent tools/evidence."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import replace
from pathlib import Path

from evals.common import MODEL
from evals.security_task_worker import _limit_provider_usage
from evals.worker import BudgetExceeded, Emitter, _close_provider, real_provider
from nanobot.utils.observability import (
    current_span_id,
    observation_context,
    point,
    span,
    usage_payload,
)


def native_provider(credentials, emit, *, request_limit=8, token_limit=32000):
    """Observe actual streamed SDK chunks and raw usage, rather than simulating deltas."""
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.registry import find_by_name

    key = Path(credentials).read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]+", key):
        raise ValueError("unsupported_credential_format")
    emit.redactions.append(key)
    spec = replace(find_by_name("deepseek"), env_key="", env_extras=())
    provider = OpenAICompatProvider(api_key=key, default_model=MODEL, spec=spec,
                                    extra_body={"thinking": {"type": "disabled"}})
    provider.generation = GenerationSettings(temperature=0, max_tokens=2048)
    provider.observation_sdk_retries = 0
    provider.observation_mode = "real"
    original = provider._client.chat.completions.create
    ledger = {"requests": 0, "known_tokens": 0}

    class ObservedStream:
        def __init__(self, source, parent):
            self.source, self.iterator, self.parent = source, source.__aiter__(), parent
            self.raw_usage, self.actual_model = {}, None
            self.closed = False

        def __aiter__(self):
            return self

        def finish(self, complete):
            if self.closed:
                return
            self.closed = True
            usage = usage_payload(self.raw_usage, source="sdk_response_usage")
            if not complete:
                usage["usage_status"] = "partial" if self.raw_usage else "unknown"
            emit.event("model_transport", parent_span_id=self.parent, actual_model=self.actual_model,
                       requested_model=MODEL, sdk_retries=0, native_stream=True, wire_stream=True,
                       complete=complete, counting_role="diagnostic_only", **usage)
            ledger["known_tokens"] += sum(value for value in self.raw_usage.values()
                                          if type(value) is int and value >= 0)

        async def __anext__(self):
            try:
                chunk = await self.iterator.__anext__()
            except StopAsyncIteration:
                self.finish(True)
                if ledger["known_tokens"] > token_limit:
                    point("budget", reason="completed_known_token_limit", known_tokens=ledger["known_tokens"])
                    raise BudgetExceeded()
                raise
            except BaseException:
                self.finish(False)
                raise
            self.actual_model = getattr(chunk, "model", None) or self.actual_model
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self.raw_usage = {name: getattr(usage, name, None) for name in
                                  ("prompt_tokens", "completion_tokens")}
            return chunk

        async def close(self):
            self.finish(False)
            await self.source.close()

    async def create(**kwargs):
        if kwargs.get("model") != MODEL or kwargs.get("stream") is not True:
            raise ValueError("native_stream_profile_mismatch")
        if ledger["requests"] >= request_limit or ledger["known_tokens"] >= token_limit:
            point("budget", reason="native_stream_admission_limit", **ledger)
            raise BudgetExceeded()
        ledger["requests"] += 1
        parent = current_span_id()
        try:
            result = await original(**kwargs)
        except BaseException as error:
            emit.event("model_transport", parent_span_id=parent, status="error",
                       error_kind=type(error).__name__, sdk_retries=0, native_stream=True,
                       wire_stream=True, requested_model=kwargs.get("model"), actual_model=None,
                       complete=False, counting_role="diagnostic_only",
                       **usage_payload({}, source="sdk_response_usage"))
            raise
        return ObservedStream(result, parent)

    provider._client.chat.completions.create = create
    return provider


async def _execute(connection, options, emit):
    from nanobot.config.loader import set_config_path

    case = options["case"]
    cid, mode, control = case["case_id"], options["mode"], options.get("channel_control")
    runtime, workspace = Path(options["runtime"]), Path(options["workspace"])
    set_config_path(runtime / "config.json")
    for name, content in case["channel_fixture"].get("files", {}).items():
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise ValueError("channel_fixture_path_escape")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    provider = None
    if mode == "R" and cid != "V065":
        provider = native_provider(options["credentials"], emit) if cid == "V066" else real_provider(options["credentials"], emit)
        if cid == "V063":
            _limit_provider_usage(provider)
    connection.send({"message": "ready", "tools": case["tools"],
                     "runtime_allowed_prefixes": case["file_policy"]["runtime_allowed_prefixes"]})
    if connection.recv() != "go":
        return
    error = None
    try:
        with span("entry", entry=case["entry"]["method"] if isinstance(case["entry"], dict) else case["entry"]):
            from evals.channel_task_evidence import delivery_text

            if cid == "V063":
                from evals.channel_task_slack import run_slack
                result = await run_slack(runtime, workspace, provider, emit, control)
            elif cid == "V065":
                from evals.channel_task_media import run_media
                result = await run_media(runtime, workspace, None, emit, control)
                point("channel_precondition", ready=result["precondition_ready"], mode=mode,
                      planned_model_requests=0, reason=result["capability_status"])
            else:
                from evals.channel_task_stream import asyncrun_stream, fixed_stream_provider
                result = await asyncrun_stream(runtime, workspace, provider or fixed_stream_provider(), emit)
            response = delivery_text(cid, result)
            point("channel_task_result", case_id=cid, mode=mode, control=control, result=result)
            if response:
                point("delivery", response=response, normal_final_reply=True,
                      observation_only=False, receipt_type="actual_delivery_reconstruction",
                      actual_business_send=False,
                      origin="actual_SDK_post_or_streamed_Agent_final")
            point("settlement", status="settled", pending=0, coverage_complete=True,
                  background_pending=0, running_subtasks=0,
                  completion_kind="product_entry_rejected" if cid == "V065" and not result["precondition_ready"] else "delivered")
    except BaseException as exc:
        error = exc
        raise
    finally:
        if provider:
            await _close_provider(provider, error)


def worker_main(connection, options):
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    from loguru import logger
    logger.remove()
    emit = Emitter(connection, options["run_id"], options["trial_id"])
    state = None
    try:
        with observation_context(emit.event, options["trial_id"]) as state:
            with span("task", case_id=options["case"]["case_id"], mode=options["mode"]):
                asyncio.run(_execute(connection, options, emit))
    except BaseException as exc:
        connection.send({"message": "worker_error", "error_kind": type(exc).__name__})
    finally:
        if state:
            connection.send({"message": "observation_summary", "producer_id": emit.producer,
                             "last_seq": emit.seq, "collector_errors": state.collector_errors})
        connection.close()
