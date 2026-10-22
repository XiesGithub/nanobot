"""Meaningful boundaries of scope-limited incremental evaluation."""

import pytest

from evals.batches import (
    OS_ISOLATION_REQUIRED,
    analyze_batch_trace,
    score,
    source_catalog,
    summarize,
)
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, span


def spec(assertions=None, supported=True):
    return {"scope": "module_contract", "supported": supported,
            "limitations": ["No full Agent task observation"],
            "assertions": assertions if assertions is not None else [
                {"key": "answer", "op": "equal", "expected": 330}]}


def evidence(observed, status="finished", valid=True):
    return {"observed": observed, "status": status, "collector_errors": [],
            "trace": {"valid": valid}}


def test_original_inventory_and_danger_boundary():
    catalog = source_catalog()
    assert len(catalog) == 120
    assert all(row["contract"] and len(row["source_sha256"]) == 64 for row in catalog.values())
    assert {"V022", "V104"} <= OS_ISOLATION_REQUIRED


@pytest.mark.parametrize("value", ["330", True, 329, {"answer": 330}])
def test_wrong_types_and_answers_fail(value):
    assert score(spec(), evidence({"answer": value}))["verdict"] == "product_fail"


def test_missing_not_known_null():
    case = spec([{"key": "missing", "op": "equal", "expected": None}])
    assert score(case, evidence({}))["verdict"] == "undetermined"


def test_scope_pass_does_not_promote_to_task():
    result = score(spec(), evidence({"answer": 330}))
    assert result["verdict"] == "pass"
    assert result["full_task_verdict"] == "undetermined"
    assert not result["formal_quality_eligible"]


def test_known_failure_overrides_missing_assertion():
    case = spec([{"key": "a", "op": "equal", "expected": 3},
                 {"key": "b", "op": "true", "expected": True}])
    assert score(case, evidence({"a": 4}))["verdict"] == "product_fail"


@pytest.mark.parametrize("status", ["blocked", "cancelled", "crashed", "setup_error"])
def test_nonexecution_never_passes(status):
    assert score(spec(), evidence({"answer": 330}, status))["verdict"] == "undetermined"


def test_invalid_trace_and_timeout():
    assert score(spec(), evidence({"answer": 330}, valid=False))["verdict"] == "invalid"
    assert score(spec(), evidence({}, status="timeout"))["verdict"] == "product_fail"


def test_unsupported_contract_does_not_pass():
    assert score(spec([], False), evidence({}))["verdict"] == "undetermined"


def test_missing_duplicate_and_unknown_cost():
    row = {"case_id": "V001", "scope": "module_contract", "verdict": "pass",
           "status": "finished", "scoring_complete": True, "observation_complete": True,
           "model_attempts": 1, "cost": None}
    result = summarize(["V001", "V002"], [row])
    assert not result["inventory_complete"]
    assert result["execution_complete"] == .5
    assert result["cost"] is None and result["full_task_score"] is None
    assert not summarize(["V001"], [row, row])["inventory_complete"]


def test_root_task_must_belong_to_trial():
    events = []

    class Connection:
        def send(self, message):
            events.append(message["event"])

    emit = Emitter(Connection(), "run", "trial")
    with observation_context(emit.event, "another-trial"):
        with span("task"):
            pass
    assert "root_task_trial_mismatch" in analyze_batch_trace(events, "trial")["errors"]


def test_explicit_unassigned_periodic_terminal_is_accounted():
    events = []

    class Connection:
        def send(self, message):
            events.append(message["event"])

    emit = Emitter(Connection(), "run", "trial")
    with observation_context(emit.event, "trial") as parent:
        with span("task"):
            with observation_context(parent.sink, "service-overhead"):
                with span("background_task", accounting="unassigned_periodic_overhead"):
                    pass
    assert analyze_batch_trace(events, "trial")["closed"]


@pytest.mark.parametrize("arguments, error", [
    (["--refresh-cases", "V001"], "invalid_rerun_plan"),
    (["--refresh-cases", "V001", "--refresh-reason", "changed fixture"],
     "rerun_previous_evidence_missing"),
])
def test_refresh_needs_reason_and_preserved_previous_evidence(tmp_path, monkeypatch, arguments, error):
    import sys

    import evals.batches as batches

    monkeypatch.setattr(batches, "ROOT", tmp_path)
    monkeypatch.setattr(batches, "catalog", lambda: {"V001": spec()})
    monkeypatch.setattr(sys, "argv", ["batches", "--output", str(tmp_path / "output"),
                                    "--cases", "V001", *arguments])
    with pytest.raises(ValueError, match=error):
        batches.main()
