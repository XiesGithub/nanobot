"""Two source-bound full-task contracts; the five-case and archive scopes stay fixed."""

import copy

from evals.common import ROOT, digest, inside, read
from evals.semantic import gold_packet, validate_packet
from evals.semantic_samples import fixed_samples

VERSION = "full-task-semantic/1.0"
METHOD = "full_task_semantic_v1"
IDS = ("V005", "V007")


def templates():
    result = {}
    for packet in fixed_samples():
        if packet["case_id"] not in result:
            result[packet["case_id"]] = validate_packet({
                key: copy.deepcopy(value) for key, value in packet.items()
                if key not in {"answer", "hard_checks"}
            })
    return result


def load_cases():
    result = []
    for case_id, template in templates().items():
        case = copy.deepcopy(read(ROOT / "evals/cases/V004.json"))
        case.update(case_id=case_id, case_version=VERSION,
                    title="指定文本翻译" if case_id == "V005" else "读取指定事实",
                    source_ref=template["origin"]["source_ref"],
                    source_sha256=template["origin"]["sha256"],
                    tags=["full_task", "semantic"], model_profile="R-" + VERSION)
        case["turns"][0]["input"] = template["question"]
        case["requirements"] = [row for row in case["requirements"] if row["id"] != "answer"]
        if case_id == "V007":
            source = ROOT / "evals/fixtures/V007/note.txt"
            expected_text = next(doc["text"] for doc in template["sources"]
                                 if doc["artifact_id"] == "V007.note.txt")
            assert source.read_bytes() == expected_text.encode("utf-8")
            case["initial_state"]["fixtures"] = [{
                "path": "note.txt", "source": "fixtures/V007/note.txt",
                "sha256": digest(source.read_bytes()),
            }]
        case["budget"]["profile"] = "R-" + VERSION
        case["observation"]["required"] += ["delivery", "entry", "file_access"] if case_id == "V007" else ["delivery", "entry"]
        case["scoring"] = {"version": VERSION, "method": METHOD,
                           "gold": gold_packet(template)["packet_id"],
                           "semantic_review": "source_bound_before_product_execution",
                           "eligibility": "derived_from_full_task_evidence_and_gates"}
        origin = inside(ROOT, case["source_ref"].split("#")[0])
        assert digest(origin.read_bytes()) == case["source_sha256"]
        assert template["question"] in origin.read_text(encoding="utf-8")
        result.append(case)
    assert tuple(case["case_id"] for case in result) == IDS
    return result


def script_for(case_id, control=None):
    answer = ("The meeting has been rescheduled for Friday at 3 p.m., with the venue unchanged."
              if case_id == "V005" else "120元")
    if control == "wrong_semantics":
        answer = "The meeting is now on Friday at 3 p.m., and the location has changed."
    elif control == "code_fence":
        answer = "```text\n" + answer + "\n```"
    elif control == "wrong_unit":
        answer = "120美元"
    if control == "product_budget":
        return [{"sleep": 60, "content": "unreachable"}]
    steps = []
    if case_id == "V007" and control != "no_read":
        steps.append({"tool_calls": [{"name": "read_file", "arguments": {"path": "note.txt"}}]})
    if control == "source_write":
        steps.append({"tool_calls": [{"name": "write_file", "arguments": {
            "path": "note.txt", "content": "预算：999元\n",
        }}]})
    return [*steps, {"content": answer}]
