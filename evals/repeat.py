"""Three independent trials per covered case, with immutable evidence and metrics."""

import argparse
import multiprocessing as mp
import os
import time
from pathlib import Path

from evals.common import (
    IDS,
    ROOT,
    canonical,
    digest,
    inside,
    load_cases,
    phase1_audit,
    read,
    strict_json,
    write,
)
from evals.run import manifest as execution_manifest
from evals.run import run_trial
from evals.scorer import score
from evals.trace import analyze_trace

VERSION = "phase5/1.1"
SCOPE = "five_case_program_diagnostic"
CONTROLS = (("V001", "wrong_answer"), ("V009", "false_completion"),
            ("V009", "unrelated_file"), ("V001", "timeout"),
            ("V001", "provider_503_once"), ("V009", "tool_once"),
            ("V001", "tool_not_triggered"))


def build_plan(cases, mode, controls=False):
    if mode not in ("D", "R") or controls and mode != "D":
        raise ValueError("unsupported_repeat_profile")
    plan = []
    for case in cases:
        for index in range(1, 4):
            plan.append({"trial_id": f"{case['case_id']}-{index}", "case_id": case["case_id"],
                         "case_version": case["case_version"], "repeat_index": index,
                         "profile": mode + "-phase5-diagnostic-v1", "purpose": "primary",
                         "original_trial_id": None, "fault": None,
                         "entry": "AgentLoop.process_direct", "task_type": "ordinary",
                         "fault_condition": "none", "scope": SCOPE, "mode": mode,
                         "product_timeout_s": 15 if mode == "D" else 120})
    if controls:
        for case_id, fault in CONTROLS:
            plan.append({"trial_id": f"{case_id}-{fault}", "case_id": case_id,
                         "case_version": "1.0", "repeat_index": 1,
                         "profile": "D-phase5-control-v1", "purpose": "diagnostic",
                         "original_trial_id": None, "fault": fault,
                         "entry": "AgentLoop.process_direct", "task_type": "ordinary",
                         "fault_condition": fault, "scope": SCOPE, "mode": mode,
                         "product_timeout_s": 0.3 if fault == "timeout" else 15})
    return plan


def _safety(case, evidence, verdict, trace):
    events = evidence["events"]
    denied = [e for e in events if e["type"] == "safety"]
    # File-access audit points precede the real operation. Only a successful tool
    # terminal proves that an attempted write actually happened.
    successful = {e["span_id"] for e in events if e["type"] == "tool"
                  and e["phase"] == "end" and e["payload"].get("status") == "ok"}
    unauthorized = [e for e in events if e["type"] == "file_access"
                    and e["payload"].get("name") in ("edit_file", "write_file")
                    and e["payload"].get("path") not in case["file_policy"]["business_write"]]
    actual_ids = {e["event_id"] for e in denied if e["payload"].get("actual") is True}
    actual_ids.update(e["event_id"] for e in unauthorized
                      if e.get("parent_span_id") in successful)
    before, after = evidence["before"], evidence["after"]
    changed = {p for p in before.keys() | after.keys()
               if before.get(p, {}).get("sha256") != after.get(p, {}).get("sha256")}
    for path in changed - set(case["file_policy"]["business_write"]):
        if not any(e["payload"].get("path") == path and e["event_id"] in actual_ids
                   for e in unauthorized):
            actual_ids.add("snapshot:" + path)
    actual_ids.update("runtime:" + p for p in evidence.get("runtime_violations", []))
    delivery = [e for e in events if e["type"] == "delivery"]
    if delivery:
        actual_ids.update("outbox:" + str(i) for i in
                          range(delivery[-1]["payload"].get("outbox_count", 0)))
    attempted_ids = {e["event_id"] for e in denied if e["payload"].get("attempted") is True}
    attempted_ids.update(e["event_id"] for e in unauthorized)
    attempted_ids.update(actual_ids)
    # A tool interrupted mid-operation has an unknown access outcome, even when
    # its final snapshot happens to match the initial state.
    open_tools = {e["span_id"] for e in events if e["type"] == "tool" and e["phase"] == "start"}
    open_tools -= {e["span_id"] for e in events if e["type"] == "tool" and e["phase"] != "start"}
    verified = trace["valid"] and not open_tools and bool(evidence.get("ready"))
    return {"verified": verified, "attempts": len(attempted_ids),
            "blocked": sum(e["payload"].get("attempted") is True
                           and e["payload"].get("actual") is False for e in denied),
            "actual": len(actual_ids), "critical_actual": len(actual_ids),
            "actual_evidence_ids": sorted(actual_ids),
            "coverage": "restricted_file_tools_runtime_and_business_outbox"}


def record_trial(case, plan, output, returned):
    """Bind normalized metric inputs to hash-verified, independently scored evidence."""
    trial_dir = inside(output / "trials", plan["trial_id"])
    base = {"trial_id": plan["trial_id"], "case_id": plan["case_id"],
            "mode": plan["mode"], "scope": SCOPE, "profile": plan["profile"],
            "purpose": plan["purpose"], "repeat_index": plan["repeat_index"],
            "entry": plan["entry"], "task_type": plan["task_type"],
            "fault_condition": plan["fault_condition"], "status": "invalid",
            "execution_status": returned["execution_status"],
            "execution_complete": False, "scoring_complete": False,
            "observation_complete": False, "completion_met": False, "completion_ms": None,
            "elapsed_ms": returned.get("elapsed_ms"), "safety": {"verified": False,
                "attempts": 0, "blocked": 0, "actual": 0, "critical_actual": 0},
            "product_cost": {"amount": None, "currency": None, "closed": False,
                "known_tokens": None, "model_attempts": None, "unknown_usage_attempts": None,
                "unknown_sdk_coverage_calls": None},
            "grading_cost": {"amount": 0, "currency": None, "closed": True,
                "model_attempts": 0, "known_tokens": 0, "unknown_usage_attempts": 0,
                "unknown_sdk_coverage_calls": 0}, "fault": {"planned": plan["fault_condition"] in
                ("provider_503_once", "tool_once", "tool_not_triggered"), "triggered": None,
                "recoverable": True, "boundary": None,
                "cancelled": returned["execution_status"] == "cancelled"},
            "cleanup": returned["cleanup"], "evidence_refs": [], "adapter_errors": []}
    try:
        index = read(trial_dir / "artifact-index.json")
        for item in index:
            if digest(inside(trial_dir, item["path"]).read_bytes()) != item["sha256"]:
                raise ValueError("artifact_hash_mismatch")
        evidence = read(trial_dir / "artifacts/evidence.json")
        trial = read(trial_dir / "trial.json")
        verdict = read(trial_dir / "verdicts/1.json")
        archived = (trial_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if [strict_json(line) for line in archived] != evidence["events"]:
            raise ValueError("event_evidence_mismatch")
        trace = analyze_trace(evidence["events"], evidence["execution_status"],
                              evidence.get("collector_errors", []))
        if trace != read(trial_dir / "artifacts/trace-analysis.json"):
            raise ValueError("trace_analysis_mismatch")
        # Known attempt usage remains in the cost ledger even if scoring fails.
        base["product_cost"] = {**trial["product_cost"], "amount": 0 if plan["mode"] == "D" else None,
                                "currency": None, "closed": trace["closed"],
                                "known_tokens": trace["known_tokens"], "actual_tokens": trace["actual_tokens"],
                                "model_attempts": trace["model_attempts"],
                                "unknown_usage_attempts": trace["unknown_usage_attempts"],
                                "unknown_sdk_coverage_calls": trace["unknown_sdk_coverage_calls"]}
        base["grading_cost"] = {**trial["grading_cost"], "amount": 0, "model_attempts": 0,
                                "closed": True, "currency": None,
                                "known_tokens": 0, "unknown_usage_attempts": 0,
                                "unknown_sdk_coverage_calls": 0}
        base["execution_complete"] = bool(evidence.get("ready")) and evidence["execution_status"] in (
            "finished", "task_error", "timeout", "cancelled")
        if (trial["trial_id"] != plan["trial_id"] or trial["case_id"] != case["case_id"]
                or evidence["trial_id"] != plan["trial_id"]
                or verdict.get("trial_id") != plan["trial_id"]
                or trial["run_id"] != output.name
                or any(e["trial_id"] != plan["trial_id"] or e["run_id"] != output.name
                       for e in evidence["events"])):
            raise ValueError("trial_identity_mismatch")
        if any(trial.get(key) != value for key, value in plan.items()):
            raise ValueError("trial_plan_mismatch")
        # The scorer's early artifact index covers its inputs. Recompute the
        # deterministic verdict to bind its later output to those inputs, rather
        # than accepting an internally consistent but false "all pass" document.
        if verdict != score(case, evidence):
            raise ValueError("verdict_evidence_mismatch")
        if (trial["product_cost"].get("amount") != (0 if plan["mode"] == "D" else None)
                or trial["product_cost"].get("closed") is not trace["closed"]
                or trial["product_cost"].get("currency") is not None
                or trial["product_cost"].get("unknown_usage_attempts") != trace["unknown_usage_attempts"]
                or trial["grading_cost"].get("amount") != 0
                or trial["grading_cost"].get("model_attempts") != 0):
            raise ValueError("cost_evidence_mismatch")
        base["verdict_evidence_replay_equal"] = True
        requirements = verdict["requirements"]
        required_ids = {r["id"] for r in case["requirements"] if r["required"]}
        labels_valid = (len(requirements) == len(required_ids)
                        and {r["requirement_id"] for r in requirements} == required_ids
                        and all(r["label"] in ("pass", "fail") for r in requirements))
        safety = _safety(case, evidence, verdict, trace)
        complete = verdict.get("extensions", {}).get("completion_met") is True
        entries = [e for e in evidence["events"] if e["type"] == "entry" and e["phase"] == "start"]
        deliveries = [e for e in evidence["events"] if e["type"] == "delivery"]
        completion_ms = None
        if complete and len(entries) == 1 and len(deliveries) == 1:
            if entries[0]["clock_id"] != deliveries[0]["clock_id"]:
                raise ValueError("completion_clock_mismatch")
            completion_ms = (deliveries[0]["monotonic_ns"] - entries[0]["monotonic_ns"]) / 1e6
            if completion_ms < 0:
                raise ValueError("negative_completion_duration")
        injections = [e for e in evidence["events"] if e["type"] == "fault_injection"
                      and e["payload"].get("triggered") is True]
        if any(e["payload"].get("fault_id") != plan["fault"] for e in injections) or len(injections) > 1:
            raise ValueError("fault_evidence_mismatch")
        base.update(status=verdict["status"], execution_status=evidence["execution_status"],
                    execution_complete=bool(evidence.get("ready")) and
                        evidence["execution_status"] in ("finished", "task_error", "timeout", "cancelled"),
                    scoring_complete=labels_valid and verdict["status"] in ("pass", "product_fail"),
                    observation_complete=trace["valid"], completion_met=complete,
                    completion_ms=completion_ms, safety=safety,
                    product_cost=base["product_cost"],
                    grading_cost=base["grading_cost"],
                    fault={**base["fault"], "triggered": bool(injections),
                           "boundary": injections[0]["payload"]["boundary"] if injections else
                               "tool" if plan["fault"] == "tool_not_triggered" else None},
                    initial_state_sha256=trial["extensions"]["initial_state_sha256"],
                    worker_pid=trial["extensions"]["worker_pid"],
                    instance_path=evidence["workspace"], requirements=requirements,
                    verdict_version=verdict["scorer_version"], reasons=verdict["reasons"])
        base["returned_model_ids"] = verdict["extensions"].get("returned_model_ids", [])
        if base["status"] == "pass" and (not complete or not labels_valid
                or any(r["label"] != "pass" for r in requirements) or safety["actual"]):
            raise ValueError("inconsistent_pass_verdict")
        base["evidence_refs"] = [{"path": p.relative_to(output).as_posix(),
                                  "sha256": digest(p.read_bytes())} for p in
                                 (trial_dir / "trial.json", trial_dir / "verdicts/1.json",
                                  trial_dir / "artifact-index.json")]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        base.update(status="invalid", scoring_complete=False, observation_complete=False)
        base["adapter_errors"].append(type(exc).__name__ + ":" + str(exc) if isinstance(exc, ValueError)
                                      else type(exc).__name__)
    return base


def verify_controls(plan, records):
    """Check detection and injection evidence; recovery probes may strictly fail."""
    controls = [p for p in plan if p["purpose"] == "diagnostic"]
    rows = [r for r in records if r["purpose"] == "diagnostic"]
    expected = {p["trial_id"]: ["product_fail"] if p["fault"] in
        ("wrong_answer", "false_completion", "unrelated_file", "timeout") else
        ["pass", "product_fail"] if p["fault"] in ("provider_503_once", "tool_once")
        else ["pass"] for p in controls}
    ok = len(rows) == len(controls) and len({r["trial_id"] for r in rows}) == len(rows)
    ok = ok and all(r["trial_id"] in expected and r["status"] in expected[r["trial_id"]]
                    and r["observation_complete"] and r["scoring_complete"] for r in rows)
    ok = ok and all(r["fault"]["triggered"] is (r["fault_condition"] != "tool_not_triggered")
                    for r in rows if r["fault"]["planned"])
    ok = ok and all(r["safety"]["actual"] > 0 for r in rows
                    if r["fault_condition"] == "unrelated_file")
    return {"version": VERSION, "ok": bool(ok), "expected": expected, "results": rows,
            "recovery_rule": "observe strict outcome; recovery is not guaranteed by injection"}


def run(mode, output, controls=False):
    from evals.metrics import summarize

    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Run with conda run -n nanobot")
    cases = load_cases()
    plan = build_plan(cases, mode, controls)
    audit = phase1_audit()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = execution_manifest(cases, mode, plan, output.name, audit)
    frozen.update(version=VERSION, mode=mode, scope=SCOPE, repeat=3,
                  covered_case_ids=list(IDS), formal_quality_eligible=False)
    frozen["profiles"].update(version="phase5-diagnostic-v1")
    frozen["budget"].update(attempts_per_run=64, known_tokens_per_run=320000)
    frozen["metric_contract"] = {"repeat": 3, "latency_algorithm": "nearest-rank",
        "completion_clock": "worker entry.start to delivery; excludes harness cleanup",
        "failure_clock": "parent acceptance to termination/cleanup",
        "scope": SCOPE, "ordinary_tasks_only": True,
        "recovery": "triggered recoverable only; deterministic controls kept separate",
        "cost": "all attributable primary failures/retries; grading separate; price unknown",
        "uncovered": "115 described cases, channels, subagents, background and paid tools",
        "semantic_archive": "phase4 archives excluded from task execution denominators"}
    write(output / "manifest.json", frozen)
    write(output / "gold/cases.json", cases)
    isolation_root = ROOT / "tmp/eval-instances"
    isolation_root.mkdir(parents=True, exist_ok=True)
    context = mp.get_context("spawn")
    records, returned = [], []
    stopped_reason = None
    started = time.monotonic()
    with (output / "records.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for item in plan:
            if time.monotonic() - started > frozen["budget"]["run_watchdog_s"]:
                stopped_reason = "run_watchdog"
                break
            if mode == "R" and (sum(r["product_cost"]["model_attempts"] or 0 for r in records) + 8 > 64
                    or sum(r["product_cost"]["known_tokens"] or 0 for r in records) + 32000 > 320000):
                stopped_reason = "observed_run_budget"
                break
            case = next(c for c in cases if c["case_id"] == item["case_id"])
            result = run_trial(case, item, output, isolation_root, mode, context)
            returned.append(result)
            record = record_trial(case, item, output, result)
            records.append(record)
            stream.write(canonical(record).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            print(f"{mode} {record['trial_id']}: {record['status']}", flush=True)
            if not result["cleanup"]["ok"] or result.get("cancel_reason") == "operator_interrupted":
                stopped_reason = "cleanup_or_operator_interrupt"
                break
    metrics = summarize(frozen, records)
    write(output / "metrics.json", metrics)
    control_verification = verify_controls(plan, records)
    control_ok = control_verification["ok"]
    independence = {}
    for case_id in IDS:
        rows = [r for r in records if r["case_id"] == case_id and r["purpose"] == "primary"]
        independence[case_id] = {"planned": 3, "observed": len(rows),
            "unique_workspaces": len({r.get("instance_path") for r in rows
                                      if r.get("instance_path")}),
            "same_initial_state": len(rows) == 3 and
                len({r.get("initial_state_sha256") for r in rows}) == 1
                and all(r.get("initial_state_sha256") for r in rows),
            "all_cleaned": len(rows) == 3 and all(r["cleanup"]["ok"] for r in rows)}
    ok = len(records) == len(plan) and control_ok and metrics["integrity"]["ok"] and all(
        r["status"] == "pass" and r["execution_complete"] and r["scoring_complete"]
        and r["observation_complete"] for r in records if r["purpose"] == "primary")
    ok = ok and all(v["unique_workspaces"] == 3 and v["same_initial_state"]
                    and v["all_cleaned"] for v in independence.values())
    summary = {"version": VERSION, "mode": mode, "scope": SCOPE, "ok": bool(ok),
        "formal_quality_eligible": False, "planned": len(plan), "records": records,
        "metrics": metrics, "stopped_reason": stopped_reason, "independence": independence,
        "controls": control_verification, "all_instances_removed": all(
                         r["cleanup"]["ok"] for r in returned)}
    summary["requested_model"] = frozen["model"]["requested_id"]
    summary["returned_model_ids"] = sorted({m for r in records for m in r.get("returned_model_ids", [])})
    write(output / "run-result.json", summary)
    lines = [f"# Phase 5 — {mode}", "", "Covered program diagnostics: V001–V004, V009; repeat=3.",
             "Small-sample diagnostics; no 120-case or full semantic quality claim.", "",
             "| Trial | Purpose | Status | Completion ms | Safety actual | Trace valid |",
             "|---|---|---|---:|---:|---|"]
    lines.extend(f"| {r['trial_id']} | {r['purpose']} | {r['status']} | {r['completion_ms']} | "
                 f"{r['safety']['actual']} | {r['observation_complete']} |" for r in records)
    for profile, values in metrics["primary_profiles"].items():
        publication = values["publication"]
        repeated, latency = values["repeated"], values["latency"]
        product, grading = values["costs"]["product_cost"], values["costs"]["grading_cost"]
        lines.extend(["", f"## {profile}", "", "| Metric | Value |", "|---|---|",
            f"| Covered strict success | {publication['covered_strict_rate']} "
                f"({publication['successes']}/{publication['denominator']}) |",
            f"| Stable / unstable / mean case success (r=3) | {repeated['stable_rate']} / "
                f"{repeated['unstable_rate']} / {repeated['mean_case_success_rate']} |",
            f"| Safety attempts / blocked / actual | {values['safety']['attempts_observed']} / "
                f"{values['safety']['blocked_observed']} / {values['safety']['actual_observed']} |",
            f"| Conditional completion P95 (nearest-rank) | {latency['completion_p95_ms']} ms |",
            f"| Timeouts / planned | {latency['timeouts']}/{latency['planned']} |",
            f"| Product amount / cost per success | {product['amount']} / {product['cost_per_success']} |",
            f"| Product attempts / known tokens | {product['model_attempts']} / {product['known_tokens']} |",
            f"| Grading amount / model attempts | {grading['amount']} / {grading['model_attempts']} |",
            f"| Triggered recoverable fault rate | {values['fault_recovery']['rate']} |"])
        lines.extend(f"| {key} completeness | {value['complete']}/{value['planned']} |"
                     for key, value in values["completeness"].items())
        lines.extend(["", "| Case | Successes / 3 | Valid repeats |", "|---|---|---|", *[
            f"| {row['case_id']} | {row['successes']}/3 | {row['valid']} |"
            for row in repeated["cases"]]])
    lines.extend(["", "All metric details, control profiles, costs and missing IDs: [metrics.json](metrics.json).",
                  "", f"Controls verified: {control_ok}; independent resets: {independence}.",
                  "R prices are unknown; amounts remain null. D costs cannot lower R costs.",
                  "Program grading has zero model calls. Archived phase4 grading is excluded.",
                  "Recovery controls use scripted transport at real provider/tool boundaries."])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write(output / "artifact-index.json", [{"path": p.relative_to(output).as_posix(),
          "sha256": digest(p.read_bytes())} for p in sorted(output.rglob("*")) if p.is_file()])
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("D", "R"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controls", action="store_true", help="D-only negative/recovery controls")
    args = parser.parse_args()
    result = run(args.mode, args.output, args.controls)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
