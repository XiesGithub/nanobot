"""CI execution receipts, bounded process lifetime and mandatory offline replay.

A green receipt means collection/replay succeeded, never system quality approval.
Raw daily/performance evidence is not rewritten, including interrupted runs.
"""

import argparse
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from evals.common import ROOT, canonical, digest, now, read, write

VERSION = "evaluation-ci/1.0"
PLATFORMS = {"windows": "Windows", "linux": "Linux"}


def runner_identity(requested_platform):
    from evals.identity import _environment

    github = {key: os.environ.get(key) for key in (
        "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW", "GITHUB_REF", "GITHUB_EVENT_NAME",
        "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")}
    remote_url = None
    if all(github[k] for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")):
        remote_url = (f"{github['GITHUB_SERVER_URL']}/{github['GITHUB_REPOSITORY']}"
                      f"/actions/runs/{github['GITHUB_RUN_ID']}")
    return {"version": VERSION, "created_at": now(), "requested_platform": requested_platform,
            "actual_platform": platform.system(), "environment": _environment(),
            "github": github, "remote_run_url": remote_url,
            "scope": "execution_and_evidence_diagnostic", "quality_passed": False,
            "os_sandbox_verified": False}


def source_identity():
    from evals.identity import _selected_paths

    files = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in _selected_paths(ROOT)}
    return {"files": files, "sha256": digest(canonical(files))}


def stop_tree(process):
    """Reap only this owned subprocess tree, on timeout or interrupt."""
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    errors = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            errors.append(type(exc).__name__)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    _, alive = psutil.wait_procs(children, timeout=5)
    return {"descendants_observed": len(children), "alive_pids": [p.pid for p in alive],
            "errors": errors, "ok": not errors and not alive and process.poll() is not None}


def replay(kind, raw):
    if kind == "daily":
        from evals.daily import audit

        return audit(raw)
    from evals.performance import audit_run

    return audit_run(raw)


def gate(command_exit, termination, result, audit, platform_matches, cleanup):
    errors = []
    if not isinstance(result, dict):
        result = {}
    if not platform_matches:
        errors.append("platform_mismatch")
    if not cleanup.get("ok"):
        errors.append("process_cleanup_incomplete")
    state = result.get("state") if isinstance(result, dict) else None
    if state not in {"passed_diagnostic", "regression", "invalid", "insufficient_evidence"}:
        errors.append("missing_or_invalid_run_result")
        state = "invalid"
    if result.get("quality_passed") is not False:
        errors.append("diagnostic_quality_claim_invalid")
    if not audit.get("ok"):
        errors.append("offline_replay_failed")
    if command_exit != 0:
        errors.append("command_failed")
    if termination:
        errors.append(termination)
    # Preserve the raw state separately; an execution failure cannot be promoted
    # by a successful synthetic/model answer or a stale run-result.json.
    passed = (not errors and state == "passed_diagnostic")
    effective = "invalid" if errors else state
    exit_code = 124 if termination == "timeout" else 130 if termination == "interrupted" else 0 if passed else 1
    return {"state": effective, "raw_state": state, "pipeline_passed": passed,
            "quality_passed": False, "quality_eligible": False, "exit_code": exit_code,
            "command_exit_code": command_exit, "termination": termination, "errors": errors}


def archive_index(output):
    entries = {}
    for path in sorted(output.rglob("*")):
        if path == output / "ci-artifact-index.json":
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(output):
            raise ValueError("ci_artifact_escape")
        if path.is_file():
            entries[path.relative_to(output).as_posix()] = digest(path.read_bytes())
    write(output / "ci-artifact-index.json", entries)


def supervise(output, command, kind, requested_platform, timeout, *, auditor=replay):
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT.resolve()) or output == ROOT.resolve():
        raise ValueError("output_must_be_new_workspace_directory")
    if timeout <= 0:
        raise ValueError("positive_supervisor_timeout_required")
    output.mkdir(parents=True, exist_ok=False)
    identity = runner_identity(requested_platform)
    write(output / "runner.json", identity)
    sources = source_identity()
    write(output / "source-identity.json", sources)
    write(output / "invocation.json", {"command": command, "kind": kind, "timeout_s": timeout,
                                       "raw_directory": "run", "credentials_logged": False})
    raw, process, termination = output / "run", None, None
    cleanup = {"ok": True, "descendants_observed": 0, "alive_pids": [], "errors": []}
    command_exit, runner_errors = None, []
    matches = platform.system() == PLATFORMS[requested_platform]
    started = time.monotonic()
    old_handlers = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupt)
        if matches:
            # R output remains only in the masked CI console, never an artifact.
            process = subprocess.Popen(command, cwd=ROOT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            write(output / "process-start.json", {"pid": process.pid, "started_at": now()})
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    termination = "timeout"
                    break
                try:
                    command_exit = process.wait(timeout=min(remaining, 0.25))
                except subprocess.TimeoutExpired:
                    continue
        else:
            termination = "platform_mismatch"
    except KeyboardInterrupt:
        termination = "interrupted"
    except (OSError, ValueError) as exc:
        runner_errors.append(type(exc).__name__)
    finally:
        if process is not None:
            try:
                cleanup = stop_tree(process)
                command_exit = process.returncode
            except (OSError, psutil.Error, subprocess.TimeoutExpired) as exc:
                cleanup = {"ok": False, "errors": [type(exc).__name__], "alive_pids": None}
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    write(output / "process-exit.json", {"command_exit_code": command_exit,
                                        "termination": termination, "cleanup": cleanup,
                                        "runner_errors": runner_errors,
                                        "elapsed_s": round(time.monotonic() - started, 3)})
    try:
        result = read(raw / "run-result.json")
    except (OSError, ValueError):
        result = {}
    audit = auditor(kind, raw)
    sources_after = source_identity()
    write(output / "source-identity-after.json", sources_after)
    if sources_after != sources:
        audit = {**audit, "ok": False,
                 "errors": [*audit.get("errors", []), "source_changed_during_run"]}
    write(output / "offline-audit.json", audit)
    receipt = {"version": VERSION, **gate(command_exit, termination, result, audit, matches, cleanup),
               "runner_errors": runner_errors, "remote_run_url": identity["remote_run_url"],
               "raw_evidence_preserved": True, "agent_reruns_by_audit": 0,
               "model_requests_by_audit": 0}
    if runner_errors:
        receipt.update(state="invalid", pipeline_passed=False, exit_code=1)
    write(output / "ci-result.json", receipt)
    archive_index(output)
    return receipt


def audit_receipt(directory):
    """Replay an archived receipt without launching an Agent or a subprocess.

    Integrity of an accurately recorded failed/partial run is distinct from
    complete raw evidence and from an execution pass.
    """
    directory = Path(directory).resolve()
    errors = []
    try:
        saved_index = read(directory / "ci-artifact-index.json")
        actual_index = {}
        for path in sorted(directory.rglob("*")):
            if path == directory / "ci-artifact-index.json":
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(directory):
                raise ValueError("ci_artifact_escape")
            if path.is_file():
                actual_index[path.relative_to(directory).as_posix()] = digest(path.read_bytes())
        if actual_index != saved_index:
            errors.append("ci_artifact_inventory_or_hash")
        invocation = read(directory / "invocation.json")
        runner = read(directory / "runner.json")
        process = read(directory / "process-exit.json")
        raw = directory / "run"
        audit = replay(invocation["kind"], raw)
        if read(directory / "source-identity.json") != read(directory / "source-identity-after.json"):
            audit = {**audit, "ok": False,
                     "errors": [*audit.get("errors", []), "source_changed_during_run"]}
        if audit != read(directory / "offline-audit.json"):
            errors.append("offline_audit_replay")
        try:
            result = read(raw / "run-result.json")
        except (OSError, ValueError):
            result = {}
        derived = gate(process["command_exit_code"], process["termination"], result, audit,
                       runner["actual_platform"] == PLATFORMS[runner["requested_platform"]],
                       process["cleanup"])
        if process["runner_errors"]:
            derived.update(state="invalid", pipeline_passed=False, exit_code=1)
        saved = read(directory / "ci-result.json")
        if any(saved.get(key) != value for key, value in derived.items()):
            errors.append("ci_gate_replay")
        if saved.get("quality_passed") is not False or runner.get("quality_passed") is not False:
            errors.append("quality_promotion")
        raw_ok = audit.get("ok") is True
    except (OSError, KeyError, TypeError, ValueError) as exc:
        errors.append("ci_audit:" + type(exc).__name__)
        raw_ok = False
    return {"ok": not errors, "errors": errors, "raw_replay_ok": raw_ok,
            "quality_passed": False, "model_requests": 0, "agent_reruns": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("daily", "performance"))
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=3900)
    parser.add_argument("--tier", choices=("development", "smoke", "nightly", "release"), default="development")
    parser.add_argument("--mode", choices=("D", "R"), default="D")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-wall-seconds", type=int)
    parser.add_argument("--opt-in-real-model", action="store_true")
    args = parser.parse_args()
    if args.audit:
        result = audit_receipt(args.audit)
        if args.report:
            if args.report.resolve().is_relative_to(args.audit.resolve()):
                parser.error("audit report must be outside the immutable evidence directory")
            write(args.report, result)
        print(result)
        return 0 if result["ok"] else 1
    if not all((args.kind, args.platform, args.output)):
        parser.error("--kind, --platform and --output are required for execution")
    raw = args.output.resolve() / "run"
    command = [sys.executable, "-m", "evals." + args.kind, "--output", str(raw)]
    if args.kind == "performance":
        command += ["--platform", args.platform]
    else:
        from evals.daily import budgets

        # Reject unsafe requests before spawning or accessing any credential.
        budgets(args.mode, args.max_attempts, args.max_tokens, args.max_wall_seconds, args.opt_in_real_model)
        command += ["--tier", args.tier, "--mode", args.mode]
        for option in ("max_attempts", "max_tokens", "max_wall_seconds"):
            if getattr(args, option) is not None:
                command += ["--" + option.replace("_", "-"), str(getattr(args, option))]
        if args.cases:
            command += ["--cases", *args.cases]
        if args.opt_in_real_model:
            command += ["--opt-in-real-model"]
    result = supervise(args.output, command, args.kind, args.platform, args.timeout)
    print({k: result[k] for k in ("state", "raw_state", "exit_code", "quality_passed")})
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
