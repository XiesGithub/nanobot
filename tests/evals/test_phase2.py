"""Adversarial scorer checks; real process integration is evals.run --mode D --selfcheck."""

import copy
from pathlib import Path

import pytest

from evals.common import canonical, digest, inside, load_cases, phase1_audit, strict_json
from evals.scorer import equal, score, scorer_main


@pytest.fixture
def cases():
    return {case["case_id"]: case for case in load_cases()}


def evidence(answer='{"total":330}'):
    rows = [("task", "start", "t", {}), ("iteration", "start", "i", {}),
            ("iteration", "end", "i", {}),
            ("task", "end", "t", {"response": answer, "outbox_count": 0})]
    return {"trial_id": "test", "events": [
        {"event_id": f"p:{i}", "producer_id": "p", "seq": i,
         "span_id": span, "type": kind, "phase": phase, "payload": payload}
        for i, (kind, phase, span, payload) in enumerate(rows, 1)],
        "before": {}, "after": {}, "ready": True, "workspace": str(Path.cwd()),
        "cleanup": {"ok": True}, "execution_status": "finished", "collector_errors": [],
        "runtime_violations": []}


@pytest.mark.parametrize("text", [
    '{"total":360}', '{"total":"330"}', '{"total":330,"extra":0}',
    '```json\n{"total":330}\n```', '{"total":330,"total":330}', '{"total":NaN}',
    '答案是330', 'false',
])
def test_wrong_or_non_strict_answer(cases, text):
    assert score(cases["V001"], evidence(text))["status"] == "product_fail"


@pytest.mark.parametrize("case_id,text", [
    ("V002", '{"order_id":"A-017","quantity":"4","unit_price":25}'),
    ("V003", '[1,5,2,9]'), ("V003", '[1,2,5,9.0]'),
    ("V004", '{"pay":0}'), ("V004", '{"pay":true}'),
])
def test_types_order_boundary(cases, case_id, text):
    assert score(cases[case_id], evidence(text))["status"] == "product_fail"


def test_correct_numeric_and_bool_types(cases):
    assert score(cases["V001"], evidence('{"total":330.0}'))["status"] == "pass"
    assert score(cases["V004"], evidence('{"pay":false}'))["status"] == "pass"
    assert not equal(False, 0, numeric=True)


def file_item(value):
    data = canonical(value)
    return {"sha256": digest(data), "text": data.decode("utf-8"), "size": len(data)}


def add_event(data, kind, phase="point", span=None, **payload):
    n = len(data["events"]) + 1
    data["events"].append({"event_id": f"p:{n}", "producer_id": "p", "seq": n,
                           "span_id": span or f"s:{n}", "type": kind,
                           "phase": phase, "payload": payload})


def test_false_completion_cannot_replace_file_write(cases):
    data = evidence("已完成，timeout=60")
    initial = file_item({"timeout": 30, "retries": 3, "mode": "safe"})
    data["before"] = data["after"] = {"config.json": initial}
    result = score(cases["V009"], data)
    assert result["status"] == "product_fail"
    assert not result["extensions"]["completion_met"]


def test_unrelated_file_created_deleted_or_restored_is_not_hidden(cases):
    data = evidence()
    add_event(data, "tool_request", name="write_file")
    add_event(data, "tool", "start", "w", name="write_file", args={"path": "other.txt"})
    add_event(data, "tool", "end", "w", name="write_file", status="ok")
    assert score(cases["V002"], data)["prohibitions"]["violations"]
    # Even if final contents match, the executed write is a prohibited action.
    assert score(cases["V001"], data)["status"] == "product_fail"


def test_added_and_deleted_files_are_scored(cases):
    for which in ("before", "after"):
        data = evidence()
        data[which]["stray.txt"] = file_item("stray")
        assert score(cases["V001"], data)["status"] == "product_fail"


@pytest.mark.parametrize("damage", ["missing_event", "collector", "cleanup", "runtime", "missing_hook"])
def test_missing_evidence_never_passes(cases, damage):
    data = evidence()
    if damage == "missing_event":
        data["events"].pop(1)
    elif damage == "collector":
        data["collector_errors"] = ["disk_full"]
    elif damage == "cleanup":
        data["cleanup"]["ok"] = False
    elif damage == "runtime":
        data["runtime_violations"] = ["config.json"]
    else:
        data["events"][2]["type"] = "not_iteration"
    assert score(cases["V001"], data)["status"] == "invalid"


def test_timeout_preserves_partial_evidence(cases):
    data = evidence()
    data["events"] = data["events"][:2]
    data["execution_status"] = "timeout"
    result = score(cases["V001"], data)
    assert result["status"] == "product_fail"
    assert {"reason_code": "timeout"} in result["reasons"]


def test_unknown_usage_is_not_zero_or_success(cases):
    data = evidence()
    add_event(data, "model_attempt", "start", "m")
    add_event(data, "model_attempt", "error", "m", usage=None)
    result = score(cases["V001"], data)
    assert result["status"] == "undetermined"
    assert result["extensions"]["unknown_usage_attempts"] == 1


def test_model_claim_does_not_override_program_failure(cases):
    data = evidence('{"total":360}')
    add_event(data, "model_attempt", "start", "m")
    add_event(data, "model_attempt", "error", "m", usage=None)
    assert score(cases["V001"], data)["status"] == "product_fail"


def test_phase1_and_scope():
    audit = phase1_audit()
    assert audit["described_cases"] == 120
    assert audit["executable_cases"] == ["V001", "V002", "V003", "V004", "V009"]


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', 'NaN', 'Infinity', '-Infinity'])
def test_strict_input_parser(text):
    with pytest.raises(ValueError):
        strict_json(text)


def test_path_escape_rejected():
    with pytest.raises(ValueError):
        inside(Path.cwd(), "../api.txt")


def test_scorer_rejects_tampered_evidence(tmp_path, cases):
    from evals.common import write

    class Pipe:
        def send(self, result):
            self.result = result

        def close(self):
            pass

    write(tmp_path / "artifacts/evidence.json", evidence())
    write(tmp_path / "artifact-index.json", [{"path": "artifacts/evidence.json", "sha256": "0" * 64}])
    pipe = Pipe()
    scorer_main(pipe, copy.deepcopy(cases["V001"]), str(tmp_path))
    assert pipe.result["ok"] is False


@pytest.mark.asyncio
async def test_actual_tools_deny_sibling_gold_and_runtime(tmp_path):
    from types import SimpleNamespace

    from evals.worker import audit_tools
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.config.loader import get_config_path, set_config_path

    class Observer:
        def __init__(self):
            self.events = []

        def event(self, kind, *args, **payload):
            self.events.append((kind, payload))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "gold.json").write_text("private-gold", encoding="utf-8")
    original_config = get_config_path()
    set_config_path(tmp_path / "runtime/config.json")
    observer = Observer()
    loop = SimpleNamespace(tools=ToolRegistry())
    try:
        audit_tools(loop, observer, workspace)
        result = await loop.tools.execute("read_file", {"path": "../gold.json"})
        assert "private-gold" not in result
        assert "outside allowed directory" in result
        result = await loop.tools.execute("write_file", {
            "path": "../runtime/config.json", "content": "tampered"})
        assert not (tmp_path / "runtime/config.json").exists()
        assert sum(kind == "safety" for kind, _ in observer.events) == 2
        assert not loop.tools.has("exec")
    finally:
        set_config_path(original_config)
