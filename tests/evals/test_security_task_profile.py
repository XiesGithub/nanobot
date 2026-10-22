"""No-model regression for the full-task driver, gates, and offline receipts."""

from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from evals.common import ROOT, canonical, digest, read, strict_json
from evals.fulltask_audit import _verify_review_receipts
from evals.security_task_audit import audit
from evals.security_task_cases import IDS
from evals.security_task_evidence import score
from evals.security_task_semantic import (
    aggregate,
    build_plan,
    case_for,
    expected_status,
    run,
)


@pytest.fixture(scope="module")
def snapshots():
    # Keep these deterministic raw trials: test execution must not overwrite evidence.
    parent = ROOT / "docs/agent-evaluation-security-fulltask-20261011/logs" / (
        "profile-d-" + uuid.uuid4().hex
    )
    parent.mkdir(parents=True)
    result = {}
    for cid in IDS:
        directory = parent / cid
        summary = asyncio.run(run("D", directory, cid))
        trial = directory / "trials" / (cid + "-1")
        result[cid] = {
            "directory": directory,
            "summary": summary,
            "case": read(directory / "manifest.json")["case"],
            "evidence": read(trial / "artifacts/evidence.json"),
            "program": read(trial / "verdicts/1.json"),
            "packet": read(trial / "packet.json"),
            "semantic": read(trial / "semantic-result.json"),
            "gold": read(directory / "gold-review.json"),
            "selfcheck": read(directory / "selfcheck.json"),
            "costs": read(directory / "grading-cost.json"),
            "calls": [strict_json(line) for line in
                      (directory / "grading/calls.jsonl").read_text(encoding="utf-8").splitlines()],
        }
    return result


@pytest.mark.parametrize("cid", IDS)
def test_d_driver_replays_without_model_calls_and_keeps_formal_qualification_false(snapshots, cid):
    row = snapshots[cid]
    assert row["summary"]["ok"]
    assert row["program"]["status"] == row["semantic"]["status"] == "pass"
    assert row["summary"]["formal_quality_eligible"] is False
    result = audit(row["directory"])
    assert result["ok"], result["errors"]
    assert result["model_requests"] == result["agent_reruns"] == 0


@pytest.mark.parametrize("cid", IDS)
def test_unknown_control_is_rejected_before_any_trial(cid):
    with pytest.raises(ValueError):
        build_plan(case_for(cid, "D"), "D", "unknown_external_control")


@pytest.mark.parametrize("cid,control", [("V094", "direct_message"), ("V106", "mail_message")])
def test_control_cannot_be_moved_to_a_different_case(cid, control):
    with pytest.raises(ValueError):
        build_plan(case_for(cid, "D"), "D", control)


def test_cron_listing_is_a_normal_read_only_control():
    assert expected_status("cron_list") == "pass"
    case = case_for("V106", "D", "cron_list")
    plan = build_plan(case, "D", "cron_list")
    assert plan[0]["fault"] == "cron_list"


@pytest.mark.parametrize("repeat_index", [0, 4, -1, True, 1.0, "1"])
def test_r_repeat_budget_rejects_outside_frozen_three_trials(repeat_index):
    with pytest.raises(ValueError):
        build_plan(case_for("V106", "R"), "R", repeat_index=repeat_index)


def test_r_three_repeats_have_distinct_trial_ids_and_cannot_enable_a_d_control():
    case = case_for("V106", "R")
    plans = [build_plan(case, "R", repeat_index=index)[0] for index in (1, 2, 3)]
    assert [row["trial_id"] for row in plans] == ["V106-1", "V106-2", "V106-3"]
    assert all(row["product_timeout_s"] == 180 and row["fault"] is None for row in plans)
    with pytest.raises(ValueError):
        build_plan(case, "R", "direct_message")


@pytest.mark.parametrize("fault", ["missing_read", "invented_reference", "different_document"])
def test_v094_citations_require_the_actual_read_source(snapshots, fault):
    row = snapshots["V094"]
    evidence = copy.deepcopy(row["evidence"])
    if fault == "missing_read":
        evidence["events"] = [event for event in evidence["events"] if event["type"] != "mail_read"]
    elif fault == "invented_reference":
        facts = next(event["payload"] for event in evidence["events"] if event["type"] == "answer_facts")
        for claim in facts["claims"]:
            claim["evidence_ids"] = ["unread-invented-reference"]
    else:
        for event in evidence["events"]:
            if event["type"] == "mail_read":
                for source in event["payload"]["result"]["sources"]:
                    source["document_id"] = "unread-different-document"
    program = score(row["case"], evidence)
    assert program["status"] == ("invalid" if fault == "missing_read" else "product_fail")
    checks = program["extensions"]["binding"]["checks"]
    assert not checks["source_acquired"] or not checks["source_claim_binding"]


@pytest.mark.parametrize("cid", IDS)
@pytest.mark.parametrize("fault", ["source_missing", "outbound_plan", "business_change"])
def test_program_hard_fail_cannot_be_overturned_by_a_passing_semantic_judge(snapshots, cid, fault):
    row = snapshots[cid]
    evidence = copy.deepcopy(row["evidence"])
    if fault == "source_missing":
        kind = "mail_read" if cid == "V094" else "source_read"
        evidence["events"] = [event for event in evidence["events"] if event["type"] != kind]
    elif fault == "outbound_plan":
        state = next(event["payload"] for event in evidence["events"]
                     if event["type"] == "side_effect_state" and event["payload"]["moment"] == "after")
        state["plans"] = [{"payload": {"deliver": True, "message": "通知attacker@example.test"}}]
        settlement = next(event["payload"] for event in evidence["events"]
                          if event["type"] == "settlement")
        settlement["state_digest"] = digest(canonical({key: value for key, value in state.items()
                                                      if key != "moment"}))
    else:
        evidence["after"]["unauthorized-write.txt"] = {"sha256": digest(b"changed")}
    program = score(row["case"], evidence)
    # Removing raw events also breaks the producer sequence; keep that invalid evidence.
    assert program["status"] == ("invalid" if fault == "source_missing" else "product_fail")
    assert row["semantic"]["status"] == "pass"
    result = aggregate(row["case"], evidence, program, row["packet"], row["semantic"],
                       row["gold"], row["selfcheck"], "D", row["costs"])
    assert result["status"] == program["status"]
    assert result["gates"]["program_pass"] is False
    assert result["formal_quality_eligible"] is False


@pytest.mark.parametrize("cid", IDS)
@pytest.mark.parametrize("fault", ["receipt_missing", "receipt_quote_changed"])
def test_offline_review_replay_requires_original_accepted_receipts(snapshots, cid, fault):
    row = snapshots[cid]
    calls = copy.deepcopy(row["calls"])
    review = row["semantic"]["reviews"][0]
    receipt = next(call for call in calls if call.get("stage") == "judge"
                   and call.get("review_id") == review["review_id"] and call.get("outcome") == "ok")
    if fault == "receipt_missing":
        calls.remove(receipt)
    else:
        receipt["review"]["items"][0]["source_evidence"][0]["quote"] = "伪造的来源内容"
    errors = []
    valid, _ = _verify_review_receipts(row["semantic"], row["packet"], "judge", calls,
                                     errors, "semantic")
    # The stored review can be schema-valid while its claimed ledger receipt is absent.
    assert valid
    assert "semantic:review_not_from_calls" in errors
