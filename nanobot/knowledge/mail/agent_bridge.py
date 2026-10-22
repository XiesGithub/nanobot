"""Protected turn interception, before any shared history/memory/checkpoint loading."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import replace
from weakref import WeakValueDictionary

from nanobot.agent.runner import AgentRunSpec
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.bus.events import OutboundMessage
from nanobot.knowledge.mail.answering import MailAnswerService
from nanobot.knowledge.mail.contracts import GENERATION_MODEL, AccessContext, MailError, MailQuery
from nanobot.knowledge.mail.dispatch import InteractionDispatch
from nanobot.knowledge.mail.routing import MailRouter
from nanobot.knowledge.mail.tool_policy import MailToolView
from nanobot.security.private_logging import private_logging
from nanobot.session.manager import SessionManager


class MailTurnExtension:
    def __init__(self, service, *, timezone="UTC"):
        self.service = service
        self.timezone = timezone
        self.sessions = SessionManager(service.store.root / "private", allow_legacy=False)
        service.private_sessions = self.sessions
        self.dispatch = InteractionDispatch(service.store)
        self._locks = WeakValueDictionary()

    def session_key(self, message):
        security = message.security
        principal = security.binding_id if security and security.authenticated else (
            "external:" + message.channel + ":" + (
                security.principal_id if security and security.principal_id else message.sender_id))
        digest = hashlib.sha256(f"{principal}\0{message.channel}\0{message.chat_id}".encode()).hexdigest()
        return "mail-private:" + digest

    @private_logging()
    async def intercept(self, loop, message):
        try:
            key = self.session_key(message)
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                security = message.security
                if security and security.origin_kind == "email_ingress":
                    return True, await self._incoming(loop, message)
                return True, await self._handle(loop, message)
        except MailError as error:
            # No generic-Agent fallback after a protected operation fails.
            return True, OutboundMessage(message.channel, message.chat_id,
                                          "邮件资料请求未完成：" + error.code, security=message.security)
        except Exception:
            # A provider/IO exception may carry the full private request in its
            # message or traceback locals. Do not let the generic loop log it
            # after this task-local privacy context has unwound.
            return True, OutboundMessage(message.channel, message.chat_id,
                "邮件资料请求未完成：mail_operation_unavailable", security=message.security)

    async def _incoming(self, loop, message):
        # The durable mode/state is authoritative across management processes.
        # Cancellation stops generation/tool scheduling; already-started external
        # effects cannot be rolled back. SMTP independently gates the actual send.
        security = message.security

        def active():
            if security.task_id:
                return self.dispatch.active(security.task_id, security.attempt_id)
            # Tracking may fail on disk-full; a degraded interaction is still
            # fenced by the mode revision captured at reception.
            mode = self.service.mode()
            return mode["trigger_agent"] and mode["mode_revision"] == security.mode_revision

        if not await asyncio.to_thread(active):
            return None
        task = asyncio.create_task(self._handle(loop, message))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.1)
                if done:
                    return await task
                if not await asyncio.to_thread(active):
                    return None
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _handle(self, loop, message):
        if hasattr(self.service, "start"):
            await self.service.start()
        if loop.model != GENERATION_MODEL:
            raise MailError("model_not_allowed")
        key = self.session_key(message)
        security = message.security
        access = None
        if security and security.authenticated and security.binding_id:
            access = AccessContext.from_security(security, key)
            self.service.access.require(access)
        # An authenticated but revoked user never falls through into external mode.
        task_id = security.task_id if security and security.origin_kind == "email_ingress" else None
        if task_id:
            if not security.attempt_id or not self.dispatch.transition(task_id, "dispatching", "running", attempt_id=security.attempt_id):
                return None
        session = self.sessions.get_or_create(key)
        checkpoint = session.metadata.get("runtime_checkpoint")
        if checkpoint and "tool_policy" not in checkpoint:
            session.metadata.pop("runtime_checkpoint", None)
            self.sessions.save(session)
            raise MailError("checkpoint_reauthorization_required")
        if access and message.content.startswith("/mail-confirm "):
            token = message.content.removeprefix("/mail-confirm ").strip()
            receipt = await self.service.confirm(access, token)
            return OutboundMessage(message.channel, message.chat_id, receipt.model_dump_json(),
                metadata={"private_mail": True, "mail_session_key": key}, security=security)
        private = False
        citations = []
        if access and self.service.config.enabled:
            router = MailRouter(self.service, loop.provider, self.timezone)
            plan = await router.plan(access, message.content)
            if plan.intent == "clarify":
                text = plan.clarification or "请明确要查询的邮件范围或对象。"
                private = True
            elif plan.intent == "mail_manage":
                from nanobot.knowledge.mail.chat_management import manage_chat
                text = await manage_chat(self.service, router, access, message.content, plan)
                private = True
            elif plan.intent == "mail_fact":
                answer = await MailAnswerService(self.service, loop.provider).answer(
                    access, message.content, router.queries(plan), continue_summary=plan.continue_summary)
                text, citations, private = answer.text, answer.citations, True
        if not private:
            view = MailToolView(loop.tools, self.service.access, access,
                                restored=checkpoint.get("tool_policy") if checkpoint else None)
            request_context = RequestContext(message.channel, message.chat_id, session_key=key,
                                             security=security)
            for name in loop.tools.tool_names:
                tool = loop.tools.get(name)
                if isinstance(tool, ContextAware):
                    tool.set_context(request_context)

            async def save_checkpoint(payload):
                if access:
                    self.service.access.require(access)
                session.metadata["runtime_checkpoint"] = payload
                self.sessions.save(session)

            # Both namespaces are private on disk. Owner evidence is never copied to
            # general memory, Dream, external history, or open-tool model turns.
            # Filter the stored records BEFORE get_history strips internal metadata.
            # Otherwise a private answer becomes ordinary history with open tools.
            clean_session = replace(session, messages=[
                m for m in session.messages if not m.get("mail_private")
            ], last_consolidated=0)
            history = clean_session.get_history(max_messages=40, max_tokens=8000)
            instructions = (
                "You are a helpful assistant. This is an isolated external conversation. "
                "Use only this conversation and the supplied incoming attachment content. "
                "Personal files, global memories and other mail are unavailable."
                if access is None else
                "You are a helpful assistant. Personal email facts require mail_search/read_source. "
                "Using private mail tools restricts the whole turn. Do not send or delegate retrieved content."
            )
            content = message.content
            if access is None and message.media:
                from nanobot.utils.document import extract_documents
                content, _ = extract_documents(content, message.media)
            result = await loop.runner.run(AgentRunSpec(
                initial_messages=[{"role": "system", "content": instructions}, *history,
                                  {"role": "user", "content": content}],
                tools=view, model=GENERATION_MODEL, max_iterations=loop.max_iterations,
                max_tool_result_chars=max(10000, loop.max_tool_result_chars), concurrent_tools=True,
                checkpoint_callback=save_checkpoint, workspace=None, session_key=key,
                llm_timeout_s=90,
            ))
            if getattr(result, "stop_reason", None) == "error":
                raise MailError("model_request_unavailable")
            if view.private_entered:
                # Discard unconstrained free text from fallback tool use. Generate and
                # validate an answer using the same dedicated evidence path.
                answer = await MailAnswerService(self.service, loop.provider).answer(
                    access, message.content, [MailQuery(query=message.content[:2000], scope_action="inherit")],
                )
                text, citations, private = answer.text, answer.citations, True
            else:
                text = result.final_content or "本轮未生成答复。"
        if access:
            self.service.access.require(access)
        session.add_message("user", message.content, mail_private=private)
        session.add_message("assistant", text, mail_private=private,
                            citations=[e.model_dump() for e in citations])
        session.metadata.update(owner_id=access.owner_id if access else None,
                                binding_id=access.binding_id if access else None,
                                channel=message.channel, chat_id=message.chat_id)
        session.metadata.pop("runtime_checkpoint", None)
        session.enforce_file_cap()  # No callback into shared memory.
        self.sessions.save(session)
        if task_id:
            self.dispatch.transition(task_id, "running", "completed", attempt_id=security.attempt_id)
        return OutboundMessage(message.channel, message.chat_id, text,
                               metadata={"citations": [e.model_dump() for e in citations], "private_mail": bool(access),
                                         "mail_session_key": key},
                               security=security)
