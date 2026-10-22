"""Fresh snapshot execution and evidence-bound, interleaved version comparison."""

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

from evals.common import ROOT, canonical, digest, inside, load_cases, read, strict_json, write

VERSION = "phase7/1.0"
PROTECTED_GROUPS = ("cases", "scoring", "judge", "environment", "conditions", "project_config")


def conditions(mode="D"):
    return {"mode": mode, "model_id": "deepseek-v4-flash", "repeat": 3,
            "entry": "AgentLoop.process_direct", "scope": "restricted_file_program_diagnostic",
            "temperature": 0, "max_tokens": 2048, "thinking": "disabled", "sdk_retries": 0,
            "concurrency": 1, "timezone": "Asia/Shanghai", "session": "empty_per_trial",
            "tools": ["read_file", "write_file", "edit_file", "list_dir"],
            "product_timeout_s": 15 if mode == "D" else 120,
            "attempts_per_trial": 0 if mode == "D" else 8,
            "tokens_per_trial": 0 if mode == "D" else 32000,
            "max_model_attempts": 0 if mode == "D" else 64,
            "max_known_tokens": 0 if mode == "D" else 320000,
            "run_watchdog_s": 1200 if mode == "D" else 3600,
            "prices": None, "grading": "program_only", "fault_condition": "none",
            "network": "no_agent_network; R_provider_only",
            "isolation": "process_and_file_tools; not_OS_sandbox",
            "ordering": "case_then_repeat; alternate_AB_BA", "historical_reuse": False}


def compatibility(baseline, candidate):
    errors = []
    for group in PROTECTED_GROUPS:
        if baseline["groups"][group]["identity_hash"] != candidate["groups"][group]["identity_hash"]:
            errors.append("incompatible_" + group)
    # A changed controller/fixture cannot grade its own candidate as comparable.
    for name, value in baseline["files"].items():
        if (name.startswith(("evals/", "tests/knowledge/mail/", "docs/validation-cases/"))
                and candidate["files"].get(name) != value):
            errors.append("incompatible_harness:" + name)
    baseline_eval = {n for n in baseline["files"] if n.startswith("evals/")}
    candidate_eval = {n for n in candidate["files"] if n.startswith("evals/")}
    if baseline_eval != candidate_eval:
        errors.append("incompatible_harness_inventory")
    return errors


def reserve_holdout(data_sha256, comparison_id):
    """A disclosed prospective set cannot be reused as an independent new round."""
    if len(data_sha256) != 64 or any(c not in "0123456789abcdef" for c in data_sha256):
        raise ValueError("invalid_holdout_data_identity")
    ledger = inside(ROOT / "tmp", "phase7-holdout-ledger/" + data_sha256 + ".json")
    record = {"data_sha256": data_sha256, "comparison_id": comparison_id,
              "policy": "reserved_once; replacement_required_for_a_new_round",
              "status": "reserved; interrupted or disclosed rounds are never silently reset"}
    try:
        write(ledger, record)
    except FileExistsError:
        raise ValueError("holdout_already_reserved_or_exposed; replace_the_set") from None
    return record


def run_process(subject, arguments, log_path, timeout, *, credential_file=None):
    env = {**os.environ, "PYTHONPATH": str(subject), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen([sys.executable, "-m", "evals.paired_worker", *arguments],
                               cwd=subject, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    timed_out = False
    interrupted = None
    try:
        data, _ = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        timed_out = isinstance(exc, subprocess.TimeoutExpired)
        if isinstance(exc, KeyboardInterrupt):
            interrupted = exc
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.kill()
        data, _ = process.communicate()
        psutil.wait_procs(descendants, timeout=3)
    # Provider diagnostics cannot place the credential into parent logs.
    credential_path = Path(credential_file) if credential_file is not None else None
    if credential_path is not None and credential_path.exists():
        for line in credential_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("sk-"):
                data = data.replace(line.strip().encode(), b"[credential_redacted]")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("xb") as stream:
        stream.write(data)
    if interrupted is not None:
        raise interrupted
    return {"returncode": process.returncode, "watchdog_timeout": timed_out}


def trial_plan(case, index, mode):
    return {"trial_id": f"{case['case_id']}-{index}", "case_id": case["case_id"],
            "case_version": case["case_version"], "repeat_index": index,
            "profile": mode + "-phase7-paired-v1", "purpose": "primary",
            "original_trial_id": None, "fault": None, "entry": "AgentLoop.process_direct",
            "task_type": "ordinary", "fault_condition": "none", "mode": mode,
            "scope": "restricted_file_program_diagnostic",
            "product_timeout_s": conditions(mode)["product_timeout_s"]}


def invalid_record(errors, process_result):
    return {"status": "invalid", "execution_complete": False, "scoring_complete": False,
            "observation_complete": False, "completion_met": False, "completion_ms": None,
            "safety": {"verified": False, "actual": None, "critical_actual": None},
            "product_cost": {"amount": None, "currency": None, "closed": False},
            "grading_cost": {"amount": None, "currency": None, "closed": False},
            "fault": {"planned": False, "triggered": False, "recoverable": False,
                      "cancelled": False}, "evidence_valid": False,
            "replay_errors": errors, "process": process_result}


def replay(case, plan, directory, process_result):
    try:
        return _replay(case, plan, directory, process_result)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return invalid_record(["replay_error:" + type(exc).__name__], process_result)


def _replay(case, plan, directory, process_result):
    from evals.repeat import record_trial

    if not (directory / "execution-result.json").exists():
        return invalid_record(["execution_result_missing"], process_result)
    claimed = read(directory / "execution-result.json")
    actual = record_trial(case, plan, directory, {
        "execution_status": claimed["execution_status"], "elapsed_ms": claimed["elapsed_ms"],
        "cleanup": claimed["cleanup"]})
    errors = []
    if canonical(actual) != canonical(claimed):
        errors.append("normalized_record_replay_mismatch")
    if process_result["returncode"] != 0 or process_result["watchdog_timeout"]:
        errors.append("subject_process_failed")
    if (directory / "complete.marker").read_bytes() != canonical({"trial_id": plan["trial_id"]}):
        errors.append("subject_complete_marker")
    evidence = read(directory / "trials" / plan["trial_id"] / "artifacts" / "evidence.json")
    expected = {f["path"]: f["sha256"] for f in case["initial_state"]["fixtures"]}
    if {p: item["sha256"] for p, item in evidence["before"].items()} != expected:
        errors.append("initial_fixture_identity")
    if (not actual["cleanup"]["ok"] or not actual.get("instance_path")
            or Path(actual["instance_path"]).exists()):
        errors.append("instance_cleanup")
    actual.update(evidence_valid=not errors and actual.get("verdict_evidence_replay_equal") is True,
                  replay_errors=errors, process=process_result, scope=plan["scope"])
    if errors:
        actual.update(status="invalid", scoring_complete=False, observation_complete=False)
    return actual


def arm_metrics(cases, records, mode):
    """Reuse Stage 5 accounting, with separate arm and split denominators."""
    from evals.metrics import summarize

    result = {}
    for arm in ("baseline", "candidate"):
        result[arm] = {}
        for split in ("development", "holdout", "all"):
            selected = [c for c in cases if split == "all" or c["split"] == split]
            ids = {c["case_id"] for c in selected}
            plan = [trial_plan(case, index, mode) for case in selected for index in range(1, 4)]
            manifest = {"mode": mode, "scope": conditions(mode)["scope"], "repeat": 3,
                        "profile": mode + "-phase7-paired-v1", "plan": plan,
                        "covered_case_ids": sorted(ids), "formal_quality_eligible": False}
            rows = [r for r in records if r["arm"] == arm and r["case_id"] in ids]
            result[arm][split] = summarize(manifest, rows)
    return result


def module_audit(directory, subject):
    from evals.batches import analyze_batch_trace, catalog, score, summarize

    result = read(directory / "run-result.json")
    contracts = read(directory / "contracts.json")
    errors = []
    if contracts != catalog():
        errors.append("module_frozen_contracts")
    archived = [strict_json(line) for line in (directory / "records.jsonl").read_text(
        encoding="utf-8").splitlines()]
    if archived != result["rows"]:
        errors.append("module_record_inventory")
    if len(result["rows"]) != 120 or len({r["case_id"] for r in result["rows"]}) != 120:
        errors.append("module_inventory")
    if result != {**summarize(list(contracts), archived), "rows": archived}:
        errors.append("module_summary_replay")
    for row in result["rows"]:
        trial = inside(directory, row["artifacts"])
        evidence = read(trial / "evidence.json")
        events = [strict_json(line) for line in (trial / "events.jsonl").read_text(
            encoding="utf-8").splitlines()]
        if analyze_batch_trace(events, row["trial_id"], evidence["status"],
                               evidence["collector_errors"]) != evidence["trace"]:
            errors.append("module_trace:" + row["case_id"])
        verdict = score(contracts[row["case_id"]], evidence)
        if (verdict != read(trial / "verdict.json") or row["verdict"] != verdict["verdict"]
                or row["status"] != evidence["status"]):
            errors.append("module_score:" + row["case_id"])
        if read(trial / "contract.json") != contracts[row["case_id"]]:
            errors.append("module_contract:" + row["case_id"])
        if any(e["trial_id"] != row["trial_id"] or e["run_id"] != directory.name for e in events):
            errors.append("module_event_identity:" + row["case_id"])
        index = read(trial / "final-artifact-index.json")
        actual_paths = {p.relative_to(trial).as_posix() for p in trial.rglob("*")
                        if p.is_file() and p != trial / "final-artifact-index.json"}
        if actual_paths != set(index):
            errors.append("module_artifact_inventory:" + row["case_id"])
        for relative, hashed in index.items():
            if digest(inside(trial, relative).read_bytes()) != hashed:
                errors.append("module_artifact:" + row["case_id"])
        spec = contracts[row["case_id"]]
        for relative, hashed in spec["fixture_hashes"].items():
            if evidence["before"].get(relative, {}).get("sha256") != hashed:
                errors.append("module_fixture:" + row["case_id"])
        for relative, item in evidence["after"].items():
            if digest(inside(trial / "files", relative).read_bytes()) != item["sha256"]:
                errors.append("module_file_evidence:" + row["case_id"])
        trace = evidence["trace"]
        scoring_complete = bool(verdict["requirements"]) and all(
            j["passed"] is not None for j in verdict["requirements"])
        if (row["scope"] != spec["scope"] or row["trial_id"] != evidence["trial_id"]
                or row["case_id"] != evidence["case_id"]
                or row["observation_complete"] != trace["closed"]
                or row["scoring_complete"] != scoring_complete
                or row["model_attempts"] != trace["model_attempts"]
                or row["cost"] != (0 if trace["model_attempts"] == 0 else None)):
            errors.append("module_accounting:" + row["case_id"])
        cleanup = read(trial / "cleanup.json")
        expected = subject / "tmp" / "phase6-instances" / row["trial_id"]
        if Path(cleanup["owned_instance"]) != expected or expected.exists() or not cleanup["removed"]:
            errors.append("module_cleanup:" + row["case_id"])
    return {"ok": not errors, "errors": errors, "records": len(result["rows"]),
            "snapshot_only": True, "historical_reuse": False}


def run(baseline_path, candidate_path, output, mode="D", module_inventory=False):
    from evals.comparison import DEFAULT_POLICY, compare
    from evals.holdout import holdout_cases, validate_holdout
    from evals.identity import _environment as current_environment
    from evals.identity import materialize, verify

    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("conda_nanobot_required")
    checked = [verify(path) for path in (baseline_path, candidate_path)]
    if any(not item["ok"] for item in checked):
        raise ValueError("snapshot_verification_failed")
    baseline, candidate = [item["manifest"] for item in checked]
    mismatch = compatibility(baseline, candidate)
    if mismatch:
        raise ValueError("comparison_conditions_mismatch:" + ",".join(mismatch))
    if baseline["environment"] != current_environment():
        raise ValueError("execution_environment_differs_from_frozen_environment")
    # The trusted current scorer/controller must itself match the frozen baseline.
    for relative, hashed in baseline["files"].items():
        if relative.startswith("evals/") and digest((ROOT / relative).read_bytes()) != hashed:
            raise ValueError("controller_snapshot_mismatch:" + relative)
    output = Path(output).resolve()
    output.relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    cases = load_cases() + holdout_cases()
    from evals.batches import catalog

    holdout_validation = validate_holdout(catalog(), cases[5:])
    fixed = conditions(mode)
    if baseline.get("conditions") != fixed or candidate.get("conditions") != fixed:
        raise ValueError("frozen_runtime_conditions_mismatch")
    comparison_id = uuid.uuid4().hex
    holdout_reservation = reserve_holdout(read(ROOT / "evals/holdout-registry.json")["data_sha256"],
                                         comparison_id)
    manifest = {"version": VERSION, "comparison_id": comparison_id, "mode": mode,
                "baseline_snapshot_id": baseline["snapshot_id"],
                "candidate_snapshot_id": candidate["snapshot_id"], "conditions": fixed,
                "conditions_id": digest(canonical(fixed)), "repeat": 3,
                "policy": DEFAULT_POLICY, "cases": [{"case_id": c["case_id"],
                    "split": c["split"], "scope": "restricted_file_program_diagnostic"} for c in cases],
                "holdout_validation": holdout_validation, "historical_reuse": False,
                "formal_quality_eligible": False, "coverage": "5 public +20 prospective holdout",
                "case_sha256": digest(canonical(cases)), "snapshot_refs": {
                    "baseline": "snapshots/baseline", "candidate": "snapshots/candidate"}}
    write(output / "manifest.json", manifest)
    write(output / "holdout-round.json", holdout_reservation)
    write(output / "gold/cases.json", cases)
    subjects = {}
    records = []
    try:
        for arm, path in (("baseline", baseline_path), ("candidate", candidate_path)):
            saved = output / "snapshots" / arm
            saved.mkdir(parents=True)
            for filename in ("manifest.json", "source.zip"):
                shutil.copyfile(Path(path) / filename, saved / filename)
            subjects[arm] = materialize(path, ROOT / "tmp" / "phase7-snapshots" /
                                        (comparison_id + "-" + arm))
        if module_inventory:
            directory = output / "current-module-inventory"
            process = run_process(subjects["baseline"], ["--module-baseline", str(directory)],
                                  output / "module-console.log", 600)
            validation = module_audit(directory, subjects["baseline"])
            validation.update(snapshot_id=baseline["snapshot_id"], process=process)
            write(output / "current-module-audit.json", validation)
        stopped = None
        started = time.monotonic()
        for case_index, case in enumerate(cases):
            for index in range(1, 4):
                arms = ("baseline", "candidate") if (case_index + index) % 2 else ("candidate", "baseline")
                for arm in arms:
                    if time.monotonic() - started > fixed["run_watchdog_s"]:
                        stopped = "run_watchdog; remaining trials absent"
                        break
                    if mode == "R" and (sum(r["product_cost"].get("model_attempts") or 0 for r in records)
                                        + 8 > fixed["max_model_attempts"] or sum(
                        r["product_cost"].get("known_tokens") or 0 for r in records)
                                        + 32000 > fixed["max_known_tokens"]):
                        stopped = "observed_run_budget; remaining trials absent"
                        break
                    trial_id = f"{arm}-{case['case_id']}-{index}"
                    directory = output / "executions" / trial_id
                    plan = trial_plan(case, index, mode)
                    request = {"output": str(directory), "case": case, "plan": plan, "mode": mode,
                               "credential_file": str(ROOT / "api.txt")}
                    request_path = output / "requests" / (trial_id + ".json")
                    write(request_path, request)
                    process = run_process(subjects[arm], ["--request", str(request_path)],
                                          output / "logs" / (trial_id + ".log"), 180 if mode == "R" else 60,
                                          credential_file=ROOT / "api.txt" if mode == "R" else None)
                    row = replay(case, plan, directory, process)
                    row.update(arm=arm, case_id=case["case_id"], repeat_index=index,
                               snapshot_id=baseline["snapshot_id"] if arm == "baseline" else candidate["snapshot_id"],
                               conditions_id=manifest["conditions_id"], evidence_ref=str(directory.relative_to(output)),
                               execution_order=len(records) + 1)
                    records.append(row)
                    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(canonical(row).decode() + "\n")
                        stream.flush()
                    print(f"paired {len(records)}/150 {trial_id}: {row['status']}", flush=True)
                if stopped:
                    break
            if stopped:
                break
        comparison = compare(manifest, records)
        write(output / "arm-metrics.json", arm_metrics(cases, records, mode))
        write(output / "comparison.json", comparison)
        write(output / "run-result.json", {"version": VERSION, "comparison": comparison,
              "records": len(records), "stopped_reason": stopped,
              "historical_reuse": False, "module_inventory": module_inventory})
    finally:
        cleanup = {}
        for arm, subject in subjects.items():
            checked_path = inside(ROOT / "tmp", "phase7-snapshots/" + comparison_id + "-" + arm)
            if checked_path != subject.resolve():
                raise ValueError("snapshot_cleanup_identity")
            shutil.rmtree(checked_path)
            cleanup[arm] = {"owned_path": str(checked_path), "removed": not checked_path.exists()}
        write(output / "subject-cleanup.json", cleanup)
    write(output / "artifact-index.json", {p.relative_to(output).as_posix(): digest(p.read_bytes())
                                          for p in output.rglob("*") if p.is_file()})
    return comparison


def audit(output):
    """Offline replay; a historical summary alone is never accepted as evidence."""
    from evals.batches import catalog
    from evals.comparison import compare
    from evals.holdout import holdout_cases, validate_holdout
    from evals.identity import verify

    output = Path(output).resolve()
    manifest = read(output / "manifest.json")
    errors = []
    index = read(output / "artifact-index.json")
    paths = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
             and p != output / "artifact-index.json"}
    if paths != set(index):
        errors.append("artifact_inventory")
    for relative, expected_hash in index.items():
        if digest(inside(output, relative).read_bytes()) != expected_hash:
            errors.append("artifact_hash:" + relative)
    snapshots = {}
    for arm in ("baseline", "candidate"):
        checked = verify(inside(output, manifest["snapshot_refs"][arm]))
        snapshots[arm] = checked["manifest"]
        if not checked["ok"] or checked["manifest"]["snapshot_id"] != manifest[arm + "_snapshot_id"]:
            errors.append("snapshot_identity:" + arm)
    errors.extend(compatibility(snapshots["baseline"], snapshots["candidate"]))
    for relative, hashed in snapshots["baseline"]["files"].items():
        if relative.startswith("evals/") and digest((ROOT / relative).read_bytes()) != hashed:
            errors.append("audit_controller_snapshot_mismatch:" + relative)
    cases = read(output / "gold/cases.json")
    if digest(canonical(cases)) != manifest["case_sha256"] or cases != load_cases() + holdout_cases():
        errors.append("case_identity")
    if (manifest["holdout_validation"] != validate_holdout(catalog(), cases[5:])
            or manifest["conditions"] != conditions(manifest["mode"])
            or manifest["historical_reuse"] is not False
            or any(s["conditions"] != manifest["conditions"] for s in snapshots.values())):
        errors.append("frozen_comparison_contract")
    round_record = read(output / "holdout-round.json")
    ledger = inside(ROOT / "tmp", "phase7-holdout-ledger/" + round_record["data_sha256"] + ".json")
    if read(ledger) != round_record or round_record["comparison_id"] != manifest["comparison_id"]:
        errors.append("holdout_round_identity")
    by_id = {c["case_id"]: c for c in cases}
    rows = [strict_json(line) for line in (output / "records.jsonl").read_text(
        encoding="utf-8").splitlines()]
    for order, claimed in enumerate(rows, 1):
        arm, case_id, index = claimed["arm"], claimed["case_id"], claimed["repeat_index"]
        request_path = output / "requests" / f"{arm}-{case_id}-{index}.json"
        request = read(request_path)
        case = by_id[case_id]
        plan = trial_plan(case, index, manifest["mode"])
        if claimed["execution_order"] != order:
            errors.append("execution_order:" + case_id)
        if request["case"] != case or request["plan"] != plan or request["mode"] != manifest["mode"]:
            errors.append("request_identity:" + case_id)
        directory = inside(output, claimed["evidence_ref"])
        if directory != output / "executions" / f"{arm}-{case_id}-{index}":
            errors.append("execution_directory_identity:" + case_id)
        actual = replay(case, plan, directory, claimed["process"])
        actual.update(arm=arm, case_id=case_id, repeat_index=index,
                      snapshot_id=manifest[arm + "_snapshot_id"], conditions_id=manifest["conditions_id"],
                      evidence_ref=claimed["evidence_ref"], execution_order=claimed["execution_order"])
        if actual != claimed:
            errors.append("paired_record_replay:" + case_id)
    comparison = compare(manifest, rows)
    if arm_metrics(cases, rows, manifest["mode"]) != read(output / "arm-metrics.json"):
        errors.append("arm_metrics_replay")
    if comparison != read(output / "comparison.json"):
        errors.append("comparison_replay")
    cleanup = read(output / "subject-cleanup.json")
    if set(cleanup) != {"baseline", "candidate"}:
        errors.append("subject_cleanup_inventory")
    for arm, item in cleanup.items():
        expected = ROOT / "tmp" / "phase7-snapshots" / (manifest["comparison_id"] + "-" + arm)
        if Path(item["owned_path"]) != expected or expected.exists() or item["removed"] is not True:
            errors.append("subject_cleanup:" + arm)
    run_result = read(output / "run-result.json")
    if run_result["comparison"] != comparison or run_result["records"] != len(rows):
        errors.append("run_summary_replay")
    if run_result["module_inventory"]:
        validation = module_audit(output / "current-module-inventory",
                                  Path(cleanup["baseline"]["owned_path"]))
        stored = read(output / "current-module-audit.json")
        if (not validation["ok"] or {k: stored[k] for k in validation} != validation
                or stored["snapshot_id"] != manifest["baseline_snapshot_id"]
                or stored["process"] != {"returncode": 0, "watchdog_timeout": False}):
            errors.append("module_baseline_replay")
    return {"version": "phase7-audit/1.0", "ok": not errors, "errors": errors,
            "records": len(rows), "outcome": comparison["outcome"],
            "model_requests": 0, "agent_reruns": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--source", type=Path, default=ROOT)
    freeze_parser.add_argument("--output", type=Path, required=True)
    freeze_parser.add_argument("--mode", choices=("D", "R"), default="D")
    pair_parser = sub.add_parser("run")
    pair_parser.add_argument("--baseline", type=Path, required=True)
    pair_parser.add_argument("--candidate", type=Path, required=True)
    pair_parser.add_argument("--output", type=Path, required=True)
    pair_parser.add_argument("--mode", choices=("D", "R"), default="D")
    pair_parser.add_argument("--module-inventory", action="store_true")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--run", type=Path, required=True)
    audit_parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        from evals.identity import freeze

        result = freeze(args.source, args.output, conditions(args.mode))
        print(result["snapshot_id"])
    elif args.command == "audit":
        result = audit(args.run)
        write(args.report, result)
        print({"ok": result["ok"], "errors": result["errors"], "records": result["records"]})
        return 0 if result["ok"] else 1
    else:
        result = run(args.baseline, args.candidate, args.output, args.mode, args.module_inventory)
        print(result["outcome"])
        return 0 if result["outcome"] in ("improved", "regression_passed_improvement_unproven") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
