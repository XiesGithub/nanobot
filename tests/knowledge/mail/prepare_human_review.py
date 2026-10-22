"""Export immutable fictional evaluation evidence for independent human review.

No model is called and no label is prefilled. The export is not acceptance evidence
until a human supplies verdicts and identifies the reviewed report versions.
"""

import hashlib
import json
from pathlib import Path

REPORTS = (
    "answers-deepseek-v2-regression.json",
    "answers-contracts-deepseek-v1.json",
    "summary-deepseek-v3-budget-regression.json",
)


def prepare(root: Path):
    review = {"reviewer": None, "reviewed_at": None, "reports": {}, "cases": []}
    lines = ["# 邮件 RAG 人工证据复核包", "",
        "状态：未复核；所有材料为虚构邮件。不得由模型代填人工判定。", "",
        "在 human-review-template.json 的副本中填写 reviewer、reviewed_at。每题填写 "
        "case_supported（true/false）及 notes；每条结论填写 human_supported（true/false）和 notes。"
        "不确定时保持 null，并说明缺失的证据。不要修改原始评测报告。", "",
        "逐条检查金额、日期、实体归属、当前/历史引用角色、推断标识及范围。"
        "逐题检查是否回答问题、遗漏冲突、把无证据当确定结论、是否隐瞒未处理范围。"
        "原文退回无需强行计作错答，但须确认没有冒充已验证结论。", "",
        "人工关键事实支持率分母为全部待评结论；有 null 时不能宣布人工门槛通过。"
        "目标为 ≥95%，任何金额/日期错答还必须逐项修复和回归。"
        "案例层面的编造、隐藏冲突或虚构来源仍独立阻断发布。", ""]
    count = 0
    for filename in REPORTS:
        path = root / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        review["reports"][filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.extend([f"## {filename}", ""])
        for index, case in enumerate(data.get("cases", data.get("rounds", [])), 1):
            case_id = f"{filename}:{index}"
            entry = {"case_id": case_id, "question": case["question"],
                     "case_supported": None, "notes": "", "claims": []}
            lines.extend([f"### {case_id}", "", case["question"], "",
                f"响应状态：{case.get('answer', {}).get('status', case.get('error', 'unknown'))}", "",
                "<details><summary>完整原答复（含范围与引用）</summary>", "",
                # HTML escaping prevents source text from becoming active markup.
                "<pre>" + escape(case.get("answer", {}).get("text", "")) + "</pre>",
                "</details>", ""])
            for claim_number, claim in enumerate(case.get("claim_reviews", []), 1):
                count += 1
                entry["claims"].append({"claim_number": claim_number,
                    "text": claim["claim"]["text"], "human_supported": None, "notes": ""})
                lines.extend([f"**结论 {claim_number}**", "", "<pre>" + escape(
                    json.dumps(claim["claim"], ensure_ascii=False, indent=2)) + "</pre>", ""])
                for source in claim["sources"]:
                    proof = {key: source.get(key) for key in (
                        "evidence_id", "mail_id", "document_id", "subject", "filename",
                        "content_role", "role_basis", "locator", "coverage", "excerpt")}
                    lines.extend(["<pre>" + escape(json.dumps(proof, ensure_ascii=False, indent=2))
                                  + "</pre>", ""])
            review["cases"].append(entry)
    review["claim_count"] = count
    (root / "human-review-template.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "human-review.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"cases": len(review["cases"]), "claims": count, "human_review": "pending"}))


def escape(text):
    import html
    return html.escape(text)


def prepare_gold(root: Path):
    """Separate label review; never overwrite the answer review while a user works."""
    names = ('local-semantic-v1.json', 'routing-deepseek-v5-summary-regression.json')
    review = {'reviewer': None, 'reviewed_at': None, 'reports': {}, 'cases': []}
    lines = ['# 检索与路由 gold 人工核验', '',
        '这些初始标签由 Agent 编制，尚未取得人工确认。此包补充答案事实复核，不能互相替代。', '',
        '复制 gold-review-template.json 为 gold-review-results.json 后填写。核对问题、检索目标原文、'
        'mail/document/block 定位与调参/留出划分；路由核对上下文 focus 与预期意图。'
        '不认可时填 false 并说明修订，存疑保持 null。所有判定必须由人填写。', '',
        '只是在固定已有集合上核验标签，不会使同集反复调参变成新的独立留出评测。', '']
    for name in names:
        path = root / name
        data = json.loads(path.read_text(encoding='utf-8'))
        review['reports'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for case in data['cases']:
            identity = name + ':' + case['id']
            expected = {k: case[k] for k in ('question', 'split', 'uid', 'gold_text',
                'gold_mail_id', 'gold_document_id', 'expected', 'focus') if k in case}
            review['cases'].append({'case_id': identity, 'expected': expected,
                'human_gold_supported': None, 'notes': ''})
            evidence = {**expected}
            if 'returned' in case:
                evidence['returned_sources'] = case['returned']
            else:
                evidence['actual'] = case.get('actual')
                evidence['plan'] = case.get('plan')
            lines.extend(['## ' + identity, '', '<pre>' + escape(json.dumps(evidence,
                ensure_ascii=False, indent=2)) + '</pre>', ''])
    (root / 'gold-review-template.json').write_text(json.dumps(review, ensure_ascii=False, indent=2)
        + '\n', encoding='utf-8')
    (root / 'gold-review.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'gold_cases': len(review['cases']), 'human_review': 'pending'}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--gold-only', action='store_true')
    args = parser.parse_args()
    (prepare_gold if args.gold_only else prepare)(Path("docs/mail-rag-evaluation"))
