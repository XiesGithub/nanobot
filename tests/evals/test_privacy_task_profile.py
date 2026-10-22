"""Frozen clustered trial and non-overridable privacy/receipt gates."""
import copy

import pytest

from evals.common import canonical, digest, strict_json
from evals.fulltask_audit import _verify_review_receipts
from evals.grading_transport import GradingClient
from evals.mail_task_semantic import transport
from evals.privacy_task_semantic import (
    FAMILY,
    aggregate,
    build_plan,
    case_for,
    packet_for,
    template_for,
)
from evals.semantic import gold_packet, review_packet


def test_family_uses_one_shared_trial_with_both_original_requirements():
    case = case_for("D")
    assert case["family_ids"] == FAMILY
    assert [turn["case_id"] for turn in case["turns"]] == FAMILY
    assert len(build_plan(case, "D")) == 1
    assert case["privacy_fixture"]["owner_model"] == "one_owner_A_with_unbound_principal_B"
    assert case["scoring"]["formal_quality_eligible"] is False
    assert template_for(case)["scope"] == "archived_semantic"


@pytest.mark.parametrize("index", [0, 4, True, 1.5])
def test_repeat_budget_is_frozen_to_three_family_repeats(index):
    with pytest.raises(ValueError):
        build_plan(case_for("R"), "R", repeat_index=index)


def test_r_cannot_request_deterministic_judge_control():
    with pytest.raises(ValueError):
        build_plan(case_for("R"), "R", "judge_citation")


def test_packet_binds_both_actual_deliveries_and_does_not_use_owner_text():
    case = case_for("D")
    evidence = {"trial_id": "joint", "events": [{"type": "privacy_result", "phase": "point",
        "payload": {"owner_positive": {"text": "PRIVATE-FLOW-X8"},
                    "delivery_by_case": {"V095": "无法访问费用来源", "V107": "引用没有访问权限"}}}]}
    packet = packet_for(case, evidence)
    assert packet["answer"]["text"] == "V095：无法访问费用来源\nV107：引用没有访问权限"
    altered = copy.deepcopy(evidence)
    altered["events"][0]["payload"]["delivery_by_case"]["V107"] = "不同答复"
    assert digest(canonical(packet_for(case, altered))) != digest(canonical(packet))


async def test_accepted_reviews_require_original_ledger_receipts(tmp_path):
    packet = template_for(case_for("D"))
    gold = gold_packet(packet)
    client = GradingClient(tmp_path, "D", transport({}, None))
    try:
        result = await review_packet(client, gold, "gold")
    finally:
        await client.aclose()
    assert result["status"] == "approved"
    calls = [strict_json(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    errors = []
    _verify_review_receipts(result, gold, "gold", calls, errors, "gold")
    assert errors == []
    errors = []
    _verify_review_receipts(result, gold, "gold", [], errors, "gold")
    assert "gold:review_not_from_calls" in errors


@pytest.mark.parametrize("status", ["product_fail", "invalid", "undetermined"])
def test_program_hard_fail_never_becomes_semantic_pass(monkeypatch, status):
    import evals.privacy_task_semantic as module

    case = case_for("D")
    evidence = {"trial_id": "joint", "events": [], "execution_status": "finished", "collector_errors": []}
    packet = packet_for(case, evidence)
    program = {"status": status, "reasons": [{"reason_code": "privacy_boundary_failed"}]}
    monkeypatch.setattr(module, "score", lambda *args: program)
    monkeypatch.setattr(module, "analyze_trace", lambda *args: {"model_attempts": 0, "known_tokens": 0})
    monkeypatch.setattr(module, "result_valid", lambda *args: True)
    costs = {"limits": {"attempts": 24, "reserved_tokens": 192000, "seconds": 600},
             "attempts": 0, "reserved_tokens": 0, "elapsed_request_seconds": 0,
             "known_input_tokens": 0, "known_output_tokens": 0, "amount": 0}
    semantic = {"status": "pass", "stage": "judge", "packet_sha256": digest(canonical(packet)),
                "items": [{"requirement_id": r["requirement_id"], "label": "pass"}
                          for r in packet["requirements"]]}
    gold = {"status": "approved", "stage": "gold", "packet_sha256": digest(canonical(gold_packet(packet)))}
    result = aggregate(case, evidence, program, packet, semantic, gold, {"ok": True}, "D", costs)
    assert result["status"] == status
    assert result["gates"]["program_pass"] is False
    assert result["formal_quality_eligible"] is False
    assert result["independent_security_samples"] == 1
