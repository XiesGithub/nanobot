import asyncio
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from evals import ci_archive
from evals.common import ROOT, canonical, digest


@pytest.fixture(scope="module")
def v065_ci_evidence(tmp_path_factory):
    from evals.v065_attachment_d import run

    return asyncio.run(run(tmp_path_factory.mktemp("v065-ci-replay") / "actual-D", mode="D"))


@pytest.mark.parametrize("mutation", [None, "missing-exit", "missing-evidence", "tampered-verdict", "tampered-wire"])
def test_new_engineering_contract_replays_actual_v065_d(tmp_path, v065_ci_evidence, mutation):
    import copy

    from evals.v065_attachment_d import score

    expected = {"sha": "a" * 40, "run_id": "123", "run_attempt": "1", "platform": "windows"}
    contract = "docs/agent-evaluation-v065-contract-20261026-r1/contract.json"
    current = {contract: hashlib.sha256((ROOT / contract).read_bytes()).hexdigest()}
    root = tmp_path / "engineering"
    _engineering(root, expected, current)
    result = json.loads((root / "engineering-result.json").read_text())
    if mutation != "missing-exit":
        result["v065_exit_code"] = 0
    _write(root / "engineering-result.json", result)
    evidence = copy.deepcopy(v065_ci_evidence)
    verdict = score(evidence)
    if mutation == "tampered-verdict":
        verdict["checks"]["cleanup_converged"] = False
    if mutation == "tampered-wire":
        evidence["wire"] = []
    if mutation != "missing-evidence":
        _write(root / "v065/evidence.json", evidence)
    _write(root / "v065/verdict.json", verdict)
    replay = ci_archive.audit_engineering_artifact(root, expected, current)
    assert replay["ok"] is (mutation is None), replay["errors"]


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _common(root: Path, expected, *, job_status="success"):
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "runner-start.json", {
        "commit": expected["sha"], "run_id": expected["run_id"],
        "run_attempt": expected["run_attempt"], "requested_platform": expected["platform"],
    })
    _write(root / "job-completion.json", {
        "job_status": job_status, "source_sha": expected["sha"],
        "run_id": expected["run_id"], "run_attempt": expected["run_attempt"],
        "runner_os": ci_archive.PLATFORMS[expected["platform"]],
        "remote_run_url": f"https://github.com/org/repo/actions/runs/{expected['run_id']}",
    })


def _engineering(root: Path, expected, current_files):
    _common(root, expected)
    packages = root / "packages.json"
    packages.write_text(json.dumps([{"name": "nanobot", "version": "1.0"}]), encoding="utf-8")
    junit = root / "junit.xml"
    junit.write_text('<testsuites><testsuite><testcase classname="x" name="y" /></testsuite></testsuites>',
                     encoding="utf-8")
    package_hash = hashlib.sha256(packages.read_bytes()).hexdigest()
    junit_hash = hashlib.sha256(junit.read_bytes()).hexdigest()
    _write(root / "metadata.json", {
        "commit": expected["sha"], "run_id": expected["run_id"],
        "run_attempt": expected["run_attempt"], "conda_environment": "nanobot",
        "python": "3.11", "packages_sha256": package_hash,
    })
    (root / "lint.log").write_text("All checks passed", encoding="utf-8")
    (root / "pytest.log").write_text("1 passed", encoding="utf-8")
    _write(root / "engineering-result.json", {
        "lint_exit_code": 0, "pytest_exit_code": 0, "passed": True,
        "packages_sha256": package_hash, "junit_sha256": junit_hash,
        "junit_tests": 1, "junit_failures": 0,
    })
    checkout = {
        "verified": True, "head": expected["sha"], "files": len(current_files),
        "sha256": digest(canonical(current_files)),
    }
    _write(root / "source-checkout.json", checkout)
    _write(root / "source-checkout-after.json", checkout)


def _execution(root: Path, expected, current_files, *, kind, raw_manifest):
    _common(root, expected)
    wrapper = root / "wrapper"
    raw = wrapper / "run"
    raw.mkdir(parents=True)
    remote_url = f"https://github.com/org/repo/actions/runs/{expected['run_id']}"
    github = {
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_SHA": expected["sha"], "GITHUB_RUN_ID": expected["run_id"],
        "GITHUB_RUN_ATTEMPT": expected["run_attempt"],
    }
    _write(wrapper / "runner.json", {
        "requested_platform": expected["platform"], "remote_run_url": remote_url,
        "github": github,
    })
    source_id = {"files": current_files, "sha256": digest(canonical(current_files))}
    _write(wrapper / "source-identity.json", source_id)
    _write(wrapper / "source-identity-after.json", source_id)
    _write(wrapper / "ci-result.json", {
        "raw_state": "passed_diagnostic", "state": "passed_diagnostic",
        "pipeline_passed": True, "quality_passed": False, "remote_run_url": remote_url,
    })
    _write(wrapper / "process-exit.json", {"command_exit_code": 0, "termination": None})
    _write(wrapper / "invocation.json", {"kind": kind})
    _write(raw / "manifest.json", raw_manifest)
    _write(raw / "snapshot" / "manifest.json", {"files": current_files})
    _write(raw / "run-result.json", {"state": "passed_diagnostic", "quality_passed": False})


def test_engineering_archive_binds_junit_packages_and_external_run(tmp_path):
    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2", "platform": "linux"}
    current_files = {"evals/ci.py": "1" * 64}
    artifact = tmp_path / "engineering-linux-42-2"
    _engineering(artifact, expected, current_files)

    observed = ci_archive.audit_engineering_artifact(artifact, expected, current_files)

    assert observed["ok"] is True
    assert observed["junit_tests"] == 1
    assert observed["quality_passed"] is False

    metadata = json.loads((artifact / "metadata.json").read_text())
    metadata["run_attempt"] = "1"
    _write(artifact / "metadata.json", metadata)
    assert "metadata_run_attempt_mismatch" in ci_archive.audit_engineering_artifact(
        artifact, expected, current_files)["errors"]

    packages_hash = hashlib.sha256((artifact / "packages.json").read_bytes()).hexdigest()
    junit = artifact / "junit.xml"
    junit.write_text("<testsuites><testsuite /></testsuites>", encoding="utf-8")
    junit_hash = hashlib.sha256(junit.read_bytes()).hexdigest()
    result = json.loads((artifact / "engineering-result.json").read_text(encoding="utf-8"))
    result.update(junit_sha256=junit_hash, junit_tests=0, junit_failures=0)
    _write(artifact / "engineering-result.json", result)
    metadata = json.loads((artifact / "metadata.json").read_text(encoding="utf-8"))
    metadata["run_attempt"] = expected["run_attempt"]
    metadata["packages_sha256"] = packages_hash
    _write(artifact / "metadata.json", metadata)
    assert "junit_has_no_testcases" in ci_archive.audit_engineering_artifact(
        artifact, expected, current_files)["errors"]

    junit.write_text('<testsuites><testsuite><testcase name="bad"><failure /></testcase>'
                     '</testsuite></testsuites>', encoding="utf-8")
    result.update(junit_sha256=hashlib.sha256(junit.read_bytes()).hexdigest(),
                  junit_tests=1, junit_failures=1)
    _write(artifact / "engineering-result.json", result)
    assert "engineering_tests_failed" in ci_archive.audit_engineering_artifact(
        artifact, expected, current_files)["errors"]


@pytest.mark.parametrize("fault,expected_error", [
    ("none", None),
    ("wrong_invocation", "daily_invocation_kind_mismatch"),
    ("targeted_plan", "daily_tier_default_plan_mismatch"),
    ("foreign_source", "inner_snapshot_source_mismatch"),
])
def test_daily_archive_requires_original_tier_default_plan(monkeypatch, tmp_path, fault, expected_error):
    from evals.daily import build_plan

    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2", "platform": "linux"}
    current_files = {"evals/ci.py": "1" * 64}
    plan = build_plan("development", "D")
    raw_manifest = {
        "mode": "D", "tier": "development", "coverage": "tier_default",
        "repeat": plan["repeat"], "plan": plan["plan"],
    }
    invocation_kind = "daily"
    if fault == "wrong_invocation":
        invocation_kind = "performance"
    elif fault == "targeted_plan":
        raw_manifest["coverage"] = "targeted"
        raw_manifest["plan"] = plan["plan"][:1]
    artifact = tmp_path / "deterministic-linux-development-42-2"
    _execution(artifact, expected, current_files, kind=invocation_kind,
               raw_manifest=raw_manifest)
    if fault == "foreign_source":
        _write(artifact / "wrapper" / "run" / "snapshot" / "manifest.json",
               {"files": {"evals/ci.py": "2" * 64}})
    monkeypatch.setattr("evals.ci.audit_receipt", lambda _: {
        "ok": True, "raw_replay_ok": True, "quality_passed": False,
        "model_requests": 0, "agent_reruns": 0,
    })

    observed = ci_archive.audit_execution_artifact(
        artifact, expected, kind="deterministic", current_files=current_files, tier="development")

    if expected_error:
        assert observed["ok"] is False
        assert expected_error in observed["errors"]
    else:
        assert observed["ok"] is True


@pytest.mark.parametrize("fault,expected_error", [
    ("none", None),
    ("daily_name", "performance_invocation_kind_mismatch"),
    ("wrong_conditions", "fixed_load_profile_conditions_mismatch"),
    ("foreign_source", "inner_fixed_load_source_mismatch"),
])
def test_fixed_load_archive_requires_v001_five_copy_performance_profile(
        monkeypatch, tmp_path, fault, expected_error):
    from evals.common import load_cases
    from evals.performance import _source_identity, profile_conditions

    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2", "platform": "linux"}
    current_files = {"evals/ci.py": "1" * 64}
    case = next(item for item in load_cases() if item["case_id"] == "V001")
    conditions = profile_conditions(case)
    if fault == "wrong_conditions":
        conditions["copies"] = 1
    invocation_kind = "daily" if fault == "daily_name" else "performance"
    artifact = tmp_path / "fixed-load-linux-42-2"
    _execution(artifact, expected, current_files, kind=invocation_kind, raw_manifest={
        "profile": "linux-fixed-load-v1", "requested_platform": "linux",
        "profile_conditions": conditions,
        "source_identity": {} if fault == "foreign_source" else _source_identity(),
    })
    monkeypatch.setattr("evals.ci.audit_receipt", lambda _: {
        "ok": True, "raw_replay_ok": True, "quality_passed": False,
        "model_requests": 0, "agent_reruns": 0,
    })

    observed = ci_archive.audit_execution_artifact(
        artifact, expected, kind="fixed-load", current_files=current_files)

    if expected_error:
        assert observed["ok"] is False
        assert expected_error in observed["errors"]
    else:
        assert observed["ok"] is True


@pytest.fixture
def checkout_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        yield repo
    finally:
        # Git deliberately marks its own Windows objects read-only. Release only
        # objects made by this fixture; production instance cleanup stays strict.
        if os.name == "nt":
            for path in (repo / ".git/objects").glob("*/*"):
                if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
                    raise RuntimeError("unexpected Git fixture object path")
                if path.is_file():
                    path.chmod(path.stat().st_mode | stat.S_IWRITE)


def test_checkout_source_hashes_are_raw_git_blob_bytes(checkout_repo):
    repo = checkout_repo
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CI test"], cwd=repo, check=True)
    source = repo / "sample.py"
    source.write_bytes(b"one\nline\n")
    subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    files = {"sample.py": hashlib.sha256(source.read_bytes()).hexdigest()}

    assert ci_archive.verify_checkout_blobs(repo, head, files)["verified"] is True
    source.write_bytes(b"one\r\nline\r\n")
    with pytest.raises(ValueError, match="checkout_bytes_differ_from_git_blob"):
        ci_archive.verify_checkout_blobs(repo, head, files)


def test_real_archive_keeps_original_failure_and_unknown_usage(monkeypatch, tmp_path):
    from evals.daily import build_plan

    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2", "platform": "linux"}
    files = {"evals/ci-real-plan.json": hashlib.sha256(
        (ROOT / "evals" / "ci-real-plan.json").read_bytes()).hexdigest()}
    artifact = tmp_path / "real-model-linux-42-2"
    _common(artifact, expected, job_status="failure")
    metadata = artifact / "runner" / "runner-metadata-42.json"
    _write(metadata, {
        "commit": expected["sha"], "run_id": expected["run_id"],
        "run_attempt": expected["run_attempt"], "tier": "development", "mode": "R",
        "budgets": {"max_attempts": 4, "max_tokens": 16000, "max_wall_seconds": 120},
    })
    wrapper = artifact / "linux-development-42-2"
    raw = wrapper / "run"
    frozen_bundle = build_plan("development", "R", selected=["V001"])
    snapshot_id = "sha256:test-r-snapshot"
    (raw / "requests").mkdir(parents=True)
    (raw / "snapshot").mkdir(parents=True)
    _write(wrapper / "runner.json", {
        "requested_platform": "linux",
        "remote_run_url": "https://github.com/org/repo/actions/runs/42",
        "github": {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "org/repo",
                   "GITHUB_SHA": expected["sha"], "GITHUB_RUN_ID": expected["run_id"],
                   "GITHUB_RUN_ATTEMPT": expected["run_attempt"], "GITHUB_REF": "refs/heads/main"},
    })
    source_id = {"files": files, "sha256": digest(canonical(files))}
    _write(wrapper / "source-identity.json", source_id)
    _write(wrapper / "source-identity-after.json", source_id)
    frozen_plan_hash = files["evals/ci-real-plan.json"]
    _write(raw / "snapshot" / "manifest.json", {
        "files": {"evals/ci-real-plan.json": frozen_plan_hash}, "snapshot_id": snapshot_id,
    })
    budget = {"max_attempts": 4, "max_tokens": 16000, "max_wall_seconds": 120, "prices": None}
    _write(raw / "manifest.json", {
        "mode": "R", "tier": "development", "coverage": "targeted", "budget": budget,
        "snapshot_id": snapshot_id, "active": frozen_bundle["active"],
        "plan": frozen_bundle["plan"],
    })
    _write(raw / "requests" / "V001-1.json", {
        "kind": "phase8_agent", "mode": "R", "plan": frozen_bundle["plan"][0],
        "case": frozen_bundle["active"]["V001"]["contract"], "snapshot_id": snapshot_id,
        "budget": {"max_attempts": 4, "max_tokens": 16000},
    })
    (raw / "records.jsonl").write_text(json.dumps({
        **frozen_bundle["plan"][0], "request_budget": {"attempts_dispatched": 1},
    }) + "\n", encoding="utf-8")
    original = {
        "state": "insufficient_evidence", "budget": budget,
        "planned_trials": 1, "recorded_trials": 1,
        "actual_model_attempts": 1, "known_tokens": None, "unknown_usage_attempts": 1,
    }
    _write(raw / "run-result.json", original)
    _write(wrapper / "ci-result.json", {
        "raw_state": "insufficient_evidence", "state": "invalid",
        "pipeline_passed": False, "quality_passed": False,
        "remote_run_url": "https://github.com/org/repo/actions/runs/42",
    })
    _write(wrapper / "process-exit.json", {"command_exit_code": 1, "termination": None})
    monkeypatch.setattr("evals.ci.audit_receipt", lambda _: {
        "ok": True, "raw_replay_ok": True, "quality_passed": False,
        "model_requests": 0, "agent_reruns": 0,
    })

    observed = ci_archive.audit_execution_artifact(
        artifact, expected, kind="real-model", current_files=files, github_ref="refs/heads/main")

    assert observed["ok"] is True
    assert observed["original_state"] == "insufficient_evidence"
    assert observed["original_ci_state"] == "invalid"
    assert observed["quality_passed"] is False
    assert observed["usage"] == {
        "actual_model_attempts": 1, "known_tokens": None, "unknown_usage_attempts": 1,
    }

    wrong_ref = ci_archive.audit_execution_artifact(
        artifact, expected, kind="real-model", current_files=files, github_ref="refs/heads/nightly")
    assert "r_runner_ref_mismatch" in wrong_ref["errors"]

    request_path = raw / "requests" / "V001-1.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["plan"]["case_id"] = "V002"
    _write(request_path, request)
    changed_plan = ci_archive.audit_execution_artifact(
        artifact, expected, kind="real-model", current_files=files)
    assert "r_request_plan_mismatch" in changed_plan["errors"]

    request["plan"] = frozen_bundle["plan"][0]
    _write(request_path, request)
    original["actual_model_attempts"] = 0
    _write(raw / "run-result.json", original)
    zero_calls = ci_archive.audit_execution_artifact(
        artifact, expected, kind="real-model", current_files=files)
    assert "r_attempt_ledger_invalid" in zero_calls["errors"]


@pytest.mark.parametrize("fault,expected_error", [
    ("missing", "missing_r_preflight_artifact"),
    ("invalid", "preflight_valid_mismatch"),
    ("protection", "preflight_protection_prevent_self_review"),
    ("ref", "preflight_github_ref_mismatch"),
    ("plan", "preflight_plan_mismatch"),
])
def test_real_preflight_requires_exact_same_run_protected_plan(tmp_path, fault, expected_error):
    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2"}
    root = tmp_path / "downloaded"
    plan = json.loads((ROOT / "evals" / "ci-real-plan.json").read_text(encoding="utf-8"))
    request = {
        "state": "request_validated", "valid": True, "errors": [],
        "protection": {"verified": True, "reviewers_configured": True,
                       "trusted_branches_only": True, "prevent_self_review": True},
        "plan": plan, "github_ref": "refs/heads/main", "source_sha": "abc123",
        "remote_run_url": "https://github.com/org/repo/actions/runs/42",
        "credentials_accessed": False, "quality_passed": False,
    }
    if fault != "missing":
        request_path = root / "real-model-preflight-42-2" / "request.json"
        if fault == "invalid":
            request["valid"] = False
            request["errors"] = ["bad_request"]
        elif fault == "protection":
            request["protection"]["prevent_self_review"] = False
        elif fault == "ref":
            request["github_ref"] = "refs/heads/codex/ci-evidence-20261019"
        elif fault == "plan":
            request["plan"]["per_platform_budget"]["max_tokens"] = 32000
        _write(request_path, request)

    observed = ci_archive.audit_real_preflight(
        root, expected, github_ref="refs/heads/main",
        remote_run_url="https://github.com/org/repo/actions/runs/42")

    assert observed["ok"] is False
    assert expected_error in observed["errors"]


def test_real_preflight_accepts_exact_protected_same_run_plan(tmp_path):
    expected = {"sha": "abc123", "run_id": "42", "run_attempt": "2"}
    root = tmp_path / "downloaded"
    plan = json.loads((ROOT / "evals" / "ci-real-plan.json").read_text(encoding="utf-8"))
    remote_url = "https://github.com/org/repo/actions/runs/42"
    _write(root / "real-model-preflight-42-2" / "request.json", {
        "state": "request_validated", "valid": True, "errors": [],
        "protection": {"verified": True, "reviewers_configured": True,
                       "trusted_branches_only": True, "prevent_self_review": True},
        "plan": plan, "github_ref": "refs/heads/main", "source_sha": "abc123",
        "remote_run_url": remote_url, "credentials_accessed": False,
        "quality_passed": False,
    })

    observed = ci_archive.audit_real_preflight(
        root, expected, github_ref="refs/heads/main", remote_run_url=remote_url)

    assert observed["ok"] is True


def test_workflow_review_branch_runs_only_uncredentialed_profiles():
    import yaml

    workflow = yaml.load((ROOT / ".github" / "workflows" / "evaluation.yml").read_text(
        encoding="utf-8"), Loader=yaml.BaseLoader)
    review_branches = [branch for branch in workflow["on"]["push"]["branches"]
                       if branch.startswith("codex/ci-evidence-")]
    assert review_branches == ["codex/ci-evidence-20261026-r1"]
    real = workflow["jobs"]["real-model-evaluation"]
    assert "refs/heads/main" in real["if"] and "refs/heads/nightly" in real["if"]
    assert all(branch not in real["if"] for branch in review_branches)
    assert workflow["env"]["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert workflow["env"]["GIT_CONFIG_VALUE_0"] == "false"


def test_downloaded_real_model_binds_external_ref_to_each_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_archive, "audit_real_preflight", lambda *a, **kw: {"ok": True})
    observed_refs = []

    def audit(root, expected, *, kind, current_files, github_ref=None):
        observed_refs.append(github_ref)
        return {"ok": github_ref == "refs/heads/main"}

    monkeypatch.setattr(ci_archive, "audit_execution_artifact", audit)
    report = ci_archive.audit_downloaded(
        "real-model", tmp_path, run_id="42", run_attempt="2", sha="abc123",
        current_files={}, github_ref="refs/heads/main",
        server_url="https://github.com", repository="org/repo")
    assert report["ok"] is True
    assert observed_refs == ["refs/heads/main", "refs/heads/main"]

