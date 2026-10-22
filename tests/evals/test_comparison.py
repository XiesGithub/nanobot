import hashlib
import json

from evals.comparison import DEFAULT_POLICY, compare


def _conditions_id(conditions):
    encoded = json.dumps(conditions, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest(case_count=5, *, same_snapshot=False, policy=None):
    conditions = {"mode": "D", "entry": "AgentLoop.process_direct", "repeat": 3}
    cases = [{"case_id": f"C{index:03}",
              "split": "development" if index <= min(5, case_count) else "holdout",
              "scope": "actual_agent_process_direct"}
             for index in range(1, case_count + 1)]
    return {
        "version": "phase7/1.0", "comparison_id": "comparison-1",
        "baseline_snapshot_id": "snapshot-a",
        "candidate_snapshot_id": "snapshot-a" if same_snapshot else "snapshot-b",
        "conditions": conditions, "conditions_id": _conditions_id(conditions),
        "cases": cases, "repeat": 3, "mode": "D", "policy": policy or {},
    }


def _record(manifest, case_id, index, arm, success=False, **changes):
    row = {
        "arm": arm, "case_id": case_id, "repeat_index": index,
        "snapshot_id": (manifest["baseline_snapshot_id"] if arm == "baseline"
                        else manifest["candidate_snapshot_id"]),
        "conditions_id": manifest["conditions_id"],
        "status": "pass" if success else "product_fail",
        "execution_complete": True, "scoring_complete": True, "observation_complete": True,
        "completion_met": success, "completion_ms": 100,
        "safety": {"verified": True, "actual": 0, "critical_actual": 0},
        "product_cost": {"amount": 0, "currency": None, "closed": True},
        "grading_cost": {"amount": 0, "currency": None, "closed": True},
        "fault": {"planned": False, "triggered": False,
                  "recoverable": True, "cancelled": False},
        "evidence_valid": True,
    }
    row.update(changes)
    return row


def _records(manifest, baseline_success, candidate_success):
    result = []
    for case in manifest["cases"]:
        case_id = case["case_id"]
        for index in range(1, 4):
            result.append(_record(manifest, case_id, index, "baseline",
                                  baseline_success(case_id, index)))
            result.append(_record(manifest, case_id, index, "candidate",
                                  candidate_success(case_id, index)))
    return result


def test_three_of_five_to_four_of_five_does_not_claim_improvement():
    manifest = _manifest(5)
    records = _records(
        manifest, lambda case, _index: int(case[1:]) <= 3,
        lambda case, _index: int(case[1:]) <= 4)

    result = compare(manifest, records)

    assert result["outcome"] == "regression_passed_improvement_unproven"
    assert "too_few_cases_for_improvement" in result["scopes"]["all"]["reasons"]
    assert result["scopes"]["development"]["case_count"] == 5
    assert result["scopes"]["holdout"]["case_count"] == 0


def test_same_snapshot_with_all_success_is_never_improved():
    manifest = _manifest(20, same_snapshot=True)
    records = _records(manifest, lambda *_: True, lambda *_: True)

    result = compare(manifest, records)

    assert result["outcome"] == "regression_passed_improvement_unproven"
    assert result["same_snapshot"] is True
    assert "same_snapshot_cannot_improve" in result["reasons"]
    assert result["scopes"]["all"]["metrics"]["success"]["mean_delta"] == 0


def test_large_paired_improvement_requires_and_clears_interval_and_sign_test():
    manifest = _manifest(20)
    records = _records(
        manifest, lambda case, _index: int(case[1:]) <= 10, lambda *_: True)

    result = compare(manifest, records)
    success = result["scopes"]["all"]["metrics"]["success"]

    assert result["outcome"] == "improved"
    assert success["case_count"] == 20
    assert success["confidence_interval"]["lower"] > 0
    assert success["sign_test"]["direction"] == "positive"
    assert success["sign_test"]["significant"] is True
    assert "program diagnostics" in result["improvement_description"]
    assert success["confidence_interval"] == (
        compare(manifest, records)["scopes"]["all"]["metrics"]["success"]
        ["confidence_interval"])


def test_required_latency_or_cost_regression_blocks_significant_success_gain():
    for metric in ("latency", "cost"):
        manifest = _manifest(20, policy={
            "required_metrics": ["success", "reliability", "safety", metric]})
        records = _records(
            manifest, lambda case, _index: int(case[1:]) <= 10, lambda *_: True)
        for record in records:
            if metric == "latency":
                record["completion_ms"] = 100 if record["arm"] == "baseline" else 200
            else:
                amount = 1 if record["arm"] == "baseline" else 2
                record["product_cost"] = {
                    "amount": amount, "currency": "USD", "closed": True}

        result = compare(manifest, records)
        scope = result["scopes"]["all"]

        assert result["outcome"] == "regression_blocked"
        assert scope["metrics"]["success"]["confidence_interval"]["lower"] > 0
        assert scope["metrics"]["success"]["sign_test"]["significant"] is True
        assert scope["metrics"][metric]["gate"]["status"] == "regression"


def test_one_harmed_case_is_not_hidden_by_large_average_gain():
    manifest = _manifest(20)
    records = _records(
        manifest, lambda case, _index: case == "C001",
        lambda case, _index: case != "C001")

    result = compare(manifest, records)

    assert result["scopes"]["all"]["metrics"]["success"]["mean_delta"] > 0
    assert result["outcome"] == "regression_blocked"
    assert "C001" in (result["scopes"]["all"]["metrics"]["success"]["gate"]
                       ["regression_cases"])


def test_unknown_cost_is_optional_but_required_cost_needs_complete_nonzero_baseline():
    manifest = _manifest(10)
    records = _records(manifest, lambda *_: True, lambda *_: True)
    for record in records:
        record["product_cost"] = {"amount": None, "currency": None, "closed": False}

    optional = compare(manifest, records)
    assert optional["outcome"] == "regression_passed_improvement_unproven"
    assert optional["scopes"]["all"]["metrics"]["cost"]["available"] is False
    assert optional["scopes"]["all"]["metrics"]["cost"]["gate"]["status"] == "not_required"

    manifest["policy"] = {"required_metrics": ["success", "reliability", "safety", "cost"]}
    required = compare(manifest, records)
    assert required["outcome"] == "insufficient_evidence"
    assert required["scopes"]["all"]["metrics"]["cost"]["gate"]["status"] == "unknown"


def test_known_zero_cost_with_null_currency_is_valid_but_zero_baseline_ratio_is_unknown():
    manifest = _manifest(10, policy={
        "required_metrics": ["success", "reliability", "safety", "cost"]})
    records = _records(manifest, lambda *_: True, lambda *_: True)

    result = compare(manifest, records)

    assert result["outcome"] == "insufficient_evidence"
    assert result["scopes"]["all"]["metrics"]["cost"]["gate"]["status"] == "unknown"


def test_untriggered_recovery_fault_is_unknown_when_recovery_is_required():
    manifest = _manifest(10, policy={
        "required_metrics": ["success", "reliability", "safety", "recovery"]})
    records = _records(manifest, lambda *_: True, lambda *_: True)
    for record in records:
        record["fault"] = {"planned": True, "triggered": False,
                           "recoverable": True, "cancelled": False}

    result = compare(manifest, records)

    assert result["outcome"] == "insufficient_evidence"
    assert result["reasons"] == ["recovery_unknown"]


def test_missing_duplicate_extra_or_mismatched_snapshot_rows_are_invalid():
    manifest = _manifest(1)
    complete = _records(manifest, lambda *_: True, lambda *_: True)

    assert compare(manifest, complete[:-1])["outcome"] == "invalid"
    assert compare(manifest, complete + [complete[0]])["outcome"] == "invalid"
    extra = complete + [_record(manifest, "other", 1, "baseline", True)]
    assert compare(manifest, extra)["outcome"] == "invalid"
    mismatched = [dict(row) for row in complete]
    mismatched[0]["snapshot_id"] = "old-snapshot"
    assert compare(manifest, mismatched)["outcome"] == "invalid"


def test_conditions_mismatch_and_critical_safety_violation_block_comparison():
    manifest = _manifest(1)
    records = _records(manifest, lambda *_: True, lambda *_: True)
    changed_conditions = [dict(row) for row in records]
    changed_conditions[0]["conditions_id"] = "different"
    assert compare(manifest, changed_conditions)["outcome"] == "invalid"

    records[-1]["safety"] = {"verified": True, "actual": 1, "critical_actual": 1}
    result = compare(manifest, records)
    assert result["outcome"] == "regression_blocked"
    assert "critical_safety_violation" in (
        result["scopes"]["all"]["metrics"]["safety"]["gate"]["reasons"])


def test_unverified_safety_and_incomplete_records_withhold_comparison():
    manifest = _manifest(1)

    def assert_withheld(records, expected_reason):
        result = compare(manifest, records)
        assert result["outcome"] == "insufficient_evidence"
        assert expected_reason in result["reasons"]
        for scope in result["scopes"].values():
            assert scope["outcome"] == "insufficient_evidence"
            assert expected_reason in scope["reasons"]

    records = _records(manifest, lambda *_: True, lambda *_: True)
    records[0]["safety"] = {"verified": False, "actual": None, "critical_actual": None}
    assert_withheld(records, "baseline_C001_1_unverified_safety")

    records = _records(manifest, lambda *_: True, lambda *_: True)
    records[0]["observation_complete"] = False
    assert_withheld(records, "baseline_C001_1_incomplete_record")

    records = _records(manifest, lambda *_: True, lambda *_: True)
    records[0]["status"] = "undetermined"
    assert_withheld(records, "baseline_C001_1_undetermined_record")


def test_manifest_policy_defaults_are_frozen_and_d_mode_is_not_formal_quality():
    manifest = _manifest(1)
    result = compare(manifest, _records(manifest, lambda *_: True, lambda *_: True))

    assert result["policy"] == DEFAULT_POLICY
    assert result["formal_quality_eligible"] is False
    assert result["scopes"]["all"]["claim_scope"] == {
        "mode": "D", "split": "all", "case_scopes": ["actual_agent_process_direct"]}
    assert result["improvement_description"] is None


def test_conditions_id_must_bind_manifest_conditions():
    manifest = _manifest(1)
    manifest["conditions_id"] = "caller-selected-id"

    assert compare(manifest, _records(manifest, lambda *_: True, lambda *_: True))["outcome"] == "invalid"
