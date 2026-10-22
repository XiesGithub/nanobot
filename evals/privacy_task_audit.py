"""Independent offline replay of the joint V095/V107 privacy family."""
import argparse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from evals.common import MODEL, ROOT, canonical, digest, inside, read, strict_json, write
from evals.fulltask_audit import (
    _check_index,
    _verify_grading_ledger,
    _verify_review_receipts,
    _verify_selfcheck,
)
from evals.fulltask_semantic import grading_limits
from evals.grading_transport import prompt_metadata
from evals.mail_task_audit import _actual_grading_budget, _trial_checks
from evals.privacy_task_cases import MODEL_PROFILE, VERSION
from evals.privacy_task_evidence import score
from evals.privacy_task_semantic import (
    FAMILY,
    aggregate,
    build_plan,
    case_for,
    expected_status,
    packet_for,
    template_for,
)
from evals.semantic import REVIEW_SCHEMA, gold_packet
from evals.trace import analyze_trace


def _audit(output):
    output = Path(output).resolve()
    errors = []
    _check_index(output, "artifact-index.json", errors, "root", exact_inventory=True)
    manifest = read(output / "manifest.json")
    mode, control = manifest["mode"], manifest["control"]
    case = case_for(mode)
    plan = build_plan(case, mode, control, manifest["repeat_index"])
    template = template_for(case)
    gold_expected = gold_packet(template)
    if (manifest["version"] != VERSION or manifest["scope"] != "full_task"
            or manifest["family_ids"] != FAMILY or manifest["independent_security_samples"] != 1
            or manifest["run_id"] != output.name or manifest["case"] != case or manifest["plan"] != plan
            or manifest["template"] != template or manifest["gold"] != gold_expected
            or read(output / "gold.json") != gold_expected or manifest["prompt"] != prompt_metadata()
            or manifest["model_profile"] != MODEL_PROFILE or manifest["grading_schema_version"] != REVIEW_SCHEMA
            or manifest["requested_model"] != (MODEL if mode == "R" else None)):
        errors.append("frozen_contract_or_profile")
    environment = manifest["environment"]
    expected = {"conda": "nanobot", "timezone": "Asia/Shanghai", "concurrency": 1,
        "network": "R_provider_and_loopback_only; no_IMAP_or_recipients",
        "isolation": "separate_process_and_tool_boundary; not_OS_sandbox", "OS_sandbox": False,
        "streaming": "protected_mail_delta_suppressed_by_product",
        "log_export_api": "absent; actual_log_sink_and_unavailable_route_observed"}
    if (set(environment) != set(expected) | {"os", "python"}
            or any(environment.get(k) != v for k, v in expected.items())
            or any(not isinstance(environment.get(k), str) or not environment[k] for k in ("os", "python"))):
        errors.append("frozen_environment_profile")
    limits = grading_limits("judge_budget" if control == "judge_budget" else None)
    if manifest["budget"] != {"product": {"attempts": 8, "tools": 16, "known_tokens": 32000,
                                          "seconds": plan[0]["product_timeout_s"]}, "grading": limits}:
        errors.append("frozen_budget")
    if manifest["sources"] != manifest["snapshot"]["file_sha256"]:
        errors.append("source_mapping")
    frozen_ref = manifest.get("frozen_contract")
    if mode == "R":
        frozen_path = inside(ROOT, frozen_ref["path"])
        frozen = read(output / "frozen-contract.json")
        if (digest(frozen_path.read_bytes()) != frozen_ref["sha256"] or read(frozen_path) != frozen
                or frozen["case"] != case or frozen["gold_template"] != template
                or frozen["sources"] != manifest["sources"]
                or frozen["plans"][manifest["repeat_index"] - 1] != plan[0]
                or output != frozen_path.parent / ("R-0" + str(manifest["repeat_index"]))):
            errors.append("frozen_R_contract_binding")
        prerequisites = frozen_path.parent / "D-prerequisites.json"
        if digest(prerequisites.read_bytes()) != frozen["D_prerequisites_sha256"] or not read(prerequisites)["ok"]:
            errors.append("D_prerequisite_binding")
    elif frozen_ref is not None or (output / "frozen-contract.json").exists():
        errors.append("unexpected_D_frozen_contract")
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(manifest["sources"]):
            errors.append("source_inventory")
        for name, sha in manifest["sources"].items():
            if digest(archive.read(name)) != sha:
                errors.append("source_hash:" + name)
            if digest(inside(ROOT, name).read_bytes()) != sha:
                errors.append("replay_source_changed:" + name)
    calls, costs = _verify_grading_ledger(output, manifest, errors)
    _actual_grading_budget(calls, costs, limits, errors)
    selfcheck = _verify_selfcheck(output, manifest, calls, errors)
    gold = read(output / "gold-review.json")
    _verify_review_receipts(gold, gold_expected, "gold", calls, errors, "gold")
    trial_id = plan[0]["trial_id"]
    trial = output / "trials" / trial_id
    _check_index(trial, "artifact-index.json", errors, "trial", exact_inventory=False)
    evidence = read(trial / "artifacts/evidence.json")
    events = [strict_json(line) for line in (trial / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if (evidence["events"] != events or evidence["trial_id"] != trial_id
            or any(e["trial_id"] != trial_id or e["run_id"] != output.name for e in events)):
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
        digest(canonical(gold_expected)): "gold", digest(canonical(packet)): "judge"}
    entry = next((e for e in events if e["type"] == "entry" and e["phase"] == "start"), None)
    delivery = next((e for e in events if e["type"] == "delivery"), None)

    def stamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    for call in calls:
        if allowed.get(call["packet_sha256"]) != call["stage"]:
            errors.append("grading_trial_binding")
        ended = stamp(call["started_at"]) + timedelta(milliseconds=call["duration_ms"])
        if call["stage"] in {"selfcheck", "gold"} and entry and ended >= stamp(entry["wall_time"]):
            errors.append("standard_not_preapproved")
        if call["stage"] == "judge" and delivery and stamp(call["started_at"]) <= stamp(delivery["wall_time"]):
            errors.append("judge_before_delivery")
    self_positions = [i for i, c in enumerate(calls) if c["stage"] == "selfcheck"]
    gold_positions = [i for i, c in enumerate(calls) if c["stage"] == "gold"]
    if self_positions and gold_positions and max(self_positions) >= min(gold_positions):
        errors.append("selfcheck_after_gold")
    verdict = aggregate(case, evidence, program, packet, semantic, gold, selfcheck, mode, costs)
    if verdict != read(trial / "full-task-verdict.json"):
        errors.append("qualification_replay")
    expected_result = {"version": VERSION, "mode": mode, "family_ids": FAMILY,
        "independent_security_samples": 1, "status": verdict["status"],
        "formal_quality_eligible": verdict["formal_quality_eligible"],
        "ok": verdict["status"] == expected_status(control) and (mode == "D" or verdict["formal_quality_eligible"]),
        "grading_cost": costs, "product_cost": {"attempts": trace["model_attempts"],
            "known_tokens": trace["known_tokens"], "amount": None if mode == "R" else 0, "currency": None}}
    if read(output / "run-result.json") != expected_result:
        errors.append("summary_or_cost_replay")
    return {"version": VERSION, "family_ids": FAMILY, "independent_security_samples": 1,
        "ok": not errors, "errors": sorted(set(errors)), "status": verdict["status"],
        "qualification": verdict["formal_quality_eligible"] and not errors, "model_requests": 0, "agent_reruns": 0}


def audit(output):
    try:
        return _audit(output)
    except Exception as exc:
        return {"version": VERSION, "ok": False, "errors": ["audit_exception:" + type(exc).__name__],
                "model_requests": 0, "agent_reruns": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(args.output.resolve()):
        raise ValueError("audit_report_must_be_external")
    result = audit(args.output)
    write(args.report, result)
    print(result)
    raise SystemExit(0 if result["ok"] else 1)
