"""Bounded original channel tasks with independent deterministic scoring."""
import argparse
import multiprocessing as mp
import os
import zipfile
from pathlib import Path

from evals.channel_task_cases import (
    CONTROL_BY_CASE,
    IDS,
    MODEL_PROFILE,
    SOURCE,
    VERSION,
    frontend_hashes,
    load_cases,
)
from evals.channel_task_evidence import score
from evals.common import MODEL, ROOT, digest, inside, phase1_audit, read, write
from evals.run import manifest as execution_manifest
from evals.run import run_trial
from evals.trace import analyze_trace


def case_for(cid, mode, control=None):
    case = next(case for case in load_cases() if case["case_id"] == cid)
    case.update(mode=mode, channel_control=control)
    return case


def build_plan(case, mode, control=None, repeat_index=1):
    if (mode not in {"D", "R"} or type(repeat_index) is not int or not 1 <= repeat_index <= 3
            or control is not None and (mode != "D" or control not in CONTROL_BY_CASE[case["case_id"]])):
        raise ValueError("unsupported_channel_plan")
    return [{"trial_id": case["case_id"] + "-" + str(repeat_index), "case_id": case["case_id"],
        "case_version": VERSION, "repeat_index": repeat_index, "profile": mode + "-" + VERSION,
        "purpose": "diagnostic" if mode == "D" else "primary", "fault": control,
        "original_trial_id": None, "scope": "full_task", "entry": case["entry"]["method"],
        "mode": mode, "setup_timeout_s": 90, "product_timeout_s": 120 if mode == "D" else 240}]


def aggregate(case, evidence, program, mode, control):
    trace = analyze_trace(evidence["events"], evidence["execution_status"], evidence["collector_errors"])
    real = (mode == "R" and case["case_id"] != "V065" and trace["model_attempts"] > 0
        and not any(event["type"] == "scripted_response" for event in evidence["events"]))
    gates = {"program_replayed": program == score(case, evidence), "program_pass": program["status"] == "pass",
        "source_model": mode == "D" or all(event["payload"].get("requested_model") == MODEL
            for event in evidence["events"] if event["type"] == "model_attempt" and event["phase"] == "start"),
        "trace": trace["valid"] and trace["closed"], "real_execution": real,
        "usage": mode == "D" or trace["actual_tokens"] is not None and trace["unknown_usage_attempts"] == 0
            and trace["unknown_sdk_coverage_calls"] == 0,
        "budget": trace["model_attempts"] <= 8 and trace["known_tokens"] <= 32000
            and trace["tool_requested"] <= 16 and not any(event["type"] == "budget" for event in evidence["events"])}
    status = program["status"]
    if status == "pass" and not all(value for name, value in gates.items()
                                   if name != "real_execution" or mode == "R"):
        status = "invalid"
    return {"version": VERSION, "scope": "full_task" if control is None else "diagnostic_control",
        "case_id": case["case_id"], "trial_id": evidence["trial_id"], "status": status,
        "program_verdict": program, "gates": gates, "trace": trace,
        "formal_quality_eligible": mode == "R" and control is None and status == "pass" and all(gates.values()),
        "grading_model_requests": 0, "grading": "Independent program: codes, dates, source reads and rendered state have deterministic standards.",
        "qualification": "Raw eligibility additionally requires a successful independent offline audit."}


def expected_status(cid, mode, control):
    if cid == "V065":
        return "undetermined" if mode == "R" else "pass" if control in {
            "image_ingress", "api_text_control", "raw_post"} else "product_fail"
    if cid == "V063":
        return "product_fail" if control else "pass"
    return "pass"


def sources_for(base, case):
    sources = base["snapshot"]["file_sha256"]
    sources[SOURCE] = digest((ROOT / SOURCE).read_bytes())
    # load_cases validates all three contracts, including the frontend dependency.
    # Archive it for every case so an isolated offline replay needs no live files.
    sources.update(frontend_hashes())
    for name in ("webui/package.json", "webui/package-lock.json", "webui/vitest.config.ts", "webui/tsconfig.json"):
        path = ROOT / name
        if path.exists():
            sources[name] = digest(path.read_bytes())
    return sources


def run(mode, output, cid, control=None, repeat_index=1, frozen_plan=None):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    case = case_for(cid, mode, control)
    plan = build_plan(case, mode, control, repeat_index)
    output = inside(ROOT, Path(output).resolve().relative_to(ROOT))
    output.mkdir(parents=True, exist_ok=False)
    base = execution_manifest([case], mode, plan, output.name, phase1_audit())
    sources = sources_for(base, case)
    frozen_ref = None
    if mode == "R":
        if frozen_plan is None:
            raise ValueError("R_requires_frozen_channel_plan")
        frozen_path = inside(ROOT, Path(frozen_plan).resolve().relative_to(ROOT))
        frozen = read(frozen_path)
        target = next(row for row in frozen["runs"] if row["case_id"] == cid and row["repeat_index"] == repeat_index)
        if (target["case"] != case or target["plan"] != plan[0] or target["sources"] != sources
                or output != frozen_path.parent / target["directory"]):
            raise ValueError("frozen_R_plan_mismatch")
        write(output / "frozen-contract.json", frozen)
        frozen_ref = {"path": frozen_path.relative_to(ROOT).as_posix(), "sha256": digest(frozen_path.read_bytes())}
    manifest = {"version": VERSION, "scope": "full_task", "run_id": output.name, "mode": mode,
        "control": control, "repeat_index": repeat_index, "case": case, "plan": plan,
        "sources": sources, "snapshot": base["snapshot"], "frozen_contract": frozen_ref,
        "requested_model": MODEL if mode == "R" else None, "model_profile": MODEL_PROFILE,
        "environment": {**base["environment"], "network": "loopback_test_transports_and_R_provider_only",
            "platform_connection": False, "OS_sandbox": False,
            "renderer": "actual_React_hook_jsdom; no_real_browser" if cid == "V066" else "local_channel_protocol"},
        "budget": {"attempts": 8, "tools": 16, "known_tokens": 32000, "seconds": plan[0]["product_timeout_s"]},
        "grading": {"mode": "independent_program", "model_requests": 0, "money": 0},
        "qualification": "Execution/read/identity/render/usage/version plus offline audit; original standards unchanged."}
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in sources.items():
            data = inside(ROOT, name).read_bytes()
            if digest(data) != sha:
                raise ValueError("source_changed")
            archive.writestr(name, data)
    isolation = ROOT / "tmp/channel-task-instances"
    isolation.mkdir(parents=True, exist_ok=True)
    run_trial(case, plan[0], output, isolation, mode, mp.get_context("spawn"))
    trial = output / "trials" / plan[0]["trial_id"]
    evidence = read(trial / "artifacts/evidence.json")
    program = read(trial / "verdicts/1.json")
    verdict = aggregate(case, evidence, program, mode, control)
    write(trial / "full-task-verdict.json", verdict)
    trace = verdict["trace"]
    result = {"version": VERSION, "case_id": cid, "mode": mode, "control": control,
        "status": verdict["status"], "formal_quality_eligible": verdict["formal_quality_eligible"],
        "ok": verdict["status"] == expected_status(cid, mode, control),
        "product_requests": trace["model_attempts"], "product_known_tokens": trace["known_tokens"],
        "product_actual_tokens": trace["actual_tokens"], "product_amount": None if mode == "R" else 0,
        "currency": None, "grading_requests": 0, "grading_amount": 0}
    write(output / "run-result.json", result)
    write(output / "artifact-index.json", [{"path": path.relative_to(output).as_posix(), "sha256": digest(path.read_bytes())}
        for path in sorted(output.rglob("*")) if path.is_file()])
    print(mode, cid, control, verdict["status"], "eligible", verdict["formal_quality_eligible"], flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["D", "R"], required=True)
    parser.add_argument("--case", choices=IDS, required=True)
    parser.add_argument("--control")
    parser.add_argument("--repeat-index", type=int, default=1)
    parser.add_argument("--frozen-plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run(args.mode, args.output, args.case, args.control, args.repeat_index, args.frozen_plan)
    raise SystemExit(0 if summary["ok"] else 1)
