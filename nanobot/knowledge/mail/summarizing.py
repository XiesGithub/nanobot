"""Bounded whole-snapshot summaries; continuation never starts a fresh search."""

from __future__ import annotations

import json

from nanobot.knowledge.mail.answering import VerifiedAnswer
from nanobot.knowledge.mail.contracts import MailError, SearchPage, SourceRead
from nanobot.knowledge.mail.store import json_text


def _batch(service, access, run, cursor, touched):
    """Walk stable mail/ordinal keys, retaining the unfinished mail on budget exhaustion."""
    service.access.require(access)
    snapshot = json.loads(run["snapshot_json"])
    evidence, size = {}, 0
    position = dict(cursor)
    with service.store.connect(query_state=True) as state:
        for mid in sorted(snapshot["mails"]):
            if mid < position["mail"] or (mid == position["mail"] and position["ordinal"] is None):
                continue
            if mid not in touched and len(touched) >= 50:
                return evidence, position, True
            touched.add(mid)
            start = position["ordinal"] if mid == position["mail"] else -1
            for row in state.execute(
                "SELECT * FROM query_evidence WHERE query_id=? AND mail_id=? AND ordinal>? ORDER BY ordinal",
                (run["query_id"], mid, start),
            ):
                try:
                    item = service.retrieval._evidence(access, row)
                except MailError as error:
                    if error.code != "source_removed":
                        raise
                    position = {"mail": mid, "ordinal": row["ordinal"]}
                    continue
                cost = len(item.model_dump_json().encode("utf-8"))
                if size + cost > 18000 or len(evidence) >= 10:
                    if not evidence:
                        raise MailError("evidence_budget_exceeded")
                    return evidence, position, True
                evidence[item.evidence_id] = item
                size += cost
                position = {"mail": mid, "ordinal": row["ordinal"]}
            position = {"mail": mid, "ordinal": None}
    return evidence, position, False


async def summarize(answerer, access, question, queries, *, continuation=False):
    service = answerer.service
    service.access.require(access)
    scope = (access.owner_id, access.binding_id, access.session_key)
    if continuation:
        with service.store.connect(query_state=True) as state:
            progress = state.execute(
                "SELECT * FROM summary_progress WHERE owner_id=? AND binding_id=? AND session_key=?",
                scope,
            ).fetchone()
        if not progress:
            raise MailError("summary_not_started")
        query_id = progress["query_id"]
        saved = json.loads(progress["cursor_json"])
        cursor, question = saved["position"], saved["question"]
        unverified = saved.get("unverified_batches", 0)
    else:
        if len(queries) != 1 or queries[0].mode != "enumerate" or queries[0].cursor:
            raise MailError("summary_requires_single_range")
        page = await service.search(access, queries[0])
        query_id, cursor = page.query_id, {"mail": "", "ordinal": None}
        unverified = 0
    run = service.retrieval._run(access, query_id)
    if json.loads(run["filters_json"]).get("mode") != "enumerate":
        raise MailError("summary_requires_single_range")
    snapshot = json.loads(run["snapshot_json"])
    coverage = json.loads(run["coverage_json"])
    warnings = list(coverage["warnings"])
    parts, citations, claims, touched = [], {}, [], set()
    more = False
    # At most four generation/audit requests, leaving room for the route request.
    # Batch failures fall back to actual sources; they never trigger extra model retries.
    for _ in range(2):
        evidence, cursor, more = _batch(service, access, run, cursor, touched)
        if evidence:
            page = SearchPage(query_id=query_id, applied_filters=json.loads(run["filters_json"]),
                matches=list(evidence.values()), matching_mail_count=coverage["matching_mail_count"],
                warnings=[*warnings, "summary_batch_is_partial_scope"])
            answer = await answerer.answer_evidence(access, question, [page], evidence,
                [query_id], page.warnings, attempts=1, summary_batch=True)
            unverified += answer.status == "unverified_draft_rejected"
            parts.append(answer.text)
            citations.update((e.evidence_id, e) for e in answer.citations)
            claims.extend(answer.claims)
        if not more or len(touched) >= 50:
            break
    # An earlier batch may have been deleted/revoked while the later one generated.
    for key in citations:
        await service.read_source(access, SourceRead(evidence_id=key))
    service.access.require(access)
    run = service.retrieval._run(access, query_id)
    live_snapshot = json.loads(run["snapshot_json"])
    live_mails = set(live_snapshot["mails"])
    statuses = [doc["status"] for doc in snapshot.get("document_statuses", {}).values()
                if doc["mail_id"] in live_mails]
    with service.store.connect(query_state=True) as state:
        evidence_mails = {row[0] for row in state.execute(
            "SELECT DISTINCT mail_id FROM query_evidence WHERE query_id=?", (query_id,))}
    processed = sum(mid in evidence_mails and (mid < cursor["mail"] or
        (mid == cursor["mail"] and cursor["ordinal"] is None)) for mid in live_mails)
    counts = {"matched_mail_count": coverage["matching_mail_count"],
        "active_matched_mail_count": len(live_mails), "processed_mail_count": processed,
        "inspected_this_turn": len(touched), "failed_documents": statuses.count("failed"),
        "partial_documents": statuses.count("partial"),
        "pending_documents": sum(s in {"pending", "queued", "processing"} for s in statuses),
        "unverified_batches": unverified}
    with service.store.connect(write=True, query_state=True) as state:
        state.execute("INSERT OR REPLACE INTO summary_progress VALUES(?,?,?,?,?)",
                      (*scope, query_id, json_text({"position": cursor, "question": question,
                                                  "unverified_batches": unverified})))
    header = (f"本次匹配集合：{counts['matched_mail_count']} 封；当前仍有效 {len(live_mails)} 封；"
        f"累计已处理可用证据 {processed} 封；本轮检查 {len(touched)} 封。\n"
        f"失败文档 {counts['failed_documents']}，部分文档 {counts['partial_documents']}，"
        f"待处理文档 {counts['pending_documents']}。")
    text = header + "\n\n" + "\n\n".join(
        f"批次 {i}（引用编号仅用于本批次）：\n{part}" for i, part in enumerate(parts, 1))
    text += ("\n\n本轮预算已用完，部分邮件或长文档尚未处理。可说“继续上一批”，"
             "或缩小范围重新查询；原集合自创建起 24 小时到期。" if more else
             "\n\n本次匹配集合已遍历；未取得的文档内容未纳入事实汇总。")
    if not parts:
        text += "\n本轮没有可生成结论的新证据。"
    if unverified:
        warnings.append(f"summary_unverified_batches:{unverified}")
        text += f"\n累计 {unverified} 个批次未能生成经验证结论，已提供原文；集合遍历完成不表示摘要验证通过。"
    return VerifiedAnswer(text=text, citations=list(citations.values()), query_ids=[query_id],
        status="summary_partial" if more else (
            "summary_complete_with_fallback" if unverified else "summary_complete"),
        warnings=warnings, claims=claims,
        coverage=counts)
