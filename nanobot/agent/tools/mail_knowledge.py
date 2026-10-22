"""Mail service tools. Authentication comes only from the task-local request context."""

from contextvars import ContextVar

from nanobot.agent.tools.base import Tool
from nanobot.knowledge.mail.contracts import (
    AccessContext,
    MailError,
    MailQuery,
    ManageAction,
    SourceRead,
)


class _MailTool(Tool):
    _scopes = {"core"}
    _operation = ""
    _request_model = None

    def __init__(self, service):
        self.service = service
        self._context = ContextVar("mail_tool_context_" + self._operation, default=None)

    @classmethod
    def enabled(cls, ctx):
        return getattr(ctx, "mail_service", None) is not None and ctx.mail_service.config.enabled

    @classmethod
    def create(cls, ctx):
        return cls(ctx.mail_service)

    @property
    def name(self):
        return self._operation

    @property
    def parameters(self):
        return self._request_model.model_json_schema() if self._request_model else {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def description(self):
        return "Access authorized personal mail evidence. Requires owner authentication; cannot confirm actions or send messages."

    @property
    def read_only(self):
        return self._operation != "mail_knowledge_manage"

    def set_context(self, ctx):
        self._context.set(ctx)

    async def execute(self, **kwargs):
        ctx = self._context.get()
        if ctx is None:
            raise MailError("unauthorized")
        access = AccessContext.from_security(ctx.security, ctx.session_key or "")
        self.service.access.require(access)
        request = self._request_model.model_validate(kwargs) if self._request_model else None
        if self._operation == "mail_search":
            result = await self.service.search(access, request)
        elif self._operation == "mail_read_source":
            result = await self.service.read_source(access, request)
        elif self._operation == "mail_knowledge_status":
            result = await self.service.status(access)
        else:
            if request.action in {"reparse", "reindex"}:
                raise MailError("management_cli_required")
            result = await self.service.manage(access, request)
        return result.model_dump_json()


class MailSearchTool(_MailTool):
    _operation = "mail_search"
    _request_model = MailQuery
    private_capability = "mail_read"


class MailReadSourceTool(_MailTool):
    _operation = "mail_read_source"
    _request_model = SourceRead
    private_capability = "mail_read"


class MailKnowledgeStatusTool(_MailTool):
    _operation = "mail_knowledge_status"
    private_capability = "mail_status"


class MailKnowledgeManageTool(_MailTool):
    _operation = "mail_knowledge_manage"
    _request_model = ManageAction
    private_capability = "mail_manage"
