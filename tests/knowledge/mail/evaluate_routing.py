"""Opt-in real-model routing evaluation over predeclared fictional cases.

Agent-authored labels are diagnostic and still require independent human review.
No mailbox, tools, real documents or user configuration are loaded.
"""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.contracts import GENERATION_MODEL, AccessContext, MailError
from nanobot.knowledge.mail.routing import MailRouter
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.check_llm import load_provider
from tests.knowledge.mail.eval_corpus import CASES

GENERAL = [
    "邮件一般怎么写？", "帮我写一封求职邮件模板", "如何礼貌地催对方回复邮件？",
    "解释一下IMAP和SMTP的区别", "Python怎么读取邮件附件？", "翻译：Please find attached my resume.",
    "报销通常需要什么凭证？", "劳动合同通常包含哪些条款？", "帮我设计一个订单数据库",
    "写一个提取订单编号的正则表达式", "为什么电子邮件需要SPF和DKIM？", "怎样在邮件里拒绝邀请？",
    "请给这句英文润色：Thanks for your reply.", "拟一份会议议程模板", "介绍混合检索和向量搜索的区别",
    "如何识别钓鱼邮件？", "一般的论文投稿流程是什么？", "把这段话翻译成英语：祝你周末愉快。",
    "帮我头脑风暴三个项目名称", "计算800乘以30%是多少",
]
MANAGE = ["检查邮件知识库的状态", "重试失败的邮件附件", "从本地知识库移除那封邮件",
          "恢复刚刚移除的邮件", "重建邮件的语义索引", "我想关闭邮件归档"]
FOLLOWUPS = ["地点呢？", "需要带什么？", "截止日期呢？", "那附件里有什么要求？", "有没有更新的安排？", "金额是多少？"]


def cases():
    rows = [{"question": question, "expected": "mail_fact", "focus": False} for _, _, question in CASES]
    rows += [{"question": question, "expected": "general", "focus": False} for question in GENERAL]
    rows += [{"question": question, "expected": "mail_manage", "focus": False} for question in MANAGE]
    rows += [{"question": question, "expected": "mail_fact", "focus": True} for question in FOLLOWUPS]
    return [{"id": f"routing-{i:03}", **row} for i, row in enumerate(rows)]


async def evaluate(credentials, output, case_ids=None):
    gold = cases()  # Fix labels before the first model call.
    if case_ids:
        gold = [row for row in gold if row["id"] in case_ids]
    provider = load_provider(credentials)
    result = {"model": GENERATION_MODEL, "thinking": "disabled", "kind": "real_model_synthetic_routing",
              "timestamp": datetime.now(UTC).isoformat(), "human_review": "pending",
              "router_sha256": hashlib.sha256(Path("nanobot/knowledge/mail/routing.py").read_bytes()).hexdigest(),
              "sample_scope": "fixed diagnostic cases; reruns after prompt changes are regression evidence, not new held-out evaluation",
              "gold_sha256": hashlib.sha256(json.dumps(gold, ensure_ascii=False).encode()).hexdigest(), "cases": []}
    with tempfile.TemporaryDirectory(prefix="nanobot-routing-eval-") as directory:
        service = MailService(Path(directory), MailKnowledgeConfig(enabled=True, semantic_enabled=False,
            owner_bindings=[MailOwnerBinding(binding_id="eval", principal_id="fictional-evaluator", channel="cli")]),
            worker_enabled=False)
        await service.start()
        security = service.access.authenticate("cli", "fictional-evaluator", private_chat=True)
        semaphore = asyncio.Semaphore(3)

        async def run(case):
            responses = []

            class RecordingProvider:
                async def chat_with_retry(self, **kwargs):
                    response = await provider.chat_with_retry(**kwargs)
                    # Fictional evaluation only. Never persist provider error bodies.
                    responses.append({"finish_reason": response.finish_reason,
                        "content": response.content if response.finish_reason != "error" else None})
                    return response

            access = AccessContext.from_security(security, "cli:" + case["id"])
            if case["focus"]:
                with service.store.connect(write=True, query_state=True) as db:
                    db.execute("INSERT INTO mail_focus VALUES(?,?,?,?,?,?)", (
                        access.owner_id, access.binding_id, access.session_key, "{}", "星桥入职邮件的报到安排是什么？", time.time() + 3600))
            begin = time.perf_counter()
            async with semaphore:
                try:
                    plan = await MailRouter(service, RecordingProvider(), "Asia/Shanghai").plan(access, case["question"])
                    row = {**case, "actual": plan.intent, "plan": plan.model_dump(mode="json")}
                except MailError as error:
                    row = {**case, "actual": "error", "error": error.code}
                except Exception:
                    row = {**case, "actual": "error", "error": "evaluation_call_failed"}
            row["seconds_including_queue"] = time.perf_counter() - begin
            row["fictional_model_responses"] = responses
            result["cases"].append(row)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Routing completed {len(result['cases'])}/{len(gold)}", flush=True)

        try:
            await asyncio.gather(*(run(case) for case in gold))
        finally:
            await provider._client.close()
            await service.close()
    positive = [row for row in result["cases"] if row["expected"] == "mail_fact"]
    negative = [row for row in result["cases"] if row["expected"] == "general"]
    result["metrics"] = {
        "mail_fact_recall": sum(r["actual"] == "mail_fact" for r in positive) / len(positive) if positive else None,
        "general_false_positive_rate": sum(r["actual"] == "mail_fact" for r in negative) / len(negative) if negative else None,
        "errors": sum(r["actual"] == "error" for r in result["cases"]),
        "positive_count": len(positive), "negative_count": len(negative),
        "management_correct": sum(r["expected"] == r["actual"] == "mail_manage" for r in result["cases"]),
    }
    result["cases"].sort(key=lambda row: row["id"])
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=Path("api.txt"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", action="append")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(evaluate(args.credentials, args.output, args.case_id))
