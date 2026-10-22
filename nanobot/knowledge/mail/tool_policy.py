"""Per-turn registry view. Complete batches enter restricted state before execution."""

from nanobot.agent.tools.ask import AskUserTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.knowledge.mail.contracts import MailError


class MailToolView(ToolRegistry):
    def __init__(self, source: ToolRegistry, auth, access=None, *, restored=None):
        super().__init__()
        self._tools = dict(source._tools)
        self.auth, self.access = auth, access
        self.restricted = access is None
        self.private_entered = False
        self._persisted = False
        if restored is not None:
            if restored.get("version") != 1:
                raise MailError("checkpoint_reauthorization_required")
            self.private_entered = bool(restored.get("private_entered"))
            self.restricted = self.restricted or self.private_entered
            if self.private_entered:
                if access is None:
                    raise MailError("unauthorized")
                auth.require(access)
                if (restored.get("binding_id"), restored.get("policy_revision")) != (
                    access.binding_id, access.policy_revision,
                ):
                    raise MailError("checkpoint_reauthorization_required")

    def policy_snapshot(self):
        return {"version": 1, "private_entered": self.private_entered,
                "restricted": self.restricted,
                "binding_id": self.access.binding_id if self.access else None,
                "policy_revision": self.access.policy_revision if self.access else None}

    async def preflight(self, calls, checkpoint):
        private = self.private_entered or any(
            getattr(self._tools.get(call.name), "private_capability", None) for call in calls
        )
        if not private:
            return
        if self.access is None:
            raise MailError("unauthorized")
        self.auth.require(self.access)
        self.private_entered = self.restricted = True
        self._cached_definitions = None
        if not self._persisted:
            if checkpoint is None:
                raise MailError("checkpoint_required")
            # Failed durability stops ALL tools in the batch, including private reads.
            await checkpoint({"phase": "restricted_preflight", "tool_policy": self.policy_snapshot(),
                              "pending_tool_calls": [call.to_openai_tool_call() for call in calls]})
            self._persisted = True

    def _allowed(self, name):
        tool = self._tools.get(name)
        if tool is None:
            return False
        if not self.restricted:
            return True
        if self.access is None:
            return type(tool) is AskUserTool
        return getattr(tool, "private_capability", None) in {"mail_read", "mail_status", "mail_manage"}

    def get_definitions(self):
        return [self._tools[name].to_schema() for name in sorted(self._tools) if self._allowed(name)]

    def prepare_call(self, name, params):
        if not self._allowed(name):
            return None, params, "Error: tool_blocked_by_mail_policy"
        tool = self._tools.get(name)
        if getattr(tool, "private_capability", None):
            if not self._persisted or not self.private_entered:
                return None, params, "Error: tool_blocked_by_mail_policy"
            self.auth.require(self.access)
        return super().prepare_call(name, params)
