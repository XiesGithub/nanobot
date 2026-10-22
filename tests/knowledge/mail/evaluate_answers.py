"""Real BGE + deepseek routing/answering over fictional mail, with human review export.

This records mechanical citation correctness and generated claims separately.
Neither the built-in model audit nor source retrieval counts as human support scoring.
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.answering import MailAnswerService
from nanobot.knowledge.mail.contracts import GENERATION_MODEL, AccessContext, MailError, SourceRead
from nanobot.knowledge.mail.routing import MailRouter
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.check_llm import load_provider
from tests.knowledge.mail.eval_corpus import VERSION, corpus
from tests.knowledge.mail.test_retrieval import ingest


async def evaluate(credentials, model_path, output, contracts=False, case_uids=None):
    import torch
    torch.set_num_threads(4)
    mails, gold = corpus()
    if contracts:
        from tests.knowledge.mail.eval_contract_cases import contract_corpus
        mails, gold = contract_corpus()
    if case_uids:
        gold = [case for case in gold if case["uid"] in case_uids]
        if not gold:
            raise ValueError("no_matching_evaluation_cases")
    result = {"kind": "real_model_end_to_end_fictional_answers", "corpus_version": VERSION,
              "model": GENERATION_MODEL, "thinking": "disabled", "timestamp": datetime.now(UTC).isoformat(),
              "human_claim_support_review": "pending", "cases": [],
              "evaluation_scope": "prospective_contract_cases" if contracts else "fixed_regression_cases",
              "gold_sha256": hashlib.sha256(json.dumps(gold, ensure_ascii=False).encode()).hexdigest(),
              "code_sha256": {name: hashlib.sha256(Path("nanobot/knowledge/mail", name).read_bytes()).hexdigest()
                              for name in ("answering.py", "routing.py", "retrieval.py")}}
    provider = load_provider(credentials)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nanobot-answer-eval-") as directory:
        service = MailService(Path(directory), MailKnowledgeConfig(enabled=True,
            embedding_model_path=str(model_path.resolve()), owner_bindings=[MailOwnerBinding(
                binding_id="eval", principal_id="fictional-evaluator", channel="cli")]), worker_enabled=False)
        await service.start()
        security = service.access.authenticate("cli", "fictional-evaluator", private_chat=True)
        identities = {mail.source.uid: ingest(service.store, mail).mail_id for mail in mails}
        encoder = service.retrieval.encoder
        if encoder is None:
            raise RuntimeError("local_encoder_unavailable")
        while encoder.index_pending(service.store):
            pass
        result["embedding_revision"] = encoder.revision
        result["mail_count"] = len(mails)
        result["attachment_count"] = sum(len(mail.parts) for mail in mails)
        print("Fictional corpus indexed; starting real routing and answer calls.", flush=True)
        gate = asyncio.Semaphore(2)

        async def run(case):
            access = AccessContext.from_security(security, "cli:answer-" + str(case["uid"]))
            responses = []

            class RecordingProvider:
                async def chat_with_retry(self, **kwargs):
                    response = await provider.chat_with_retry(**kwargs)
                    # This opt-in runner handles fictional inputs only. Never
                    # persist provider error bodies, credentials or client state.
                    responses.append({
                        "finish_reason": response.finish_reason,
                        "content": response.content if response.finish_reason != "error" else None,
                        "validation_feedback": [message["content"] for message in kwargs["messages"]
                            if message["role"] == "user" and isinstance(message.get("content"), str)
                            and message["content"].startswith("The draft failed validation (")],
                    })
                    return response

            recording_provider = RecordingProvider()
            async with gate:
                started = time.perf_counter()
                row = {**case, "gold_mail_id": identities[case["uid"]], "claim_reviews": []}
                row["gold_mail_ids"] = [identities[uid] for uid in case.get("gold_uids", [case["uid"]])]
                try:
                    router = MailRouter(service, recording_provider, "Asia/Shanghai")
                    plan = await router.plan(access, case["question"])
                    row["plan"] = plan.model_dump(mode="json")
                    if plan.intent != "mail_fact":
                        row["error"] = "not_routed_to_mail_fact"
                    else:
                        answer = await MailAnswerService(service, recording_provider).answer(access, case["question"], router.queries(plan))
                        checks = []
                        for citation in answer.citations:
                            source = (await service.read_source(access, SourceRead(evidence_id=citation.evidence_id))).sources[0]
                            reconstructed = []
                            with service.store.connect() as db:
                                for span in source.locator["spans"]:
                                    block = db.execute("SELECT text,locator_json FROM document_blocks WHERE document_id=? AND generation=? AND ordinal=?",
                                        (source.document_id, source.generation, span["block_ordinal"])).fetchone()
                                    checks.append(block is not None and json.loads(block[1]) == span["locator"])
                                    if block:
                                        reconstructed.append(block[0][span["start"]:span["end"]])
                            checks.append(source == citation and "".join(reconstructed) == source.excerpt)
                        row.update(answer=answer.model_dump(mode="json"), citation_checks=checks,
                            cites_gold_mail=any(c.mail_id == row["gold_mail_id"] for c in answer.citations),
                            cites_all_gold_mails=set(row["gold_mail_ids"]) <= {c.mail_id for c in answer.citations})
                        evidence = {item.evidence_id: item.model_dump(mode="json") for item in answer.citations}
                        row["claim_reviews"] = [{"claim": claim.model_dump(mode="json"),
                            "sources": [evidence[key] for key in claim.evidence_ids],
                            "human_supported": None, "human_notes": ""} for claim in answer.claims]
                except MailError as error:
                    row["error"] = error.code
                except Exception:
                    row["error"] = "evaluation_failed"
                row["seconds"] = time.perf_counter() - started
                row["fictional_model_responses"] = responses
                result["cases"].append(row)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"Answers completed {len(result['cases'])}/{len(gold)}", flush=True)

        try:
            await asyncio.gather(*(run(case) for case in gold))
        finally:
            await provider._client.close()
            await service.close()
    rows = result["cases"]
    checks = [check for row in rows for check in row.get("citation_checks", [])]
    result["metrics"] = {
        "cases": len(rows), "errors": sum("error" in row for row in rows),
        "answered": sum(row.get("answer", {}).get("status") == "answered" for row in rows),
        "fallback": sum(row.get("answer", {}).get("status") == "unverified_draft_rejected" for row in rows),
        "citation_checks": len(checks), "citation_checks_passed": sum(checks),
        "cases_citing_gold_mail": sum(row.get("cites_gold_mail", False) for row in rows),
        "claims_pending_human_review": sum(len(row["claim_reviews"]) for row in rows),
        "human_claim_support_rate": None,
        "median_seconds": statistics.median(row["seconds"] for row in rows),
    }
    result["cases"].sort(key=lambda row: row["uid"])
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path("api.txt"))
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contracts", action="store_true")
    parser.add_argument("--case-uid", type=int, action="append")
    args = parser.parse_args()
    asyncio.run(evaluate(args.credentials, args.model_path, args.output, args.contracts, args.case_uid))
