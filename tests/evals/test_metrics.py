from copy import deepcopy

import pytest

from evals.metrics import summarize


def _cost(amount=1.0, *, closed=True, tokens=10, attempts=2,
          unknown_usage=0, unknown_coverage=0):
    return {"amount": amount, "currency": "USD", "closed": closed,
            "known_tokens": tokens, "model_attempts": attempts,
            "unknown_usage_attempts": unknown_usage,
            "unknown_sdk_coverage_calls": unknown_coverage}


def _plan(case_id, index, *, purpose="primary", profile="R-basic", **extra):
    row = {"trial_id": f"{case_id}-{index}-{purpose}-{profile}", "case_id": case_id,
           "repeat_index": index, "purpose": purpose, "profile": profile,
           "task_type": "answer", "entry": "process_direct", "fault_condition": "none"}
    row.update(extra)
    return row


def _record(plan, status="pass", **extra):
    done = status in ("pass", "product_fail")
    row = {
        "trial_id": plan["trial_id"], "case_id": plan["case_id"],
        "repeat_index": plan["repeat_index"], "purpose": plan["purpose"],
        "profile": plan["profile"], "mode": "R", "scope": "archive-v1",
        "entry": plan["entry"], "task_type": plan["task_type"],
        "fault_condition": plan["fault_condition"],
        "status": status, "execution_status": "finished",
        "execution_complete": True, "scoring_complete": True,
        "observation_complete": True, "completion_met": done,
        "completion_ms": 10 * plan["repeat_index"] if done else None,
        "elapsed_ms": 10 * plan["repeat_index"],
        "safety": {"verified": True, "attempts": 0, "blocked": 0,
                   "actual": 0, "critical_actual": 0},
        "product_cost": _cost(), "grading_cost": _cost(0.1, tokens=2, attempts=1),
        "fault": {"planned": False, "triggered": False, "recoverable": False,
                  "boundary": None, "cancelled": False},
        "evidence_refs": [], "evidence_sha256": "a" * 64,
    }
    row.update(extra)
    return row


def _run(statuses, *, repeat=3, extra_plan=(), record_mutator=None):
    plan = []
    records = []
    for case_id, outcomes in statuses.items():
        for index, status in enumerate(outcomes, 1):
            item = _plan(case_id, index)
            plan.append(item)
            record = _record(item, status)
            if record_mutator:
                record_mutator(item, record)
            records.append(record)
    for item in extra_plan:
        plan.append(item)
        records.append(_record(item))
    primary_cases = [case_id for case_id, outcomes in statuses.items()]
    return {"mode": "R", "scope": "archive-v1", "repeat": repeat,
            "covered_case_ids": primary_cases, "plan": plan}, records


def test_three_repeat_rates_reliability_cost_latency_and_diagnostics_are_separate():
    diagnostic = _plan("control", 1, purpose="diagnostic", profile="D-selfcheck",
                       fault_condition="wrong_answer")
    manifest, records = _run({
        "all-good": ["pass", "pass", "pass"],
        "flaky": ["pass", "pass", "product_fail"],
        "all-fail": ["product_fail", "product_fail", "product_fail"],
    }, extra_plan=[diagnostic])
    records[-1]["product_cost"] = _cost(1000)
    metrics = summarize(manifest, records)

    primary = metrics["primary_profiles"]["R-basic"]
    assert primary["publication"]["available"]
    assert primary["publication"]["covered_strict_rate"] == 5 / 9
    assert primary["repeated"]["stable_all_success_cases"] == 1
    assert primary["repeated"]["unstable_cases"] == 1
    assert primary["repeated"]["zero_success_cases"] == 1
    assert primary["repeated"]["mean_case_success_rate"] == pytest.approx(5 / 9)
    assert primary["costs"]["product_cost"]["amount"] == 9.0
    assert primary["costs"]["product_cost"]["cost_per_success"] == 1.8
    assert primary["costs"]["grading_cost"]["amount"] == 0.9
    assert primary["costs"]["grading_cost"]["cost_per_success"] == 0.18
    assert primary["costs"]["product_cost"]["model_attempts"] == 18
    assert primary["latency"]["completion_p95_ms"] == 30
    assert primary["latency"]["groups"][0]["task_type"] == "answer"
    assert primary["latency"]["groups"][0]["entry"] == "process_direct"
    assert primary["latency"]["groups"][0]["fault_condition"] == "none"
    assert primary["latency"]["overall_deadline_p95_status"] == "no_timeout_observed"
    assert metrics["diagnostic_profiles"]["D-selfcheck"]["publication"]["available"] is False
    assert metrics["diagnostic_profiles"]["D-selfcheck"]["costs"]["product_cost"]["amount"] == 1000
    assert primary["costs"]["product_cost"]["amount"] != 1000
    assert metrics["formal_quality_eligible"] is False


def test_missing_repeat_stays_in_denominator_and_withholds_strict_and_reliability():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    missing_id = manifest["plan"][1]["trial_id"]
    records = [record for record in records if record["trial_id"] != missing_id]
    summary = summarize(manifest, records)
    primary = summary["primary_profiles"]["R-basic"]

    assert summary["integrity"]["missing_trial_ids"] == [missing_id]
    assert primary["publication"]["covered_strict_rate"] is None
    assert primary["completeness"]["execution"]["rate"] == 2 / 3
    assert primary["completeness"]["scoring"]["rate"] == 2 / 3
    assert primary["completeness"]["observation"]["rate"] == 2 / 3
    assert primary["repeated"]["all_repeats_valid"] is False
    assert primary["repeated"]["mean_case_success_rate"] is None
    assert primary["costs"]["product_cost"]["amount"] is None


def test_duplicate_trial_record_is_not_double_counted_or_published():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    records.append(deepcopy(records[0]))
    summary = summarize(manifest, records)
    primary = summary["primary_profiles"]["R-basic"]

    assert summary["integrity"]["duplicate_record_trial_ids"] == [records[0]["trial_id"]]
    assert summary["integrity"]["duplicate_record_count"] == 1
    assert primary["publication"]["available"] is False
    assert primary["publication"]["covered_strict_rate"] is None
    assert primary["recorded"] == 2


def test_missing_covered_case_withholds_score_and_is_in_completeness_denominator():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    manifest["covered_case_ids"].append("omitted")
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["planned"] == 6
    assert primary["publication"]["denominator"] == 6
    assert primary["publication"]["covered_strict_rate"] is None
    assert primary["publication"]["missing_covered_case_ids"] == ["omitted"]
    assert primary["completeness"]["execution"] == {
        "planned": 6, "complete": 3, "incomplete": 3, "rate": 0.5}


def test_three_repeat_plan_shape_is_required_to_publish_rate():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    omitted_id = manifest["plan"][2]["trial_id"]
    manifest["plan"].pop(2)
    records = [record for record in records if record["trial_id"] != omitted_id]
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["publication"]["available"] is False
    assert primary["publication"]["denominator"] == 3
    assert primary["publication"]["plan_repeat_integrity"] is False
    assert primary["completeness"]["execution"]["rate"] == 2 / 3


def test_manifest_coverage_and_repeat_indices_detect_missing_case_or_repeat():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    manifest["covered_case_ids"].append("missing-case")
    summary = summarize(manifest, records)
    primary = summary["primary_profiles"]["R-basic"]
    assert primary["publication"]["covered_strict_rate"] is None
    assert primary["publication"]["denominator"] == 6
    assert summary["integrity"]["ok"] is False

    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    manifest["plan"][2]["repeat_index"] = 4
    records[2]["repeat_index"] = 4
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]
    assert primary["publication"]["available"] is False
    assert primary["publication"]["denominator"] == 3
    assert primary["publication"]["plan_repeat_integrity"] is False


def test_unknown_amount_or_usage_keeps_partial_tokens_but_not_total_cost():
    def make_unknown(plan, record):
        if plan["repeat_index"] == 2:
            record["product_cost"]["amount"] = None
            record["product_cost"]["unknown_usage_attempts"] = 1
            record["grading_cost"]["amount"] = None

    manifest, records = _run({"one": ["pass", "pass", "pass"]}, record_mutator=make_unknown)
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]
    product = primary["costs"]["product_cost"]
    grading = primary["costs"]["grading_cost"]

    assert primary["publication"]["covered_strict_rate"] == 1
    assert product["amount"] is None
    assert product["amount_complete"] is False
    assert product["known_tokens"] == 30
    assert product["actual_tokens"] is None
    assert product["unknown_usage_attempts"] == 1
    assert product["cost_per_success"] is None
    assert grading["amount"] is None
    assert grading["cost_per_success"] is None


def test_actual_tokens_stay_unknown_when_usage_or_attempt_coverage_fields_are_missing():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    product = records[0]["product_cost"]
    product.pop("unknown_usage_attempts")
    product.pop("unknown_sdk_coverage_calls")
    product.pop("model_attempts")
    metric = summarize(manifest, records)["primary_profiles"]["R-basic"]["costs"]["product_cost"]

    assert metric["amount"] is None
    assert metric["actual_tokens"] is None
    assert metric["unknown_usage_metadata_trials"] == 1
    assert metric["unknown_coverage_metadata_trials"] == 1
    assert metric["unknown_attempt_trials"] == 1


def test_currency_is_required_for_nonzero_total_but_zero_diagnostic_charge_is_known():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    records[0]["product_cost"]["currency"] = None
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]
    product = primary["costs"]["product_cost"]
    assert product["amount"] is None
    assert product["known_amount_partial"] == 2
    assert product["known_amount_by_currency"] == [{"currency": "USD", "amount": 2}]

    diagnostic = _plan("control", 1, purpose="diagnostic", profile="D",
                       fault_condition="none")
    manifest["plan"].append(diagnostic)
    manifest["plan"][-1]["profile"] = "D"
    zero = _record(diagnostic)
    zero["product_cost"] = _cost(0)
    zero["product_cost"]["currency"] = None
    summary = summarize(manifest, records + [zero])
    assert summary["diagnostic_profiles"]["D"]["costs"]["product_cost"]["amount"] == 0


def test_zero_success_has_valid_zero_rate_and_null_cost_per_success():
    manifest, records = _run({"one": ["product_fail"], "two": ["product_fail"]}, repeat=1)
    summary = summarize(manifest, records)
    primary = summary["primary_profiles"]["R-basic"]

    assert primary["publication"]["available"]
    assert primary["publication"]["covered_strict_rate"] == 0
    assert primary["costs"]["product_cost"]["amount"] == 2
    assert primary["costs"]["product_cost"]["cost_per_success"] is None
    assert primary["costs"]["product_cost"]["cost_per_success_reason"] == "zero_successes"
    assert primary["repeated"]["stable_rate"] is None
    assert primary["repeated"]["unstable_rate"] is None


def test_timeout_remains_failure_duration_and_is_excluded_from_completion_p95():
    manifest, records = _run({"one": ["pass", "pass", "product_fail"]})
    timeout = records[-1]
    timeout["execution_status"] = "timeout"
    timeout["completion_met"] = False
    timeout["completion_ms"] = None
    timeout["elapsed_ms"] = 120000
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["latency"]["completed_samples"] == 2
    assert primary["latency"]["completion_p95_ms"] == 20
    assert primary["latency"]["failure_elapsed_ms"] == [120000]
    assert primary["latency"]["timeouts"] == 1
    assert primary["latency"]["timeout_rate"] == 1 / 3
    assert primary["latency"]["overall_deadline_p95_ms"] is None
    assert primary["latency"]["overall_deadline_p95_status"] == "beyond_measurement_window"


def test_only_triggered_recoverable_faults_enter_recovery_denominator():
    plan = []
    records = []
    specs = [
        ("recovered", "pass", True, True, False),
        ("failed", "product_fail", True, True, False),
        ("not-triggered", "pass", False, True, False),
        ("permanent", "product_fail", True, False, False),
    ]
    for index, (case_id, status, triggered, recoverable, cancelled) in enumerate(specs, 1):
        item = _plan(case_id, 1, fault_condition="provider_503", fault_boundary="provider")
        plan.append(item)
        record = _record(item, status)
        record["fault"] = {"planned": True, "triggered": triggered,
                           "recoverable": recoverable, "boundary": "provider",
                           "cancelled": cancelled}
        records.append(record)
    manifest = {"mode": "R", "scope": "archive-v1", "repeat": 1,
                "covered_case_ids": [row["case_id"] for row in plan], "plan": plan}
    recovery = summarize(manifest, records)["primary_profiles"]["R-basic"]["fault_recovery"]

    assert recovery["planned_fault_trials"] == 4
    assert recovery["triggered_recoverable"] == 2
    assert recovery["recovered"] == 1
    assert recovery["rate"] == 0.5
    assert recovery["not_triggered"] == 1
    assert recovery["permanent_or_nonrecoverable"] == 1
    assert recovery["by_boundary"]["provider"]["rate"] == 0.5


def test_cancelled_recovery_outcome_is_separate_and_withholds_rate():
    plan = _plan("cancel", 1, fault_condition="tool_error")
    record = _record(plan, "undetermined", execution_complete=False, scoring_complete=False,
                     execution_status="cancelled")
    record["fault"] = {"planned": True, "triggered": True, "recoverable": True,
                       "boundary": "tool", "cancelled": True}
    recovery = summarize({"mode": "R", "scope": "archive-v1", "repeat": 1,
                          "covered_case_ids": ["cancel"], "plan": [plan]},
                         [record])["primary_profiles"]["R-basic"]["fault_recovery"]

    assert recovery["cancelled"] == 1
    assert recovery["triggered_recoverable"] == 1
    assert recovery["outcome_unknown"] == 1
    assert recovery["rate"] is None


def test_unverified_zero_safety_value_does_not_publish_zero_critical_violations():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    records[0]["safety"]["verified"] = False
    records[0]["safety"]["critical_actual"] = 0
    safety = summarize(manifest, records)["primary_profiles"]["R-basic"]["safety"]

    assert safety["critical_actual_observed"] == 0
    assert safety["critical_actual_total"] is None
    assert safety["unverified_trials"] == 1
    assert safety["gate"]["status"] == "unverified"
    assert safety["gate"]["passing_conclusion"] is False


def test_verified_safety_with_unknown_actual_side_effects_is_not_a_pass():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    records[0]["safety"]["actual"] = None
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["publication"]["available"] is False
    assert primary["safety"]["actual_violation_unknown_trials"] == 1
    assert primary["safety"]["critical_actual_total"] is None
    assert primary["safety"]["gate"]["status"] == "unverified"
    assert primary["safety"]["gate"]["passing_conclusion"] is False


def test_actual_critical_safety_violation_blocks_passing_conclusion_even_with_rate():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    records[0]["safety"]["actual"] = 1
    records[0]["safety"]["critical_actual"] = 1
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["publication"]["covered_strict_rate"] == 2 / 3
    assert primary["publication"]["blocked_by_critical_safety"] is True
    assert primary["publication"]["passing_conclusion"] is False
    assert primary["safety"]["gate"]["status"] == "blocked"


def test_diagnostic_controls_are_counted_by_trial_id_not_case_repeat_pair():
    plan = []
    records = []
    control_faults = [(True, True), (True, True), (False, True)]
    for index in range(1, 8):
        item = _plan("V001", 1, purpose="diagnostic", profile="D-phase5-control-v1",
                     fault_condition="control" if index <= 3 else "none")
        item["trial_id"] = f"control-{index}"
        plan.append(item)
        record = _record(item)
        record["mode"] = "D"
        triggered, recoverable = control_faults[index - 1] if index <= 3 else (False, False)
        record["fault"] = {"planned": index <= 3, "triggered": triggered if index <= 3 else False,
                           "recoverable": recoverable, "boundary": "provider",
                           "cancelled": False}
        records.append(record)
        records[-1]["scope"] = "control-v1"
    summary = summarize({"mode": "D", "scope": "control-v1", "repeat": 3,
                         "covered_case_ids": ["V001"], "plan": plan}, records)
    diagnostic = summary["diagnostic_profiles"]["D-phase5-control-v1"]

    assert diagnostic["planned"] == 7
    assert diagnostic["recorded"] == 7
    assert diagnostic["completeness"]["execution"]["rate"] == 1
    assert diagnostic["repeated"]["available"] is False
    assert diagnostic["publication"]["available"] is False
    assert diagnostic["fault_recovery"]["planned_fault_trials"] == 3
    assert diagnostic["fault_recovery"]["triggered_recoverable"] == 2
    assert diagnostic["fault_recovery"]["recovered"] == 2
    assert diagnostic["fault_recovery"]["not_triggered"] == 1
    assert diagnostic["fault_recovery"]["rate"] == 1


def test_undetermined_semantic_dispute_cannot_be_counted_as_success():
    manifest, records = _run({"one": ["pass", "undetermined", "pass"]})
    records[1]["scoring_complete"] = False
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["publication"]["available"] is False
    assert primary["publication"]["covered_strict_rate"] is None
    assert primary["publication"]["unresolved"] == 1
    assert primary["completeness"]["scoring"]["rate"] == 2 / 3
    assert primary["repeated"]["all_repeats_valid"] is False


def test_mode_scope_and_diagnostic_profiles_are_never_blended():
    diagnostic = _plan("control", 1, purpose="diagnostic", profile="D-selfcheck")
    manifest, records = _run({"one": ["pass", "pass", "pass"]}, extra_plan=[diagnostic])
    records[0]["mode"] = "D"
    summary = summarize(manifest, records)

    assert summary["mode"] == "R"
    assert summary["scope"] == "archive-v1"
    assert summary["integrity"]["mismatched_records"][0]["errors"] == ["mode_mismatch"]
    assert summary["primary_profiles"]["R-basic"]["publication"]["available"] is False
    assert summary["diagnostic_profiles"]["D-selfcheck"]["publication"]["available"] is False


def test_entry_task_and_fault_group_dimensions_are_frozen_and_separated():
    manifest, records = _run({"one": ["pass", "pass", "pass"]})
    manifest["plan"][0].update(task_type="tool", entry="spawn_task",
                               fault_condition="provider_503")
    records[0].update(task_type="tool", entry="spawn_task", fault_condition="provider_503")
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]

    assert primary["publication"]["available"]
    assert len(primary["latency"]["groups"]) == 2
    dimensions = {(row["task_type"], row["entry"], row["fault_condition"])
                  for row in primary["latency"]["groups"]}
    assert dimensions == {("answer", "process_direct", "none"),
                          ("tool", "spawn_task", "provider_503")}

    records[0].pop("entry")
    primary = summarize(manifest, records)["primary_profiles"]["R-basic"]
    assert primary["publication"]["available"] is False
