"""Isolated real-WebUI process orchestration; scripted and real profiles stay separate."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import psutil

from evals.common import MODEL, ROOT, digest, now, read, write

VERSION = "browser-full-task/1.2"
SOURCE = "docs/validation-cases/09-webui.md"
FIXTURES = {"V073": {}, "V074": {"config.json": '{"timeout":30,"retries":3}'},
            "V075": {}, "V076": {"fact.txt": "值73"}, "V077": {}, "V078": {},
            "V079": {"question": "竹桥预算是多少？给出邮件依据", "subject": "竹桥预算",
                     "body": "竹桥预算120元", "expected_source_excerpt": "竹桥预算120元"},
            "V080": {"question": "返回MOBILE17"}}
CONTROLS = {"wrong_answer": {"answer": "9"}, "skip_edit": {"skip_edit": True},
            "duplicate_delivery": {"duplicate_delivery": True}, "refresh_resubmit": {}}


def sources():
    files = []
    for folder in (ROOT / "nanobot", ROOT / "evals", ROOT / "webui/src"):
        files.extend(path for path in folder.rglob("*") if path.is_file() and
                     path.suffix in {".py", ".md", ".json", ".ts", ".tsx", ".css"})
    files.extend(ROOT / name for name in (SOURCE, "webui/package-lock.json", "webui/package.json",
                                        "webui/vite.config.ts", "webui/index.html", "pyproject.toml"))
    return {path.relative_to(ROOT).as_posix(): digest(path.read_bytes()) for path in sorted(files)}


def wait_ready(path, process, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return read(path)
        if process.poll() is not None:
            raise RuntimeError("gateway_exited_before_ready")
        time.sleep(0.2)
    raise TimeoutError("gateway_ready_timeout")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _select_frozen_trial(frozen, plan_path, output, case_id, theme):
    """Select exactly the frozen row bound to this output directory.

    Several independent trials may share a case and theme; the directory is
    the per-trial identity, while case/theme remain consistency checks.
    """
    output = Path(output).resolve()
    plan_path = Path(plan_path).resolve()
    matches = [row for row in frozen.get("runs", [])
               if row.get("directory") == output.name]
    if len(matches) != 1:
        raise ValueError("frozen_real_directory_missing_or_ambiguous")
    target = matches[0]
    if target.get("case_id") != case_id or target.get("theme") != theme:
        raise ValueError("frozen_real_case_or_theme_mismatch")
    if plan_path.parent != output.parent:
        raise ValueError("frozen_real_plan_parent_mismatch")
    return target


def _endpoint_closed(port):
    if port is None:
        return True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=0.3):
            return False
    except urllib.error.HTTPError:
        return False
    except (OSError, urllib.error.URLError, TimeoutError):
        return True


def _trial_processes(trial_path):
    """Return non-ancestor processes whose command line still names this trial."""
    current = psutil.Process()
    excluded = {current.pid, *(parent.pid for parent in current.parents())}
    needle = str(Path(trial_path).resolve()).casefold()
    matches = []
    access_denied = 0
    for process in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            if process.info["pid"] in excluded:
                continue
            command = process.info.get("cmdline") or []
            if any(needle in str(argument).casefold() for argument in command):
                matches.append({"pid": process.info["pid"], "ppid": process.info.get("ppid"),
                                "name": process.info.get("name"), "cmdline": command})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            access_denied += 1
    return {"query_ok": True, "query_exitcode": 0,
            "access_denied_process_count": access_denied, "matching_processes": matches}


def _capture_process_tree(pid, role, observed):
    if pid is None:
        return
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for process in processes:
        try:
            created = process.create_time()
            observed[(process.pid, created)] = {"pid": process.pid, "create_time": created,
                "name": process.name(), "root_role": role}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _wait_with_tree_tracking(process, timeout, observed, related=(), primary_role="process"):
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        _capture_process_tree(process.pid, primary_role, observed)
        for related_process, role in related:
            if related_process is not None:
                _capture_process_tree(related_process.pid, role, observed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            pass
    _capture_process_tree(process.pid, primary_role, observed)
    for related_process, role in related:
        if related_process is not None:
            _capture_process_tree(related_process.pid, role, observed)
    return True


def _remaining_observed_processes(observed):
    remaining = []
    unknown = []
    for record in observed.values():
        try:
            process = psutil.Process(record["pid"])
            same_identity = process_identity_is_live(
                record, process.create_time(),
                process.is_running() and process.status() != psutil.STATUS_ZOMBIE)
            if same_identity:
                remaining.append(record)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unknown.append(record)
    return remaining, unknown


def process_identity_is_live(recorded, current_create_time, process_is_running):
    """Treat a PID as residual only when its observed creation identity still lives."""
    return (abs(float(current_create_time) - float(recorded["create_time"])) <= 0.01
            and process_is_running is True)


def run(case_id, profile, output, control=None, frozen_plan=None, theme=None):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    if profile not in {"B-scripted", "B-real"} or case_id not in FIXTURES:
        raise ValueError("unsupported_browser_profile")
    if profile == "B-real" and (control is not None or frozen_plan is None):
        raise ValueError("real_requires_frozen_plan_no_controls")
    if control is not None and control not in CONTROLS:
        raise ValueError("unsupported_control")
    if (case_id == "V080" and theme not in {"light", "dark"}) or (case_id != "V080" and theme is not None):
        raise ValueError("theme_required_only_for_V080")
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("output_outside_workspace")
    frozen = read(frozen_plan) if frozen_plan else None
    target = (_select_frozen_trial(frozen, frozen_plan, output, case_id, theme)
              if profile == "B-real" else None)
    output.mkdir(parents=True, exist_ok=False)
    runtime, workspace = output / "runtime", output / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    write(runtime / "config.json", {"agents": {"defaults": {"model": MODEL}},
                                   "tools": {"restrictToWorkspace": True}})
    fixture = {**FIXTURES[case_id], **({"theme": theme} if theme else {})}
    for name, text in (FIXTURES[case_id].items() if case_id not in {"V079", "V080"} else []):
        (workspace / name).write_text(text, encoding="utf-8")
    manifest = {"version": VERSION, "created_at": now(), "case_id": case_id,
        "trial_id": output.name, "profile": profile, "control": control,
        "source_sha256": digest((ROOT / SOURCE).read_bytes()), "sources": sources(),
        "fixture": fixture, "requested_model": MODEL if profile == "B-real" else None,
        "budget": {"requests": 8, "known_tokens": 32000, "seconds": 180},
        "business_clock": "2026-10-01T01:00:00Z",
        "gateway": {"host": "127.0.0.1", "transport": "real WebSocketChannel",
                    "front_entry": "same-origin production build via existing static_dist_path"},
        "browser": {"viewport": {"width": 390, "height": 844} if theme else {"width": 1280, "height": 800},
                    "theme": theme or "light", "profile": "fresh isolated context"},
        "scope": "real browser, actual WebUI and actual Agent/tools; no external channel recipients"}
    manifest["frozen_contract"] = {"fixture": fixture, "source_sha256": manifest["source_sha256"],
                                   "completion": "DOM plus real gateway/tool/model ledger; original requirements unchanged"}
    environment_path = output.parent / "environment.json"
    if not environment_path.exists():
        raise ValueError("browser_environment_not_frozen")
    manifest["browser_environment"] = read(environment_path)
    manifest["browser_environment_sha256"] = digest(environment_path.read_bytes())
    if frozen_plan:
        if (target["sources"] != manifest["sources"] or target["directory"] != output.name
                or output.parent != Path(frozen_plan).resolve().parent):
            raise ValueError("frozen_real_source_or_trial_drift")
        limits = target.get("budget", manifest["budget"])
        if (set(limits) != {"requests", "known_tokens", "seconds"}
                or any(type(limits[key]) is not int or not 0 < limits[key] <= manifest["budget"][key]
                       for key in limits)):
            raise ValueError("frozen_real_budget")
        manifest["budget"] = limits
        write(output / "frozen-contract.json", frozen)
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in manifest["sources"].items():
            data = (ROOT / name).read_bytes()
            if digest(data) != sha:
                raise ValueError("source_changed")
            archive.writestr(name, data)
    gateway = None
    driver = None
    frontend_build = None
    observed_processes = {}
    frontend_build_pid = None
    frontend_build_returncode = None
    frontend_artifacts = None
    driver_pid = None
    driver_returncode = None
    ready = None
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    gateway_log = (output / "gateway.log").open("x", encoding="utf-8")
    build_log = (output / "frontend-build.log").open("x", encoding="utf-8")
    primary = None
    try:
        frontend = output / "frontend"
        frontend_build = subprocess.Popen(
            ["node", str(ROOT / "webui/node_modules/vite/bin/vite.js"), "build",
             "--outDir", str(frontend)], cwd=ROOT / "webui", env=env,
            stdout=build_log, stderr=subprocess.STDOUT)
        frontend_build_pid = frontend_build.pid
        _capture_process_tree(frontend_build.pid, "frontend_build", observed_processes)
        if not _wait_with_tree_tracking(frontend_build, 90, observed_processes,
                                        primary_role="frontend_build"):
            stop(frontend_build)
            frontend_build_returncode = frontend_build.returncode
            raise TimeoutError("isolated_frontend_build_timeout")
        frontend_build_returncode = frontend_build.returncode
        if frontend_build_returncode != 0 or not (frontend / "index.html").is_file():
            raise RuntimeError("isolated_frontend_build_failed")
        frontend_artifacts = {
            "sources": {name: sha for name, sha in manifest["sources"].items() if name.startswith("webui/")},
            "files": {path.relative_to(frontend).as_posix(): digest(path.read_bytes())
                      for path in sorted(frontend.rglob("*")) if path.is_file()}}
        command = [sys.executable, "-m", "evals.browser_task_gateway", "--case", case_id,
                   "--profile", profile, "--runtime", str(runtime), "--workspace", str(workspace),
                   "--output", str(output), "--ready", str(output / "ready.json"),
                   "--frontend-dir", str(frontend)]
        if profile == "B-real":
            command.extend(["--credentials", str(ROOT / "api.txt"),
                            "--request-limit", str(manifest["budget"]["requests"]),
                            "--known-token-limit", str(manifest["budget"]["known_tokens"])])
        if control:
            write(output / "control.json", CONTROLS[control])
            command.extend(["--control", str(output / "control.json")])
        gateway = subprocess.Popen(command, cwd=ROOT, env=env, stdout=gateway_log, stderr=subprocess.STDOUT)
        _capture_process_tree(gateway.pid, "gateway", observed_processes)
        ready = wait_ready(output / "ready.json", gateway)
        browser_url = "http://127.0.0.1:" + str(ready["port"])
        deadline = time.monotonic() + 30
        while True:
            if gateway.poll() is not None:
                raise RuntimeError("isolated_gateway_failed_to_start")
            try:
                with urllib.request.urlopen(browser_url, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("WebUI_ready_timeout")
            time.sleep(0.2)
        command = [sys.executable, "-m", "evals.browser_task_driver", "--case", case_id,
                   "--profile", profile, "--url", browser_url, "--output", str(output)]
        if ready.get("control_url"):
            command.extend(["--control-url", ready["control_url"]])
        for field, option in (("gate_state_url", "--gate-state-url"), ("gate_release_url", "--gate-release-url")):
            if ready.get(field):
                command.extend([option, ready[field]])
        if case_id == "V077":
            for actor in ("A", "B"):
                fixture_actor = ready["fixture_sessions"].get(actor) or ready["fixture_sessions"][actor.lower()]
                command.extend(["--v077-" + actor.lower() + "-chat-id",
                                fixture_actor["chat_id"]])
        if case_id == "V079":
            command.extend(["--ready-json", str(output / "ready.json")])
        if case_id == "V080":
            command.extend(["--theme", theme, "--viewport", "390x844"])
        if control:
            command.extend(["--control", control])
        with (output / "browser.log").open("x", encoding="utf-8") as log:
            driver = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            driver_pid = driver.pid
            _capture_process_tree(driver.pid, "driver", observed_processes)
            if not _wait_with_tree_tracking(driver, 180, observed_processes,
                                            related=((gateway, "gateway"),),
                                            primary_role="driver"):
                stop(driver)
                driver_returncode = driver.poll()
                raise TimeoutError("browser_driver_timeout")
            driver_returncode = driver.returncode
    except Exception as error:
        primary = type(error).__name__
        write(output / "orchestrator-error.json", {"type": primary})
    finally:
        (runtime / "stop").touch()
        if frontend_build is not None:
            _capture_process_tree(frontend_build.pid, "frontend_build", observed_processes)
            if frontend_build.poll() is None:
                stop(frontend_build)
            frontend_build_returncode = frontend_build.returncode
        if gateway is not None:
            if not _wait_with_tree_tracking(gateway, 30, observed_processes,
                                            primary_role="gateway"):
                stop(gateway)
        if driver is not None and driver.poll() is None:
            _capture_process_tree(driver.pid, "driver", observed_processes)
            stop(driver)
        gateway_log.close()
        build_log.close()
    gateway_stopped = gateway is None or gateway.poll() is not None
    gateway_returncode = gateway.returncode if gateway is not None else None
    driver_stopped = driver is None or driver.poll() is not None
    driver_returncode = driver.returncode if driver is not None else None
    frontend_build_stopped = frontend_build is None or frontend_build.poll() is not None
    frontend_build_returncode = (frontend_build.returncode if frontend_build is not None
                                 else frontend_build_returncode)
    port = ready.get("port") if isinstance(ready, dict) else None
    port_closed = _endpoint_closed(port) if gateway is not None else True
    process_observation = _trial_processes(output)
    remaining_observed_processes, unknown_observed_processes = _remaining_observed_processes(
        observed_processes)
    remaining_build_processes = [row for row in remaining_observed_processes
                                 if row.get("root_role") == "frontend_build"]
    unknown_build_processes = [row for row in unknown_observed_processes
                               if row.get("root_role") == "frontend_build"]
    cleanup_ok = bool(frontend_build_stopped and gateway_stopped and driver_stopped and port_closed
                      and process_observation["query_ok"]
                      and process_observation["access_denied_process_count"] == 0
                      and not process_observation["matching_processes"]
                      and not remaining_observed_processes and not unknown_observed_processes)
    if frontend_build_pid is not None:
        write(output / "frontend-build.json", {
            **(frontend_artifacts or {}),
            "pid": frontend_build_pid, "exitcode": frontend_build_returncode,
            "wait_returncode": frontend_build_returncode, "stopped": frontend_build_stopped,
        })
    write(output / "driver-process.json", {"pid": driver_pid,
        "exitcode": driver_returncode, "wait_returncode": driver_returncode,
        "stopped": driver_stopped})
    write(output / "gateway-process.json", {"pid": gateway.pid if gateway is not None else None,
        "wait_returncode": gateway_returncode, "stopped": gateway_stopped,
        "ready_loopback_port": port, "ready_loopback_closed": port_closed})
    write(output / "process-tree.json", {"observed": list(observed_processes.values()),
        "remaining": remaining_observed_processes, "unknown": unknown_observed_processes})
    write(output / "cleanup.json", {"frontend_build_stopped": frontend_build_stopped,
        "frontend_build_pid": frontend_build_pid,
        "frontend_build_wait_returncode": frontend_build_returncode,
        "frontend_build_remaining_processes": remaining_build_processes,
        "frontend_build_unknown_processes": unknown_build_processes,
        "gateway_stopped": gateway_stopped,
        "gateway_pid": gateway.pid if gateway is not None else None,
        "gateway_wait_returncode": gateway_returncode,
        "driver_stopped": driver_stopped, "driver_pid": driver_pid,
        "driver_wait_returncode": driver_returncode,
        "ready_loopback_port": port, "ready_loopback_closed": port_closed,
        "trial_process_query_ok": process_observation["query_ok"],
        "trial_process_query_exitcode": process_observation["query_exitcode"],
        "access_denied_process_count": process_observation["access_denied_process_count"],
        "matching_trial_processes": process_observation["matching_processes"],
        "trial_processes_absent": not process_observation["matching_processes"],
        "observed_process_tree_count": len(observed_processes),
        "remaining_observed_processes": remaining_observed_processes,
        "unknown_observed_processes": unknown_observed_processes,
        "cleanup_ok": cleanup_ok, "vite_stopped": frontend_build_stopped,
        "frontend_server": "gateway", "error_type": primary, "evidence_retained": True})
    print(output.name, "captured", "error", primary, flush=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=FIXTURES, required=True)
    parser.add_argument("--profile", choices=["B-scripted", "B-real"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control")
    parser.add_argument("--frozen-plan", type=Path)
    parser.add_argument("--theme", choices=["light", "dark"])
    args = parser.parse_args()
    run(args.case, args.profile, args.output, args.control, args.frozen_plan, args.theme)
