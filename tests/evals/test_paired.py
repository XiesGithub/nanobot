"""Evidence replay and comparison isolation boundaries, without live model calls."""

from copy import deepcopy

import pytest

from evals.common import canonical, load_cases, write
from evals.paired import (
    PROTECTED_GROUPS,
    arm_metrics,
    compatibility,
    conditions,
    replay,
    reserve_holdout,
    trial_plan,
)
from evals.repeat import record_trial
from evals.scorer import score
from tests.evals.test_phase2 import evidence
from tests.evals.test_repeat import _artifact_set


def manifest():
    return {"groups": {g: {"identity_hash": "unchanged"} for g in PROTECTED_GROUPS},
            "files": {"evals/scorer.py": "score", "nanobot/agent/runner.py": "subject"}}


def test_subject_change_allowed_but_judge_and_harness_changes_rejected():
    baseline = manifest()
    candidate = deepcopy(baseline)
    candidate["files"]["nanobot/agent/runner.py"] = "candidate"
    assert compatibility(baseline, candidate) == []
    candidate["groups"]["judge"]["identity_hash"] = "new judge"
    candidate["files"]["evals/scorer.py"] = "new score"
    assert "incompatible_judge" in compatibility(baseline, candidate)
    assert "incompatible_harness:evals/scorer.py" in compatibility(baseline, candidate)


def test_missing_evidence_is_invalid(tmp_path):
    case = load_cases()[0]
    row = replay(case, trial_plan(case, 1, "D"), tmp_path,
                 {"returncode": 1, "watchdog_timeout": False})
    assert row["status"] == "invalid" and not row["evidence_valid"]


def test_forged_normalized_result_is_rejected_against_raw_verdict(tmp_path):
    case = load_cases()[0]
    plan = trial_plan(case, 1, "D")
    output = tmp_path / "one-execution"
    output.mkdir()
    _, returned, _, _ = _artifact_set(output, case, plan, response='{"total":360}')
    claimed = record_trial(case, plan, output, returned)
    assert claimed["status"] == "product_fail"
    claimed["status"] = "pass"
    write(output / "execution-result.json", claimed)
    (output / "complete.marker").write_bytes(canonical({"trial_id": plan["trial_id"]}))
    row = replay(case, plan, output, {"returncode": 0, "watchdog_timeout": False})
    assert row["status"] == "invalid"
    assert "normalized_record_replay_mismatch" in row["replay_errors"]


def test_strict_holdout_json_types_do_not_change_historical_numeric_contract():
    case = load_cases()[0]
    assert score(case, evidence('{"total":330.0}'))["status"] == "pass"
    case["case_id"] = "H001"
    case["scoring"]["strict_json_types"] = True
    assert score(case, evidence('{"total":330.0}'))["status"] == "product_fail"
    assert score(case, evidence('{"total":330}'))["status"] == "pass"


def test_modes_have_distinct_frozen_budgets_and_no_historical_reuse():
    assert conditions("D")["attempts_per_trial"] == 0
    assert conditions("R")["attempts_per_trial"] == 8
    assert conditions("D") != conditions("R")
    assert not conditions()["historical_reuse"]


def test_arm_metrics_withhold_missing_records_in_each_arm():
    metrics = arm_metrics(load_cases(), [], "D")
    for arm in ("baseline", "candidate"):
        assert metrics[arm]["development"]["integrity"]["missing_trial_ids"]
        profile = metrics[arm]["development"]["primary_profiles"]["D-phase7-paired-v1"]
        assert not profile["publication"]["available"]


def test_holdout_cannot_be_silently_reused_after_round_reservation(tmp_path, monkeypatch):
    import evals.paired as paired

    monkeypatch.setattr(paired, "ROOT", tmp_path)
    assert reserve_holdout("a" * 64, "round-1")["comparison_id"] == "round-1"
    with pytest.raises(ValueError, match="holdout_already_reserved_or_exposed"):
        reserve_holdout("a" * 64, "round-2")
    assert reserve_holdout("b" * 64, "round-2")["comparison_id"] == "round-2"
