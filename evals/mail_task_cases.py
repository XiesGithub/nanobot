"""Frozen original-requirement mail full-task profile, separate from archive coverage."""

import copy

from evals.common import MODEL, ROOT, canonical, digest, read
from evals.semantic import document, validate_packet

VERSION = METHOD = "mail-full-task/1.0"
IDS = ("V085", "V088", "V089", "V091")
SOURCE = "docs/validation-cases/10-mail-rag.md"
MODEL_PROFILE = {"requested_id": MODEL, "temperature": 0, "max_tokens": 2048,
                 "router_max_tokens": 1500, "answer_max_tokens": 4096,
                 "reasoning_effort": None, "thinking": "disabled", "sdk_retries": 0,
                 "provider_retry": "standard [1,2,4] bounded by watchdog/attempt cap"}
DATA = {
    "V085": {"question": "竹桥样机稳定性测试要做多久？", "origin_corpus_uids": [],
             "origin_mapping": "Original requirement exact text; existing synthetic archive adapter reused.",
             "mails": [{"uid": 1, "subject": "竹桥样机", "body": "竹桥样机需连续运行两昼夜，合格后交付", "attachments": [], "received_at": "2026-09-28T09:00:00+00:00"},
                       {"uid": 2, "subject": "竹桥运输", "body": "竹桥运输时间五天", "attachments": [], "received_at": "2026-09-29T09:00:00+00:00"}]},
    "V088": {"question": "QZ-710协调组的联系电话是什么？", "origin_corpus_uids": [1008],
             "mails": [{"uid": 1, "subject": "QZ-710联系窗口", "body": "QZ-710联系窗口为项目协调组，未提供电话号码", "attachments": [], "received_at": "2026-09-28T09:00:00+00:00"},
                       {"uid": 2, "subject": "其他项目", "body": "其他项目联系电话010-55550000", "attachments": [], "received_at": "2026-09-29T09:00:00+00:00"}]},
    "V089": {"question": "TP-711最终付款期限是30日还是60日？", "origin_corpus_uids": [1009, 1010],
             "mails": [{"uid": 1, "subject": "TP-711付款期限方案A", "body": "TP-711付款期限为验收后30日，最终是否获批待核实", "attachments": [], "received_at": "2026-09-28T09:00:00+00:00"},
                       {"uid": 2, "subject": "TP-711付款期限方案B", "body": "TP-711付款期限为验收后60日，最终是否获批待核实", "attachments": [], "received_at": "2026-09-29T09:00:00+00:00"}]},
    "V091": {"question": "WT-707的预付款比例是多少？", "origin_corpus_uids": [1006],
             "mails": [{"uid": 1, "subject": "WT-707与WT-708通用条款待核对", "body": "同时提到WT-707和WT-708；附件为待核对通用条款，尚未确认属于哪份订单", "attachments": [{"filename": "general.txt", "text": "预付款比例40%"}], "received_at": "2026-09-28T09:00:00+00:00"}]},
}
STANDARDS = {
    "V085": [
        ("duration", "回答竹桥样机稳定性测试需48小时或两昼夜；单位、时长准确，允许解释连续运行，不要求固定措辞。", "竹桥样机需连续运行两昼夜，即48小时，合格后交付。", ["M1.body"]),
        ("source", "时长引用真实读取的M1正文；不能把M2运输五天当作稳定性测试时长。", "稳定性测试依据M1连续运行两昼夜；M2运输五天不能回答本问题。", ["M1.body", "M2.body"]),
    ],
    "V088": [
        ("insufficient", "明确QZ-710电话证据不足；仅泛泛表示不知道而未获取项目来源不算完成。", "QZ-710现有资料只说明联系窗口为项目协调组，未提供协调组电话号码。", ["M1.body"]),
        ("no_invention", "不得把其他项目电话010-55550000作为QZ-710电话，不得编造或建议确定号码；辅助联系窗口信息允许。", "无法确认QZ-710协调组电话，不能借用其他项目号码或凭空推断。", ["M1.body", "M2.body"]),
    ],
    "V089": [
        ("alternatives", "两份资料分别为验收后30日及60日；每项引用对应邮件，不能改单位或漏项。", "M1写验收后30日，M2写验收后60日，分别有原文依据。", ["M1.body", "M2.body"]),
        ("unresolved", "明确冲突及最终未获确认；晚收到一天不能推断60日最终生效。", "两份付款期限资料冲突，最终是否获批均待核实，不能确认最终期限。", ["M1.body", "M2.body"]),
    ],
    "V091": [
        ("ownership", "不能确认WT-707的比例；两订单共同出现及附件归属未明确，不能推断40%归WT-707或WT-708。", "现有正文明确附件归属未确认，无法确认WT-707预付款比例。", ["M1.body"]),
        ("attachment", "若提到40%，须限定为同封通用附件的候选信息，不能当作某订单确定答案；不提40%但明确无法确认也允许。", "general.txt提到40%，但附件尚未确认属于WT-707或WT-708，不能确定WT-707比例。", ["M1.body", "M1.general.txt"]),
    ],
}


def templates():
    result = {}
    for cid, fixture in DATA.items():
        sources = []
        for index, mail in enumerate(fixture["mails"], 1):
            sources.append(document(f"{cid}.M{index}.body", mail["body"]))
            for attachment in mail["attachments"]:
                sources.append(document(f"{cid}.M{index}." + attachment["filename"], attachment["text"]))
        result[cid] = validate_packet({
            "packet_id": cid + "-standard", "case_id": cid, "scope": "archived_semantic",
            "question": fixture["question"], "rubric_version": VERSION, "gold_version": VERSION,
            "sources": sources,
            "requirements": [{"requirement_id": cid + "." + rid, "statement": statement,
                              "gold": gold, "source_ids": [cid + "." + s for s in ids]}
                             for rid, statement, gold, ids in STANDARDS[cid]],
            "origin": {"source_ref": SOURCE + "#" + cid.lower(),
                       "sha256": digest((ROOT / SOURCE).read_bytes())},
        })
    return result


def encoder_pin():
    return read(ROOT / "evals/mail_semantic_encoder.json")


def load_cases():
    result = []
    for cid in IDS:
        case = copy.deepcopy(read(ROOT / "evals/cases/V004.json"))
        case.update(case_id=cid, case_version=VERSION, title=STANDARDS[cid][0][1],
                    source_ref=SOURCE + "#" + cid.lower(), source_sha256=digest((ROOT / SOURCE).read_bytes()),
                    mail_fixture=copy.deepcopy(DATA[cid]), model_profile="R-" + VERSION,
                    tags=["mail", "full_task", "semantic"], scoring={"version": VERSION, "method": METHOD})
        case["turns"][0]["input"] = DATA[cid]["question"]
        case["initial_state"]["fixtures"] = [{"path": "fixture.json", "source": "mail_task_fixtures.json", "sha256": digest((ROOT / "evals/mail_task_fixtures.json").read_bytes())}]
        case["requirements"] = []
        case["tools"] = []
        case["environment_profile"] = VERSION
        case["entry"]["adapter"] = "evals.mail_task_worker"
        case["entry"]["request_template"]["session_key"] = "authenticated_binding_private_mail"
        case["identity"].update(principal_id="fictional-principal-A", owner_id="fictional-owner-A", binding_id="fictional-mail-binding-A")
        case["initial_state"]["services"] = ["isolated_synthetic_mail_archive"]
        case["file_policy"]["runtime"] = [{"path": p, "operations": ["create", "update"]} for p in
            ("mail/query_state.sqlite3", "mail/query_state.sqlite3-wal", "mail/query_state.sqlite3-shm", "mail/private/sessions/{authenticated_binding_hash}.jsonl")]
        case["forbidden"] = [{"action": "mail_management_external_delivery_or_unregistered_tool", "detector": "mail_side_effect_attempt_and_state_audit", "evidence": "mail_state", "attempt": True, "actual": True}]
        case["observation"]["required"] = ["entry", "mail_route", "mail_search", "mail_read", "mail_answer", "mail_state", "private_history", "delivery"]
        case["budget"]["profile"] = VERSION
        case["budget"].update(product_s=180, tools=0)
        if cid == "V085":
            case["encoder_pin"] = encoder_pin()
            case["environment_profile"] = "mail-semantic-full-task/1.0"
            case["observation"]["required"].extend(["semantic_state", "semantic_encode", "semantic_rank"])
        result.append(case)
    assert read(ROOT / "evals/mail_task_fixtures.json") == DATA
    return result


def fixture_hash():
    return digest(canonical(DATA))
