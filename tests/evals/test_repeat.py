"""Phase 5 repeated-trial contracts over frozen plans and evidence."""

import json
from copy import deepcopy

import pytest

from evals import repeat
from evals.common import canonical, digest, load_cases, write
from evals.scorer import score
from evals.trace import analyze_trace
from evals.worker import Emitter


@pytest.fixture
def cases():
    return load_cases()


def test_build_plan_freezes_three_independent_primaries_and_separate_controls(cases):
    plan = repeat.build_plan(cases, "D", controls=True)
    primary = [row for row in plan if row["purpose"] == "primary"]
    controls = [row for row in plan if row["purpose"] == "diagnostic"]

    assert len(primary) == 5 * 3
    assert [row["repeat_index"] for row in primary[:3]] == [1, 2, 3]
    assert len({row["trial_id"] for row in primary}) == 15
    assert {row["case_id"] for row in primary} == {case["case_id"] for case in cases}
    assert all(row["fault"] is None and row["profile"] == "D-phase5-diagnostic-v1"
               for row in primary)
    assert len(controls) == 7
    assert all(row["repeat_index"] == 1 and row["fault"] for row in controls)
    assert {row["fault"] for row in controls} == {
        "wrong_answer", "false_completion", "unrelated_file", "timeout",
        "provider_503_once", "tool_once", "tool_not_triggered",
    }
    assert all(row["profile"] == "D-phase5-control-v1" for row in controls)


def test_build_plan_rejects_real_model_fault_controls(cases):
    with pytest.raises(ValueError, match="unsupported_repeat_profile"):
        repeat.build_plan(cases, "R", controls=True)


class EventPipe:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _artifact_set(output, case, plan, *, split_clock=False, response='{"total":330}',
                  verdict_edit=None):
    trial_dir = output / "trials" / plan["trial_id"]
    trial_dir.mkdir(parents=True)
    pipe = EventPipe()
    emit = Emitter(pipe, output.name, plan["trial_id"])
    emit.event("task", "start", span="task")
    entry_start = emit.event("entry", "start", span="entry", parent_span_id="task")
    emit.event("entry", "end", span=entry_start, parent_span_id="task")
    emit.event("iteration", "start", span="iteration", parent_span_id="task")
    emit.event("iteration", "end", span="iteration", parent_span_id="task")
    delivery = emit.event("delivery", parent_span_id="task", response=response,
                          outbox_count=0)
    emit.event("task", "end", span="task")
    events = [message["event"] for message in pipe.messages]
    if split_clock:
        events[5]["clock_id"] = "another-process"
    evidence = {
        "trial_id": plan["trial_id"], "observation_version": "phase3/1.0",
        "execution_status": "finished", "ready": True, "events": events,
        "before": {}, "after": {}, "workspace": str(output / "workspace"),
        "cleanup": {"ok": True, "instance_removed": True},
        "collector_errors": [], "runtime_violations": [],
    }
    trace = analyze_trace(events, "finished", [])
    verdict = score(case, evidence)
    if verdict_edit:
        verdict_edit(verdict)
    write(trial_dir / "artifacts/evidence.json", evidence)
    (trial_dir / "events.jsonl").write_text(
        "".join(canonical(event).decode("utf-8") + "\n" for event in events),
        encoding="utf-8",
    )
    write(trial_dir / "artifacts/trace-analysis.json", trace)
    write(trial_dir / "verdicts/1.json", verdict)
    product_cost = {
        "usage_coverage": "provider_call_boundary",
        "amount": 0 if plan["mode"] == "D" else None,
        "currency": None, "closed": trace["closed"],
        "unknown_usage_attempts": trace["unknown_usage_attempts"],
        "unknown_reason": "prices_unknown; uninstrumented_components_out_of_scope",
    }
    write(trial_dir / "trial.json", {
        **deepcopy(plan), "trial_id": plan["trial_id"], "case_id": case["case_id"],
        "run_id": output.name, "product_cost": product_cost,
        "grading_cost": {"amount": 0, "model_attempts": 0},
        "extensions": {"initial_state_sha256": digest(canonical({})), "worker_pid": 123},
    })
    indexed = ("artifacts/evidence.json", "events.jsonl", "artifacts/trace-analysis.json")
    write(trial_dir / "artifact-index.json", [
        {"path": path, "sha256": digest((trial_dir / path).read_bytes())}
        for path in indexed
    ])
    returned = {"execution_status": "finished", "cleanup": evidence["cleanup"],
                # Harness elapsed time deliberately includes far more than task completion.
                "elapsed_ms": 9876.5}
    return trial_dir, returned, events, delivery


def _primary_plan(case):
    return repeat.build_plan([case], "D")[0]


def test_record_trial_uses_same_worker_clock_for_task_latency_and_excludes_cleanup(
        tmp_path, cases):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / "phase5-run"
    output.mkdir()
    _, returned, events, _ = _artifact_set(output, case, plan)

    result = repeat.record_trial(case, plan, output, returned)

    entry = next(event for event in events if event["type"] == "entry"
                 and event["phase"] == "start")
    delivery = next(event for event in events if event["type"] == "delivery")
    assert entry["clock_id"] == delivery["clock_id"]
    assert result["status"] == "pass"
    assert result["completion_ms"] == pytest.approx(
        (delivery["monotonic_ns"] - entry["monotonic_ns"]) / 1e6)
    assert result["completion_ms"] < returned["elapsed_ms"]


def test_record_trial_rejects_cross_process_completion_clock(tmp_path, cases):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / "clock-run"
    output.mkdir()
    _, returned, _, _ = _artifact_set(output, case, plan, split_clock=True)

    result = repeat.record_trial(case, plan, output, returned)

    assert result["status"] == "invalid"
    assert any("completion_clock_mismatch" in error for error in result["adapter_errors"])


@pytest.mark.parametrize("terminal_status,expected_actual", [("error", 0), ("ok", 1)])
def test_safety_distinguishes_attempted_file_write_from_successful_side_effect(
        cases, terminal_status, expected_actual):
    case = next(case for case in cases if case["case_id"] == "V009")
    events = [
        {"event_id": "access", "type": "file_access", "phase": "point",
         "span_id": "access", "parent_span_id": "tool-span",
         "payload": {"name": "write_file", "path": "unrelated.txt"}},
        {"event_id": "terminal", "type": "tool", "phase": "end",
         "span_id": "tool-span", "payload": {"status": terminal_status}},
        {"event_id": "denied", "type": "safety", "phase": "point",
         "span_id": "denied", "payload": {"attempted": True, "actual": False}},
    ]
    evidence = {"events": events, "before": {}, "after": {}, "runtime_violations": [],
                "ready": True}

    safety = repeat._safety(case, evidence,
                            {"requirements": []}, {"valid": True})

    assert safety["attempts"] == 2
    assert safety["blocked"] == 1
    assert safety["actual"] == expected_actual


def test_record_trial_rejects_missing_required_requirement_ids(tmp_path, cases):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / "requirements-run"
    output.mkdir()

    def remove_one_requirement(verdict):
        verdict["status"] = "product_fail"
        verdict["requirements"].pop()

    _, returned, _, _ = _artifact_set(output, case, plan,
                                      verdict_edit=remove_one_requirement)
    result = repeat.record_trial(case, plan, output, returned)

    assert result["scoring_complete"] is False


@pytest.mark.parametrize("tamper", ["artifact_hash", "identity", "false_pass"])
def test_record_trial_does_not_accept_tampered_or_inconsistent_pass(
        tmp_path, cases, tamper):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / f"{tamper}-run"
    output.mkdir()

    def make_false_pass(verdict):
        verdict["status"] = "pass"
        verdict["requirements"][0]["label"] = "fail"

    trial_dir, returned, _, _ = _artifact_set(
        output, case, plan,
        verdict_edit=make_false_pass if tamper == "false_pass" else None,
    )
    if tamper == "artifact_hash":
        (trial_dir / "events.jsonl").write_text("tampered\n", encoding="utf-8")
    elif tamper == "identity":
        trial_path = trial_dir / "trial.json"
        trial = json.loads(trial_path.read_text(encoding="utf-8"))
        trial["case_id"] = "V009"
        trial_path.write_text(json.dumps(trial), encoding="utf-8")

    result = repeat.record_trial(case, plan, output, returned)

    assert result["status"] == "invalid"
    assert result["scoring_complete"] is False


def test_wrong_answer_with_forged_complete_pass_verdict_is_rejected(tmp_path, cases):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / "forged-verdict-run"
    output.mkdir()

    def forge_full_pass(verdict):
        verdict["status"] = "pass"
        for requirement in verdict["requirements"]:
            requirement["label"] = "pass"
        verdict["extensions"]["completion_met"] = True

    _, returned, _, _ = _artifact_set(
        output, case, plan, response='{"total":360}', verdict_edit=forge_full_pass,
    )
    result = repeat.record_trial(case, plan, output, returned)

    assert result["status"] == "invalid"
    assert any("verdict_evidence_mismatch" in error for error in result["adapter_errors"])


@pytest.mark.parametrize("field,value", [("profile", "other-profile"), ("repeat_index", 2)])
def test_record_trial_rejects_trial_plan_field_mutation(tmp_path, cases, field, value):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / f"plan-mutation-{field}-run"
    output.mkdir()
    trial_dir, returned, _, _ = _artifact_set(output, case, plan)
    trial_path = trial_dir / "trial.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    trial[field] = value
    trial_path.write_text(json.dumps(trial), encoding="utf-8")

    result = repeat.record_trial(case, plan, output, returned)

    assert result["status"] == "invalid"
    assert any("trial_plan_mismatch" in error for error in result["adapter_errors"])


def test_record_trial_rejects_forged_real_mode_zero_fee_and_keeps_unknown_amount(
        tmp_path, cases):
    case = cases[0]
    plan = repeat.build_plan([case], "R")[0]
    output = tmp_path / "real-fee-run"
    output.mkdir()
    trial_dir, returned, _, _ = _artifact_set(output, case, plan)
    trial_path = trial_dir / "trial.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    trial["product_cost"]["amount"] = 0
    trial_path.write_text(json.dumps(trial), encoding="utf-8")

    result = repeat.record_trial(case, plan, output, returned)

    assert result["status"] == "invalid"
    assert result["product_cost"]["amount"] is None
    assert any("cost_evidence_mismatch" in error for error in result["adapter_errors"])


def test_missing_evidence_remains_invalid_with_unknown_cost(tmp_path, cases):
    case = cases[0]
    plan = _primary_plan(case)
    output = tmp_path / "missing-run"
    output.mkdir()
    (output / "trials" / plan["trial_id"]).mkdir(parents=True)

    result = repeat.record_trial(
        case, plan, output,
        {"execution_status": "finished", "cleanup": {"ok": True}, "elapsed_ms": 10},
    )

    assert result["status"] == "invalid"
    assert result["product_cost"]["amount"] is None
    assert result["product_cost"]["closed"] is False
    assert result["grading_cost"]["amount"] == 0


def test_early_run_stop_keeps_unexecuted_planned_trials_missing(tmp_path, monkeypatch, cases):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setattr(repeat, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(repeat, "load_cases", lambda: cases)
    monkeypatch.setattr(repeat, "phase1_audit", lambda: {"status": "pass"})
    monkeypatch.setattr(repeat, "execution_manifest", lambda *args: {
        "run_id": args[3], "plan": args[2], "mode": args[1],
        "scope": repeat.SCOPE, "profiles": {}, "budget": {"run_watchdog_s": 600},
        "model": {"requested_id": "deepseek-v4-flash"},
    })

    def stop_after_first(*args):
        return {"execution_status": "crashed", "cleanup": {"ok": False},
                "elapsed_ms": 1}

    monkeypatch.setattr(repeat, "run_trial", stop_after_first)
    monkeypatch.setattr(repeat, "record_trial", lambda case, plan, output, returned: {
        "trial_id": plan["trial_id"], "case_id": plan["case_id"], "mode": "D",
        "scope": repeat.SCOPE, "profile": plan["profile"], "purpose": plan["purpose"],
        "repeat_index": plan["repeat_index"], "status": "invalid",
        "execution_complete": False, "scoring_complete": False,
        "observation_complete": False, "completion_met": False, "completion_ms": None,
            "elapsed_ms": 1, "safety": {"verified": False, "actual": 0,
                                           "critical_actual": 0},
        "cleanup": {"ok": False}, "entry": plan["entry"],
        "task_type": plan["task_type"], "fault_condition": plan["fault_condition"],
        "product_cost": {"amount": 0, "currency": None, "closed": True},
        "grading_cost": {"amount": 0, "currency": None, "closed": True},
    })

    result = repeat.run("D", tmp_path / "partial-run")
    primary = result["metrics"]["primary_profiles"]["D-phase5-diagnostic-v1"]

    assert result["planned"] == 15
    assert len(result["records"]) == 1
    assert result["ok"] is False
    assert result["stopped_reason"] == "cleanup_or_operator_interrupt"
    assert primary["publication"]["available"] is False
    assert primary["publication"]["denominator"] == 15
    assert primary["publication"]["unresolved"] == 15


def test_recovery_probe_keeps_strict_failure_and_requires_actual_trigger(cases):
    plan = repeat.build_plan(cases, "D", controls=True)
    rows = []
    for item in plan:
        if item["purpose"] != "diagnostic":
            continue
        condition = item["fault_condition"]
        injected = condition in ("provider_503_once", "tool_once", "tool_not_triggered")
        rows.append({**item, "status": "pass" if condition in
                     ("tool_once", "tool_not_triggered") else "product_fail",
                     "scoring_complete": True, "observation_complete": True,
                     "fault": {"planned": injected,
                               "triggered": injected and condition != "tool_not_triggered"},
                     "safety": {"actual": int(condition in
                                ("unrelated_file", "provider_503_once"))}})
    result = repeat.verify_controls(plan, rows)
    assert result["ok"]
    provider = next(r for r in result["results"] if r["fault_condition"] == "provider_503_once")
    assert provider["status"] == "product_fail"
    provider["fault"]["triggered"] = False
    assert not repeat.verify_controls(plan, rows)["ok"]
