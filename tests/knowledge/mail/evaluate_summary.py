"""Opt-in real-model summary/continuation, exclusively fictional mails and identities."""

import argparse
import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.answering import MailAnswerService
from nanobot.knowledge.mail.contracts import GENERATION_MODEL, AccessContext, SourceRead
from nanobot.knowledge.mail.routing import MailRouter
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.check_llm import load_provider
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


async def evaluate(credentials, output):
    provider = load_provider(credentials)
    result = {"kind": "real_deepseek_fictional_full_range_summary", "model": GENERATION_MODEL,
        "timestamp": datetime.now(UTC).isoformat(), "mail_count": 55,
        "human_claim_support_review": "pending", "rounds": [],
        "code_sha256": {name: hashlib.sha256(Path("nanobot/knowledge/mail", name).read_bytes()).hexdigest()
                        for name in ("summarizing.py", "answering.py", "routing.py")}}
    calls = []

    class CountedProvider:
        async def chat_with_retry(self, **kwargs):
            assert kwargs["model"] == GENERATION_MODEL and kwargs["tools"] is None
            response = await provider.chat_with_retry(**kwargs)
            calls.append({"model": kwargs["model"], "max_tokens": kwargs["max_tokens"],
                "finish_reason": response.finish_reason,
                "content": response.content if response.finish_reason != "error" else None})
            return response

    counted = CountedProvider()
    try:
        with tempfile.TemporaryDirectory(prefix="nanobot-summary-eval-") as directory:
            service = MailService(Path(directory), MailKnowledgeConfig(enabled=True, semantic_enabled=False,
                owner_bindings=[MailOwnerBinding(binding_id="eval", principal_id="fictional", channel="cli")]),
                worker_enabled=False)
            await service.start()
            security = service.access.authenticate("cli", "fictional", private_chat=True)
            access = AccessContext.from_security(security, "cli:summary-eval")
            expected = set()
            for i in range(55):
                mail = envelope()
                mail.subject = f"虚构项目 SX-{i:03d}"
                mail.body = f"SX-{i:03d}项目的交付数量为{i + 10}件，交付地点为虚构园区。"
                expected.add(ingest(service.store, mail).mail_id)
            seen, snapshot = set(), None
            try:
                for round_number in range(8):
                    question = "汇总所有已归档项目邮件，逐封列出项目编号、交付数量和地点。" if round_number == 0 else "继续上一批"
                    before = len(calls)
                    router = MailRouter(service, counted, "Asia/Shanghai")
                    plan = await router.plan(access, question)
                    row = {"question": question, "plan": plan.model_dump(mode="json")}
                    if plan.intent != "mail_fact":
                        row["error"] = "not_routed_to_mail_fact"
                        result["rounds"].append(row)
                        break
                    answer = await MailAnswerService(service, counted).answer(access, question,
                        router.queries(plan), continue_summary=plan.continue_summary)
                    checks = []
                    for citation in answer.citations:
                        source = (await service.read_source(access, SourceRead(evidence_id=citation.evidence_id))).sources[0]
                        checks.append(source == citation and source.mail_id in expected)
                        seen.add(source.mail_id)
                    sources = {source.evidence_id: source.model_dump(mode="json") for source in answer.citations}
                    row.update(answer=answer.model_dump(mode="json"), model_calls=len(calls) - before,
                        fictional_model_responses=calls[before:],
                        citation_checks=checks, claim_reviews=[{"claim": claim.model_dump(mode="json"),
                            "sources": [sources[key] for key in claim.evidence_ids],
                            "human_supported": None, "human_notes": ""} for claim in answer.claims])
                    if snapshot is None:
                        snapshot = answer.query_ids
                        # An ordinary new arrival must not alter the continued snapshot.
                        late = envelope()
                        late.subject = "快照建立后的新增邮件"
                        late.body = "新增项目 Late-999 的数量是999件。"
                        ingest(service.store, late)
                    row["same_snapshot"] = answer.query_ids == snapshot
                    result["rounds"].append(row)
                    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print(f"Summary round {round_number + 1}: {answer.status}, {len(calls) - before} calls", flush=True)
                    if answer.status in {"summary_complete", "summary_complete_with_fallback"}:
                        break
                checks = [value for row in result["rounds"] for value in row.get("citation_checks", [])]
                result["metrics"] = {"rounds": len(result["rounds"]), "model_calls": len(calls),
                    "each_round_within_six_calls": all(row.get("model_calls", 99) <= 6 for row in result["rounds"]),
                    "snapshot_fixed": all(row.get("same_snapshot", False) for row in result["rounds"]),
                    "cited_unique_original_mails": len(seen), "citation_checks": len(checks),
                    "citation_checks_passed": sum(checks),
                    "completed": result["rounds"][-1].get("answer", {}).get("status") == "summary_complete"}
                result["metrics"]["claims_pending_human_review"] = sum(
                    len(row.get("claim_reviews", [])) for row in result["rounds"])
                result["metrics"]["raw_fallback_batches"] = sum(
                    row.get("answer", {}).get("text", "").count("当前无法可靠生成结论")
                    for row in result["rounds"])
            finally:
                await service.close()
    finally:
        await provider._client.close()
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path("api.txt"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(evaluate(args.credentials, args.output))
