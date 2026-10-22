import pytest

from evals.common import ROOT, digest
from evals.fulltask_evidence import (
    FULL_TASK_CASE_VERSION,
    binding,
    program_score,
)
from evals.worker import audit_tools
from nanobot.utils.observability import observation_context, span


def _v007_case():
    raw = (ROOT / "evals/fixtures/V007/note.txt").read_bytes()
    return {
        "case_id": "V007",
        "case_version": FULL_TASK_CASE_VERSION,
        "scope": "full_task",
        "source_ref": "docs/validation-cases/02-tools.md#V007",
        "source_sha256": "652d04c780b92343f9b632f5e2439f8eca9ef82862b7e85d6c82ef26450a2297",
        "initial_state": {"fixtures": [{
            "path": "note.txt", "source": "fixtures/V007/note.txt", "sha256": digest(raw),
        }]},
        "scoring": {"method": "full_task_semantic_v1"},
    }


def _event(event_id, kind, phase, payload, *, span_id=None, parent_span_id=None):
    return {"event_id": event_id, "type": kind, "phase": phase,
            "span_id": span_id or event_id, "parent_span_id": parent_span_id,
            "trial_id": "V007-1", "payload": payload}


def _v007_evidence(*, response="120元", result_text=None, read=True,
                   tool_status="ok", source_hash=None, result_hash=None,
                   mutate_after=False, status="finished", add_delivery=True):
    case = _v007_case()
    source_path = ROOT / "evals/fixtures/V007/note.txt"
    raw = source_path.read_bytes()
    source_text = raw.decode("utf-8")
    source_hash = digest(raw) if source_hash is None else source_hash
    result_text = result_text if result_text is not None else "1| 项目：竹桥\n2| 预算：120元\n3| 负责人：林青\n\n(End of file — 3 lines total)"
    result_hash = result_hash or digest(result_text.encode("utf-8"))
    events = [
        _event("tool-start", "tool", "start", {"name": "read_file", "call_id": "call-1"},
               span_id="tool-span"),
        _event("execution", "tool_execution", "point",
               {"name": "read_file", "call_id": "call-1", "stage": "execution_started"},
               parent_span_id="tool-span"),
        _event("file-access", "file_access", "point",
               {"name": "read_file", "path": "note.txt"}, parent_span_id="tool-span"),
    ]
    if read:
        events.append(_event("source-read", "source_read", "point", {
            "path": "note.txt", "source_sha256": source_hash,
            "result_text": result_text, "result_sha256": result_hash,
            "offset": 1, "limit": None, "status": "ok", "call_id": "call-1",
        }, parent_span_id="tool-span"))
    events.append(_event("tool-end", "tool", "end",
                         {"name": "read_file", "call_id": "call-1", "status": tool_status},
                         span_id="tool-span"))
    if add_delivery:
        events.append(_event("delivery", "delivery", "point",
                             {"response": response, "outbox_count": 0}, parent_span_id="entry"))
    after = {"note.txt": {"sha256": digest(raw), "size": len(raw), "text": source_text}}
    if mutate_after:
        after["note.txt"] = {"sha256": digest(b"changed"), "size": 7, "text": "changed"}
    evidence = {"trial_id": "V007-1", "execution_status": status,
                "events": events,
                "before": {"note.txt": {"sha256": digest(raw), "size": len(raw),
                                         "text": source_text}},
                "after": after}
    return case, evidence


def _append_read(evidence, *, path="note.txt", result_text=None):
    text = result_text or "1| 项目：竹桥\n2| 预算：120元\n3| 负责人：林青"
    call_id, span_id = "call-2", "tool-span-2"
    events = [
        _event("tool-start-2", "tool", "start", {"name": "read_file", "call_id": call_id},
               span_id=span_id),
        _event("execution-2", "tool_execution", "point",
               {"name": "read_file", "call_id": call_id, "stage": "execution_started"},
               parent_span_id=span_id),
        _event("file-access-2", "file_access", "point",
               {"name": "read_file", "path": path}, parent_span_id=span_id),
        _event("source-read-2", "source_read", "point", {
            "path": path,
            "source_sha256": evidence["before"]["note.txt"]["sha256"],
            "result_text": text, "result_sha256": digest(text.encode("utf-8")),
            "offset": 1, "limit": None, "status": "ok", "call_id": call_id,
        }, parent_span_id=span_id),
        _event("tool-end-2", "tool", "end",
               {"name": "read_file", "call_id": call_id, "status": "ok"}, span_id=span_id),
    ]
    evidence["events"].extend(events)


def test_fulltask_binding_links_delivery_tool_read_and_unchanged_fixture():
    case, evidence = _v007_evidence()

    result = binding(case, evidence)

    assert result["trial_id"] == "V007-1"
    assert result["delivery_event_id"] == "delivery"
    assert result["read_event_ids"] == ["source-read"]
    assert result["read_tool_call_id"] == "call-1"
    assert result["source_file_sha256"] == case["initial_state"]["fixtures"][0]["sha256"]
    assert all(result["checks"].values())


@pytest.mark.parametrize("response", [
    "120元", "120 yuan", "120.00 Yuan", "120 CNY", "120 RMB", "CNY 120", "RMB 120",
    "人民币120元", "人民币120", "¥120", "￥120.00", "120 ¥",
])
def test_v007_accepts_only_supported_yuan_amount_forms(response):
    case, evidence = _v007_evidence(response=response)
    assert binding(case, evidence)["checks"]["output_format_met"]


@pytest.mark.parametrize("response", [
    "120 dollars", "120 USD", "120 元，含税", "预算是120元", "120元或130元", "",
])
def test_v007_rejects_wrong_unit_or_non_amount_only_delivery(response):
    case, evidence = _v007_evidence(response=response)
    result = binding(case, evidence)
    assert not result["checks"]["output_format_met"]
    assert "output_format_failed" in result["issues"]


def test_missing_delivery_or_tool_read_cannot_pass():
    case, missing_delivery = _v007_evidence(add_delivery=False)
    case2, missing_read = _v007_evidence(read=False)

    assert not binding(case, missing_delivery)["checks"]["delivery_met"]
    read_result = binding(case2, missing_read)
    assert not read_result["checks"]["source_read_met"]
    assert "source_read_missing" in read_result["issues"]


def test_repeated_or_partial_read_does_not_discard_valid_source_evidence():
    case, repeated = _v007_evidence()
    _append_read(repeated)
    result = binding(case, repeated)
    assert result["checks"]["source_read_met"]
    assert result["read_event_ids"] == ["source-read", "source-read-2"]

    case, partial_then_valid = _v007_evidence(
        result_text="1| 项目：竹桥\n(Showing lines 1-1 of 3. Use offset=2 to continue.)"
    )
    _append_read(partial_then_valid)
    result = binding(case, partial_then_valid)
    assert result["checks"]["source_read_met"]
    assert result["read_event_ids"] == ["source-read-2"]

    case, other_path_then_valid = _v007_evidence()
    _append_read(other_path_then_valid, path="unrelated.txt")
    result = binding(case, other_path_then_valid)
    assert result["checks"]["source_read_met"]
    assert result["read_event_ids"] == ["source-read"]


@pytest.mark.parametrize("change", [
    {"result_text": "2| 预算：120元", "result_hash": "bad"},
    {"result_text": "1| 项目：竹桥", "result_hash": None},
    {"source_hash": "bad"},
    {"mutate_after": True},
    {"tool_status": "error"},
    {"status": "cancelled"},
])
def test_unverified_source_read_cannot_satisfy_v007(change):
    case, evidence = _v007_evidence(**change)

    result = binding(case, evidence)

    assert not result["checks"]["source_read_met"]


def test_duplicate_delivery_and_fixture_hash_tampering_are_rejected():
    case, evidence = _v007_evidence()
    evidence["events"].append(_event("delivery-2", "delivery", "point",
                                     {"response": "120元", "outbox_count": 0}))
    duplicate = binding(case, evidence)
    assert not duplicate["checks"]["delivery_met"]

    case, evidence = _v007_evidence()
    case["source_sha256"] = "0" * 64
    tampered = binding(case, evidence)
    assert not tampered["checks"]["source_document_met"]


def test_v005_plain_single_paragraph_rule():
    case = {
        "case_id": "V005", "case_version": FULL_TASK_CASE_VERSION,
        "scope": "full_task", "source_ref": "docs/validation-cases/01-basic.md#V005",
        "source_sha256": "676cfd1f042bec91ca7238de56ac6bdee85726ba988c872f87d4583ae6bc8f79",
        "initial_state": {"fixtures": []}, "scoring": {"method": "full_task_semantic_v1"},
    }
    evidence = {"trial_id": "V005-1", "execution_status": "finished", "events": [
        _event("delivery", "delivery", "point",
               {"response": "The meeting is moved to Friday at 3 p.m.; the location remains the same.",
                "outbox_count": 0})], "before": {}, "after": {}}
    assert binding(case, evidence)["checks"]["output_format_met"]
    evidence["events"][0]["payload"]["response"] = "```text\ntranslation\n```"
    assert not binding(case, evidence)["checks"]["output_format_met"]


def test_read_instrumentation_records_real_tool_output_only_for_fulltask(tmp_path):
    import asyncio

    (tmp_path / "note.txt").write_text("项目：竹桥\n预算：120元\n负责人：林青\n", encoding="utf-8")

    class Emit:
        def __init__(self):
            self.events = []

        def event(self, kind, **payload):
            self.events.append({"type": kind, **payload})

    class Loop:
        def __init__(self):
            self.tools = ToolSet()

    class ToolSet:
        def __init__(self):
            self.items = {}

        def register(self, tool):
            self.items[tool.name] = tool

        def __getitem__(self, name):
            return self.items[name]

    async def read(full_task_semantic):
        emit, loop = Emit(), Loop()
        audit_tools(loop, emit, tmp_path, full_task_semantic=full_task_semantic)
        with observation_context(emit.event, "V007-test"):
            with span("tool", name="read_file", call_id="call-1"):
                result = await loop.tools["read_file"].execute(path="note.txt")
        return result, emit.events

    result, events = asyncio.run(read(True))
    observed = [event for event in events if event["type"] == "source_read"]
    assert "2| 预算：120元" in result.splitlines()
    assert len(observed) == 1
    assert observed[0]["path"] == "note.txt"
    assert observed[0]["result_text"] == result
    assert observed[0]["result_sha256"] == digest(result.encode("utf-8"))
    assert observed[0]["call_id"] == "call-1"
    assert observed[0]["status"] == "ok"

    ordinary_result, ordinary_events = asyncio.run(read(False))
    assert ordinary_result == result
    assert not any(event["type"] == "source_read" for event in ordinary_events)


def test_program_score_never_sets_formal_eligibility_true_or_overturns_base_failure():
    case, evidence = _v007_evidence()
    case["requirements"] = []
    case["file_policy"] = {"business_write": []}
    case["case_version"] = FULL_TASK_CASE_VERSION
    case["scoring"]["method"] = "full_task_semantic_v1"
    case["observation_version"] = "phase3/1.0"
    case["case_id"] = "V007"
    case["completion_rule"] = {}
    # The legacy scorer's required fields are deliberately supplied by this
    # minimal independent evidence fixture; semantic eligibility remains pending.
    evidence.update({"ready": True, "cleanup": {"ok": True}, "runtime_violations": [],
                     "collector_errors": [], "workspace": str(ROOT), "cancel_reason": None})
    evidence["events"] = [
        _event("task-start", "task", "start", {}, span_id="task-span"),
        *evidence["events"],
        _event("task-end", "task", "end", {}, span_id="task-span"),
        _event("iteration-start", "iteration", "start", {}, span_id="iteration-span"),
        _event("iteration-end", "iteration", "end", {}, span_id="iteration-span"),
        _event("tool-request", "tool_request", "point", {"name": "read_file"}),
    ]
    for sequence, event in enumerate(evidence["events"], 1):
        event["producer_id"] = "synthetic"
        event["seq"] = sequence
    verdict = program_score(case, evidence)
    assert verdict["metric_eligibility"]["formal_quality"] is False
    assert verdict["gates"]["full_task_evidence"]
    assert verdict["finalization"] == "program_only_full_task_semantic_pending"

    case["requirements"] = [{"id": "base-hard-failure", "predicate": "outbox_equal",
                             "expected": 1, "required": True}]
    failed = program_score(case, evidence)
    assert failed["status"] == "product_fail"
