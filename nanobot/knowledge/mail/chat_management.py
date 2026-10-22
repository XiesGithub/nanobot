"""Bounded owner chat management; model plans never consume confirmation tokens."""

import asyncio
import json
import re
import time

from nanobot.knowledge.mail.contracts import MailError, MailQuery, ManageAction
from nanobot.knowledge.mail.routing import QueryPlan
from nanobot.knowledge.mail.store import identifier


async def manage_chat(service, router, access, question, plan):
    request = plan.management
    if request is None or request.action == "status":
        return (await service.status(access)).model_dump_json()
    service.access.require(access, "manage")
    if request.action == "clarify":
        return plan.clarification or "请明确要操作的邮件、附件或筛选范围。"
    if request.action == "cli_only":
        return "重新解析和重建索引请使用本机 nanobot mail manage 的 reparse / reindex 命令。"
    if request.action == "set_mode":
        if request.archive_enabled is None and request.trigger_agent is None:
            raise MailError("invalid_request")
        mode = await asyncio.to_thread(service.mode)
        result = await service.change_mode(access=access,
            archive_enabled=bool(mode["archive_enabled"]) if request.archive_enabled is None else request.archive_enabled,
            trigger_agent=bool(mode["trigger_agent"]) if request.trigger_agent is None else request.trigger_agent,
            expected_revision=mode["mode_revision"])
        return json.dumps(result, ensure_ascii=False)

    # IDs absent from the user's input cannot become an authorization shortcut.
    explicit = set(re.findall(r"\b(?:mail|doc|task|ev)_[a-f0-9]+\b", question))
    if any(target not in explicit for target in request.target_ids):
        raise MailError("target_selection_required")
    targets = list(dict.fromkeys(request.target_ids))
    if targets and (request.selection or request.use_focus):
        raise MailError("ambiguous_target_selection")
    if not targets:
        if request.action in {"request_restore", "dismiss_interaction", "prepare_interaction_retry"}:
            return "请从已移除来源或不确定交互列表选择明确的 ID。"
        if request.use_focus:
            with service.store.connect(query_state=True) as db:
                focus = db.execute("SELECT filters_json FROM mail_focus WHERE owner_id=? AND binding_id=? AND session_key=? AND expires_at>?",
                    (access.owner_id, access.binding_id, access.session_key, time.time())).fetchone()
            if not focus:
                return "当前没有有效的邮件选择，请先查询并明确目标。"
            if len(json.loads(focus[0]).get("mail_ids") or []) != 1:
                return "上一范围包含多封邮件或尚未确定目标，请提供明确来源 ID 或筛选条件。"
            query = MailQuery(mode="enumerate", scope_action="inherit")
        elif request.selection:
            query = router.queries(QueryPlan(intent="mail_fact", queries=[request.selection]))[0]
            # Never silently throw away semantic text to obtain an all-mail mutation.
            filters = (query.exact_terms or query.sender_addresses or query.sender_name_hint
                or query.subject_terms or query.received_from or query.received_before
                or query.mail_ids or query.document_ids)
            if query.query or not filters or query.scope_action != "replace":
                return "请用明确的发件人、时间、主题、编号或来源 ID 限定管理范围。"
            query = query.model_copy(update={"mode": "enumerate"})
        else:
            return "请先选择要操作的邮件或附件；不会把未明确的范围当作全部资料。"
        page = await service.search(access, query)
        with service.store.connect(query_state=True) as db:
            row = db.execute("SELECT snapshot_json FROM query_runs WHERE query_id=? AND owner_id=? AND binding_id=? AND session_key=?",
                (page.query_id, access.owner_id, access.binding_id, access.session_key)).fetchone()
        if not row:
            raise MailError("query_state_unavailable")
        targets = list(json.loads(row[0])["mails"])
        if len(targets) > 100:
            return f"当前范围匹配 {len(targets)} 封邮件，超过单次 100 封上限。请缩小范围后重新预览。"
    if request.action in {"retry", "prepare_refetch"}:
        with service.store.connect() as db:
            targets = [row[0] for row in db.execute("""SELECT d.document_id FROM documents d
                JOIN mail_items m USING(mail_id) WHERE m.owner_id=? AND m.deleted_at IS NULL
                AND (d.document_id IN (SELECT value FROM json_each(?)) OR
                     m.mail_id IN (SELECT value FROM json_each(?)))
                AND (d.extract_status!='ready' OR json_extract(d.coverage_json,'$.complete')=0)
                ORDER BY d.document_id LIMIT 101""", (access.owner_id, json.dumps(targets), json.dumps(targets)))]
    if not targets:
        return "当前范围没有符合此操作条件的来源。"
    if len(targets) > 100:
        return "当前匹配的文档超过单次 100 项上限，请缩小范围。"
    receipt = await service.manage(access, ManageAction(action=request.action,
        target_ids=targets, request_id=identifier("chat_")))
    text = receipt.model_dump_json()
    if receipt.confirmation_token:
        if request.action == "prepare_remove":
            text += "\n将移除所列邮件及附件的本地正文、原件与索引，使旧引用失效。服务器邮件不变。"
            text += "已交付的历史答案、系统备份与介质残留不承诺擦除；仅保留恢复所需的账户、文件夹、UID 定位和旧 ID。"
        elif request.action in {"prepare_refetch", "request_restore"}:
            text += "\n确认后会连接原邮箱，仅 PEEK 获取所选来源。不标已读，不运行 Agent，不发送邮件。"
        text += "\n\n请核对以上目标。若确定，在本会话发送：\n/mail-confirm " + receipt.confirmation_token
        text += "\n令牌有效期 5 分钟，仅使用一次。模型不能代为确认。"
    return text
