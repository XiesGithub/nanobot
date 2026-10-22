"""Evidence-bound development, smoke, nightly and release diagnostics.

Engineering tests are a separate CI job. D never publishes a model quality score.
Unsupported adapters, unknown fees and missing evidence stay visible in the plan.
"""

import argparse
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from evals.common import (
    IDS,
    MODEL,
    ROOT,
    canonical,
    digest,
    inside,
    load_cases,
    now,
    read,
    strict_json,
    write,
)

VERSION = "phase8-daily/1.0"
TIERS = {"development": (5, 1), "smoke": (24, 1), "nightly": (60, 3), "release": (60, 5)}


def budgets(mode, attempts=None, tokens=None, wall=None, opt_in=False):
    if wall is not None and (type(wall) is not int or wall <= 0):
        raise ValueError("positive_wall_budget_required")
    if mode == "R":
        if not opt_in:
            raise ValueError("real_model_explicit_opt_in_required")
        if any(type(v) is not int or v <= 0 for v in (attempts, tokens, wall)):
            raise ValueError("real_model_positive_attempt_token_wall_budgets_required")
        return {"max_attempts": attempts, "max_tokens": tokens, "max_wall_seconds": wall,
                "prices": None, "token_policy": "pre_dispatch_conservative_byte_reservation; stop_on_unknown_usage",
                "financial_budget": "unknown_prices; no_financial_pass"}
    if any(v is not None and v != 0 for v in (attempts, tokens)):
        raise ValueError("deterministic_mode_has_no_model_budget")
    return {"max_attempts": 0, "max_tokens": 0, "max_wall_seconds": wall or 3600, "prices": None}


def build_plan(tier, mode, selected=None, nonce=None):
    from evals.batches import catalog

    public = catalog()
    agent = {c["case_id"]: c for c in load_cases()}
    count, repeat = TIERS[tier]
    # Keep the established 24-case milestone; blocked V022 remains in larger plans.
    order = [f"V{i:03}" for i in range(1, 26) if i != 22]
    order += sorted(set(public) - set(order))
    ids = list(IDS) if tier == "development" else order[:count]
    if selected is not None:
        if tier != "development" or not selected or len(selected) > 10:
            raise ValueError("targeted_cases_development_only_max_10")
        if len(set(selected)) != len(selected) or not set(selected) <= set(public):
            raise ValueError("unknown_or_duplicate_target_case")
        ids = list(selected)
    active = {case_id: {"kind": "agent" if case_id in agent else "module",
                        "contract": agent.get(case_id, public[case_id])} for case_id in ids}
    gold = None
    if tier == "release":
        from evals.holdout import create_release_set

        gold = create_release_set(nonce or str(uuid.uuid4()), public)
        for case in gold["cases"]:
            active[case["case_id"]] = {"kind": "agent", "contract": case}
    plan = []
    for case_id, item in active.items():
        case = item["contract"]
        for index in range(1, repeat + 1):
            plan.append({"trial_id": f"{case_id}-{index}", "case_id": case_id,
                         "case_version": case["case_version"], "repeat_index": index,
                         "profile": f"{mode}-phase8-{tier}-v1", "purpose": "primary",
                         "original_trial_id": None, "fault": None,
                         "entry": "AgentLoop.process_direct" if item["kind"] == "agent" else case["entry"],
                         "task_type": "ordinary", "fault_condition": "none", "mode": mode,
                         "scope": "restricted_file_program_diagnostic" if item["kind"] == "agent" else case["scope"],
                         "kind": item["kind"], "product_timeout_s": 15 if mode == "D" else 120})
    return {"active": active, "plan": plan, "repeat": repeat, "gold": gold,
            "coverage": "targeted" if selected else "tier_default"}


def gate(manifest, rows, stop_reason=None, errors=()):
    reasons = list(errors)
    expected = {p["trial_id"]: p for p in manifest["plan"]}
    ids = [r.get("trial_id") for r in rows]
    if len(ids) != len(set(ids)) or set(ids) - set(expected):
        reasons.append("duplicate_or_unplanned_records")
    for row in rows:
        plan = expected.get(row.get("trial_id"), {})
        if any(row.get(k) != plan.get(k) for k in
               ("case_id", "repeat_index", "profile", "entry", "scope", "mode")):
            reasons.append("record_plan_identity")
        if row.get("evidence_valid") is not True or row.get("status") == "invalid":
            reasons.append("invalid_raw_evidence:" + str(row.get("trial_id")))
    if reasons:
        state = "invalid"
    elif any(r.get("status") == "product_fail" or
             (r.get("safety", {}).get("actual") or 0) > 0 for r in rows):
        state = "regression"
        reasons.append("program_contract_or_safety_failure")
    else:
        if len(rows) != len(expected):
            reasons.append("missing_planned_trials")
        if stop_reason:
            reasons.append(stop_reason)
        if any(r.get("status") != "pass" or any(r.get(k) is not True for k in
               ("execution_complete", "scoring_complete", "observation_complete")) for r in rows):
            reasons.append("incomplete_execution_scoring_or_observation")
        if manifest["mode"] == "R":
            if any(r.get("product_cost", {}).get("unknown_usage_attempts") != 0 or
                   r.get("request_budget", {}).get("usage_complete") is False for r in rows):
                reasons.append("unknown_model_usage")
            if any(r.get("product_cost", {}).get("closed") is not True or
                   r.get("product_cost", {}).get("amount") is None or
                   r.get("grading_cost", {}).get("closed") is not True or
                   r.get("grading_cost", {}).get("amount") is None for r in rows):
                reasons.append("unknown_product_or_grading_fee")
        state = "insufficient_evidence" if reasons else "passed_diagnostic"
    return {"state": state, "quality_passed": False, "reasons": sorted(set(reasons)),
            "formal_quality_score": None, "optimization_effective": False}


def agent_replay(case, plan, directory, process):
    from evals.daily_worker import _agent_budget_record
    from evals.paired import replay

    row = {**plan, **replay(case, plan, directory, process)}
    if plan["mode"] == "R":
        try:
            request = read(directory.parent.parent / "requests" / (plan["trial_id"] + ".json"))
            evidence = read(directory / "trials" / plan["trial_id"] / "artifacts" / "evidence.json")
            actual = _agent_budget_record(evidence, request["budget"])
            if actual != read(directory / "model-budget.json"):
                raise ValueError("budget_replay")
            row["request_budget"] = actual
            if actual.get("trace_accounting_errors"):
                raise ValueError("request_accounting")
            if (actual["attempts_dispatched"] > actual["max_attempts"] or
                    actual["known_tokens"] is not None and actual["known_tokens"] > actual["max_tokens"]):
                raise ValueError("budget_overrun")
        except (ValueError, KeyError, OSError, TypeError) as exc:
            row.update(status="invalid", evidence_valid=False,
                       replay_errors=[*row.get("replay_errors", []), "request_budget:" + type(exc).__name__])
    return row


def module_replay(contract, plan, directory, process, subject, snapshot_id):
    from evals.batch_audit import audit
    from evals.paired import invalid_record

    try:
        child = read(directory / "manifest.json")
        raw = read(directory / "execution-result.json")
        archived = [strict_json(s) for s in (directory / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        errors = []
        if child["cases"] != {plan["case_id"]: contract} or child.get("snapshot_id") != snapshot_id:
            errors.append("module_contract_or_snapshot_identity")
        checked = audit(directory, source_root=subject)
        errors += checked["errors"]
        frozen = read(directory.parent.parent / "snapshot" / "manifest.json")
        if child["sources"] != {"evals/common.py": frozen["files"]["evals/common.py"]}:
            errors.append("module_source_identity")
        if archived != [raw] or process != {"returncode": 0, "watchdog_timeout": False}:
            errors.append("module_record_or_process")
        if (directory / "complete.marker").read_bytes() != canonical({"trial_id": plan["trial_id"]}):
            errors.append("module_complete_marker")
        trial = inside(directory, raw["artifacts"])
        evidence = read(trial / "evidence.json")
        zero = {"amount": 0, "currency": "USD", "closed": True, "model_attempts": 0,
                "known_tokens": 0, "unknown_usage_attempts": 0}
        if raw["model_attempts"] != 0 or plan["mode"] != "D":
            errors.append("module_not_deterministic")
        return {**plan, "status": "invalid" if errors else raw["verdict"],
                "module_trial_id": raw["trial_id"], "execution_status": raw["status"],
                "execution_complete": raw["status"] in ("finished", "timeout"),
                "scoring_complete": raw["scoring_complete"],
                "observation_complete": raw["observation_complete"],
                "completion_met": raw["status"] == "finished", "completion_ms": evidence.get("duration_ms"),
                "safety": {"verified": False, "actual": None, "critical_actual": None},
                "product_cost": zero, "grading_cost": dict(zero),
                "fault": {"planned": False, "triggered": False, "recoverable": False, "cancelled": False},
                "cleanup": {"ok": read(trial / "cleanup.json")["removed"]},
                "evidence_valid": not errors, "replay_errors": errors, "process": process}
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {**plan, **invalid_record(["module_replay:" + type(exc).__name__], process)}


def scope_metrics(manifest, rows):
    from evals.metrics import summarize

    result = {}
    for scope in sorted({p["scope"] for p in manifest["plan"]}):
        plans = [p for p in manifest["plan"] if p["scope"] == scope]
        result[scope] = summarize({"mode": manifest["mode"], "scope": scope,
                                   "repeat": manifest["repeat"], "plan": plans,
                                   "covered_case_ids": sorted({p["case_id"] for p in plans}),
                                   "formal_quality_eligible": False},
                                  [r for r in rows if r["scope"] == scope])
    return result


def result_for(manifest, rows, stop_reason, errors):
    def total(field):
        values = [r["product_cost"].get(field) for r in rows]
        return sum(values) if all(type(v) is int and v >= 0 for v in values) else None

    actual_sdk = [0 if manifest["mode"] == "D" or r["kind"] == "module"
                  else r.get("request_budget", {}).get("attempts_dispatched") for r in rows]

    return {"version": VERSION, "run_id": manifest["run_id"], "tier": manifest["tier"],
            "mode": manifest["mode"], "snapshot_id": manifest["snapshot_id"],
            **gate(manifest, rows, stop_reason, errors),
            "planned_trials": len(manifest["plan"]), "recorded_trials": len(rows),
            "verdict_counts": dict(Counter(r["status"] for r in rows)),
            "scope_metrics": scope_metrics(manifest, rows), "budget": manifest["budget"],
            "actual_model_attempts": sum(actual_sdk) if all(type(v) is int for v in actual_sdk) else None,
            "logical_model_attempts": total("model_attempts"),
            "known_tokens": total("known_tokens"),
            "unknown_usage_attempts": total("unknown_usage_attempts"),
            "stop_reason": stop_reason, "controller_errors": errors,
            "historical_results_reused": False, "engineering_result": "reported_separately"}


def inventory(output):
    return {p.relative_to(output).as_posix(): digest(p.read_bytes()) for p in sorted(output.rglob("*"))
            if p.is_file() and p != output / "artifact-index.json"}


def run(tier, mode, output, budget, selected=None):
    from evals.identity import freeze, materialize
    from evals.paired import reserve_holdout, run_process

    output = Path(output).resolve()
    if not output.is_relative_to(ROOT.resolve()) or output == ROOT.resolve():
        raise ValueError("output_must_be_new_workspace_directory")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    bundle = build_plan(tier, mode, selected)
    if bundle["gold"]:
        reserve_holdout(bundle["gold"]["data_sha256"], output.name)
        write(output / "gold" / "generated-release-set.json", bundle["gold"])
    active, plan = bundle["active"], bundle["plan"]
    identity = freeze(ROOT, output / "snapshot", {
        "mode": mode, "model_id": MODEL, "tier": tier, "repeat": bundle["repeat"],
        "plan_sha256": digest(canonical(plan)), "contracts_sha256": digest(canonical(active)),
        "budget": budget, "concurrency": 1, "temperature": 0, "max_tokens": 2048,
        "thinking": "disabled", "sdk_retries": 0, "grading": "program_only",
        "historical_reuse": False, "isolation": "process_and_restricted_file_tools; no_OS_sandbox"})
    subject_path = inside(ROOT / "tmp", "phase7-snapshots/phase8-" + uuid.uuid4().hex)
    manifest = {"version": VERSION, "run_id": output.name, "created_at": now(), "tier": tier,
                "mode": mode, "repeat": bundle["repeat"], "coverage": bundle["coverage"],
                "active": active, "plan": plan, "budget": budget,
                "snapshot_id": identity["snapshot_id"], "subject_path": str(subject_path),
                "contracts_sha256": digest(canonical(active)), "plan_sha256": digest(canonical(plan)),
                "gold_data_sha256": bundle["gold"]["data_sha256"] if bundle["gold"] else None}
    write(output / "manifest.json", manifest)
    rows, errors, stop_reason = [], [], None
    (output / "records.jsonl").touch(exist_ok=False)
    subject = None
    try:
        subject = materialize(output / "snapshot", subject_path)
        for trial in plan:
            remaining = budget["max_wall_seconds"] - (time.monotonic() - started)
            if remaining <= 0:
                stop_reason = "wall_budget_exhausted"
                break
            directory = output / "executions" / trial["trial_id"]
            if mode == "R" and trial["kind"] == "module":
                row = {**trial, "status": "undetermined", "evidence_valid": True,
                       "execution_complete": False, "scoring_complete": False,
                       "observation_complete": False, "completion_met": False, "completion_ms": None,
                       "safety": {"verified": False, "actual": None, "critical_actual": None},
                       "product_cost": {"amount": 0, "closed": True, "model_attempts": 0, "known_tokens": 0, "unknown_usage_attempts": 0},
                       "grading_cost": {"amount": 0, "closed": True},
                       "fault": {"planned": False, "triggered": False, "recoverable": False, "cancelled": False},
                       "reason": "real_model_entry_adapter_unavailable"}
                write(directory / "unsupported.json", row)
            else:
                used_attempts = sum(r["product_cost"].get("model_attempts") or 0 for r in rows)
                used_tokens = sum(r["product_cost"].get("known_tokens") or 0 for r in rows)
                left_attempts, left_tokens = budget["max_attempts"] - used_attempts, budget["max_tokens"] - used_tokens
                if mode == "R" and (left_attempts <= 0 or left_tokens <= 2048 or any(
                        r["product_cost"].get("unknown_usage_attempts") != 0
                        or r["product_cost"].get("model_attempts") is None
                        or r["product_cost"].get("known_tokens") is None for r in rows)):
                    stop_reason = "request_token_budget_or_unknown_usage_stop"
                    break
                request = {"kind": "phase8_" + trial["kind"], "output": str(directory),
                           "case": active[trial["case_id"]]["contract"], "plan": trial, "mode": mode,
                           "credential_file": str(ROOT / "api.txt"), "snapshot_id": manifest["snapshot_id"],
                           "budget": {"max_attempts": min(8, left_attempts), "max_tokens": min(32000, left_tokens)}}
                request_path = output / "requests" / (trial["trial_id"] + ".json")
                write(request_path, request)
                process = run_process(subject, ["--request", str(request_path)],
                                      output / "logs" / (trial["trial_id"] + ".log"), min(remaining, 180),
                                      credential_file=ROOT / "api.txt" if mode == "R" else None)
                if trial["kind"] == "agent":
                    row = agent_replay(request["case"], trial, directory, process)
                else:
                    row = module_replay(request["case"], trial, directory, process, subject, manifest["snapshot_id"])
            rows.append(row)
            with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(canonical(row).decode() + "\n")
                stream.flush()
            print(f"{mode} {tier} {trial['trial_id']}: {row['status']}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        errors.append("controller:" + type(exc).__name__)
    finally:
        cleanup_error = None
        if subject is not None:
            if subject.resolve() != subject_path or not subject_path.is_relative_to((ROOT / "tmp" / "phase7-snapshots").resolve()):
                raise ValueError("unsafe_source_cleanup")
            try:
                shutil.rmtree(subject)
            except OSError as exc:
                from evals.run import _failure_details
                cleanup_error = _failure_details(
                    exc, "remove_source_snapshot", "tmp/phase7-snapshots/" + subject_path.name,
                    "evals.daily.run")
                errors.append("source_cleanup:" + type(exc).__name__)
        write(output / "cleanup.json", {"owned_source": str(subject_path),
                                        "removed": not subject_path.exists(), "error": cleanup_error})
    result = result_for(manifest, rows, stop_reason, errors)
    write(output / "run-result.json", result)
    (output / "report.md").write_text(
        f"# Stage 8 {tier} / {mode}\n\nState: {result['state']}; quality_passed=false.\n\n"
        f"Fresh records: {len(rows)}/{len(plan)}; counts: {result['verdict_counts']}.\n\n"
        "Engineering tests and fixed load are reported separately. Module evidence is scoped; "
        "D makes no real requests. R fees remain unknown. No optimization claim.\n\n"
        f"Reasons: {result['reasons']}\n", encoding="utf-8")
    write(output / "artifact-index.json", inventory(output))
    return result


def audit(output):
    try:
        return _audit(output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"ok": False, "errors": ["audit_error:" + type(exc).__name__],
                "model_requests": 0, "agent_reruns": 0, "quality_passed": False}


def _audit(output):
    from evals.identity import verify

    output = Path(output).resolve()
    errors = []
    if inventory(output) != read(output / "artifact-index.json"):
        errors.append("artifact_inventory_or_hash")
    checked = verify(output / "snapshot")
    errors.extend(checked["errors"])
    manifest = read(output / "manifest.json")
    if checked["manifest"].get("snapshot_id") != manifest["snapshot_id"]:
        errors.append("snapshot_identity")
    conditions = read(output / "snapshot" / "manifest.json")["conditions"]
    for key in ("mode", "tier", "repeat", "budget"):
        if conditions[key] != manifest[key]:
            errors.append("conditions:" + key)
    for field, value in (("plan_sha256", manifest["plan"]), ("contracts_sha256", manifest["active"])):
        if digest(canonical(value)) != manifest[field] or conditions[field] != manifest[field]:
            errors.append(field)
    if manifest["tier"] == "release":
        from evals.batches import catalog
        from evals.holdout import validate_release_set

        gold = read(output / "gold" / "generated-release-set.json")
        validate_release_set(gold, catalog())
        if gold["data_sha256"] != manifest["gold_data_sha256"]:
            errors.append("release_gold_identity")
    rows = [strict_json(s) for s in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    plans = {p["trial_id"]: p for p in manifest["plan"]}
    for claimed in rows:
        plan = plans[claimed["trial_id"]]
        contract = manifest["active"][plan["case_id"]]["contract"]
        directory = output / "executions" / plan["trial_id"]
        if manifest["mode"] == "R" and plan["kind"] == "module":
            actual = read(directory / "unsupported.json")
        elif plan["kind"] == "agent":
            actual = agent_replay(contract, plan, directory, claimed["process"])
        else:
            actual = module_replay(contract, plan, directory, claimed["process"],
                                   manifest["subject_path"], manifest["snapshot_id"])
        if canonical(actual) != canonical(claimed):
            errors.append("raw_record_replay:" + plan["trial_id"])
    saved = read(output / "run-result.json")
    if saved != result_for(manifest, rows, saved["stop_reason"], saved["controller_errors"]):
        errors.append("gate_or_metric_replay")
    cleanup = read(output / "cleanup.json")
    expected_cleanup = {"owned_source": manifest["subject_path"], "removed": True}
    if "error" in cleanup:
        expected_cleanup["error"] = None
    if cleanup != expected_cleanup or Path(manifest["subject_path"]).exists():
        errors.append("source_cleanup")
    return {"ok": not errors, "errors": errors, "records": len(rows),
            "model_requests": 0, "agent_reruns": 0, "quality_passed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=TIERS, default="development")
    parser.add_argument("--mode", choices=("D", "R"), default="D")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-wall-seconds", type=int)
    parser.add_argument("--opt-in-real-model", action="store_true")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.audit:
        result = audit(args.audit)
        if args.report:
            write(args.report, result)
        print(result)
        return 0 if result["ok"] else 1
    if not args.output:
        parser.error("--output required")
    limits = budgets(args.mode, args.max_attempts, args.max_tokens, args.max_wall_seconds, args.opt_in_real_model)
    result = run(args.tier, args.mode, args.output, limits, args.cases)
    print({"state": result["state"], "quality_passed": result["quality_passed"]})
    return 0 if result["state"] == "passed_diagnostic" else 1


if __name__ == "__main__":
    raise SystemExit(main())
