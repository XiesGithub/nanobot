"""Offline channel-profile replay of identities, render state, costs and completion."""
import argparse
import zipfile
from pathlib import Path

from evals.channel_task_cases import MODEL_PROFILE, VERSION
from evals.channel_task_evidence import score
from evals.channel_task_run import aggregate, build_plan, case_for, expected_status
from evals.common import MODEL, ROOT, digest, inside, read, strict_json, write
from evals.fulltask_audit import _check_index
from evals.mail_task_audit import _trial_checks
from evals.trace import analyze_trace


def _audit(output):
    output = Path(output).resolve()
    errors = []
    _check_index(output, "artifact-index.json", errors, "root", exact_inventory=True)
    manifest = read(output / "manifest.json")
    cid, mode, control = manifest["case"]["case_id"], manifest["mode"], manifest["control"]
    case = case_for(cid, mode, control)
    plan = build_plan(case, mode, control, manifest["repeat_index"])
    if (manifest["version"] != VERSION or manifest["scope"] != "full_task" or manifest["run_id"] != output.name
            or manifest["case"] != case or manifest["plan"] != plan or manifest["model_profile"] != MODEL_PROFILE
            or manifest["requested_model"] != (MODEL if mode == "R" else None)
            or manifest["sources"] != manifest["snapshot"]["file_sha256"]
            or manifest["grading"] != {"mode": "independent_program", "model_requests": 0, "money": 0}):
        errors.append("frozen_case_version_or_model")
    if manifest["budget"] != {"attempts": 8, "tools": 16, "known_tokens": 32000, "seconds": plan[0]["product_timeout_s"]}:
        errors.append("budget_drift")
    environment = manifest["environment"]
    expected = {"conda": "nanobot", "timezone": "Asia/Shanghai", "concurrency": 1,
        "network": "loopback_test_transports_and_R_provider_only",
        "isolation": "separate_process_and_tool_boundary; not_OS_sandbox", "platform_connection": False,
        "OS_sandbox": False, "renderer": "actual_React_hook_jsdom; no_real_browser" if cid == "V066" else "local_channel_protocol"}
    if (set(environment) != set(expected) | {"os", "python"}
            or any(environment.get(key) != value for key, value in expected.items())):
        errors.append("environment_drift")
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        if set(archive.namelist()) != set(manifest["sources"]) or len(archive.namelist()) != len(set(archive.namelist())):
            errors.append("source_inventory")
        for name, sha in manifest["sources"].items():
            if digest(archive.read(name)) != sha or digest(inside(ROOT, name).read_bytes()) != sha:
                errors.append("source_changed:" + name)
    if mode == "R":
        ref = manifest["frozen_contract"]
        frozen_path = inside(ROOT, ref["path"])
        frozen = read(output / "frozen-contract.json")
        if read(frozen_path) != frozen or digest(frozen_path.read_bytes()) != ref["sha256"]:
            errors.append("frozen_R_contract_hash")
        target = next(row for row in frozen["runs"] if row["case_id"] == cid and row["repeat_index"] == manifest["repeat_index"])
        if target["case"] != case or target["plan"] != plan[0] or target["sources"] != manifest["sources"] or output != frozen_path.parent / target["directory"]:
            errors.append("frozen_R_trial_binding")
        prerequisite = frozen_path.parent / "D-prerequisites.json"
        if not read(prerequisite)["ok"] or digest(prerequisite.read_bytes()) != frozen["D_prerequisites_sha256"]:
            errors.append("D_prerequisites")
    elif manifest.get("frozen_contract") is not None:
        errors.append("D_frozen_R_contract")
    trial = output / "trials" / plan[0]["trial_id"]
    _check_index(trial, "artifact-index.json", errors, "trial", exact_inventory=False)
    evidence = read(trial / "artifacts/evidence.json")
    events = [strict_json(line) for line in (trial / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if evidence["events"] != events or evidence["trial_id"] != plan[0]["trial_id"] or any(
            event["trial_id"] != plan[0]["trial_id"] or event["run_id"] != output.name for event in events):
        errors.append("cross_trial_or_raw_event_binding")
    trace = analyze_trace(events, evidence["execution_status"], evidence["collector_errors"])
    if trace != read(trial / "artifacts/trace-analysis.json"):
        errors.append("trace_replay")
    program = score(case, evidence)
    _trial_checks(trial, output, case, plan[0], evidence, events, trace, program, errors)
    if program != read(trial / "verdicts/1.json"):
        errors.append("program_replay")
    verdict = aggregate(case, evidence, program, mode, control)
    if verdict != read(trial / "full-task-verdict.json"):
        errors.append("qualification_replay")
    expected_result = {"version": VERSION, "case_id": cid, "mode": mode, "control": control,
        "status": verdict["status"], "formal_quality_eligible": verdict["formal_quality_eligible"],
        "ok": verdict["status"] == expected_status(cid, mode, control), "product_requests": trace["model_attempts"],
        "product_known_tokens": trace["known_tokens"], "product_actual_tokens": trace["actual_tokens"],
        "product_amount": None if mode == "R" else 0, "currency": None, "grading_requests": 0, "grading_amount": 0}
    if read(output / "run-result.json") != expected_result:
        errors.append("result_or_cost_replay")
    return {"version": VERSION, "case_id": cid, "status": verdict["status"], "ok": not errors,
        "errors": sorted(set(errors)), "qualification": verdict["formal_quality_eligible"] and not errors,
        "model_requests": 0, "agent_reruns": 0, "renderer": environment["renderer"]}


def audit(output):
    try:
        return _audit(output)
    except Exception as error:
        return {"version": VERSION, "ok": False, "errors": ["audit_exception:" + type(error).__name__],
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
