"""Incremental, scope-explicit validation of the 120 public scenarios.

Adapter contracts are frozen before execution. Module evidence is never promoted
to a full Agent task result. Unsupported environments remain in the denominator.
"""

import argparse
import asyncio
import importlib
import logging
import multiprocessing
import shutil
import time
import traceback
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from evals.common import ROOT, canonical, digest, inside, read, snapshot, write
from evals.run import stop_process
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, point, span

VERSION = "phase6/1.0"
MODULES = ("evals.batch_basic", "evals.batch_services", "evals.batch_mail")
MILESTONES = (24, 60, 120)
# No command execution or cross-tool bypass probe on this native host.
OS_ISOLATION_REQUIRED = frozenset({"V022", "V104"})


def source_catalog():
    result = {}
    directory = ROOT / "docs" / "validation-cases"
    for path in sorted(directory.glob("[0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        for block in text.split("\n## ")[1:]:
            header, _, body = block.partition("\n")
            case_id, _, title = header.partition(" ")
            if case_id in result:
                raise ValueError("duplicate_source_case")
            result[case_id] = {"id": case_id, "title": title.strip(),
                               "source": path.relative_to(ROOT).as_posix(),
                               "source_sha256": digest(path.read_bytes()),
                               "contract": body.strip()}
    if set(result) != {f"V{i:03}" for i in range(1, 121)}:
        raise ValueError("source_inventory_not_120")
    return result


def catalog():
    source = source_catalog()
    specs = {}
    for module_name in MODULES:
        for case_id, spec in importlib.import_module(module_name).cases().items():
            if case_id not in source or case_id in specs:
                raise ValueError("invalid_adapter_inventory")
            required = {"scope", "entry", "fixtures", "assertions", "supported", "limitations"}
            if not required <= spec.keys():
                raise ValueError(f"incomplete_adapter:{case_id}")
            if spec["supported"] and not spec["assertions"]:
                raise ValueError(f"empty_scoring_contract:{case_id}")
            if not spec["supported"] and not spec["limitations"]:
                raise ValueError(f"unexplained_gap:{case_id}")
            for assertion in spec["assertions"]:
                if assertion["op"] not in {"equal", "contains", "true"}:
                    raise ValueError("unsupported_predicate")
            spec = {**source[case_id], **spec, "adapter": module_name,
                    "mode": "D", "case_version": VERSION,
                    "scoring_version": "phase6-program/1.0", "full_task_eligible": False}
            if case_id in OS_ISOLATION_REQUIRED:
                spec.update(supported=False, assertions=[],
                            limitations=["当前为 Windows 原生主机，无一次性 VM/容器及受控出站；禁止执行此危险场景。"])
            spec["fixture_hashes"] = {p: digest(v.encode("utf-8"))
                                      for p, v in spec["fixtures"].items()}
            specs[case_id] = spec
    if set(specs) != set(source):
        raise ValueError("adapter_inventory_not_120")
    return specs


def score(spec, evidence):
    """Independent declarative program scorer; missing values never equal null."""
    judgments = []
    observed = evidence.get("observed", {})
    for assertion in spec["assertions"]:
        key, op = assertion["key"], assertion["op"]
        if key not in observed:
            passed = None
        elif op == "equal":
            passed = canonical(observed[key]) == canonical(assertion["expected"])
        elif op == "contains":
            value = observed[key]
            expected = assertion["expected"]
            passed = expected in value if isinstance(value, (str, list, dict)) else False
        else:
            passed = observed[key] is True
        judgments.append({**assertion, "actual": observed.get(key), "passed": passed})
    status = evidence["status"]
    if evidence.get("collector_errors") or not evidence.get("trace", {}).get("valid"):
        verdict = "invalid"
    elif status == "timeout" or any(j["passed"] is False for j in judgments):
        verdict = "product_fail"
    elif status != "finished" or not spec["supported"] or not judgments or any(
            j["passed"] is None for j in judgments):
        verdict = "undetermined"
    else:
        verdict = "pass"
    return {"version": VERSION, "scope": spec["scope"], "verdict": verdict,
            "requirements": judgments, "full_task_verdict": "undetermined",
            "formal_quality_eligible": False, "limitations": spec["limitations"],
            "grading_cost": {"kind": "program", "amount": 0, "model_attempts": 0}}


def analyze_batch_trace(events, trial_id, status="finished", collector_errors=()):
    trace = analyze_trace(events, status, collector_errors)
    errors = list(trace["errors"])
    starts = {e["span_id"]: e for e in events if e["phase"] == "start"}
    roots = [e for e in starts.values() if e["type"] == "task"
             and e.get("parent_span_id") is None and e.get("parent_task_id") is None]
    if not any(e["task_id"] == trial_id for e in roots):
        errors.append("root_task_trial_mismatch")
    task_ids = {e["task_id"] for e in events}
    for event in events:
        if event["task_id"] == trial_id:
            continue
        ancestor = starts.get(event["span_id"]) or starts.get(event.get("parent_span_id"))
        chain, visited = [], set()
        while ancestor and ancestor["span_id"] not in visited:
            chain.append(ancestor)
            visited.add(ancestor["span_id"])
            ancestor = starts.get(ancestor.get("parent_span_id"))
        if any(e["payload"].get("accounting") == "unassigned_periodic_overhead" for e in chain):
            continue
        parent = event.get("parent_task_id")
        if parent not in task_ids or parent == event["task_id"] or not any(
                e["task_id"] == parent for e in chain):
            errors.append("child_task_origin_missing")
    trace["errors"] = sorted(set(errors))
    if errors:
        trace.update(valid=False, closed=False, actual_tokens=None)
    return trace


def _child(connection, run_id, trial_id, spec, workspace):
    logging.disable(logging.CRITICAL)
    emit = Emitter(connection, run_id, trial_id)

    async def execute():
        with observation_context(emit.event, trial_id) as observation:
            with span("task", case_id=spec["id"], scope=spec["scope"]):
                if not spec["supported"]:
                    point("capability_gap", reasons=spec["limitations"])
                    observed, status = {}, "blocked"
                else:
                    module = importlib.import_module(spec["adapter"])
                    with span("entry", adapter=spec["entry"]):
                        observed = await module.execute(spec["id"], Path(workspace))
                    status = "blocked" if "__environment_unverified__" in observed else "finished"
                    if status == "blocked":
                        point("capability_gap", reasons=[observed["__environment_unverified__"]])
                pending = [task for task in asyncio.all_tasks()
                           if task is not asyncio.current_task() and not task.done()]
                point("observation_window", pending_tasks=len(pending), accounting="closed")
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    observation.collector_errors.append({"error_kind": "background_not_drained"})
            return {"observed": observed, "status": status,
                    "collector_errors": observation.collector_errors}

    try:
        result = asyncio.run(execute())
    except BaseException as exc:
        # Fixture data may be sensitive; retain exception class only.
        result = {"status": "setup_error", "observed": {},
                  "collector_errors": [{"error_kind": type(exc).__name__,
                                        "frames": [{"file": frame.filename,
                                                    "line": frame.lineno,
                                                    "function": frame.name}
                                                   for frame in traceback.extract_tb(exc.__traceback__)]}]}
    import psutil

    descendants = psutil.Process().children(recursive=True)
    if descendants:
        result["collector_errors"].append({"error_kind": "adapter_child_process_not_closed"})
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(descendants, timeout=3)
    connection.send({"message": "result", "result": result})
    connection.close()


def run_case(spec, output, run_id, timeout=45):
    trial_id = f"{spec['id']}-{uuid.uuid4().hex[:12]}"
    directory = output / "trials" / trial_id
    directory.mkdir(parents=True)
    instance = inside(ROOT / "tmp", "phase6-instances/" + trial_id)
    workspace = instance / "workspace"
    workspace.mkdir(parents=True)
    events, result = [], None
    before = {}
    started = time.monotonic()
    ctx = multiprocessing.get_context("spawn")
    receive = send = None
    process = None
    try:
        receive, send = ctx.Pipe(duplex=False)
        for relative, content in spec["fixtures"].items():
            path = inside(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        before = snapshot(workspace)
        process = ctx.Process(target=_child, args=(send, run_id, trial_id, spec, str(workspace)))
        process.start()
        send.close()
        deadline = time.monotonic() + timeout
        with (directory / "events.jsonl").open("x", encoding="utf-8") as stream:
            while time.monotonic() < deadline:
                try:
                    available = receive.poll(0.1)
                except (EOFError, BrokenPipeError, OSError):
                    break
                if available:
                    try:
                        message = receive.recv()
                    except (EOFError, BrokenPipeError, OSError):
                        break
                    if message["message"] == "event":
                        events.append(message["event"])
                        stream.write(canonical(message["event"]).decode() + "\n")
                        stream.flush()
                    else:
                        result = message["result"]
                # Drain until EOF even if the child exited; buffered final evidence
                # must not be lost on Windows when its pipe handle closes.
            if result is not None or time.monotonic() < deadline:
                process.join(3)
            if process.is_alive():
                parent = Emitter(type("Sink", (), {"send": lambda _, m: events.append(m["event"])})(),
                                 run_id, trial_id)
                parent.producer = "parent-" + parent.producer
                starts = {e["span_id"] for e in events if e["phase"] == "start"}
                ends = {e["span_id"] for e in events if e["phase"] in {"end", "error", "cancelled"}}
                parent.event("watchdog", open_span_ids=sorted(starts - ends))
                stream.write(canonical(events[-1]).decode() + "\n")
                stop_process(process)
                if result is not None:
                    result["collector_errors"].append({"error_kind": "worker_did_not_exit"})
                else:
                    result = {"status": "timeout" if time.monotonic() >= deadline else "crashed",
                              "observed": {}, "collector_errors": []}
        process.join(5)
        if process.is_alive():
            stop_process(process)
        result = result or {"status": "crashed", "observed": {}, "collector_errors": []}
        after = snapshot(workspace)
        for relative in after:
            actual = inside(workspace, relative)
            archived = inside(directory / "files", relative)
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual, archived)
        result.update(case_id=spec["id"], trial_id=trial_id, before=before, after=after,
                      duration_ms=(time.monotonic() - started) * 1000)
        result["trace"] = analyze_batch_trace(events, trial_id, result["status"], result["collector_errors"])
        write(directory / "evidence.json", result)
        verdict = score(spec, result)
        write(directory / "verdict.json", verdict)
        write(directory / "contract.json", spec)
        hashes = {p.relative_to(directory).as_posix(): digest(p.read_bytes())
                  for p in directory.rglob("*") if p.is_file()}
        write(directory / "artifact-index.json", hashes)
        return {"case_id": spec["id"], "trial_id": trial_id, "scope": spec["scope"],
                "status": result["status"], "verdict": verdict["verdict"],
                "observation_complete": result["trace"]["closed"],
                "scoring_complete": bool(verdict["requirements"]) and all(
                    j["passed"] is not None for j in verdict["requirements"]),
                "model_attempts": result["trace"]["model_attempts"],
                "cost": 0 if result["trace"]["model_attempts"] == 0 else None,
                "limitations": spec["limitations"], "artifacts": str(directory.relative_to(output))}
    finally:
        if receive is not None:
            receive.close()
        if send is not None:
            send.close()
        if process is not None and process.is_alive():
            stop_process(process)
        # Resolve and check again at deletion; this instance is the only owned target.
        verified = inside(ROOT / "tmp", "phase6-instances/" + trial_id)
        if verified == instance.resolve() and verified.exists():
            shutil.rmtree(verified)
        write(directory / "cleanup.json", {"owned_instance": str(instance),
                                            "removed": not verified.exists()})
        if (directory / "artifact-index.json").exists():
            write(directory / "final-artifact-index.json", {
                p.relative_to(directory).as_posix(): digest(p.read_bytes())
                for p in directory.rglob("*") if p.is_file()})


def summarize(planned, rows):
    ids = [r["case_id"] for r in rows]
    complete = set(ids) == set(planned) and len(ids) == len(set(ids))
    counts = Counter(r["verdict"] for r in rows)
    return {"planned": len(planned), "records": len(rows), "inventory_complete": complete,
            "verdict_counts": dict(counts), "execution_complete": sum(
                r["status"] == "finished" for r in rows) / len(planned),
            "scoring_complete": sum(r["scoring_complete"] for r in rows) / len(planned),
            "observation_complete": sum(r["observation_complete"] for r in rows) / len(planned),
            "scope_counts": dict(Counter(r["scope"] for r in rows)),
            "full_task_score": None, "formal_quality_eligible": False,
            "model_attempts": sum(r["model_attempts"] for r in rows),
            "cost": sum(r["cost"] for r in rows) if all(r["cost"] is not None for r in rows) else None,
            "grading_cost": 0, "uncovered": [r["case_id"] for r in rows if r["verdict"] != "pass"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", choices=MILESTONES, type=int, default=120)
    parser.add_argument("--cases", nargs="+", help="Targeted verification; never a 120-case grade")
    parser.add_argument("--reuse-from", nargs="+", type=Path,
                        help="Adopt audited, identical stage6 contracts without rerunning them")
    parser.add_argument("--rerun-invalid", nargs="+", default=[],
                        help="Explicitly rerun only prior invalid trials; preserve their evidence")
    parser.add_argument("--refresh-cases", nargs="+", default=[],
                        help="Verify explicit cases after an implementation/fixture change")
    parser.add_argument("--refresh-reason", help="Required explanation for refreshing valid evidence")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("output_must_be_new")
    output.relative_to(ROOT)
    specs = catalog()
    output.mkdir(parents=True)
    run_id = uuid.uuid4().hex
    first = [f"V{i:03}" for i in range(1, 26) if i != 22]
    plan = first + [i for i in sorted(specs) if i not in first]
    if args.cases:
        if len(set(args.cases)) != len(args.cases) or not set(args.cases) <= specs.keys():
            raise ValueError("invalid_targeted_plan")
        plan = args.cases
    else:
        plan = plan[:args.limit]
    if (not set(args.rerun_invalid + args.refresh_cases) <= set(plan)
            or set(args.rerun_invalid) & set(args.refresh_cases)
            or args.refresh_cases and not args.refresh_reason):
        raise ValueError("invalid_rerun_plan")
    reused = {}
    reused_run_ids = {}
    rerun_found = set()
    provenance = []
    for previous in args.reuse_from or []:
        from evals.batch_audit import audit

        previous = previous.resolve()
        previous.relative_to(ROOT)
        checked = audit(previous)
        if not checked["ok"]:
            raise ValueError("reuse_audit_failed")
        previous_manifest = read(previous / "manifest.json")
        previous_result = read(previous / "run-result.json")
        adopted = []
        for row in previous_result["rows"]:
            case_id = row["case_id"]
            if case_id not in plan:
                continue
            if case_id in args.rerun_invalid + args.refresh_cases:
                if case_id in args.rerun_invalid and row["verdict"] != "invalid":
                    raise ValueError("rerun_requires_invalid_evidence")
                rerun_found.add(case_id)
                continue
            if case_id in reused or canonical(previous_manifest["cases"][case_id]) != canonical(specs[case_id]):
                raise ValueError("reuse_contract_conflict")
            source_directory = inside(previous, row["artifacts"])
            destination = inside(output, row["artifacts"])
            shutil.copytree(source_directory, destination)
            reused[case_id] = row
            reused_run_ids[case_id] = previous_manifest.get("trial_run_ids", {}).get(
                case_id, previous_manifest["run_id"])
            adopted.append(case_id)
        provenance.append({"path": str(previous), "manifest_sha256": digest(
            (previous / "manifest.json").read_bytes()), "source_snapshot_sha256": digest(
                (previous / "source-snapshot.zip").read_bytes()),
            "run_id": previous_manifest["run_id"], "cases": adopted})
        reuse_path = output / "reused-sources" / previous_manifest["run_id"]
        reuse_path.mkdir(parents=True)
        shutil.copyfile(previous / "manifest.json", reuse_path / "manifest.json")
        shutil.copyfile(previous / "source-snapshot.zip", reuse_path / "source-snapshot.zip")
        if (previous / "reused-sources").exists():
            shutil.copytree(previous / "reused-sources", reuse_path / "reused-sources")
    if rerun_found != set(args.rerun_invalid + args.refresh_cases):
        raise ValueError("rerun_previous_evidence_missing")
    frozen = {"version": VERSION, "run_id": run_id, "mode": "D", "plan": plan,
              "milestones": [n for n in MILESTONES if n <= len(plan)], "cases": specs,
              "reused_runs": provenance,
              "trial_run_ids": {case_id: reused_run_ids.get(case_id, run_id) for case_id in plan},
              "rerun_invalid": args.rerun_invalid,
              "refreshed_cases": args.refresh_cases, "refresh_reason": args.refresh_reason,
              "sources": {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
                          for group in (ROOT / "evals", ROOT / "nanobot",
                                        ROOT / "tests" / "knowledge" / "mail")
                          for p in group.rglob("*.py")},
              "platform": "native tool boundary; no OS sandbox", "model": "deepseek-v4-flash"}
    write(output / "manifest.json", frozen)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for relative, expected_hash in frozen["sources"].items():
            data = inside(ROOT, relative).read_bytes()
            if digest(data) != expected_hash:
                raise ValueError("source_changed_during_freeze")
            archive.writestr(relative, data)
    rows = []
    for case_id in frozen["plan"]:
        row = reused.get(case_id) or run_case(specs[case_id], output, run_id)
        rows.append(row)
        with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(row).decode() + "\n")
        print(f"{len(rows)}/{len(plan)} {case_id} {row['status']} {row['verdict']}", flush=True)
        if len(rows) in MILESTONES:
            write(output / f"milestone-{len(rows)}.json", summarize(frozen["plan"][:len(rows)], rows))
    summary = summarize(frozen["plan"], rows)
    write(output / "run-result.json", {**summary, "rows": rows})
    (output / "report.md").write_text(
        f"# 第6阶段确定性模块验证\n\n120项原始契约完整保留，本次计划{len(plan)}项。"
        "模块通过不表示真实Agent整题成功。缺口不通过、不删题。\n\n"
        + "\n".join(f"- {r['case_id']} [{r['scope']}] {r['verdict']}："
                    + "；".join(r["limitations"]) for r in rows) + "\n", encoding="utf-8")
    # A completed gap report is a successful harness run, not an all-pass grade.
    return 0 if summary["inventory_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
