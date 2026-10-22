import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _workflow():
    path = ROOT / ".github" / "workflows" / "evaluation.yml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_workflow_has_separate_engineering_and_evaluation_triggers():
    workflow = _workflow()
    assert set(workflow["on"]) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["tier"]["options"] == ["development", "smoke", "nightly", "release"]
    assert dispatch["mode"]["options"] == ["D", "R"]
    assert workflow["jobs"]["engineering"]["name"].startswith("Engineering tests")
    step = _step(workflow["jobs"]["engineering"], "Lint and run the engineering suite")
    assert "--ignore=tests/evals/test_holdout.py" in step["run"]


def test_engineering_browser_controls_have_a_pinned_cross_platform_runtime():
    from evals.browser_task_driver import PLAYWRIGHT_VERSION

    job = _workflow()["jobs"]["engineering"]
    setup = _step(job, "Install the pinned browser for engineering clock controls")
    suite = _step(job, "Lint and run the engineering suite")
    assert job["steps"].index(setup) < job["steps"].index(suite)
    assert "if" not in setup  # Both hosted operating systems need the runtime.
    script = setup["run"]
    assert f'"playwright=={PLAYWRIGHT_VERSION}"' in script
    assert "--name nanobot python -m playwright install --with-deps chromium" in script
    assert "p.chromium.executable_path" in script and "p.stop()" in script
    assert "NANOBOT_EVAL_CHROME=$browser" in script and "$env:GITHUB_ENV" in script
    assert "Test-Path -LiteralPath $browser -PathType Leaf" in script
    assert "executable_sha256" in script and "reports/engineering/browser-runtime.json" in script
    assert script.count("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }") == 3
    assert "test_browser_task_timing.py" not in suite["run"]  # No new ignore/skip.


def test_holdout_contract_only_runs_in_manual_release_job():
    jobs = _workflow()["jobs"]
    release_job = jobs["release-gold-contract"]
    assert "workflow_dispatch" in release_job["if"]
    assert "inputs.tier == 'release'" in release_job["if"]
    test_step = next(step for step in release_job["steps"]
                     if step.get("name") == "Verify the fixed release holdout gold contract")
    assert "tests/evals/test_holdout.py" in test_step["run"]
    assert "reports/release-gold-contract/junit.xml" in test_step["run"]
    assert _step(jobs["engineering"], "Lint and run the engineering suite")["run"].count("test_holdout.py") == 1


def test_deterministic_tiers_and_regression_states_cannot_pass_the_wrapper_gate():
    job = _workflow()["jobs"]["deterministic-evaluation"]
    matrix = job["strategy"]["matrix"]["tier"]
    assert '"development","smoke"' in matrix
    assert '"nightly"' in matrix
    assert "inputs.tier" in matrix
    step = next(step for step in job["steps"] if step.get("name", "").startswith(
        "Run deterministic evaluation"))
    assert '"-m", "evals.ci"' in step["run"]
    assert '"--kind", "daily"' in step["run"]
    assert '"--timeout", "3900"' in step["run"]
    assert '"--mode", "D"' in step["run"]
    assert "--output" in step["run"]  # The tested supervisor owns fail-closed states and replay.
    assert "if ($runExit -ne 0) { exit $runExit }" in step["run"]


def test_real_model_secret_is_dispatch_only_budgeted_and_not_archived():
    workflow = _workflow()
    real_job = workflow["jobs"]["real-model-evaluation"]
    assert "workflow_dispatch" in real_job["if"]
    assert "github.ref == 'refs/heads/main'" in real_job["if"]
    assert "github.ref == 'refs/heads/nightly'" in real_job["if"]
    assert "needs.real-model-preflight.outputs.valid" in real_job["if"]
    assert real_job["environment"]["name"] == "real-model-evaluation"
    workflow_source = (ROOT / ".github" / "workflows" / "evaluation.yml").read_text(encoding="utf-8")
    assert "repository settings must configure required reviewers" in workflow_source
    preflight_job = workflow["jobs"]["real-model-preflight"]
    assert preflight_job["if"] == "${{ github.event_name == 'workflow_dispatch' && inputs.mode == 'R' }}"
    secret_references = []
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            for value in step.get("env", {}).values():
                if "secrets.NANOBOT_EVAL_API_KEY" in value:
                    secret_references.append((job_id, step.get("name")))
    assert secret_references == [("real-model-evaluation", "Run budgeted real-model evaluation")]
    run_step = next(step for step in real_job["steps"] if step.get("name") ==
                    "Run budgeted real-model evaluation")
    for option in ("--max-attempts", "--max-tokens", "--max-wall-seconds", "--opt-in-real-model", "--cases", "V001", "--kind", "daily", "--platform"):
        assert option in run_step["run"]
    assert "Tee-Object" not in run_step["run"]
    python_runner = run_step["run"]
    embedded_python = re.search(r"(?m)^@'\n(.*?)^'@ \| Set-Content", python_runner, re.DOTALL)
    assert embedded_python
    ast.parse(embedded_python.group(1))
    for contract in ("NANOBOT_EVAL_API_KEY", "os.O_EXCL", "0o600", "finally:", "credential_path.unlink"):
        assert contract in python_runner
    assert "$env:NANOBOT_EVAL_API_KEY" not in python_runner
    preflight = _step(preflight_job, "Validate request and remote environment protection without model credentials")
    script = preflight["with"]["script"]
    for contract in ("repos.getEnvironment", "listDeploymentBranchPolicies", "required_reviewers",
                     "prevent_self_review", "custom_branch_policies", "policies.length === 2",
                     "protected_environment_lookup_failed", "credentials_accessed: false",
                     "core.setFailed", "frozen_budget", "frozen_development_tier_required"):
        assert contract in script
    assert preflight["uses"] == "actions/github-script@v8"
    assert "['refs/heads/main', 'refs/heads/nightly']" in script
    assert not any("secrets." in str(value) for value in preflight.get("env", {}).values())
    assert set(real_job["needs"]) == {"engineering", "deterministic-evaluation", "fixed-load-profile", "real-model-preflight", "diagnostic-archive-replay"}
    assert '"evals.ci", *sys.argv[1:]' in python_runner
    upload = next(step for step in real_job["steps"] if step.get("uses", "").startswith(
        "actions/upload-artifact"))
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "reports/real-model/"


def test_fixed_load_is_separate_cross_platform_and_never_quality_eligible():
    job = _workflow()["jobs"]["fixed-load-profile"]
    platforms = {item["platform"]: item["runner"] for item in job["strategy"]["matrix"]["include"]}
    assert platforms == {"linux": "ubuntu-latest", "windows": "windows-latest"}
    run_step = next(step for step in job["steps"] if step.get("name") == "Run fixed deterministic workload")
    assert '"-m", "evals.ci"' in run_step["run"]
    assert '"--kind", "performance"' in run_step["run"]
    assert '"--timeout", "1800"' in run_step["run"]
    assert "--platform" in run_step["run"]
    assert "--output" in run_step["run"]
    assert 'Tee-Object -FilePath $logPath' in run_step["run"]
    assert 'Tee-Object -FilePath "$output/' not in run_step["run"]


def test_artifacts_are_uploaded_even_after_failure_and_pipeline_status_is_explicit():
    jobs = _workflow()["jobs"]
    for job_id in ("engineering", "deterministic-evaluation", "real-model-evaluation", "fixed-load-profile"):
        uploads = [step for step in jobs[job_id]["steps"]
                   if step.get("uses", "").startswith("actions/upload-artifact")]
        assert uploads
        assert all(step.get("if") == "always()" for step in uploads)
    pipeline = jobs["evidence-pipeline-status"]
    assert pipeline["if"] == "always()"
    assert "do not grant quality approval" in pipeline["name"]
    assert "real_model_request_invalid_no_quality_pass" in pipeline["steps"][0]["run"]


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_all_profiles_are_cross_platform_and_use_conda():
    workflow = _workflow()
    jobs = workflow["jobs"]
    for name in ("engineering", "deterministic-evaluation", "fixed-load-profile", "real-model-evaluation"):
        job = jobs[name]
        assert job["runs-on"] == "${{ matrix.runner }}"
        assert {item["platform"]: item["runner"] for item in job["strategy"]["matrix"]["include"]} == {
            "linux": "ubuntu-latest", "windows": "windows-latest"}
        assert any(step.get("uses", "").startswith("conda-incubator/setup-miniconda") for step in job["steps"])
        assert "abrupt_runner_loss_closed = $false" in _step(job, "Record job completion (best effort; abrupt runner loss remains unclosed)")["run"]
        upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
        assert upload["with"]["if-no-files-found"] == "error"
    matrix = jobs["deterministic-evaluation"]["strategy"]["matrix"]
    assert matrix["platform"] == ["linux", "windows"]
    assert "inputs.mode == 'R'" in matrix["tier"]
    assert "if" not in jobs["deterministic-evaluation"]
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "3.14" in ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert "windows-latest" in ci["jobs"]["test"]["strategy"]["matrix"]["os"]
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "uv run" not in source and "uv sync" not in source
    assert "--name nanobot" in source


def test_real_plan_is_frozen_small_and_never_quality_eligible():
    plan = json.loads((ROOT / "evals/ci-real-plan.json").read_text(encoding="utf-8"))
    assert plan["cases"] == ["V001"]
    assert plan["tier"] == "development" and plan["mode"] == "R"
    assert plan["model"] == "deepseek-v4-flash"
    assert plan["repeats_per_platform"] == 1
    assert set(plan["platforms"]) == {"windows", "linux"}
    assert plan["per_platform_budget"] == {"max_attempts": 4, "max_tokens": 16000, "max_wall_seconds": 120}
    assert plan["total_budget"] == {k: v * 2 for k, v in plan["per_platform_budget"].items()}
    assert plan["quality_passed"] is False
    assert plan["dangerous_scenarios_authorized"] is False


@pytest.mark.parametrize("fault", [None, "lookup", "self_review", "reviewers", "wildcard", "extra_tag", "budget", "ref", "opt_in", "tier"])
def test_remote_protection_preflight_executes_fail_closed_without_credentials(fault):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the GitHub-script control")
    script = _step(_workflow()["jobs"]["real-model-preflight"],
                   "Validate request and remote environment protection without model credentials")["with"]["script"]
    plan = json.loads((ROOT / "evals/ci-real-plan.json").read_text(encoding="utf-8"))
    harness = r"""
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const config = JSON.parse(process.argv[1]);
const fault = config.fault;
Object.assign(process.env, {OPT_IN: 'true', TIER: 'development', MAX_ATTEMPTS: '4', MAX_TOKENS: '16000', MAX_WALL_SECONDS: '120'});
if (fault === 'budget') process.env.MAX_TOKENS = '16001';
if (fault === 'opt_in') process.env.OPT_IN = 'false';
if (fault === 'tier') process.env.TIER = 'release';
const context = {ref: fault === 'ref' ? 'refs/heads/untrusted' : 'refs/heads/main', repo: {owner: 'synthetic', repo: 'fixture'}, sha: 'synthetic-sha', serverUrl: 'https://github.com', runId: 1};
const env = {protection_rules: [{type: 'required_reviewers', prevent_self_review: fault !== 'self_review', reviewers: fault === 'reviewers' ? [] : [{type: 'User'}]}], deployment_branch_policy: {custom_branch_policies: true, protected_branches: false}};
const policies = [{name: fault === 'wildcard' ? '*' : 'main', type: 'branch'}, {name: 'nightly', type: 'branch'}];
if (fault === 'extra_tag') policies.push({name: 'release*', type: 'tag'});
const github = {rest: {repos: {getEnvironment: async () => {if (fault === 'lookup') throw {status: 404}; return {data: env};}, listDeploymentBranchPolicies: () => {}}}, paginate: async () => policies};
let receipt, valid, failed = false;
const fs = {mkdirSync() {}, readFileSync: () => JSON.stringify(config.plan), writeFileSync: (path, data) => {receipt = JSON.parse(data);}};
const core = {setOutput: (key, value) => {valid = value;}, setFailed: () => {failed = true;}};
new AsyncFunction('require', 'github', 'context', 'core', config.script)(() => fs, github, context, core).then(() => console.log(JSON.stringify({receipt, valid, failed}))).catch(e => {console.error(e); process.exit(1);});
"""
    completed = subprocess.run([node, "-e", harness, json.dumps({"fault": fault, "plan": plan, "script": script})],
                               text=True, capture_output=True, check=True, timeout=10)
    observed = json.loads(completed.stdout)
    assert observed["valid"] == ("true" if fault is None else "false")
    assert observed["failed"] is (fault is not None)
    assert observed["receipt"]["quality_passed"] is False
    assert observed["receipt"]["credentials_accessed"] is False
    assert bool(observed["receipt"]["errors"]) is (fault is not None)


def test_downloaded_archive_replay_is_required_bounded_and_integrity_only():
    jobs = _workflow()["jobs"]
    for name, patterns in (("diagnostic-archive-replay", {"engineering-*", "deterministic-*", "fixed-load-*"}),
                           ("real-archive-replay", {"real-model-*"})):
        job = jobs[name]
        assert "always()" in job["if"]
        assert job["timeout-minutes"] == "30"
        downloads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/download-artifact")]
        assert {step["with"]["pattern"] for step in downloads} == patterns
        assert all(step["with"]["merge-multiple"] == "false" and step["if"] == "always()" for step in downloads)
        run = _step(job, "Replay downloaded receipts and reject missing evidence")["run"]
        for requirement in ("-m', 'evals.ci_archive", "--run-id", "--run-attempt",
                            "--sha", "reports/replay/", "$auditExit -ne 0",
                            "quality_passed = $false", "agent_reruns = 0", "exit 1"):
            assert requirement in run
        assert "ci_archive.py" not in run
        upload = _step(job, "Upload downloaded archive replay results")
        assert upload["if"] == "always()"
        assert upload["with"]["if-no-files-found"] == "error"
    diagnostic = jobs["diagnostic-archive-replay"]
    assert set(diagnostic["needs"]) == {"engineering", "deterministic-evaluation", "fixed-load-profile"}
    run = _step(diagnostic, "Replay downloaded receipts and reject missing evidence")["run"]
    assert "@('development', 'smoke')" in run
    assert "@('nightly')" in run
    assert "$env:MODE -eq 'R'" in run
    assert jobs["real-archive-replay"]["needs"] == ["real-model-evaluation"]
    assert "inputs.mode == 'R'" in jobs["real-archive-replay"]["if"]
    real_run = _step(jobs["real-archive-replay"], "Replay downloaded receipts and reject missing evidence")["run"]
    assert "--kind', 'real-model'" in real_run and "--sha" in real_run
    summary = jobs["evidence-pipeline-status"]
    assert {"diagnostic-archive-replay", "real-archive-replay"} <= set(summary["needs"])
    script = summary["steps"][0]["run"]
    assert "diagnostic_archive_replay=" in script and "real_archive_replay=" in script
