"""Independent offline replay for the three mail full-task cases; no requests."""

import argparse
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from evals.common import MODEL, ROOT, SCHEMA, canonical, digest, read, strict_json, write
from evals.fulltask_audit import (
    _check_index,
    _verify_grading_ledger,
    _verify_review_receipts,
    _verify_selfcheck,
)
from evals.fulltask_semantic import grading_limits
from evals.grading_transport import prompt_metadata
from evals.mail_task_cases import MODEL_PROFILE, VERSION, encoder_pin, load_cases, templates
from evals.mail_task_evidence import score
from evals.mail_task_semantic import aggregate, build_plan, expected_status, packet_for
from evals.semantic import REVIEW_SCHEMA, gold_packet
from evals.trace import analyze_trace


def _profile_checks(manifest, errors):
    mode = manifest["mode"]
    if (manifest.get("grading_schema_version") != REVIEW_SCHEMA
            or manifest.get("requested_model") != (MODEL if mode == "R" else None)
            or manifest.get("sources") != manifest.get("snapshot", {}).get("file_sha256")):
        errors.append("frozen_version_model_or_source_mapping")
    if manifest.get("model_profile") != MODEL_PROFILE:
        errors.append("frozen_product_model_profile")
    expected_environment = {
        "conda": "nanobot", "timezone": "Asia/Shanghai", "concurrency": 1,
        "network": "R_provider_only; no_agent_network_tools",
        "isolation": "separate_process_and_tool_boundary; not_OS_sandbox",
        "encoder": manifest["case"].get("encoder_pin"), "retrieval": "bge-hybrid-v1" if manifest["case"].get("encoder_pin") else "fixed-text-v1", "sending": False,
        "archive_enabled": True, "unifiedSession": False, "OS_sandbox": False,
    }
    environment = manifest.get("environment", {})
    if (set(environment) != set(expected_environment) | {"os", "python"}
            or any(environment.get(key) != value for key, value in expected_environment.items())
            or any(not isinstance(environment.get(key), str) or not environment[key]
                   for key in ("os", "python"))):
        errors.append("frozen_environment_profile")


def _actual_grading_budget(calls, costs, limits, errors):
    total = costs.get("known_input_tokens", 0) + costs.get("known_output_tokens", 0)
    if total > limits["reserved_tokens"]:
        errors.append("grading_actual_run_budget_exceeded")
    stages = defaultdict(int)
    for call in calls:
        stages[(call["packet_sha256"], call["stage"])] += sum(
            value for value in (call.get("input_tokens"), call.get("output_tokens"))
            if type(value) is int and value >= 0)
    if any(value > limits["per_packet_stage_reserved_tokens"] for value in stages.values()):
        errors.append("grading_actual_stage_budget_exceeded")


def _trial_checks(trial_dir, output, case, plan, evidence, events, trace, program, errors):
    trial = read(trial_dir / "trial.json")
    index = read(trial_dir / "artifact-index.json")
    expected_paths = {"artifacts/evidence.json", "events.jsonl", "artifacts/trace-analysis.json"}
    if {row.get("path") for row in index} != expected_paths:
        errors.append("trial_index_inventory")
    expected = {"schema_version": SCHEMA, "run_id": output.name, **plan,
                "execution_status": evidence["execution_status"],
                "cancel_reason": evidence.get("cancel_reason"),
                "stop_reason": evidence["execution_status"],
                "accepted_event_id": next((e["event_id"] for e in events
                                            if e["type"] == "task" and e["phase"] == "start"), None),
                "terminal_event_id": next((e["event_id"] for e in reversed(events)
                                            if e["type"] == "task" and e["phase"] != "start"), None),
                "completion": {"met": program["extensions"].get("completion_met", False),
                               "elapsed_ms": evidence["elapsed_ms"],
                               "event_ids": [e["event_id"] for e in events
                                             if e["type"] == "snapshot"
                                             or e["type"] == "task" and e["phase"] == "end"],
                               "reason": "returned_and_required_write_observed"},
                "evidence": index,
                "observation": {"expected": case["observation"]["required"],
                                "observed": dict(Counter(e["type"] for e in events)),
                                "missing": [r["reason_code"] for r in program["reasons"]
                                            if "missing" in r["reason_code"]],
                                "collector_errors": evidence["collector_errors"],
                                "closed": trace["closed"], "scope": "phase3",
                                "trace_analysis_ref": "artifacts/trace-analysis.json"},
                "budget_usage": program["extensions"],
                "product_cost": {"usage_coverage": "provider_call_boundary",
                                 "amount": 0 if case["mode"] == "D" else None,
                                 "currency": None, "closed": trace["closed"],
                                 "unknown_usage_attempts": trace["unknown_usage_attempts"],
                                 "unknown_reason": "prices_unknown; uninstrumented_components_out_of_scope"},
                "grading_cost": {"amount": 0, "model_attempts": 0},
                "cleanup": evidence["cleanup"], "verdict_refs": ["verdicts/1.json"]}
    for key, value in expected.items():
        if trial.get(key) != value:
            errors.append("trial_replay:" + key)
    if set(trial) != set(expected) | {"extensions"}:
        errors.append("trial_schema")
    extension = trial.get("extensions", {})
    if (set(extension) != {"initial_state_sha256", "worker_pid"}
            or extension.get("initial_state_sha256") != digest(canonical(evidence["before"]))
            or type(extension.get("worker_pid")) is not int or extension["worker_pid"] <= 0):
        errors.append("trial_initial_state_or_worker_identity")
    if (type(evidence["elapsed_ms"]) not in (int, float) or evidence["elapsed_ms"] < 0
            or evidence["execution_status"] == "finished"
            and evidence["elapsed_ms"] > plan["product_timeout_s"] * 1000):
        errors.append("product_elapsed_budget")
    terminal = next((e for e in reversed(events)
                     if e["type"] == "trial" and e["phase"] != "start"), None)
    if (not terminal or terminal["payload"].get("execution_status") != evidence["execution_status"]
            or terminal["payload"].get("process_stopped") != evidence["cleanup"]["process_stopped"]
            or terminal["payload"].get("instance_removed") != evidence["cleanup"]["instance_removed"]
            or evidence["worker_process"]["stopped"] != evidence["cleanup"]["process_stopped"]):
        errors.append("trial_terminal_or_cleanup_binding")


def _audit(output):
    output = Path(output).resolve()
    errors = []
    _check_index(output, "artifact-index.json", errors, "root", exact_inventory=True)
    manifest = read(output / "manifest.json")
    _profile_checks(manifest, errors)
    cid, mode, control = manifest["case"]["case_id"], manifest["mode"], manifest["control"]
    case = next(c for c in load_cases() if c["case_id"] == cid)
    case["mode"] = mode
    if manifest.get("semantic_profile", False):
        case["encoder_pin"] = encoder_pin()
        case["environment_profile"] = "mail-semantic-full-task/1.0"
    plan = build_plan(case, mode, control, manifest.get("repeat_index", 1))
    template = templates()[cid]
    gold_packet_expected = gold_packet(template)
    if (manifest["version"] != VERSION or manifest["run_id"] != output.name or manifest["scope"] != "full_task"
            or manifest["case"] != case or manifest["plan"] != plan or manifest["template"] != template
            or manifest["gold"] != gold_packet_expected or read(output / "gold.json") != gold_packet_expected
            or manifest["prompt"] != prompt_metadata()):
        errors.append("frozen_contract_or_profile")
    limits = grading_limits("judge_budget" if control == "judge_budget" else None)
    if manifest["budget"] != {"product": {"attempts": 8, "known_tokens": 32000, "seconds": plan[0]["product_timeout_s"]}, "grading": limits}:
        errors.append("frozen_budget")
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        if set(archive.namelist()) != set(manifest["sources"]) or len(archive.namelist()) != len(set(archive.namelist())):
            errors.append("source_inventory")
        for name, sha in manifest["sources"].items():
            if digest(archive.read(name)) != sha:
                errors.append("source_hash:" + name)
    for name in ("evals/common.py", "evals/run.py", "evals/worker.py", "evals/mail_task_cases.py", "evals/mail_task_fixtures.json",
                 "evals/mail_task_worker.py", "evals/mail_task_evidence.py", "evals/mail_task_semantic.py", "evals/mail_task_audit.py",
                 "evals/fulltask_semantic.py", "evals/fulltask_audit.py", "evals/semantic.py", "evals/semantic_run.py",
                 "evals/semantic_samples.py", "evals/grading_transport.py", "evals/trace.py", case["source_ref"].split("#")[0]):
        if digest((ROOT / name).read_bytes()) != manifest["sources"].get(name):
            errors.append("replay_source_changed:" + name)
    if case.get("encoder_pin"):
        for name in ("evals/mail_semantic_encoder.json", "evals/mail_semantic_evidence.py"):
            if digest((ROOT / name).read_bytes()) != manifest["sources"].get(name):
                errors.append("replay_source_changed:" + name)
    calls, costs = _verify_grading_ledger(output, manifest, errors)
    _actual_grading_budget(calls, costs, limits, errors)
    selfcheck = _verify_selfcheck(output, manifest, calls, errors)
    gold = read(output / "gold-review.json")
    _verify_review_receipts(gold, gold_packet_expected, "gold", calls, errors, "gold")
    trial_id = plan[0]["trial_id"]
    trial = output / "trials" / trial_id
    _check_index(trial, "artifact-index.json", errors, "trial", exact_inventory=False)
    evidence = read(trial / "artifacts/evidence.json")
    events = [strict_json(line) for line in (trial / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if evidence["events"] != events or any(e["trial_id"] != trial_id or e["run_id"] != output.name for e in events) or evidence["trial_id"] != trial_id:
        errors.append("cross_trial_or_event_binding")
    trace = analyze_trace(events, evidence["execution_status"], evidence["collector_errors"])
    if trace != read(trial / "artifacts/trace-analysis.json"):
        errors.append("trace_replay")
    program = score(case, evidence)
    _trial_checks(trial, output, case, plan[0], evidence, events, trace, program, errors)
    if program != read(trial / "verdicts/1.json"):
        errors.append("program_replay")
    packet = packet_for(case, evidence)
    if packet != read(trial / "packet.json"):
        errors.append("delivery_packet_binding")
    semantic = read(trial / "semantic-result.json")
    _verify_review_receipts(semantic, packet, "judge", calls, errors, "semantic")
    allowed = {digest(canonical(manifest["selfcheck"]["packet"])): "selfcheck",
               digest(canonical(gold_packet_expected)): "gold", digest(canonical(packet)): "judge"}
    for call in calls:
        if allowed.get(call["packet_sha256"]) != call["stage"]:
            errors.append("grading_trial_binding")
    def timestamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    entry = next((e for e in events if e["type"] == "entry" and e["phase"] == "start"), None)
    delivery = next((e for e in events if e["type"] == "delivery"), None)
    for call in calls:
        ended = timestamp(call["started_at"]) + timedelta(milliseconds=call["duration_ms"])
        if call["stage"] in {"selfcheck", "gold"} and entry and ended >= timestamp(entry["wall_time"]):
            errors.append("standard_not_preapproved")
        if call["stage"] == "judge" and delivery and timestamp(call["started_at"]) <= timestamp(delivery["wall_time"]):
            errors.append("judge_before_delivery")
    self_positions = [i for i, c in enumerate(calls) if c["stage"] == "selfcheck"]
    gold_positions = [i for i, c in enumerate(calls) if c["stage"] == "gold"]
    if self_positions and gold_positions and max(self_positions) >= min(gold_positions):
        errors.append("selfcheck_after_gold")
    verdict = aggregate(case, evidence, program, packet, semantic, gold, selfcheck, mode, costs)
    if verdict != read(trial / "full-task-verdict.json"):
        errors.append("qualification_replay")
    result = read(output / "run-result.json")
    expected = expected_status(control)
    expected_ok = verdict["status"] == expected and (mode == "D" or verdict["formal_quality_eligible"])
    expected_product = {"attempts": trace["model_attempts"], "known_tokens": trace["known_tokens"], "amount": None if mode == "R" else 0, "currency": None}
    if result != {"version": VERSION, "mode": mode, "case_id": cid, "status": verdict["status"],
                  "formal_quality_eligible": verdict["formal_quality_eligible"], "ok": expected_ok,
                  "grading_cost": costs, "product_cost": expected_product}:
        errors.append("summary_or_cost_replay")
    return {"version": VERSION, "ok": not errors, "errors": sorted(set(errors)), "case_id": cid,
            "status": verdict["status"], "qualification": verdict["formal_quality_eligible"] and not errors,
            "model_requests": 0, "agent_reruns": 0}


def audit(output):
    try:
        return _audit(output)
    except Exception as exc:
        return {"version": VERSION, "ok": False, "errors": ["audit_exception:" + type(exc).__name__],
                "model_requests": 0, "agent_reruns": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(args.output.resolve()):
        raise ValueError("audit_report_must_be_external")
    result = audit(args.output)
    write(args.report, result)
    print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
