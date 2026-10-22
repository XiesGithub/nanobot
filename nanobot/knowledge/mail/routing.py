"""Bounded intent/query planning and deterministic calendar arithmetic."""

import asyncio
import calendar
import json
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from nanobot.knowledge.mail.contracts import GENERATION_MODEL, MailError, MailQuery, Record
from nanobot.security.private_logging import private_logging


def calendar_window(period: str, now: datetime, timezone: str):
    local = now.astimezone(ZoneInfo(timezone))
    previous_year = local.year - (local.month == 1)
    previous_month = 12 if local.month == 1 else local.month - 1
    if period == "previous_month":
        end = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return end.replace(year=previous_year, month=previous_month), end
    if period == "rolling_month":
        return local.replace(year=previous_year, month=previous_month,
                             day=min(local.day, calendar.monthrange(previous_year, previous_month)[1])), local
    if period == "last_30_days":
        return local - timedelta(days=30), local
    raise MailError("invalid_date_period")


class PlannedQuery(MailQuery):
    relative_period: Literal["previous_month", "rolling_month", "last_30_days"] | None = None


class ManagementPlan(Record):
    action: Literal["status", "set_mode", "retry", "prepare_remove", "request_restore",
                    "prepare_refetch", "dismiss_interaction", "prepare_interaction_retry",
                    "cli_only", "clarify"]
    target_ids: list[str] = Field(default_factory=list, max_length=100)
    selection: PlannedQuery | None = None
    use_focus: bool = False
    archive_enabled: bool | None = None
    trigger_agent: bool | None = None


class QueryPlan(Record):
    intent: Literal["mail_fact", "mail_manage", "general", "clarify"]
    queries: list[PlannedQuery] = Field(default_factory=list, max_length=5)
    clarification: str = Field(default="", max_length=500)
    management: ManagementPlan | None = None
    continue_summary: bool = False


class MailRouter:
    def __init__(self, service, provider, timezone="UTC"):
        self.service, self.provider, self.timezone = service, provider, timezone

    @private_logging()
    async def plan(self, access, text: str) -> QueryPlan:
        self.service.access.require(access)
        if len(text) > 10000:
            raise MailError("question_too_long")
        with self.service.store.connect(query_state=True) as db:
            focus = db.execute("SELECT filters_json,last_query FROM mail_focus WHERE owner_id=? AND binding_id=? AND session_key=? AND expires_at>strftime('%s','now')",
                               (access.owner_id, access.binding_id, access.session_key)).fetchone()
        context = {"has_mail_focus": bool(focus), "last_question": focus["last_query"] if focus else None}
        instruction = """Classify the owner's request as mail_fact, mail_manage, general, or clarify.
mail_manage includes inspecting archive health/status, enabling/disabling mail archiving or
incoming-agent interaction, retrying failures, removing/restoring sources and rebuilding indexes.
These are operational requests even when they contain no question about a mail's contents.
Personal archived-email facts include synonyms and short followups (e.g. 地点呢).
An owner asking about requirements, terms, arrangements, limits or permissions for a
specifically named project, organization, agreement or event needs source-grounded mail_fact
routing, even without saying "email". Do not substitute generic advice for unknown specific
rules. For example, a particular project's visitor requirements or a particular outage's
available functions require evidence. Public encyclopedic facts, general tutorials and
composition requests remain general. Specific agreement terms differ from general legal advice.
Ordinary composition/advice like 邮件一般怎么写 is general; do not search it.
Return JSON matching the schema. exact_terms are mandatory literal filters, not search
keywords. Use them for explicit identifiers (order numbers, email addresses), explicitly
requested exact names or quoted literal matches; preserve punctuation. Otherwise leave
exact_terms empty and put the full semantic request, including proper names, in query.
Do not turn ordinary descriptions, paraphrases or a name joined to a generic noun into
mandatory literal filters. The archive may use synonyms or a different full name.
Use exact_operator=any only when the user explicitly requests either/or; default to all.
Comparison of different orders uses separate all queries, never one all query combining orders.
Use scope_action=inherit only for a relevant followup, expand for later changes/other sources,
replace for a new subject. Do not invent source IDs. Explicit document restrictions remain hard.
Use relative_period previous_month for 上个月; rolling_month for 最近一个月; last_30_days for 最近30天.
Use mode=enumerate for complete-range requests and mention scope limits. Other facts use hybrid.
Set continue_summary=true ONLY for an explicit request to continue the previous summary batch.
Do not set it for an ordinary short factual followup or a new summary with changed filters.
For a complete-range summary use ONE enumerate query with explicit metadata/exact filters.
Its query is the summary topic, not a semantic filter of the enumerated mail set; do not
promise semantic completeness of the entire mailbox.
No evidence is provided at this stage, so do not answer the question or invent sender mappings.
For mail_manage, fill management. status only reads health; set_mode changes only the
explicitly requested archive_enabled/trigger_agent flag and leaves the other null.
Removal/refetch/restore/interaction retry ONLY prepare a preview requiring a separate
authenticated confirmation; you cannot confirm. retry means reprocessing failed mail
attachments using existing local input, never rerunning the Agent. prepare_interaction_retry
is only for explicitly rerunning an uncertain incoming Agent interaction that may have had
external effects. Do not confuse these two operations.
reparse/reindex are cli_only. Copy opaque target_ids only if present in the user's request;
never invent IDs. A clear reference to previously selected mail uses use_focus=true.
For a named range use selection with explicit sender/time/subject/exact filters and mode
enumerate, query empty. Do not use semantic top-k as a deletion range. If the requested
scope is ambiguous, retain intent=mail_manage and use management.action=clarify.
Restoring removed sources requires their old opaque IDs.
"""
        response = await asyncio.wait_for(self.provider.chat_with_retry(
            model=GENERATION_MODEL, tools=None, temperature=0, max_tokens=1500,
            messages=[{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps({
                "text": text, "context": context, "schema": QueryPlan.model_json_schema(),
                "timezone": self.timezone, "now": datetime.now(ZoneInfo(self.timezone)).isoformat(),
            }, ensure_ascii=False)}],
        ), timeout=45)
        self.service.access.require(access)
        if response.finish_reason == "error" or response.tool_calls:
            raise MailError("intent_unavailable")
        try:
            plan = QueryPlan.model_validate_json(response.content or "")
        except ValueError:
            raise MailError("intent_unavailable") from None
        if plan.intent == "mail_fact" and not plan.queries:
            plan.queries = [PlannedQuery(query=text[:2000])]
        return plan

    def queries(self, plan: QueryPlan):
        result = []
        for planned in plan.queries:
            data = planned.model_dump(exclude={"relative_period"}, exclude_unset=True)
            if planned.relative_period:
                begin, end = calendar_window(planned.relative_period, datetime.now(ZoneInfo(self.timezone)), self.timezone)
                data.update(received_from=begin, received_before=end)
            result.append(MailQuery.model_validate(data))
        return result
