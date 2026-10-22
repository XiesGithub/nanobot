"""One-owner, two-principal Agent and transport privacy evaluation worker."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from evals.common import ROOT, digest
from evals.mail_task_worker import (
    _fixture_service,
    _mail_source_state,
    _observe_mail_calls,
    _observe_mail_model_requests,
)
from evals.security_task_worker import _limit_provider_usage
from evals.worker import BudgetExceeded, Emitter, _close_provider, real_provider, scripted_provider
from nanobot.utils.observability import observation_context, point, span

_actor = ContextVar("privacy_eval_actor", default="setup")
_case = ContextVar("privacy_eval_case", default=None)


class CaptureConnection:
    """Only the channel's wire sink is replaced; identity and serialization are real."""

    def __init__(self, role, visible):
        self.role = role
        self.visible = visible
        self.sent = []
        self.remote_address = ("synthetic-connection", 0)

    async def send(self, raw):
        self.sent.append(raw)
        self.visible.append({"surface": "websocket", "actor": self.role, "value": raw})


def _script_provider(emit, references):
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    provider = scripted_provider([{"content": "无权限或无可访问资料。"}], emit)
    calls = 0

    async def chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 8:
            point("budget", reason="model_attempt_limit")
            raise BudgetExceeded()
        if _actor.get() == "B":
            identity = references[_case.get()]["evidence_id"]
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                f"privacy-read-{calls}", "mail_read_source", {"evidence_id": identity}
            )], finish_reason="tool_calls")
        data = None
        for message in reversed(messages):
            if message.get("role") == "user":
                try:
                    data = json.loads(message.get("content", ""))
                    break
                except (ValueError, TypeError):
                    continue
        title = data.get("schema", {}).get("title") if isinstance(data, dict) else None
        if title == "QueryPlan":
            content = {"intent": "mail_fact", "queries": [
                {"query": "私有费用", "exact_terms": ["PRIVATE-A17"], "mode": "keyword"},
                {"query": "私有流式来源", "exact_terms": ["PRIVATE-FLOW-X8"], "mode": "keyword"},
            ]}
        elif title == "AnswerDraft":
            evidence = data["evidence"]
            one = next(row for row in evidence if "PRIVATE-A17" in row["excerpt"])
            two = next(row for row in evidence if "PRIVATE-FLOW-X8" in row["excerpt"])
            content = {"status": "answered", "claims": [
                {"text": "私有费用120元，内部编号PRIVATE-A17。", "evidence_ids": [one["evidence_id"]]},
                {"text": "私有流式来源标记为PRIVATE-FLOW-X8。", "evidence_ids": [two["evidence_id"]]},
            ]}
        else:
            content = {"supported": [True] * len(data)}
        return LLMResponse(content=json.dumps(content, ensure_ascii=False), finish_reason="stop")

    provider.chat = chat
    return provider


def _snapshot_cache(service, extension, actors):
    with service.store.connect(query_state=True) as db:
        runs = [dict(row) for row in db.execute("SELECT * FROM query_runs ORDER BY query_id")]
        evidence = [dict(row) for row in db.execute("SELECT * FROM query_evidence ORDER BY evidence_id")]
    histories = {}
    for role, actor in actors.items():
        key = actor["session_key"]
        data = extension.sessions.read_session_file(key)
        histories[role] = {"session_key": key, "messages": data["messages"] if data else [],
                           "metadata": data["metadata"] if data else {}}
    security = actors["B"]["security"]
    accessible = [row for row in runs if security.authenticated
                  and row["owner_id"] == security.owner_id and row["binding_id"] == security.binding_id
                  and row["session_key"] == actors["B"]["session_key"]]
    return {"query_runs": runs, "query_evidence": evidence, "histories": histories,
            "B_accessible_query_rows": accessible, "scope": "protected_internal_snapshot",
            "session_cache_keys": sorted(extension.sessions._cache)}


def _protected_store_state(service):
    # Real WS source expansion persists idempotency/chat-binding records. All
    # other archive, authorization and management state must remain unchanged.
    mutable = {"chat_bindings", "web_requests", "query_runs", "query_evidence",
               "mail_focus", "summary_progress"}
    with service.store.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: sorted([{key: {"opaque_bytes_sha256": digest(value), "length": len(value)}
                              if isinstance(value, bytes) else value for key, value in dict(row).items()}
                             for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')],
                             key=lambda row: json.dumps(row, sort_keys=True))
                for name in sorted(names) if name not in mutable}


async def _execute_task(connection, options, emit):
    from loguru import logger

    from evals.batch_mail import OWNER
    from evals.privacy_task_boundaries import probe_boundaries
    from evals.trace import EvalHook
    from nanobot.agent.hook import AgentHook, CompositeHook
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR
    from nanobot.agent.tools.mail_knowledge import MailReadSourceTool, MailSearchTool
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
    from nanobot.config.loader import set_config_path
    from nanobot.config.schema import MailOwnerBinding, ToolsConfig
    from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
    from nanobot.knowledge.mail.contracts import AccessContext, MailError, MailQuery, SourceRead
    from nanobot.knowledge.mail.tool_policy import MailToolView
    from nanobot.session.manager import SessionManager

    runtime, workspace = Path(options["runtime"]), Path(options["workspace"])
    fixture = options["privacy_fixture"]
    references = {}
    visible, logs, boundaries = [], [], []
    set_config_path(runtime / "config.json")

    def log_sink(message):
        record = message.record
        exception = record["exception"]
        extra = {name: ("[redacted]" if any(word in name.casefold() for word in
                 ("token", "password", "authorization", "api_key", "secret")) else value)
                 for name, value in record["extra"].items()}
        logs.append({"surface": "runtime_log", "actor": _actor.get(),
                     "value": {"message": record["message"],
                               "extra": json.loads(json.dumps(extra, default=lambda value: "[non_serializable]")),
                               "exception": {"type": exception.type.__name__, "value": str(exception.value)} if exception else None},
                     "level": record["level"].name})

    sink = logger.add(log_sink, level="DEBUG", format="{message}", enqueue=False)
    service = provider = None
    primary_error = None
    try:
        service, _, prepare = _fixture_service(runtime, fixture, workspace=ROOT, emit=emit)
        await prepare()
        service.access.grant(MailOwnerBinding(binding_id=fixture["binding_id"], principal_id=fixture["principal_id"],
            owner_id=OWNER, channel="websocket", scopes={"read", "manage"}))
        extension = MailTurnExtension(service, timezone="Asia/Shanghai")
        bus = MessageBus()
        channel = WebSocketChannel(WebSocketConfig(enabled=True, allow_from=["*"], streaming=True,
            host="127.0.0.1", token_issue_secret="SYNTHETIC-PRIVACY-LOGIN", mail_owner_bootstrap=True,
            mail_owner_principal=fixture["principal_id"]), bus, mail_service=service,
            session_manager=SessionManager(runtime / "sessions", allow_legacy=False))
        web = channel._mail_access
        web.issue("synthetic-owner-token", 3600, owner_principal=fixture["principal_id"])
        web.issue("synthetic-visitor-token", 3600)
        actors = {}
        for role, token in (("A", "synthetic-owner-token"), ("B", "synthetic-visitor-token")):
            conn = CaptureConnection(role, visible)
            web.bind(conn, token)
            chat_id = "privacy-" + role
            channel._attach(conn, chat_id)
            security = web.security(conn)
            inbound = InboundMessage("websocket", role, chat_id, "", security=security)
            key = extension.session_key(inbound)
            actors[role] = {"role": role, "token": token, "connection": conn, "chat_id": chat_id,
                            "security": security, "session_key": key,
                            "access": AccessContext.from_security(security, key) if security.authenticated else None}
        actor_a, actor_b = actors["A"], actors["B"]

        def identity(role):
            security = actors[role]["security"]
            return {"principal_id": security.principal_id, "binding_id": security.binding_id,
                    "owner_id": security.owner_id}
        if options["mode"] == "D":
            provider = _script_provider(emit, references)
        else:
            provider = real_provider(options["credentials"], emit)
        _limit_provider_usage(provider)
        _observe_mail_model_requests(provider)
        original_chat = provider.chat
        requested_tools = 0

        async def observed_chat(messages, **kwargs):
            nonlocal requested_tools
            if _actor.get() == "B":
                visible.append({"surface": "model_input", "actor": "B", "value": json.loads(json.dumps(messages)),
                                "protected_internal": True})
            response = await original_chat(messages, **kwargs)
            # Runner preflight precedes before_execute_tools; observe the actual
            # provider requests here so policy rejection remains in the ledger.
            for request in response.tool_calls:
                requested_tools += 1
                point("privacy_tool_attempt", actor=_actor.get(), name=request.name,
                      call_id=request.id, arguments=request.arguments)
                if requested_tools > 16:
                    point("budget", reason="tool_request_limit")
                    raise BudgetExceeded()
            return response

        provider.chat = observed_chat

        class PrivacyHook(AgentHook):
            def __init__(self):
                super().__init__(reraise=True)

            async def after_iteration(self, context):
                if _actor.get() == "B" and context.tool_results:
                    visible.append({"surface": "tool_result", "actor": "B", "value": context.tool_results,
                                    "protected_internal": True})

        class PrivateLoop(AgentLoop):
            def _register_default_tools(self):
                self.tools.register(MailSearchTool(service))
                self.tools.register(MailReadSourceTool(service))

        loop = PrivateLoop(bus, provider, workspace, model="deepseek-v4-flash", max_iterations=8,
            session_manager=channel._session_manager, tools_config=ToolsConfig(restrict_to_workspace=True),
            restrict_to_workspace=True, timezone="Asia/Shanghai", turn_extension=extension,
            mail_service=service, hooks=[EvalHook(), PrivacyHook()],
            disabled_skills=[path.name for path in BUILTIN_SKILLS_DIR.iterdir() if path.is_dir()])
        original_run = loop.runner.run

        async def observed_run(spec):
            if spec.hook is None:
                spec = replace(spec, hook=CompositeHook([EvalHook(), PrivacyHook()]))
            with span("privacy_runner", actor=_actor.get(), case_id=_case.get()):
                try:
                    return await original_run(spec)
                except MailError as error:
                    boundaries.append({"name": "agent_read_B", "actor": _actor.get(), "allowed": False,
                                       "denied": True, "code": error.code, "stage": "MailToolView.preflight",
                                       **identity(_actor.get())})
                    raise

        loop.runner.run = observed_run
        original_delta = channel.send_delta

        async def send_delta(chat_id, delta, metadata=None):
            point("privacy_stream_attempt", actor=_actor.get(), chat_id=chat_id,
                  delta_sha256=digest(delta.encode()), metadata=metadata or {})
            return await original_delta(chat_id, delta, metadata)

        channel.send_delta = send_delta
        allowed = ["mail/" + database + suffix for database in
                   ("query_state.sqlite3", "knowledge.sqlite3") for suffix in ("", "-wal", "-shm")]
        for actor in actors.values():
            allowed.append(extension.sessions._get_session_path(actor["session_key"]).relative_to(runtime).as_posix())
        connection.send({"message": "ready", "tools": loop.tool_names, "runtime_allowed_paths": allowed,
                         "session_file": extension.sessions._get_session_path(actor_b["session_key"]).name})
        if connection.recv() != "go":
            return
        with ExitStack() as stack, span("entry", entry="WebSocketChannel→AgentLoop._dispatch", sender_id="eval"):
            store_before = _protected_store_state(service)
            point("privacy_store_state", moment="before", protected_state=store_before)
            _, _, plan_patch, answer_patch = _observe_mail_calls(service, emit)
            stack.enter_context(plan_patch)
            stack.enter_context(answer_patch)
            source_state, sources, _ = _mail_source_state(service, fixture, "privacy")
            for source in sources:
                source.update(identity("A"), fixture_sha256=fixture["fixture_sha256"],
                              instance_id=source_state["mode"]["instance_id"])
            point("privacy_sources", sources=sources)
            point("tool_inventory", tools=[{"name": name, "class": type(loop.tools.get(name)).__name__}
                                           for name in loop.tool_names], transport="real_ws_serialization/synthetic_connection")

            async def turn(role, case_id, content):
                actor = actors[role]
                role_token = _actor.set(role)
                case_token = _case.set(case_id)
                start_frames = len(actor["connection"].sent)
                outbound = []
                try:
                    with span("privacy_agent_turn", role=role, case_id=case_id,
                              security={"principal_id": actor["security"].principal_id,
                                        "binding_id": actor["security"].binding_id,
                                        "owner_id": actor["security"].owner_id,
                                        "authenticated": actor["security"].authenticated}):
                        await channel._dispatch_envelope(actor["connection"], "synthetic-client-" + role,
                            {"type": "message", "chat_id": actor["chat_id"], "content": content})
                        inbound = await bus.consume_inbound()
                        await loop._dispatch(inbound)
                        while not bus.outbound.empty():
                            response = bus.outbound.get_nowait()
                            outbound.append(response)
                            if response.metadata.get("_stream_delta") or response.metadata.get("_stream_end"):
                                await channel.send_delta(response.chat_id, response.content, response.metadata)
                            else:
                                await channel.send(response)
                        pending = [task for task in loop._background_tasks if not task.done()]
                        if pending:
                            await asyncio.gather(*pending)
                finally:
                    _actor.reset(role_token)
                    _case.reset(case_token)
                finals = [item for item in outbound if not any(item.metadata.get(key) for key in
                          ("_turn_end", "_session_updated", "_progress", "_stream_delta", "_stream_end"))]
                response = finals[-1] if finals else None
                return {"response": response.content if response else None,
                        "metadata": response.metadata if response else {},
                        "frames": actor["connection"].sent[start_frames:],
                        "stream_callback_count": sum(bool(item.metadata.get("_stream_delta")) for item in outbound),
                        "stream_end_callback_count": sum(bool(item.metadata.get("_stream_end")) for item in outbound)}

            owner_token = _actor.set("A")
            try:
                positive = await turn("A", "owner-positive", "请查询私有费用及内部编号，并给出私有流式来源标记和出处。")
                owner_positive = []
                for case_id, uid in (("V095", 1), ("V107", 2)):
                    source = next(row for row in sources if row["uid"] == uid and row["kind"] == "body")
                    attachment = next(row for row in sources if row["uid"] == uid and row["kind"] == "attachment")
                    marker = "PRIVATE-A17" if uid == 1 else "PRIVATE-FLOW-X8"
                    search = await service.search(actor_a["access"], MailQuery(query=marker,
                        exact_terms=[marker], document_ids=[source["document_id"]], mode="keyword", limit=8))
                    match = next(row for row in search.matches if row.document_id == source["document_id"])
                    page = await service.read_source(actor_a["access"], SourceRead(evidence_id=match.evidence_id, limit=8000))
                    pagination = await service.search(actor_a["access"], MailQuery(query=marker,
                        exact_terms=[marker], mode="keyword", limit=1))
                    item = page.sources[0]
                    references[case_id] = {"evidence_id": item.evidence_id, "query_id": item.query_id,
                                           "mail_id": item.mail_id, "document_id": item.document_id,
                                           "cursor": pagination.next_cursor, "cursor_query_id": pagination.query_id,
                                           "attachment_document_id": attachment["document_id"]}
                    owner_positive.append({"case_id": case_id, "allowed": True,
                                           "source": item.model_dump(mode="json"), "search": search.model_dump(mode="json"),
                                           "agent_delivery": positive})
            finally:
                _actor.reset(owner_token)
            cache_before = _snapshot_cache(service, extension, actors)
            delivery_by_case = {}
            for case_id in ("V095", "V107"):
                question = fixture["questions"][case_id]
                question = question.replace("{evidence_id}", references[case_id]["evidence_id"])
                if references[case_id]["evidence_id"] not in question:
                    question += "\n引用ID：" + references[case_id]["evidence_id"]
                delivery_by_case[case_id] = await turn("B", case_id, question)
                delivery_by_case[case_id]["input"] = question
                point("privacy_case_delivery", case_id=case_id, actor="B", **delivery_by_case[case_id])
                boundaries.append({"name": "stream_callbacks_B", "actor": "B", "case_id": case_id,
                                   **identity("B"),
                                   "allowed": False, "denied": delivery_by_case[case_id]["stream_callback_count"] == 0,
                                   "code": "protected_nonstreaming", "stage": "MailTurnExtension",
                                   "callback_count": delivery_by_case[case_id]["stream_callback_count"]})
            boundaries.append({"name": "session_key_B", "actor": "B", "allowed": False,
                               **identity("B"),
                               "denied": actor_a["session_key"] != actor_b["session_key"],
                               "code": "principal_namespaces_differ", "stage": "MailTurnExtension.session_key"})
            view = MailToolView(loop.tools, service.access)
            definitions = view.get_definitions()
            boundaries.append({"name": "agent_read_B", "actor": "B", "allowed": bool(definitions),
                               "denied": not definitions, "attempted": False,
                               "code": "unauthenticated_private_tools_hidden", "stage": "MailToolView.get_definitions",
                               **identity("B"), "definitions": definitions})
            role_token = _actor.set("B")
            try:
                count_before = len(actor_b["connection"].sent)
                await channel.send_delta(actor_b["chat_id"], "PRIVATE-FLOW-X8", {"_stream_delta": True})
                boundaries.append({"name": "stream_delta_B", "actor": "B", "allowed": False,
                                   **identity("B"),
                                   "denied": len(actor_b["connection"].sent) == count_before,
                                   "code": "protected_channel_delta_suppressed", "stage": "WebSocketChannel.send_delta"})
                helper = await probe_boundaries(service, web, channel, actor_a, actor_b, sources, references, runtime)
            finally:
                _actor.reset(role_token)
            boundaries.extend(helper["boundaries"])
            visible.extend(helper["visible"])
            visible.extend(logs)
            visible.append({"surface": "runtime_log_capture", "actor": "B",
                            "value": [row["value"] for row in logs if row["actor"] == "B"],
                            "actual_sink_messages": sum(row["actor"] == "B" for row in logs)})
            cache_after = _snapshot_cache(service, extension, actors)
            store_after = _protected_store_state(service)
            point("privacy_store_state", moment="after", protected_state=store_after)
            visible.append({"surface": "private_history", "actor": "B", "value": cache_after["histories"]["B"]})
            actor_public = {role: {"role": "owner-A" if role == "A" else "principal-B",
                                  "principal_id": actor["security"].principal_id,
                                  "binding_id": actor["security"].binding_id, "owner_id": actor["security"].owner_id,
                                  "authenticated": actor["security"].authenticated,
                                  "origin": actor["security"].origin_kind, "chat_id": actor["chat_id"],
                                  "session_key": actor["session_key"], "server_issued": True}
                            for role, actor in actors.items()}
            result = {"actors": actor_public, "owner_positive": owner_positive, "boundaries": boundaries,
                      "fixture_sha256": fixture["fixture_sha256"],
                      "store_before": store_before, "store_after": store_after,
                      "visible": visible, "cache_before": cache_before, "cache_after": cache_after,
                      "delivery_by_case": delivery_by_case, "sources": sources, "references": references,
                      "coverage": helper["coverage"] + ["agent_read_B", "stream_delta_B", "session_key_B", "stream_callbacks_B", "real_agent_B", "real_owner_agent_positive", "runtime_log_sink", "model_input_B", "private_history_B"],
                      "owner_session_keys": helper.get("owner_session_keys", [actor_a["session_key"]]),
                      "protected_internal": "A evidence, A delivery, and whole-store cache snapshots are trusted audit evidence, not B-visible responses.",
                      "limitations": ["Single-owner product: B has an empty authorized view, no fabricated second owner binding.",
                                      "MailTurnExtension does not stream protected drafts; only validated final delivery.",
                                      "Browser/client-side caches and arbitrary plugin/OS paths are not covered."]}
            point("privacy_result", **result)
            point("delivery", response=delivery_by_case["V107"]["response"], metadata=delivery_by_case["V107"]["metadata"],
                  normal_final_reply=True, outbox_count=0)
            point("settlement", status="settled", coverage_complete=True,
                  background_pending=sum(not task.done() for task in loop._background_tasks),
                  running_subtasks=loop.subagents.get_running_count(), incoming_queue=bus.inbound.qsize(),
                  outgoing_queue=bus.outbound.qsize())
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if provider:
            await _close_provider(provider, primary_error)
        if service:
            await service.close()
        logger.remove(sink)


def worker_main(connection, options):
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    from loguru import logger
    logger.remove()  # _execute_task immediately installs the real capture sink.
    emit = Emitter(connection, options["run_id"], options["trial_id"])
    state = None
    try:
        with observation_context(emit.event, options["trial_id"],
                                 parent_span_id=options.get("parent_span_id")) as state:
            with span("task", role="foreground", case_id=options["case_id"], family_ids=["V095", "V107"]):
                asyncio.run(_execute_task(connection, options, emit))
    except BaseException as error:
        connection.send({"message": "worker_error", "error_kind": type(error).__name__})
    finally:
        if state is not None:
            connection.send({"message": "observation_summary", "producer_id": emit.producer,
                             "last_seq": emit.seq, "collector_errors": state.collector_errors})
        connection.close()
