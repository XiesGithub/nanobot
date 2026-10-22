"""SAVE faults separate task errors, delivery, process exit and cleanup errors."""

import errno
import json
import multiprocessing as mp
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evals import daily, run
from evals.common import ROOT, canonical, digest, load_cases, read
from evals.repeat import build_plan, record_trial
from evals.scorer import score
from evals.worker import _close_provider, _validate_fault_mode
from nanobot.utils.observability import observation_context


@pytest.mark.parametrize("fault,operation", [
    ("save_open", "temp_open"), ("save_replace", "os_replace"),
    ("save_replace_cleanup", "os_replace"),
])
def test_save_error_is_not_successful_worker_exit(tmp_path, fault, operation):
    case = next(c for c in load_cases() if c["case_id"] == "V004")
    plan = build_plan([case], "D")[0]
    plan.update(trial_id="V004-" + fault, fault=fault, fault_condition=fault, purpose="diagnostic")
    output, isolation = tmp_path / "run", tmp_path / "instances"
    output.mkdir()
    isolation.mkdir()
    result = run.run_trial(case, plan, output, isolation, "D", mp.get_context("spawn"))
    assert result["execution_status"] == "task_error"
    assert result["status"] == "invalid"
    assert result["cleanup"]["ok"] is True
    assert not list(isolation.iterdir())
    trial = output / "trials" / plan["trial_id"]
    evidence = read(trial / "artifacts" / "evidence.json")
    assert evidence["worker_process"]["stopped"] is True
    assert isinstance(evidence["worker_process"]["exitcode"], int)
    assert evidence["entry_phase"] == "error"
    assert evidence["delivery_observed"] is False
    events = evidence["events"]
    assert not any(e["type"] == "delivery" for e in events)
    assert any(e["type"] == "stage" and e["phase"] == "error"
               and e["payload"]["stage_name"] == "SAVE" for e in events)
    failures = [e["payload"] for e in events if e["type"] == "session_save_failure"]
    assert failures[0]["operation"] == operation
    assert failures[0]["errno"] == errno.EACCES
    assert failures[0]["winerror"] == 5
    assert not failures[0]["path"].startswith(str(tmp_path))
    if fault == "save_replace_cleanup":
        assert failures[1]["operation"] == "temp_cleanup"
        assert failures[1]["winerror"] == 32
        assert next(e for e in events if e["type"] == "entry" and e["phase"] == "error")[
            "payload"]["error_kind"] == "PermissionError"
    assert "synthetic save failure" not in json.dumps(failures)
    metric = record_trial(case, plan, output, result)
    assert metric["execution_complete"] is True
    assert metric["completion_met"] is False
    assert metric["scoring_complete"] is False
    assert metric["status"] == "invalid"


@pytest.mark.parametrize("fault", ["save_open", "save_replace", "save_replace_cleanup"])
def test_real_mode_rejects_save_fault_injection(fault):
    with pytest.raises(ValueError, match="fault_injection_requires_deterministic_mode"):
        _validate_fault_mode("R", fault)


def test_old_invalid_is_preserved():
    path = ROOT / "docs/agent-evaluation-current-20261010/R-agent-2/executions/V004-1/trials/V004-1"
    evidence = read(path / "artifacts" / "evidence.json")
    original = read(path / "verdicts" / "1.json")
    case = next(c for c in load_cases() if c["case_id"] == "V004")
    replay = score(case, evidence)
    assert replay["status"] == original["status"] == "invalid"
    assert replay["reasons"] == original["reasons"]
    assert not any(e["type"] == "session_save_failure" for e in evidence["events"])


@pytest.mark.parametrize("has_primary", [False, True])
async def test_provider_cleanup_retains_primary_error_and_redacts_details(has_primary):
    primary = PermissionError(errno.EACCES, "PRIVATE-SESSION-BODY")
    secondary = OSError(errno.EBUSY, "PRIVATE-API-CREDENTIAL", "/private/secret")
    provider = SimpleNamespace(_client=SimpleNamespace(close=AsyncMock(side_effect=secondary)))
    events = []
    with observation_context(lambda kind, **fields: events.append((kind, fields)), "test"):
        with pytest.raises(OSError) as caught:
            await _close_provider(provider, primary if has_primary else None)
    assert caught.value is (primary if has_primary else secondary)
    assert events[0][0] == "cleanup_error"
    assert events[0][1]["errno"] == errno.EBUSY
    assert "PRIVATE" not in json.dumps(events)
    assert "/private" not in json.dumps(events)


def test_harness_cleanup_failure_does_not_replace_primary(tmp_path, monkeypatch):
    primary = PermissionError(errno.EACCES, "PRIVATE-SESSION-BODY")
    secondary = PermissionError(errno.EBUSY, "PRIVATE-API-CREDENTIAL")

    def execute(*args):
        args[-1].mkdir()
        raise primary

    def cleanup(*args):
        raise secondary

    monkeypatch.setattr(run, "_run_trial", execute)
    monkeypatch.setattr(run, "cleanup_instance", cleanup)
    result = run.run_trial({"case_id": "V004"}, {
        "trial_id": "test", "purpose": "diagnostic", "fault": None},
        tmp_path / "output", tmp_path, "D", None)
    assert result["reasons"][0]["errno"] == errno.EACCES
    assert result["cleanup"]["error"]["errno"] == errno.EBUSY
    assert result["cleanup"]["ok"] is False
    assert "PRIVATE" not in json.dumps(result)


def test_source_cleanup_failure_preserves_controller_record(tmp_path, monkeypatch):
    import evals.identity as identity
    import evals.paired as paired

    def materialize(snapshot, destination):
        destination.mkdir(parents=True)
        return destination

    def fail_process(*args, **kwargs):
        raise ValueError("PRIVATE-PRIMARY")

    def fail_cleanup(*args, **kwargs):
        raise PermissionError(errno.EACCES, "PRIVATE-CLEANUP")

    monkeypatch.setattr(daily, "ROOT", tmp_path)
    monkeypatch.setattr(identity, "freeze", lambda *a, **k: {"snapshot_id": "synthetic"})
    monkeypatch.setattr(identity, "materialize", materialize)
    monkeypatch.setattr(paired, "run_process", fail_process)
    monkeypatch.setattr(daily.shutil, "rmtree", fail_cleanup)
    output = tmp_path / "output"
    result = daily.run("development", "D", output, daily.budgets("D"), ["V004"])
    cleanup = read(output / "cleanup.json")
    assert cleanup["removed"] is False
    assert cleanup["error"]["errno"] == errno.EACCES
    assert "PRIVATE" not in json.dumps(cleanup)
    assert read(output / "run-result.json") == result
    assert "controller:ValueError" in result["controller_errors"]
    assert "source_cleanup:PermissionError" in result["controller_errors"]


@pytest.mark.parametrize("change,ok", [
    ({}, True), ({"error": None}, True),
    ({"error": {"errno": errno.EACCES}}, False),
    ({"removed": False}, False), ({"owned_source": "other"}, False),
    ({"unexpected": None}, False),
])
def test_cleanup_audit_accepts_only_no_error_and_removed_owned_source(tmp_path, monkeypatch, change, ok):
    import evals.identity as identity

    manifest = {"snapshot_id": "synthetic", "subject_path": str(tmp_path / "removed-source"),
                "mode": "D", "tier": "development", "repeat": 1, "budget": {},
                "plan": [], "active": {}, "plan_sha256": digest(canonical([])),
                "contracts_sha256": digest(canonical({}))}
    conditions = {k: manifest[k] for k in (
        "mode", "tier", "repeat", "budget", "plan_sha256", "contracts_sha256")}
    saved = {"stop_reason": None, "controller_errors": []}
    files = {"artifact-index.json": [], "manifest.json": manifest,
             "snapshot/manifest.json": {"snapshot_id": "synthetic", "conditions": conditions},
             "run-result.json": saved,
             "cleanup.json": {"owned_source": manifest["subject_path"], "removed": True, **change}}
    (tmp_path / "records.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(daily, "inventory", lambda output: [])
    monkeypatch.setattr(daily, "read", lambda path: files[path.relative_to(tmp_path).as_posix()])
    monkeypatch.setattr(daily, "result_for", lambda *args: saved)
    monkeypatch.setattr(identity, "verify", lambda *args: {
        "ok": True, "errors": [], "manifest": {"snapshot_id": "synthetic"}})
    result = daily.audit(tmp_path)
    assert result["ok"] is ok
    assert result["errors"] == ([] if ok else ["source_cleanup"])
    assert result["agent_reruns"] == result["model_requests"] == 0
