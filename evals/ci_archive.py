"""Bind downloaded CI evidence to the requested GitHub run and source bytes.

This layer validates provenance and replays existing receipts. It never turns a
failed product run into a successful run and never makes a quality claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from evals.common import ROOT, canonical, digest, read

PLATFORMS = {"linux": "Linux", "windows": "Windows"}


def _json(path: Path) -> dict[str, Any]:
    value = read(path)
    if not isinstance(value, dict):
        raise ValueError("expected_json_object:" + path.name)
    return value


def current_source_map(root: Path = ROOT) -> dict[str, str]:
    """Return the exact byte hashes used by evaluation-ci source identity."""
    from evals.identity import _selected_paths

    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in _selected_paths(root)}


def verify_checkout_blobs(root: Path, expected_sha: str,
                          files: dict[str, str]) -> dict[str, Any]:
    """Reject checkout EOL/filter drift by comparing worktree bytes with HEAD blobs."""
    root = root.resolve()
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                         capture_output=True, text=True).stdout.strip()
    if rev.lower() != expected_sha.lower():
        raise ValueError("checkout_head_mismatch")
    tree = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", expected_sha],
                          cwd=root, check=True, capture_output=True).stdout
    blobs: dict[str, str] = {}
    for item in tree.split(b"\0"):
        if not item:
            continue
        header, raw_path = item.split(b"\t", 1)
        mode, kind, object_id = header.decode("ascii").split()
        if kind == "blob" and mode != "160000":
            blobs[raw_path.decode("utf-8")] = object_id
    missing = sorted(set(files) - set(blobs))
    if missing:
        raise ValueError("selected_source_not_in_git_tree:" + missing[0])
    names = sorted(files)
    hashed = subprocess.run(["git", "hash-object", "--no-filters", "--stdin-paths"], cwd=root,
                            input=("\n".join(names) + "\n").encode("utf-8"), check=True,
                            capture_output=True).stdout.decode("ascii").splitlines()
    if len(hashed) != len(names):
        raise ValueError("git_worktree_hash_count")
    changed = [name for name, object_id in zip(names, hashed, strict=True)
               if blobs[name] != object_id]
    if changed:
        raise ValueError("checkout_bytes_differ_from_git_blob:" + changed[0])
    return {"verified": True, "head": rev, "files": len(files),
            "sha256": digest(canonical(files))}


def _identity_errors(document: dict[str, Any], expected: dict[str, str],
                     *, github_fields: bool = False) -> list[str]:
    errors: list[str] = []
    if github_fields:
        github = document.get("github")
        if not isinstance(github, dict):
            return ["missing_github_identity"]
        observed = {"sha": github.get("GITHUB_SHA"), "run_id": github.get("GITHUB_RUN_ID"),
                    "run_attempt": github.get("GITHUB_RUN_ATTEMPT")}
    else:
        observed = {"sha": document.get("commit", document.get("source_sha")),
                    "run_id": document.get("run_id"),
                    "run_attempt": document.get("run_attempt")}
    for field in ("sha", "run_id", "run_attempt"):
        if str(observed.get(field, "")) != str(expected[field]):
            errors.append("external_" + field + "_mismatch")
    platform_value = (document.get("requested_platform") if github_fields
                      else document.get("requested_platform", document.get("matrix")))
    if platform_value is None and not github_fields:
        platform_value = document.get("runner_os")
    if platform_value not in (expected["platform"], PLATFORMS[expected["platform"]]):
        errors.append("external_platform_mismatch")
    return errors


def _check_common_files(root: Path, expected: dict[str, str], *,
                        allow_failed: bool = False) -> list[str]:
    required = ("runner-start.json", "job-completion.json")
    errors = ["missing:" + name for name in required if not (root / name).is_file()]
    if errors:
        return errors
    for name in required:
        errors.extend(name + ":" + error for error in
                      _identity_errors(_json(root / name), expected))
    completion = _json(root / "job-completion.json")
    allowed_statuses = {"success", "failure"} if allow_failed else {"success"}
    if completion.get("job_status") not in allowed_statuses:
        errors.append("job_not_success")
    expected_url_tail = "/actions/runs/" + str(expected["run_id"])
    for document in (_json(root / name) for name in required):
        remote_url = document.get("remote_run_url")
        if remote_url is not None and not str(remote_url).endswith(expected_url_tail):
            errors.append("remote_run_url_mismatch")
    return errors


def audit_engineering_artifact(root: str | Path, expected: dict[str, str],
                              current_files: dict[str, str]) -> dict[str, Any]:
    root = Path(root)
    errors = _check_common_files(root, expected)
    required = ("metadata.json", "packages.json", "lint.log", "pytest.log",
                "engineering-result.json", "junit.xml", "source-checkout.json",
                "source-checkout-after.json")
    errors.extend("missing:" + name for name in required if not (root / name).is_file())
    if errors:
        return {"ok": False, "errors": errors, "quality_passed": False}
    metadata = _json(root / "metadata.json")
    result = _json(root / "engineering-result.json")
    source_checkout = _json(root / "source-checkout.json")
    source_after = _json(root / "source-checkout-after.json")
    if (source_checkout.get("verified") is not True or
            source_checkout.get("head") != expected["sha"] or
            source_checkout.get("sha256") != digest(canonical(current_files)) or
            source_checkout.get("files") != len(current_files) or
            source_after != source_checkout):
        errors.append("engineering_source_checkout_identity")
    packages = json.loads((root / "packages.json").read_text(encoding="utf-8-sig"))
    if not isinstance(packages, list) or not packages:
        errors.append("package_manifest_invalid")
    for field in ("commit", "run_id", "run_attempt"):
        if str(metadata.get(field, "")) != str(expected["sha" if field == "commit" else field]):
            errors.append("metadata_" + field + "_mismatch")
    if metadata.get("conda_environment") != "nanobot" or metadata.get("python") != "3.11":
        errors.append("engineering_environment_identity")
    junit_bytes = (root / "junit.xml").read_bytes()
    try:
        junit = ET.fromstring(junit_bytes)
        cases = list(junit.iter("testcase"))
        failed = sum(len(list(case.iter("failure"))) + len(list(case.iter("error")))
                     for case in cases)
    except ET.ParseError:
        cases, failed = [], -1
        errors.append("junit_invalid")
    package_hash = hashlib.sha256((root / "packages.json").read_bytes()).hexdigest()
    junit_hash = hashlib.sha256(junit_bytes).hexdigest()
    if metadata.get("packages_sha256") != package_hash:
        errors.append("package_manifest_identity")
    if result.get("packages_sha256") != package_hash or result.get("junit_sha256") != junit_hash:
        errors.append("engineering_result_artifact_identity")
    if result.get("junit_tests") != len(cases) or result.get("junit_failures") != failed:
        errors.append("junit_counts_mismatch")
    if not cases:
        errors.append("junit_has_no_testcases")
    if (failed != 0 or result.get("passed") is not True or
            result.get("lint_exit_code") != 0 or result.get("pytest_exit_code") != 0):
        errors.append("engineering_tests_failed")
    contract_path = "docs/agent-evaluation-v065-contract-20261026-r1/contract.json"
    if contract_path in current_files:
        if type(result.get("v065_exit_code")) is not int or result["v065_exit_code"] != 0:
            errors.append("v065_independent_D_execution_failed_or_missing")
        evidence_path = root / "v065/evidence.json"
        verdict_path = root / "v065/verdict.json"
        if not evidence_path.is_file() or not verdict_path.is_file():
            errors.append("v065_independent_D_artifacts_missing")
        else:
            from evals.v065_attachment_d import score

            replayed = score(_json(evidence_path))
            if replayed != _json(verdict_path):
                errors.append("v065_independent_D_verdict_replay_mismatch")
            if replayed.get("status") != "pass":
                errors.append("v065_independent_D_not_passed")
    return {"ok": not errors, "errors": errors, "quality_passed": False,
            "junit_tests": len(cases), "junit_failures": failed,
            "package_manifest_sha256": package_hash}


def _check_r_frozen_plan(wrapper: Path, artifact_root: Path, expected: dict[str, str],
                         result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    frozen = read(ROOT / "evals" / "ci-real-plan.json")
    if (frozen.get("mode") != "R" or frozen.get("tier") != "development"
            or frozen.get("model") != "deepseek-v4-flash"
            or frozen.get("cases") != ["V001"] or frozen.get("repeats_per_platform") != 1
            or frozen.get("platforms") != ["windows", "linux"]
            or frozen.get("quality_passed") is not False or frozen.get("prices") is not None):
        errors.append("frozen_r_plan_invalid")
        return errors
    budget = frozen["per_platform_budget"]
    run_id = expected["run_id"]
    metadata_path = artifact_root / "runner" / f"runner-metadata-{run_id}.json"
    if not metadata_path.is_file():
        return ["missing:frozen_r_runner_metadata"]
    metadata = _json(metadata_path)
    observed_budget = metadata.get("budgets")
    if observed_budget != budget:
        errors.append("protected_r_budget_mismatch")
    for key, value in (("commit", expected["sha"]), ("run_id", expected["run_id"]),
                       ("run_attempt", expected["run_attempt"]), ("tier", "development"),
                       ("mode", "R")):
        if str(metadata.get(key, "")) != str(value):
            errors.append("frozen_r_metadata_" + key)
    raw = wrapper / "run"
    manifest = _json(raw / "manifest.json")
    inner_budget = manifest.get("budget", {})
    if manifest.get("mode") != "R" or manifest.get("tier") != "development":
        errors.append("r_manifest_mode_or_tier")
    if any(inner_budget.get(key) != budget.get(key) for key in
           ("max_attempts", "max_tokens", "max_wall_seconds")) or inner_budget.get("prices") is not None:
        errors.append("r_manifest_budget_mismatch")
    result_budget = result.get("budget", {})
    if (any(result_budget.get(key) != budget.get(key) for key in
            ("max_attempts", "max_tokens", "max_wall_seconds")) or
            result_budget.get("prices") is not None):
        errors.append("r_result_budget_mismatch")
    frozen_snapshot = _json(raw / "snapshot" / "manifest.json")
    frozen_plan_hash = hashlib.sha256((ROOT / "evals" / "ci-real-plan.json").read_bytes()).hexdigest()
    if frozen_snapshot.get("files", {}).get("evals/ci-real-plan.json") != frozen_plan_hash:
        errors.append("r_snapshot_frozen_plan_mismatch")
    if frozen_snapshot.get("snapshot_id") != manifest.get("snapshot_id"):
        errors.append("r_snapshot_id_mismatch")
    from evals.daily import build_plan

    frozen_bundle = build_plan(frozen["tier"], frozen["mode"], selected=frozen["cases"])
    plan = manifest.get("plan", [])
    if (manifest.get("coverage") != "targeted" or plan != frozen_bundle["plan"] or
            manifest.get("active") != frozen_bundle["active"]):
        errors.append("r_trial_plan_mismatch")
    if (result.get("planned_trials") != len(plan) or
            result.get("recorded_trials") != frozen["repeats_per_platform"]):
        errors.append("r_trial_ledger_incomplete")
    try:
        records = [json.loads(line) for line in (raw / "records.jsonl").read_text(
            encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        records = []
        errors.append("r_records_missing_or_invalid")
    if len(records) != len(plan) or [row.get("trial_id") for row in records] != [
            item.get("trial_id") for item in plan]:
        errors.append("r_records_do_not_match_plan")
    if len(records) == len(plan):
        plan_fields = ("case_id", "case_version", "repeat_index", "profile", "purpose",
                       "original_trial_id", "fault", "entry", "task_type", "fault_condition",
                       "mode", "scope", "kind", "product_timeout_s")
        for row, item in zip(records, plan, strict=True):
            if any(row.get(field) != item.get(field) for field in plan_fields):
                errors.append("r_record_plan_identity")
                break
    request_paths = sorted((raw / "requests").glob("*.json"))
    expected_request_names = {str(item.get("trial_id")) + ".json" for item in plan}
    if {path.name for path in request_paths} != expected_request_names:
        errors.append("r_request_inventory_mismatch")
    plan_by_trial = {str(item.get("trial_id")): item for item in plan}
    active = manifest.get("active", {})
    for request_path in request_paths:
        request = _json(request_path)
        plan_item = plan_by_trial.get(request_path.stem)
        contract = active.get(plan_item.get("case_id"), {}).get("contract") if plan_item else None
        if (plan_item is None or request.get("mode") != "R" or
                request.get("kind") != "phase8_" + str(plan_item.get("kind")) or
                request.get("plan") != plan_item or request.get("case") != contract or
                request.get("snapshot_id") != manifest.get("snapshot_id")):
            errors.append("r_request_plan_mismatch")
        request_budget = request.get("budget", {})
        if (type(request_budget.get("max_attempts")) is not int or
                request_budget["max_attempts"] != budget["max_attempts"] or
                type(request_budget.get("max_tokens")) is not int or
                request_budget["max_tokens"] != budget["max_tokens"]):
            errors.append("r_request_budget_overrun")
    actual_attempts = result.get("actual_model_attempts")
    if type(actual_attempts) is not int or actual_attempts < 1 or actual_attempts > budget["max_attempts"]:
        errors.append("r_attempt_ledger_invalid")
    record_attempts = [row.get("request_budget", {}).get("attempts_dispatched")
                       for row in records]
    if any(type(value) is not int or value < 1 for value in record_attempts):
        errors.append("r_record_attempt_ledger_invalid")
    elif actual_attempts != sum(record_attempts):
        errors.append("r_attempt_count_mismatch")
    known_tokens = result.get("known_tokens")
    if known_tokens is not None and (type(known_tokens) is not int or known_tokens > budget["max_tokens"]):
        errors.append("r_token_ledger_invalid")
    # Unknown values remain null; never replace them with a zero in the replay report.
    for field in ("actual_model_attempts", "known_tokens", "unknown_usage_attempts"):
        if field not in result:
            errors.append("r_usage_ledger_missing:" + field)
        value = result.get(field)
        if value is not None and (type(value) is not int or value < 0):
            errors.append("r_usage_ledger_invalid:" + field)
    if result.get("budget", {}).get("prices") is not None:
        errors.append("r_price_claim_forbidden")
    return errors


def audit_real_preflight(download_root: str | Path, expected: dict[str, str], *,
                         github_ref: str, remote_run_url: str) -> dict[str, Any]:
    """Require the same-run protected preflight receipt before accepting R archives."""
    name = f"real-model-preflight-{expected['run_id']}-{expected['run_attempt']}"
    path = Path(download_root) / name / "request.json"
    if not path.is_file():
        return {"ok": False, "errors": ["missing_r_preflight_artifact"],
                "quality_passed": False}
    request = _json(path)
    plan = _json(ROOT / "evals" / "ci-real-plan.json")
    errors: list[str] = []
    for field, value in (("valid", True), ("state", "request_validated"),
                         ("errors", []), ("source_sha", expected["sha"]),
                         ("github_ref", github_ref), ("remote_run_url", remote_run_url),
                         ("credentials_accessed", False), ("quality_passed", False),
                         ("plan", plan)):
        if request.get(field) != value:
            errors.append("preflight_" + field + "_mismatch")
    if github_ref not in {"refs/heads/main", "refs/heads/nightly"}:
        errors.append("preflight_untrusted_ref")
    protection = request.get("protection", {})
    for field in ("verified", "reviewers_configured", "trusted_branches_only", "prevent_self_review"):
        if protection.get(field) is not True:
            errors.append("preflight_protection_" + field)
    return {"ok": not errors, "errors": errors, "artifact": name,
            "quality_passed": False, "github_ref": request.get("github_ref"),
            "source_sha": request.get("source_sha"),
            "remote_run_url": request.get("remote_run_url"),
            "protection": protection}


def audit_execution_artifact(root: str | Path, expected: dict[str, str], *, kind: str,
                             current_files: dict[str, str], github_ref: str | None = None,
                             tier: str | None = None) -> dict[str, Any]:
    root = Path(root)
    errors = _check_common_files(root, expected, allow_failed=kind == "real-model")
    receipts = sorted(root.rglob("ci-result.json"))
    if len(receipts) != 1:
        errors.append("ci_receipt_count:" + str(len(receipts)))
        return {"ok": False, "errors": errors, "quality_passed": False}
    wrapper = receipts[0].parent
    try:
        from evals.ci import audit_receipt

        replay = audit_receipt(wrapper)
        receipt = _json(wrapper / "ci-result.json")
        runner = _json(wrapper / "runner.json")
        source = _json(wrapper / "source-identity.json")
        source_after = _json(wrapper / "source-identity-after.json")
        raw_result = _json(wrapper / "run" / "run-result.json")
        errors.extend("runner:" + item for item in
                      _identity_errors(runner, expected, github_fields=True))
        github = runner.get("github", {})
        remote_url = (f"{github.get('GITHUB_SERVER_URL')}/{github.get('GITHUB_REPOSITORY')}"
                      f"/actions/runs/{expected['run_id']}")
        if (not github.get("GITHUB_SERVER_URL") or not github.get("GITHUB_REPOSITORY")
                or runner.get("remote_run_url") != remote_url
                or receipt.get("remote_run_url") != remote_url):
            errors.append("ci_remote_run_url_mismatch")
        if kind == "real-model" and github_ref is not None and github.get("GITHUB_REF") != github_ref:
            errors.append("r_runner_ref_mismatch")
        if source.get("files") != current_files or source_after != source:
            errors.append("source_map_mismatch")
        source_hash = digest(canonical(current_files))
        if source.get("sha256") != source_hash:
            errors.append("source_map_hash_mismatch")
        if kind in {"deterministic", "real-model"}:
            snapshot = _json(wrapper / "run" / "snapshot" / "manifest.json")
            if snapshot.get("files") != current_files:
                errors.append("inner_snapshot_source_mismatch")
        if receipt.get("raw_state") != raw_result.get("state"):
            errors.append("original_run_state_changed")
        if receipt.get("quality_passed") is not False:
            errors.append("quality_claim_forbidden")
        if not replay.get("ok"):
            errors.append("receipt_replay_failed")
        if replay.get("raw_replay_ok") is not True:
            errors.append("raw_replay_not_ok")
        if kind in {"deterministic", "fixed-load"}:
            if receipt.get("pipeline_passed") is not True or replay.get("raw_replay_ok") is not True:
                errors.append("diagnostic_pipeline_not_passed")
            process = _json(wrapper / "process-exit.json")
            if process.get("command_exit_code") != 0 or process.get("termination") is not None:
                errors.append("diagnostic_process_failed")
            invocation = _json(wrapper / "invocation.json")
            raw_manifest = _json(wrapper / "run" / "manifest.json")
            if kind == "deterministic":
                if invocation.get("kind") != "daily":
                    errors.append("daily_invocation_kind_mismatch")
                if not tier:
                    errors.append("daily_tier_missing")
                else:
                    from evals.daily import build_plan

                    expected_plan = build_plan(tier, "D")
                    if (raw_manifest.get("mode") != "D" or raw_manifest.get("tier") != tier or
                            raw_manifest.get("coverage") != "tier_default" or
                            raw_manifest.get("repeat") != expected_plan["repeat"] or
                            raw_manifest.get("plan") != expected_plan["plan"]):
                        errors.append("daily_tier_default_plan_mismatch")
            else:
                if invocation.get("kind") != "performance":
                    errors.append("performance_invocation_kind_mismatch")
                from evals.common import load_cases
                from evals.performance import _source_identity, profile_conditions

                case = next(item for item in load_cases() if item["case_id"] == "V001")
                conditions = raw_manifest.get("profile_conditions")
                if (raw_manifest.get("profile") != f"{expected['platform']}-fixed-load-v1" or
                        raw_manifest.get("requested_platform") != expected["platform"] or
                        conditions != profile_conditions(case)):
                    errors.append("fixed_load_profile_conditions_mismatch")
                if raw_manifest.get("source_identity") != _source_identity():
                    errors.append("inner_fixed_load_source_mismatch")
        elif kind == "real-model":
            errors.extend(_check_r_frozen_plan(wrapper, root, expected, raw_result))
            if raw_result.get("state") not in {
                    "passed_diagnostic", "regression", "invalid", "insufficient_evidence"}:
                errors.append("r_original_state_invalid")
        else:
            errors.append("unknown_execution_kind")
        return {"ok": not errors, "errors": errors, "quality_passed": False,
                "artifact_kind": kind, "original_state": raw_result.get("state"),
                "original_ci_state": receipt.get("state"),
                "pipeline_passed": receipt.get("pipeline_passed"),
                "job_status": _json(root / "job-completion.json").get("job_status"),
                "raw_replay_ok": replay.get("raw_replay_ok"),
                "model_requests": replay.get("model_requests"),
                "agent_reruns": replay.get("agent_reruns"),
                "usage": {key: raw_result.get(key) for key in
                          ("actual_model_attempts", "known_tokens", "unknown_usage_attempts")}}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append("artifact_audit:" + type(exc).__name__)
        return {"ok": False, "errors": errors, "quality_passed": False}


def audit_downloaded(kind: str, download_root: str | Path, *, run_id: str,
                     run_attempt: str, sha: str, platforms: tuple[str, ...] = ("linux", "windows"),
                     current_files: dict[str, str] | None = None,
                     tier: str | None = None, github_ref: str | None = None,
                     server_url: str | None = None, repository: str | None = None) -> dict[str, Any]:
    if kind not in {"engineering", "deterministic", "fixed-load", "real-model"}:
        raise ValueError("unsupported_artifact_kind")
    download_root = Path(download_root)
    current_files = current_source_map() if current_files is None else current_files
    audits: list[dict[str, Any]] = []
    errors: list[str] = []
    if kind == "real-model":
        if not github_ref or not server_url or not repository:
            raise ValueError("real_model_external_ref_required")
        remote_url = f"{server_url.rstrip('/')}/{repository}/actions/runs/{run_id}"
        preflight = audit_real_preflight(
            download_root, {"run_id": str(run_id), "run_attempt": str(run_attempt), "sha": sha},
            github_ref=github_ref, remote_run_url=remote_url)
        audits.append(preflight)
        if not preflight.get("ok"):
            errors.append("r_preflight_failed")
    for platform_name in platforms:
        if platform_name not in PLATFORMS:
            raise ValueError("unsupported_platform")
        expected = {"run_id": str(run_id), "run_attempt": str(run_attempt), "sha": sha,
                    "platform": platform_name}
        if kind == "engineering":
            artifact_name = f"engineering-{platform_name}-{run_id}-{run_attempt}"
            result = audit_engineering_artifact(download_root / artifact_name, expected,
                                               current_files)
        elif kind == "deterministic":
            if not tier:
                raise ValueError("deterministic_tier_required")
            artifact_name = f"deterministic-{platform_name}-{tier}-{run_id}-{run_attempt}"
            result = audit_execution_artifact(download_root / artifact_name, expected,
                                              kind=kind, current_files=current_files,
                                              github_ref=github_ref, tier=tier)
        else:
            artifact_name = f"{kind}-{platform_name}-{run_id}-{run_attempt}"
            result = audit_execution_artifact(download_root / artifact_name, expected,
                                              kind=kind, current_files=current_files,
                                              github_ref=github_ref)
        audits.append({"artifact": artifact_name, **result})
        if not result.get("ok"):
            errors.append("artifact_failed:" + artifact_name)
    expected_count = len(platforms) + (1 if kind == "real-model" else 0)
    return {"kind": kind, "expected": expected_count, "audited": len(audits),
            "audits": audits, "errors": errors, "ok": not errors,
            "quality_passed": False, "model_requests": 0, "agent_reruns": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("engineering", "deterministic", "fixed-load", "real-model"))
    parser.add_argument("--download-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--tier")
    parser.add_argument("--github-ref")
    parser.add_argument("--server-url")
    parser.add_argument("--repository")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--verify-checkout-only", action="store_true")
    args = parser.parse_args()
    if args.verify_checkout_only:
        try:
            files = current_source_map(ROOT)
            report = verify_checkout_blobs(ROOT, args.sha, files)
            report["verified"] = True
        except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError) as exc:
            report = {"verified": False, "head": None, "files": 0, "sha256": None,
                      "error": type(exc).__name__}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["verified"] else 1
    if not all((args.kind, args.download_root, args.run_id, args.run_attempt, args.report)):
        parser.error("archive audit requires --kind, --download-root, --run-id, --run-attempt and --report")
    current_files: dict[str, str] | None = None
    try:
        current_files = current_source_map(ROOT)
        checkout = verify_checkout_blobs(ROOT, args.sha, current_files)
        report = audit_downloaded(args.kind, args.download_root, run_id=args.run_id,
                                  run_attempt=args.run_attempt, sha=args.sha,
                                  current_files=current_files, tier=args.tier,
                                  github_ref=args.github_ref, server_url=args.server_url,
                                  repository=args.repository)
        report["checkout_git_blob_verification"] = checkout
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError) as exc:
        report = {"kind": args.kind, "expected": 2, "audited": 0, "audits": [],
                  "errors": ["checkout_or_archive_audit:" + type(exc).__name__],
                  "ok": False, "quality_passed": False,
                  "model_requests": 0, "agent_reruns": 0}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"ok": report["ok"], "errors": report["errors"],
                      "quality_passed": False}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
