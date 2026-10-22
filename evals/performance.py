"""Run and audit a small, deterministic fixed-load performance profile.

This profile measures five concurrent copies of the same V001 request. It uses
the existing process and file-tool boundaries in ``evals.run`` and a scripted
response; it does not issue a model request and is kept separate from capability
scores. A completed profile is diagnostic evidence, never a quality pass.
"""

import argparse
import concurrent.futures
import json
import math
import multiprocessing as mp
import os
import platform
import sys
import time
from pathlib import Path

from evals.common import MODEL, ROOT, canonical, digest, load_cases, now, read, strict_json, write

VERSION = "phase8-fixed-load/1.0"
SCOPE = "fixed_load_performance_diagnostic"
TASK_COUNT = 5
CONCURRENCY = 2
REQUESTED_PLATFORMS = {"windows": "Windows", "linux": "Linux"}
MAX_P95_RATIO = 1.15
MIN_THROUGHPUT_RATIO = 0.90


def _platform_matches(requested, system=None):
    if requested not in REQUESTED_PLATFORMS:
        raise ValueError("unsupported_platform_profile")
    return (system or platform.system()) == REQUESTED_PLATFORMS[requested]


def _environment():
    import psutil

    try:
        physical_cpus = psutil.cpu_count(logical=False)
    except (AttributeError, OSError):
        physical_cpus = None
    try:
        memory_bytes = psutil.virtual_memory().total
    except (AttributeError, OSError):
        memory_bytes = None
    try:
        processor = platform.processor() or None
    except OSError:
        processor = None
    packages = {}
    for name in ("nanobot-ai", "pydantic", "psutil", "pytest"):
        try:
            from importlib.metadata import version

            packages[name] = version(name)
        except Exception:
            packages[name] = None
    return {
        "conda_environment": os.environ.get("CONDA_DEFAULT_ENV"),
        "python_prefix": sys.prefix,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": processor,
        "logical_cpus": os.cpu_count(),
        "physical_cpus": physical_cpus,
        "memory_bytes": memory_bytes,
        "timezone": list(time.tzname),
        "timezone_offset_seconds": time.timezone,
        "packages": packages,
    }


def _source_identity():
    names = {
        "evals/performance.py",
        "evals/common.py",
        "evals/run.py",
        "evals/worker.py",
        "evals/scorer.py",
        "evals/repeat.py",
        "evals/trace.py",
        "evals/cases/V001.json",
        "docs/validation-cases/01-basic.md",
        "pyproject.toml",
        "uv.lock",
        "AGENTS.md",
        ".agent/design.md",
        ".agent/security.md",
        ".agent/gotchas.md",
    }
    # D still exercises the actual AgentLoop, provider adapter, and built-in
    # tools; bind those sources too so a result cannot be mistaken for another
    # application revision. No runtime credentials or user config live here.
    source_root = ROOT / "nanobot"
    allowed_suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".toml",
                        ".jinja", ".j2", ".txt"}
    for path in source_root.rglob("*"):
        if (path.is_file() and path.suffix.lower() in allowed_suffixes
                and not any(part in {"__pycache__", "dist", "node_modules", ".cache"}
                            for part in path.parts)):
            names.add(path.relative_to(ROOT).as_posix())
    result = {}
    for name in sorted(names):
        path = ROOT / name
        if path.is_file():
            result[name] = digest(path.read_bytes())
    return result


def _case_and_plan():
    case = next(item for item in load_cases() if item["case_id"] == "V001")
    plan = []
    for index in range(1, TASK_COUNT + 1):
        plan.append({
            "trial_id": f"LOAD-{index:03}",
            "case_id": case["case_id"],
            "case_version": case["case_version"],
            "repeat_index": index,
            "profile": "D-phase8-fixed-load-v1",
            "purpose": "primary",
            "original_trial_id": None,
            "fault": None,
            "entry": "AgentLoop.process_direct",
            "task_type": "ordinary",
            "fault_condition": "none",
            "scope": "five_case_program_diagnostic",
            "mode": "D",
            "product_timeout_s": 15,
        })
    return case, plan


def profile_conditions(case):
    payload = case["turns"][0]["input"]
    return {
        "profile_version": VERSION,
        "requested_model": MODEL,
        "execution_mode": "D_scripted_no_model_calls",
        "agent_entry": "AgentLoop.process_direct",
        "case_id": case["case_id"],
        "case_version": case["case_version"],
        "copies": TASK_COUNT,
        "concurrency": CONCURRENCY,
        "payload_sha256": digest(payload.encode("utf-8")),
        "payload_utf8_bytes": len(payload.encode("utf-8")),
        "workspace_cache": "fresh_workspace_and_runtime_per_copy",
        "os_page_cache": "not_flushed_or_guaranteed",
        "timeout_s_per_copy": 15,
        "native_shell": "not_enabled",
        "tool_boundary": "restricted_file_tools; not_OS_sandbox",
        "performance_thresholds": {
            "p95_max_ratio": MAX_P95_RATIO,
            "throughput_min_ratio": MIN_THROUGHPUT_RATIO,
        },
    }


def _nearest_rank(values, percentile=0.95):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((len(ordered) * percentile) + 0.999999999))
    return ordered[rank - 1]


def summarize_performance(records, wall_ms):
    wall_samples = [row["trial_wall_ms"] for row in records
                    if isinstance(row.get("trial_wall_ms"), (int, float))
                    and row["trial_wall_ms"] >= 0]
    agent_samples = [row["trial"].get("completion_ms") for row in records
                     if isinstance(row.get("trial", {}).get("completion_ms"), (int, float))
                     and row["trial"]["completion_ms"] >= 0]
    completed = sum(row.get("trial", {}).get("execution_complete") is True for row in records)
    return {
        "p95_trial_wall_ms": _nearest_rank(wall_samples),
        "p95_agent_completion_ms": _nearest_rank(agent_samples),
        "wall_latency_samples": len(wall_samples),
        "agent_completion_samples": len(agent_samples),
        "throughput_tasks_per_second": (completed / (wall_ms / 1000)
                                        if wall_ms and wall_ms > 0 else None),
        "observed_tasks": len(records),
        "completed_tasks": completed,
        "total_wall_ms": round(wall_ms, 3),
    }


def _trial_state(rows, plan):
    if len(rows) != len(plan) or len({row.get("trial_id") for row in rows}) != len(rows):
        return "insufficient_evidence"
    if any(row.get("trial", {}).get("status") == "invalid" for row in rows):
        return "invalid"
    if any(row.get("trial", {}).get("status") not in
           ("pass", "product_fail", "undetermined") for row in rows):
        return "invalid"
    if any(row.get("trial", {}).get("status") == "product_fail" or
           row.get("trial", {}).get("safety", {}).get("actual") != 0 for row in rows):
        return "regression"
    for row in rows:
        trial = row.get("trial", {})
        if (trial.get("status") == "undetermined" or
                any(trial.get(field) is not True for field in
                    ("execution_complete", "scoring_complete", "observation_complete")) or
                trial.get("cleanup", {}).get("ok") is not True or
                trial.get("completion_ms") is None or row.get("trial_wall_ms") is None):
            return "insufficient_evidence"
    return "passed_diagnostic"


def _compare_baseline(current, baseline):
    """Return a diagnostic comparison; incompatible or incomplete baselines never pass."""
    if not isinstance(baseline, dict):
        return {"state": "insufficient_evidence", "reason": "baseline_invalid"}
    for key in ("profile_conditions_sha256", "environment_id"):
        if baseline.get(key) != current.get(key):
            return {"state": "insufficient_evidence", "reason": "baseline_conditions_mismatch"}
    if baseline.get("state") != "passed_diagnostic":
        return {"state": "insufficient_evidence", "reason": "baseline_not_passed_diagnostic"}
    before = baseline.get("performance", {})
    after = current.get("performance", {})
    old_p95, new_p95 = before.get("p95_trial_wall_ms"), after.get("p95_trial_wall_ms")
    old_rate, new_rate = before.get("throughput_tasks_per_second"), after.get(
        "throughput_tasks_per_second")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               for v in (old_p95, new_p95, old_rate, new_rate)):
        return {"state": "insufficient_evidence", "reason": "baseline_or_current_metric_unknown"}
    p95_ratio = new_p95 / old_p95
    throughput_ratio = new_rate / old_rate
    regression = (p95_ratio > MAX_P95_RATIO or throughput_ratio < MIN_THROUGHPUT_RATIO)
    return {
        "state": "regression" if regression else "passed_diagnostic",
        "p95_ratio": p95_ratio,
        "throughput_ratio": throughput_ratio,
        "thresholds": {"p95_max_ratio": MAX_P95_RATIO,
                       "throughput_min_ratio": MIN_THROUGHPUT_RATIO},
    }


def _write_report(output, result):
    state = result["state"]
    perf = result.get("performance", {})
    lines = [
        "# Phase 8 fixed-load performance diagnostic",
        "",
        f"State: `{state}`. `quality_passed` is always false for this profile.",
        "",
        "This run uses a deterministic scripted response and makes zero model calls. It is a "
        "separate performance diagnostic; it does not contribute to serialized capability scores "
        "or establish an optimization.",
        "",
        f"- Profile: `{result.get('profile')}` on `{result.get('requested_platform')}`",
        f"- Fixed workload: {TASK_COUNT} identical V001 requests, concurrency {CONCURRENCY}",
        f"- Payload SHA-256: `{result.get('profile_conditions', {}).get('payload_sha256')}`",
        f"- P95 trial wall time: {perf.get('p95_trial_wall_ms')} ms "
        f"({perf.get('wall_latency_samples')} samples)",
        f"- P95 agent completion time: {perf.get('p95_agent_completion_ms')} ms "
        f"({perf.get('agent_completion_samples')} samples)",
        f"- Throughput: {perf.get('throughput_tasks_per_second')} tasks/s",
        f"- Cleanup complete: {result.get('cleanup_complete')}",
        "- Workspace/runtime start fresh per copy; operating system page cache is not flushed "
        "or guaranteed cold.",
        "- Native shell is disabled. File-tool isolation is not an OS sandbox.",
    ]
    comparison = result.get("baseline_comparison")
    if comparison is not None:
        lines.extend(["", f"Baseline comparison: `{comparison.get('state')}`.",
                      f"Reason: `{comparison.get('reason', 'metrics compared')}`."])
    lines.append("")
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _write_index(output):
    entries = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink() or not path.is_file() or path == output / "artifact-index.json":
            continue
        if not path.resolve().is_relative_to(output.resolve()):
            raise ValueError("artifact_escape")
        entries.append({"path": path.relative_to(output).as_posix(),
                        "sha256": digest(path.read_bytes()), "size": path.stat().st_size})
    write(output / "artifact-index.json", entries)


def _baseline_result(path):
    path = Path(path)
    result_path = path / "run-result.json" if path.is_dir() else path
    if result_path.name != "run-result.json":
        raise ValueError("baseline_must_be_run_result_or_directory")
    audit = audit_run(result_path.parent)
    if not audit["ok"]:
        raise ValueError("baseline_evidence_invalid")
    return read(result_path)


def run(output, requested_platform, baseline_path=None):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Run with conda run -n nanobot")
    if requested_platform not in REQUESTED_PLATFORMS:
        raise ValueError("unsupported_platform_profile")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("output_must_be_new")
    output.mkdir(parents=True)
    case, plan = _case_and_plan()
    conditions = profile_conditions(case)
    environment = _environment()
    source_identity = _source_identity()
    profile_id = f"{requested_platform}-fixed-load-v1"
    conditions_hash = digest(canonical(conditions))
    environment_id = digest(canonical(environment))
    run_id = output.name
    manifest = {
        "schema_version": VERSION,
        "run_id": run_id,
        "created_at": now(),
        "profile": profile_id,
        "requested_platform": requested_platform,
        "supported_here": _platform_matches(requested_platform),
        "profile_conditions": conditions,
        "profile_conditions_sha256": conditions_hash,
        "environment": environment,
        "environment_id": environment_id,
        "source_identity": source_identity,
        "source_identity_sha256": digest(canonical(source_identity)),
        "plan": plan,
        "historical_results_reused": False,
        "model_budget": {"model": MODEL, "model_calls": 0, "tokens": 0,
                         "mode": "scripted deterministic D"},
        "quality_eligible": False,
    }
    write(output / "manifest.json", manifest)
    write(output / "gold/case.json", case)
    (output / "records.jsonl").write_text("", encoding="utf-8", newline="\n")

    if not manifest["supported_here"]:
        result = {
            **manifest,
            "state": "insufficient_evidence",
            "quality_passed": False,
            "quality_eligible": False,
            "reason": "requested_platform_does_not_match_current_host",
            "records": [],
            "performance": {"p95_trial_wall_ms": None,
                            "p95_agent_completion_ms": None,
                            "throughput_tasks_per_second": None,
                            "observed_tasks": 0, "completed_tasks": 0,
                            "total_wall_ms": 0},
            "cleanup_complete": True,
            "baseline_comparison": None,
        }
        write(output / "run-result.json", result)
        _write_report(output, result)
        _write_index(output)
        return result

    from evals.repeat import record_trial
    from evals.run import run_trial

    isolation_root = ROOT / "tmp" / "eval-instances"
    isolation_root.mkdir(parents=True, exist_ok=True)
    context = mp.get_context("spawn")
    started_run = time.perf_counter()

    def execute(item):
        started = time.perf_counter()
        execution = run_trial(case, item, output, isolation_root, "D", context)
        elapsed = (time.perf_counter() - started) * 1000
        normalized = record_trial(case, item, output, execution)
        return {"trial_id": item["trial_id"], "trial": normalized,
                "trial_wall_ms": round(elapsed, 3)}

    rows = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {executor.submit(execute, item): item for item in plan}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"trial_id": item["trial_id"],
                           "trial": {"trial_id": item["trial_id"], "case_id": case["case_id"],
                                     "status": "invalid", "execution_complete": False,
                                     "scoring_complete": False, "observation_complete": False,
                                     "completion_ms": None,
                                     "cleanup": {"ok": False, "instance_removed": False},
                                     "adapter_errors": [type(exc).__name__]},
                           "trial_wall_ms": None}
                rows.append(row)
                with (output / "records.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(canonical(row).decode("utf-8") + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
    except KeyboardInterrupt:
        # ThreadPoolExecutor waits for already-started run_trial calls to close their
        # child processes; unstarted evidence stays missing and blocks the result.
        pass
    total_wall_ms = (time.perf_counter() - started_run) * 1000
    write(output / "timing.json", {"total_wall_ms": total_wall_ms})
    performance = summarize_performance(rows, total_wall_ms)
    state = _trial_state(rows, plan)
    cleanup_complete = (len(rows) == TASK_COUNT and all(
        row.get("trial", {}).get("cleanup", {}).get("ok") is True and
        row.get("trial", {}).get("cleanup", {}).get("instance_removed") is True
        for row in rows))
    if state == "passed_diagnostic" and not cleanup_complete:
        state = "insufficient_evidence"

    current_for_compare = {
        "profile_conditions_sha256": conditions_hash,
        "environment_id": environment_id,
        "performance": performance,
    }
    baseline_comparison = None
    if baseline_path is not None and state == "passed_diagnostic":
        try:
            baseline = _baseline_result(baseline_path)
            baseline_comparison = _compare_baseline(current_for_compare, baseline)
        except (OSError, ValueError, KeyError, TypeError):
            baseline_comparison = {"state": "insufficient_evidence",
                                   "reason": "baseline_evidence_invalid"}
        state = baseline_comparison["state"]

    result = {
        **manifest,
        "state": state,
        "quality_passed": False,
        "quality_eligible": False,
        "quality_note": "performance diagnostic does not establish quality or optimization",
        "records": rows,
        "performance": performance,
        "cleanup_complete": cleanup_complete,
        "baseline_comparison": baseline_comparison,
        "observed_outcomes": {status: sum(row.get("trial", {}).get("status") == status
                                           for row in rows)
                              for status in ("pass", "product_fail", "undetermined", "invalid")},
    }
    write(output / "run-result.json", result)
    _write_report(output, result)
    _write_index(output)
    audit = audit_run(output)
    if not audit["ok"]:
        result["state"] = "invalid"
        result["quality_passed"] = False
        result["audit_errors"] = audit["errors"]
        (output / "run-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8", newline="\n")
        _write_report(output, result)
        (output / "artifact-index.json").unlink()
        _write_index(output)
    return result


def audit_run(directory):
    directory = Path(directory).resolve()
    errors = []
    try:
        manifest = read(directory / "manifest.json")
        result = read(directory / "run-result.json")
        index = read(directory / "artifact-index.json")
        archived = [strict_json(line) for line in
                    (directory / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        if manifest.get("schema_version") != VERSION:
            errors.append("profile_version_mismatch")
        if manifest.get("profile_conditions_sha256") != digest(
                canonical(manifest.get("profile_conditions"))):
            errors.append("profile_conditions_hash_mismatch")
        if manifest.get("environment_id") != digest(canonical(manifest.get("environment"))):
            errors.append("environment_identity_mismatch")
        if result.get("run_id") != manifest.get("run_id"):
            errors.append("run_identity_mismatch")
        for field in ("profile", "requested_platform", "supported_here", "profile_conditions",
                      "profile_conditions_sha256", "environment", "environment_id",
                      "source_identity", "source_identity_sha256", "plan", "model_budget"):
            if result.get(field) != manifest.get(field):
                errors.append("result_manifest_identity:" + field)
        sources = manifest.get("source_identity")
        if (not isinstance(sources, dict) or not sources or any(
                not isinstance(name, str) or not isinstance(value, str)
                or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for name, value in sources.items())):
            errors.append("source_identity_invalid")
        if ("source_identity_sha256" in manifest and manifest["source_identity_sha256"]
                != digest(canonical(sources))):
            errors.append("source_identity_hash_mismatch")
        if result.get("quality_passed") is not False or result.get("quality_eligible") is not False:
            errors.append("quality_gate_must_remain_false")
        if archived != result.get("records"):
            errors.append("record_archive_mismatch")
        entries = {item["path"]: item for item in index}
        if len(entries) != len(index):
            errors.append("artifact_index_duplicate_path")
        expected_files = {path.relative_to(directory).as_posix(): path
                          for path in directory.rglob("*")
                          if path.is_file() and path != directory / "artifact-index.json"}
        if set(entries) != set(expected_files):
            errors.append("artifact_inventory_mismatch")
        for name, path in expected_files.items():
            if entries.get(name, {}).get("sha256") != digest(path.read_bytes()):
                errors.append("artifact_hash_mismatch:" + name)

        case, plan = _case_and_plan()
        if manifest.get("plan") != plan or manifest.get("profile_conditions") != profile_conditions(case):
            errors.append("frozen_profile_mismatch")
        if len(archived) != len(plan) or len({row.get("trial_id") for row in archived}) != len(archived):
            errors.append("trial_inventory_incomplete")
        from evals.repeat import record_trial

        by_id = {row.get("trial_id"): row for row in archived}
        for item in plan:
            row = by_id.get(item["trial_id"])
            if row is None:
                continue
            trial = row.get("trial", {})
            actual = record_trial(case, item, directory, {
                "execution_status": trial.get("execution_status"),
                "elapsed_ms": trial.get("elapsed_ms"),
                "cleanup": trial.get("cleanup", {}),
            })
            if canonical(actual) != canonical(trial):
                errors.append("trial_evidence_replay_mismatch:" + item["trial_id"])
        state = result.get("state")
        if state not in {"passed_diagnostic", "regression", "invalid", "insufficient_evidence"}:
            errors.append("unknown_state")
        trial_state = _trial_state(archived, plan)
        if state == "passed_diagnostic" and trial_state != "passed_diagnostic":
            errors.append("quality_state_overstates_trial_evidence")
        comparison = result.get("baseline_comparison")
        if comparison is not None:
            if state == "passed_diagnostic" and comparison.get("state") != "passed_diagnostic":
                errors.append("baseline_gate_state_mismatch")
            if (state == "regression" and trial_state != "regression"
                    and comparison.get("state") != "regression"):
                errors.append("regression_state_lacks_evidence")
        if result.get("performance", {}).get("observed_tasks") != len(archived):
            errors.append("performance_count_mismatch")
        # New runs preserve the independently collected wall duration. Older
        # archives retain their original rounded duration and weaker timing scope.
        timing_path = directory / "timing.json"
        wall_ms = (read(timing_path)["total_wall_ms"] if timing_path.exists()
                   else result.get("performance", {}).get("total_wall_ms"))
        if type(wall_ms) not in (int, float) or not math.isfinite(wall_ms) or wall_ms < 0:
            errors.append("performance_wall_time_invalid")
        else:
            recomputed = summarize_performance(archived, wall_ms)
            saved_performance = result.get("performance", {})
            for key, expected in recomputed.items():
                actual = saved_performance.get(key)
                if (key == "throughput_tasks_per_second" and expected is not None
                        and type(actual) in (int, float)):
                    matches = math.isclose(actual, expected, rel_tol=1e-4, abs_tol=1e-9)
                else:
                    matches = actual == expected
                if not matches:
                    errors.append("performance_replay_mismatch:" + key)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append("audit_error:" + type(exc).__name__)
    return {"ok": not errors, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=tuple(REQUESTED_PLATFORMS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path,
                        help="optional run-result.json or run directory on the same profile/environment")
    args = parser.parse_args()
    result = run(args.output, args.platform, args.baseline)
    print(json.dumps({"state": result["state"], "quality_passed": result["quality_passed"],
                      "run_result": str(args.output / "run-result.json")}, ensure_ascii=False))
    raise SystemExit(0 if result["state"] == "passed_diagnostic" else 1)


if __name__ == "__main__":
    main()
