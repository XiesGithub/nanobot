"""One command prepares, executes, independently scores, reports and cleans five cases."""

import argparse
import multiprocessing as mp
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import psutil

from evals.common import (
    IDS,
    MODEL,
    ROOT,
    SCHEMA,
    VERSION,
    canonical,
    digest,
    inside,
    load_cases,
    now,
    phase1_audit,
    snapshot,
    write,
)
from evals.scorer import scorer_main
from evals.trace import TRACE_VERSION, analyze_trace
from evals.worker import Emitter, worker_main


def script_for(case, fault=None):
    """Control responses only; never used in R mode."""
    if fault == "timeout":
        return [{"sleep": 60, "content": "unreachable"}]
    if fault == "wrong_answer":
        return [{"content": '{"total":360}'}]
    if fault == "false_completion":
        return [{"content": "已完成，timeout 已改为60。"}]
    if case["case_id"] != "V009":
        answer = next(r["expected"] for r in case["requirements"] if r["predicate"] == "json_equal")
        return [{"content": canonical(answer).decode("utf-8")}]
    calls = [{"name": "edit_file", "arguments": {"path": "config.json",
                                                   "old_text": '"timeout":30',
                                                   "new_text": '"timeout":60'}}]
    if fault == "unrelated_file":
        calls.append({"name": "write_file", "arguments": {"path": "unrelated.txt",
                                                              "content": "unauthorized"}})
    responses = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "config.json"}}]},
                 {"tool_calls": calls}]
    if fault == "tool_once":
        responses.append({"tool_calls": calls})
    return [*responses, {"content": "已完成。"}]


def stop_process(process):
    """Only the owned worker tree; never terminate by executable name."""
    descendants = []
    if process.pid:
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            pass
    for child in reversed(descendants):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.is_alive():
        process.terminate()
    process.join(3)
    if process.is_alive():
        process.kill()
        process.join(2)
    _, alive = psutil.wait_procs(descendants, timeout=2)
    return not process.is_alive() and not alive


def cleanup_instance(instance, isolation_root):
    target = Path(instance).resolve()
    root = Path(isolation_root).resolve()
    if target == root or not target.is_relative_to(root) or target.is_symlink():
        raise ValueError("unsafe_cleanup_target")
    shutil.rmtree(target)
    return not target.exists()


def _failure_details(exc, operation, relative_path, location):
    return {"error_kind": type(exc).__name__, "operation": operation,
            "relative_path": relative_path, "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None), "location": location}


def run_trial(case, plan, run_dir, isolation_root, mode, context):
    instance = isolation_root / (plan["trial_id"] + "-" + uuid.uuid4().hex)
    try:
        return _run_trial(case, plan, run_dir, isolation_root, mode, context, instance)
    except Exception as exc:
        removed = not instance.exists()
        cleanup_error = None
        if not removed:
            try:
                removed = cleanup_instance(instance, isolation_root)
            except OSError as cleanup_exc:
                cleanup_error = _failure_details(
                    cleanup_exc, "remove_instance", instance.name, "evals.run.cleanup_instance")
        record = {"schema_version": SCHEMA, "trial_id": plan["trial_id"],
                  "case_id": case["case_id"], "purpose": plan["purpose"], "fault": plan["fault"],
                  "execution_status": "setup_error", "status": "invalid",
                  "reasons": [{"reason_code": "harness_error", **_failure_details(
                      exc, "run_trial", instance.name, "evals.run._run_trial")}],
                  "elapsed_ms": None, "initial_state_sha256": None,
                  "cleanup": {"ok": removed, "instance_removed": removed,
                              "error": cleanup_error}, "extensions": {}}
        write(run_dir / "trials" / plan["trial_id"] / "harness-error.json", record)
        return record


def _run_trial(case, plan, run_dir, isolation_root, mode, context, instance):
    trial_id = plan["trial_id"]
    trial_dir = run_dir / "trials" / trial_id
    trial_dir.mkdir(parents=True)
    # Python 3.13 mkdtemp's Windows 0700 ACL can exclude the sandbox token.
    # Unique directories inherit the already-restricted workspace ACL instead.
    instance.mkdir()
    workspace, runtime = instance / "workspace", instance / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    write(runtime / "config.json", {"model": MODEL, "tools": list(case["tools"]),
                                     "profile": plan["profile"], "credentials": "provider_memory_only"})
    for fixture in case["initial_state"]["fixtures"]:
        destination = inside(workspace, fixture["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(ROOT / "evals", fixture["source"]), destination)
    parent, child = context.Pipe()
    options = {"run_id": run_dir.name, "trial_id": trial_id, "mode": mode,
               "workspace": str(workspace), "runtime": str(runtime),
               "input": case["turns"][0]["input"]}
    scoring_method = case.get("scoring", {}).get("method")
    mail_full_task = scoring_method == "mail-full-task/1.0"
    security_full_task = scoring_method == "security-full-task/1.0"
    privacy_full_task = scoring_method == "privacy-family-full-task/1.0"
    channel_full_task = scoring_method == "channel-full-task/1.0"
    if channel_full_task:
        options.update(case=case, case_id=case["case_id"], channel_control=plan.get("fault"))
    if privacy_full_task:
        options.update(case_id=case["case_id"], privacy_fixture=case["privacy_fixture"],
                       privacy_control=plan.get("fault"), case=case)
    if security_full_task:
        for name, text in case["security_fixture"].get("files", {}).items():
            destination = inside(workspace, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        options.update(case_id=case["case_id"], security_fixture=case["security_fixture"],
                       security_control=plan.get("fault"),
                       expected_tools=case["tools"], case=case)
    if scoring_method == "full_task_semantic_v1":
        options["full_task_semantic"] = True
    if mail_full_task:
        options["case_id"] = case["case_id"]
        options["mail_fixture"] = case["mail_fixture"]
        options["mail_control"] = plan.get("fault")
        if case.get("encoder_pin"):
            options["encoder_pin"] = case["encoder_pin"]
    if mode == "D":
        if not mail_full_task and not security_full_task and not privacy_full_task and not channel_full_task:
            options["script"] = plan.get("script") or script_for(case, plan["fault"])
        options["fault"] = plan["fault"] if plan["fault"] in (
            "provider_503_once", "tool_once", "tool_not_triggered",
            "save_open", "save_replace", "save_replace_cleanup") else None
    else:
        options["credentials"] = str(ROOT / "api.txt")
    if channel_full_task:
        from evals.channel_task_worker import worker_main as selected_worker
    elif privacy_full_task:
        from evals.privacy_task_worker import worker_main as selected_worker
    elif mail_full_task:
        from evals.mail_task_worker import worker_main as selected_worker
    elif security_full_task:
        from evals.security_task_worker import worker_main as selected_worker
    else:
        selected_worker = worker_main
    process = context.Process(target=selected_worker, args=(child, options))
    events, errors = [], []
    before = snapshot(workspace, contents=True)
    runtime_before = snapshot(runtime)
    ready, accepted, terminal_at = None, None, None
    observation_summary = None
    status = "setup_error"
    cancel_reason = None
    started = time.monotonic()
    product_timeout = plan["product_timeout_s"]
    # Pipe data is persisted by the trusted parent, never by agent filesystem tools.
    stream = (trial_dir / "events.jsonl").open("x", encoding="utf-8", newline="\n")

    class Collector:
        def send(self, message):
            event = message["event"]
            events.append(event)
            try:
                stream.write(canonical(event).decode("utf-8") + "\n")
                stream.flush()
            except Exception as exc:
                errors.append({"reason": "jsonl_write_failed", "error_kind": type(exc).__name__,
                               "event_id": event["event_id"]})

    collector = Collector()
    observer = Emitter(collector, run_dir.name, trial_id)
    observer.producer = "parent-" + observer.producer
    trial_span = observer.event("trial", "start", profile=plan["profile"])
    options["parent_span_id"] = trial_span
    try:
        process.start()
        child.close()
        while True:
            elapsed = time.monotonic() - (accepted or started)
            limit = product_timeout if accepted else plan.get("setup_timeout_s", 30)
            if elapsed > limit and terminal_at is None:
                status = "timeout" if accepted else "setup_error"
                observer.event("budget", reason="product_timeout" if accepted else "setup_timeout",
                               elapsed_s=elapsed, limit_s=limit)
                break
            if terminal_at and time.monotonic() - terminal_at > 10:
                errors.append("worker_did_not_exit")
                break
            try:
                available = parent.poll(0.05)
            except (EOFError, BrokenPipeError, OSError):
                break
            if available:
                try:
                    message = parent.recv()
                except EOFError:
                    break
                if message["message"] == "ready":
                    ready = message
                    before = snapshot(workspace, contents=True)
                    runtime_before = snapshot(runtime)
                    observer.event("snapshot", phase="point", moment="before",
                                   sha256=digest(canonical(before)), empty_session=True)
                    accepted = time.monotonic()
                    status = "running"
                    parent.send("go")
                elif message["message"] == "event":
                    event = message["event"]
                    collector.send(message)
                    if event["type"] == "entry" and event["phase"] in ("end", "error", "cancelled"):
                        terminal_at = time.monotonic()
                        status = {"end": "finished", "error": "task_error",
                                  "cancelled": "cancelled"}[event["phase"]]
                elif message["message"] == "observation_summary":
                    observation_summary = message
                    errors.extend(message["collector_errors"])
                else:
                    errors.append("worker_error:" + message.get("error_kind", "unknown"))
            elif not process.is_alive():
                break
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            cancel_reason = "operator_interrupted"
            status = "cancelled"
            observer.event("operator_interrupt", error_kind=type(exc).__name__)
        else:
            errors.append("parent_error:" + type(exc).__name__)
            status = "crashed"
    finally:
        stopped = stop_process(process) if process.pid else True
        elapsed_ms = (time.monotonic() - (accepted or started)) * 1000
        parent.close()
        if status == "running":
            status = "crashed"
        open_spans = {}
        for item in events:
            if item["phase"] == "start":
                open_spans[item["span_id"]] = item
            elif item["phase"] in ("end", "error", "cancelled"):
                open_spans.pop(item["span_id"], None)
        if status in ("timeout", "crashed", "cancelled"):
            observer.event("watchdog", reason=status, process_stopped=stopped,
                           open_span_ids=[sid for sid in open_spans if sid != trial_span],
                           product_terminals_synthesized=False)
        elif observation_summary is None:
            errors.append("observation_summary_missing")
        if observation_summary:
            delivered = [e["seq"] for e in events
                         if e["producer_id"] == observation_summary["producer_id"]]
            if not delivered or max(delivered) != observation_summary["last_seq"]:
                errors.append("collector_tail_missing")
        try:
            after = snapshot(workspace, contents=True)
            runtime_after = snapshot(runtime)
        except Exception as exc:
            errors.append("snapshot_error:" + type(exc).__name__)
            after, runtime_after = {}, {}
        try:
            removed = cleanup_instance(instance, isolation_root)
        except Exception as exc:
            removed = False
            errors.append("cleanup_error:" + type(exc).__name__)
            observer.event("cleanup_error", **_failure_details(
                exc, "remove_instance", instance.name, "evals.run.cleanup_instance"))
        observer.event("snapshot", moment="after", sha256=digest(canonical(after)))
        observer.event("trial", "end" if status == "finished" else "error", trial_span,
                       execution_status=status, process_stopped=stopped, instance_removed=removed)
        stream.close()
    runtime_changed = {p for p in runtime_before.keys() | runtime_after.keys()
                       if runtime_before.get(p) != runtime_after.get(p)}
    allowed_runtime = set()
    if ready:
        allowed_runtime.update(ready.get("runtime_allowed_paths", []))
        if channel_full_task:
            prefixes = case["file_policy"]["runtime_allowed_prefixes"]
            if ready.get("runtime_allowed_prefixes") == prefixes:
                allowed_runtime.update(path for path in runtime_changed
                    if any(path.startswith(prefix) if prefix.endswith("/") else path == prefix
                           for prefix in prefixes))
        if not mail_full_task and not security_full_task and not channel_full_task:
            allowed_runtime.add("sessions/" + ready["session_file"])
    evidence = {"schema_version": SCHEMA, "trial_id": trial_id,
                "observation_version": TRACE_VERSION,
                "execution_status": status, "cancel_reason": cancel_reason,
                "events": events, "ready": ready,
                "before": before, "after": after, "runtime_before": runtime_before,
                "runtime_after": runtime_after,
                "runtime_violations": sorted(runtime_changed - allowed_runtime),
                "workspace": str(workspace), "elapsed_ms": elapsed_ms,
                "collector_errors": errors,
                "cleanup": {"ok": stopped and removed, "process_stopped": stopped,
                            "instance_removed": removed}}
    evidence["worker_process"] = {"exitcode": process.exitcode, "stopped": stopped}
    evidence["entry_phase"] = next((e["phase"] for e in reversed(events)
                                    if e["type"] == "entry" and e["phase"] != "start"), None)
    evidence["delivery_observed"] = any(e["type"] == "delivery" for e in events)
    trace = analyze_trace(events, status, errors)
    write(trial_dir / "artifacts/trace-analysis.json", trace)
    write(trial_dir / "artifacts/evidence.json", evidence)
    index = [{"path": p, "sha256": digest((trial_dir / p).read_bytes()),
              "media_type": "application/json", "producer": "trusted_parent"}
             for p in ("artifacts/evidence.json", "events.jsonl", "artifacts/trace-analysis.json")]
    write(trial_dir / "artifact-index.json", index)
    parent, child = context.Pipe()
    if channel_full_task:
        from evals.channel_task_evidence import scorer_main as selected_scorer
    elif privacy_full_task:
        from evals.privacy_task_evidence import scorer_main as selected_scorer
    elif mail_full_task:
        from evals.mail_task_evidence import scorer_main as selected_scorer
    elif security_full_task:
        from evals.security_task_evidence import scorer_main as selected_scorer
    else:
        selected_scorer = scorer_main
    scorer = context.Process(target=selected_scorer, args=(child, case, str(trial_dir)))
    scorer.start()
    child.close()
    result = parent.recv() if parent.poll(20) else {"ok": False, "error_kind": "scorer_timeout"}
    scorer.join(2)
    stop_process(scorer)
    parent.close()
    if not result["ok"]:
        result["verdict"] = {"status": "invalid", "requirements": [],
                             "reasons": [{"reason_code": result["error_kind"]}], "extensions": {}}
        write(trial_dir / "verdicts/1.json", result["verdict"])
    verdict = result["verdict"]
    cost = {"usage_coverage": "provider_call_boundary", "amount": 0 if mode == "D" else None,
            "currency": None, "closed": trace["closed"],
            "unknown_usage_attempts": trace["unknown_usage_attempts"],
            "unknown_reason": "prices_unknown; uninstrumented_components_out_of_scope"}
    trial = {"schema_version": SCHEMA, "run_id": run_dir.name, **plan,
             "execution_status": status, "cancel_reason": cancel_reason, "stop_reason": status,
             "accepted_event_id": next((e["event_id"] for e in events
                                        if e["type"] == "task" and e["phase"] == "start"), None),
             "terminal_event_id": next((e["event_id"] for e in reversed(events)
                                        if e["type"] == "task" and e["phase"] != "start"), None),
             "completion": {"met": verdict["extensions"].get("completion_met", False),
                            "elapsed_ms": elapsed_ms,
                            "event_ids": [e["event_id"] for e in events
                                          if (e["type"] == "task" and e["phase"] == "end")
                                          or e["type"] == "snapshot"],
                            "reason": "returned_and_required_write_observed"},
             "evidence": index, "observation": {
                 "expected": case["observation"]["required"],
                 "observed": dict(Counter(e["type"] for e in events)),
                 "missing": [r["reason_code"] for r in verdict["reasons"]
                             if "missing" in r["reason_code"]],
                 "collector_errors": errors, "closed": trace["closed"], "scope": "phase3",
                 "trace_analysis_ref": "artifacts/trace-analysis.json"},
             "budget_usage": verdict["extensions"], "product_cost": cost,
             "grading_cost": {"amount": 0, "model_attempts": 0}, "cleanup": evidence["cleanup"],
             "verdict_refs": ["verdicts/1.json"], "extensions": {
                 "initial_state_sha256": digest(canonical(before)), "worker_pid": process.pid}}
    write(trial_dir / "trial.json", trial)
    return {"trial_id": trial_id, "case_id": case["case_id"], "purpose": plan["purpose"],
            "fault": plan["fault"], "status": verdict["status"],
            "execution_status": status, "cancel_reason": cancel_reason,
            "reasons": verdict["reasons"], "elapsed_ms": round(elapsed_ms, 1),
            "initial_state_sha256": trial["extensions"]["initial_state_sha256"],
            "cleanup": evidence["cleanup"], **verdict["extensions"]}


def manifest(cases, mode, plan, run_id, audit):
    allow = []
    for folder in (ROOT / "nanobot", ROOT / "evals"):
        allow.extend(p for p in folder.rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".json"))
    allow.extend(ROOT / name for name in ("pyproject.toml", "uv.lock") if (ROOT / name).exists())
    hashes = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(allow)}
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    return {"schema_version": SCHEMA, "run_id": run_id, "created_at": now(),
            "data_kind": "measured", "snapshot": {"git_sha": git_sha, "dirty": dirty,
                                                  "file_sha256": hashes},
            "dataset": {"version": VERSION, "sha256": digest(canonical(cases)), "case_ids": list(IDS)},
            "scorer": {"version": TRACE_VERSION, "sha256": hashes["evals/scorer.py"]},
            "profiles": {"mode": mode, "version": "phase3-diagnostic-v1",
                         "token_pre_reservation": "not_verified_diagnostic_only",
                         "tools": list(cases[0]["tools"]), "concurrent_tools": False},
            "prices": None,
            "environment": {"os": platform.platform(), "conda": os.environ.get("CONDA_DEFAULT_ENV"),
                            "python": sys.version, "timezone": "Asia/Shanghai", "concurrency": 1,
                            "network": "R_provider_only; no_agent_network_tools",
                            "isolation": "separate_process_and_tool_boundary; not_OS_sandbox"},
            "plan": plan, "budget": {"run_watchdog_s": 600 if mode == "D" else 3600,
                                      "attempts_per_trial": 0 if mode == "D" else 8,
                                      "tokens_per_trial": 0 if mode == "D" else 32000},
            "seed": None, "ordering": "case_then_repeat_then_selfchecks",
            "model": {"requested_id": MODEL, "temperature": 0, "max_tokens": 2048,
                      "reasoning_effort": None, "thinking": "disabled", "sdk_retries": 0,
                      "provider_retry": "standard [1,2,4] bounded by watchdog/attempt cap"},
            "artifact_policy": "hashes_and_fictional_evidence_only; no_credentials_or_reasoning",
            "extensions": {"phase1_audit": audit, "formal_quality_eligible": False}}


def run(mode, output, repeat=1, selfcheck=False):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Run with conda run -n nanobot")
    if (mode not in ("R", "D") or repeat < 1 or repeat > 2
            or (selfcheck and mode != "D") or (mode == "R" and repeat != 1)):
        raise ValueError("unsupported_run_profile")
    cases = load_cases()
    audit = phase1_audit()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("output_must_be_new")
    output.mkdir(parents=True)
    isolation_root = ROOT / "tmp" / "eval-instances"
    isolation_root.mkdir(parents=True, exist_ok=True)
    plan = []
    for case in cases:
        for index in range(repeat):
            plan.append({"trial_id": f"{case['case_id']}-{index + 1}", "case_id": case["case_id"],
                         "case_version": case["case_version"], "repeat_index": index + 1,
                         "profile": mode + "-phase3-diagnostic-v1", "purpose": "primary",
                         "original_trial_id": None, "fault": None,
                         "product_timeout_s": 15 if mode == "D" else 120})
    if selfcheck:
        for case_id, fault in (("V001", "wrong_answer"), ("V009", "false_completion"),
                               ("V009", "unrelated_file"), ("V001", "timeout")):
            plan.append({"trial_id": case_id + "-" + fault, "case_id": case_id,
                         "case_version": "1.0", "repeat_index": 1,
                         "profile": "D-selfcheck-v1", "purpose": "diagnostic",
                         "original_trial_id": None, "fault": fault,
                         "product_timeout_s": 0.3 if fault == "timeout" else 15})
    write(output / "manifest.json", manifest(cases, mode, plan, output.name, audit))
    write(output / "gold/cases.json", cases)
    context = mp.get_context("spawn")
    results = []
    started = time.monotonic()
    for item in plan:
        if time.monotonic() - started > (600 if mode == "D" else 3600):
            break
        if sum(r["elapsed_ms"] or 0 for r in results) >= (100000 if mode == "D" else 750000):
            break
        if mode == "R" and (sum(r.get("model_attempts", 0) for r in results) >= 40
                            or sum(r.get("known_tokens", 0) for r in results) >= 160000):
            break
        case = next(c for c in cases if c["case_id"] == item["case_id"])
        row = run_trial(case, item, output, isolation_root, mode, context)
        results.append(row)
        print(f"{mode} {row['trial_id']}: {row['status']}", flush=True)
        if not row["cleanup"]["ok"] or row.get("cancel_reason") == "operator_interrupted":
            break
    primary = [r for r in results if r["purpose"] == "primary"]
    controls = [r for r in results if r["purpose"] == "diagnostic"]
    resets = {case_id: (len({r["initial_state_sha256"] for r in primary
                             if r["case_id"] == case_id}) == 1 if repeat > 1 else None)
              for case_id in IDS}
    missing = [p["trial_id"] for p in plan if p["trial_id"] not in {r["trial_id"] for r in results}]
    summary = {"schema_version": SCHEMA, "mode": mode, "scope": "five_case_program_diagnostic",
               "formal_quality_eligible": False, "planned": len(plan), "started": len(results),
               "terminal": len(results), "scored": len(results), "missing_ids": missing,
               "overall_status": "incomplete" if missing or any(
                   r.get("cancel_reason") == "operator_interrupted" for r in results) else "complete",
               "primary_counts": dict(Counter(r["status"] for r in primary)),
               "selfcheck_detected": sum(r["status"] == "product_fail" for r in controls),
               "selfcheck_planned": 4 if selfcheck else 0,
               "consecutive_initial_states_equal": resets, "repeat": repeat,
               "all_instances_removed": all(r["cleanup"]["instance_removed"] for r in results),
               "requested_model": MODEL,
               "returned_model_ids": sorted({model for r in results
                                              for model in r.get("returned_model_ids", [])}),
               "results": results,
               "cost": {"known_tokens": sum(r.get("known_tokens", 0) for r in results),
                        "attempts": sum(r.get("model_attempts", 0) for r in results),
                        "amount": 0 if mode == "D" else None, "closed": False}}
    write(output / "run-result.json", summary)
    lines = [f"# Phase 3 — {mode} mode", "", "Diagnostic only: V001–V004 and V009.", "",
             "No formal semantic acceptance, 120-case score, or closed whole-project cost claim.", "",
             "| Trial | Purpose | Verdict | Time (ms) | Cleanup |", "|---|---|---|---:|---|"]
    for row in results:
        lines.append(f"| {row['trial_id']} | {row['purpose']} | {row['status']} | "
                     f"{row['elapsed_ms']} | {row['cleanup']['ok']} |")
    lines.extend(["", f"Primary counts: {summary['primary_counts']}",
                  f"Selfcheck detections: {summary['selfcheck_detected']}/{summary['selfcheck_planned']}",
                  f"Missing trials: {missing}", f"Fresh-state comparison: {resets} (repeat={repeat})",
                  f"Requested model: {MODEL}; returned IDs: {summary['returned_model_ids']}.",
                  f"Observed provider attempts/tokens: {summary['cost']['attempts']}/"
                  f"{summary['cost']['known_tokens']}; R amount unknown.",
                  "", "Token admission upper bound is unverified; this is a diagnostic profile.",
                  "A differing returned model ID is service alias/drift evidence, not a client fallback.",
                  "Phase 3 core traces included; non-core background coverage and Phase 4 gold review remain pending."])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("D", "R"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=1)
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args()
    result = run(args.mode, args.output, args.repeat, args.selfcheck)
    ok = (result["overall_status"] == "complete" and
          result["primary_counts"] == {"pass": 5 * args.repeat} and
          result["selfcheck_detected"] == result["selfcheck_planned"] and
          result["all_instances_removed"])
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
