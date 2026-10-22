"""Fixed-load profile contracts and gate boundaries; no model calls are made."""

import json

import pytest

from evals import performance
from evals.common import canonical, write


def _row(status="pass", *, complete=True, actual=0, trial_wall_ms=100.0):
    return {
        "trial_id": "LOAD-001",
        "trial_wall_ms": trial_wall_ms,
        "trial": {
            "status": status,
            "completion_ms": 80.0 if complete else None,
            "execution_complete": complete,
            "scoring_complete": complete,
            "observation_complete": complete,
            "cleanup": {"ok": complete, "instance_removed": complete},
            "safety": {"actual": actual},
        },
    }


def _five(status="pass", *, complete=True, actual=0):
    rows = [_row(status, complete=complete, actual=actual) for _ in range(5)]
    for index, row in enumerate(rows, 1):
        row["trial_id"] = f"LOAD-{index:03}"
    return rows


def test_fixed_load_contract_repeats_one_payload_and_declares_uncontrolled_os_cache():
    case, plan = performance._case_and_plan()
    conditions = performance.profile_conditions(case)
    assert len(plan) == 5
    assert {item["case_id"] for item in plan} == {"V001"}
    assert len({case["turns"][0]["input"] for _ in plan}) == 1
    assert conditions["copies"] == 5
    assert conditions["concurrency"] == 2
    assert conditions["os_page_cache"] == "not_flushed_or_guaranteed"
    assert conditions["execution_mode"] == "D_scripted_no_model_calls"
    assert conditions["native_shell"] == "not_enabled"


def test_summary_uses_nearest_rank_and_calculates_throughput():
    rows = [_row(trial_wall_ms=value) for value in (10, 20, 30, 40, 50)]
    summary = performance.summarize_performance(rows, 1000)
    assert summary["p95_trial_wall_ms"] == 50
    assert summary["p95_agent_completion_ms"] == 80
    assert summary["throughput_tasks_per_second"] == 5


@pytest.mark.parametrize("rows, expected", [
    (_five(), "passed_diagnostic"),
    (_five("product_fail"), "regression"),
    (_five("invalid"), "invalid"),
    (_five(complete=False), "insufficient_evidence"),
    (_five(actual=1), "regression"),
    (_five()[:4], "insufficient_evidence"),
])
def test_diagnostic_state_never_treats_regression_or_missing_evidence_as_pass(rows, expected):
    _, plan = performance._case_and_plan()
    assert performance._trial_state(rows, plan) == expected


def test_baseline_compare_requires_same_profile_and_applies_frozen_thresholds():
    current = {
        "profile_conditions_sha256": "conditions",
        "environment_id": "machine",
        "performance": {"p95_trial_wall_ms": 110, "throughput_tasks_per_second": 9},
    }
    baseline = {
        "state": "passed_diagnostic",
        "profile_conditions_sha256": "conditions",
        "environment_id": "machine",
        "performance": {"p95_trial_wall_ms": 100, "throughput_tasks_per_second": 10},
    }
    compared = performance._compare_baseline(current, baseline)
    assert compared["state"] == "passed_diagnostic"
    current["performance"]["p95_trial_wall_ms"] = 120
    assert performance._compare_baseline(current, baseline)["state"] == "regression"
    baseline["environment_id"] = "other-machine"
    assert performance._compare_baseline(current, baseline)["state"] == "insufficient_evidence"


def test_unsupported_platform_is_reported_without_running_or_quality_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setattr(performance, "_environment", lambda: {"os_system": "Windows"})
    monkeypatch.setattr(performance, "_source_identity", lambda: {})
    monkeypatch.setattr(performance, "_platform_matches", lambda _platform: False)
    result = performance.run(tmp_path / "linux-run", "linux")
    assert result["state"] == "insufficient_evidence"
    assert result["quality_passed"] is False
    assert result["records"] == []
    assert (tmp_path / "linux-run" / "artifact-index.json").is_file()


def test_audit_rejects_quality_promotion_even_if_report_claims_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setattr(performance, "_environment", lambda: {"os_system": "Windows"})
    monkeypatch.setattr(performance, "_source_identity", lambda: {})
    monkeypatch.setattr(performance, "_platform_matches", lambda _platform: False)
    output = tmp_path / "unsupported"
    performance.run(output, "linux")
    result_path = output / "run-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["quality_passed"] = True
    result_path.write_text(json.dumps(result), encoding="utf-8")
    audit = performance.audit_run(output)
    assert not audit["ok"]
    assert "quality_gate_must_remain_false" in audit["errors"]


def test_requested_platform_must_be_windows_or_linux():
    with pytest.raises(ValueError, match="unsupported_platform_profile"):
        performance._platform_matches("macos")


def _auditable_profile(tmp_path, monkeypatch):
    """Bind existing trial replay separately from the top-level metric checks."""
    case, plan = performance._case_and_plan()
    rows = _five()
    for row in rows:
        row["trial"].update(execution_status="finished", elapsed_ms=100)
    conditions = performance.profile_conditions(case)
    environment = {"os_system": "Windows"}
    source_identity = {"evals/performance.py": "a" * 64}
    manifest = {
        "schema_version": performance.VERSION, "run_id": tmp_path.name,
        "profile": "windows-fixed-load-v1", "requested_platform": "windows",
        "supported_here": True, "profile_conditions": conditions,
        "profile_conditions_sha256": performance.digest(canonical(conditions)),
        "environment": environment,
        "environment_id": performance.digest(canonical(environment)),
        "source_identity": source_identity,
        "source_identity_sha256": performance.digest(canonical(source_identity)),
        "plan": plan, "model_budget": {"model_calls": 0},
    }
    result = {**manifest, "state": "passed_diagnostic", "quality_passed": False,
              "quality_eligible": False, "records": rows, "baseline_comparison": None,
              "performance": performance.summarize_performance(rows, 1000)}
    write(tmp_path / "manifest.json", manifest)
    write(tmp_path / "run-result.json", result)
    write(tmp_path / "timing.json", {"total_wall_ms": 1000})
    (tmp_path / "records.jsonl").write_text(
        "".join(canonical(row).decode() + "\n" for row in rows), encoding="utf-8")
    import evals.repeat

    monkeypatch.setattr(evals.repeat, "record_trial", lambda _case, item, *_args:
                        rows[int(item["trial_id"].split("-")[1]) - 1]["trial"])
    performance._write_index(tmp_path)
    assert performance.audit_run(tmp_path)["ok"]
    return result


def _replace_and_rehash(directory, name, value):
    (directory / name).write_text(json.dumps(value), encoding="utf-8")
    (directory / "artifact-index.json").unlink()
    performance._write_index(directory)


@pytest.mark.parametrize("field,value", [
    ("p95_trial_wall_ms", 1), ("throughput_tasks_per_second", 999),
    ("total_wall_ms", 1), ("wall_latency_samples", 1),
])
def test_audit_recomputes_metrics_after_top_level_rehash(tmp_path, monkeypatch, field, value):
    result = _auditable_profile(tmp_path, monkeypatch)
    result["performance"][field] = value
    _replace_and_rehash(tmp_path, "run-result.json", result)
    assert "performance_replay_mismatch:" + field in performance.audit_run(tmp_path)["errors"]


def test_audit_binds_result_source_and_environment_to_manifest(tmp_path, monkeypatch):
    result = _auditable_profile(tmp_path, monkeypatch)
    result["source_identity"] = {"evals/performance.py": "b" * 64}
    result["environment"] = {"os_system": "Linux"}
    _replace_and_rehash(tmp_path, "run-result.json", result)
    errors = performance.audit_run(tmp_path)["errors"]
    assert "result_manifest_identity:source_identity" in errors
    assert "result_manifest_identity:environment" in errors


def test_audit_rejects_malformed_source_hash_even_after_rehash(tmp_path, monkeypatch):
    result = _auditable_profile(tmp_path, monkeypatch)
    result["source_identity"] = {"evals/performance.py": "unknown"}
    result["source_identity_sha256"] = performance.digest(canonical(result["source_identity"]))
    manifest = performance.read(tmp_path / "manifest.json")
    manifest.update(source_identity=result["source_identity"],
                    source_identity_sha256=result["source_identity_sha256"])
    _replace_and_rehash(tmp_path, "manifest.json", manifest)
    _replace_and_rehash(tmp_path, "run-result.json", result)
    assert "source_identity_invalid" in performance.audit_run(tmp_path)["errors"]
