"""Actual child lifetime and CI exit contracts; synthetic records, no model."""

import json
import platform
import sys
import threading

import psutil
import pytest

from evals import ci
from evals.common import read


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    monkeypatch.setattr(ci, "source_identity", lambda: {"sha256": "fixed"})
    script = tmp_path / "controlled_child.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "raw=pathlib.Path(sys.argv[1]); raw.mkdir()\n"
        "(raw/'events.jsonl').write_text('{\"event\":\"started\"}\\n')\n"
        "mode=sys.argv[2]\n"
        "if mode in ('timeout','interrupted'): time.sleep(60)\n"
        "if mode=='malformed': (raw/'run-result.json').write_text('{')\n"
        "elif mode!='missing': (raw/'run-result.json').write_text(json.dumps({"
        "'state': 'passed_diagnostic' if mode=='bad_exit' else mode, 'quality_passed':False}))\n"
        "sys.exit(7 if mode=='bad_exit' else 0)\n", encoding="utf-8")
    return tmp_path, script


@pytest.mark.parametrize("state,expected", [
    ("passed_diagnostic", 0), ("regression", 1), ("invalid", 1),
    ("insufficient_evidence", 1), ("bad_exit", 1), ("malformed", 1), ("missing", 1),
])
def test_actual_process_exit_and_raw_failure_state(harness, state, expected):
    root, script = harness
    output = root / state
    calls = []

    def replay(kind, raw):
        calls.append(raw)
        return {"ok": (raw / "run-result.json").exists(), "model_requests": 0, "agent_reruns": 0}

    result = ci.supervise(output, [sys.executable, str(script), str(output / "run"), state],
                          "daily", platform.system().lower(), 10, auditor=replay)
    assert result["exit_code"] == expected
    assert result["quality_passed"] is result["quality_eligible"] is False
    assert len(calls) == 1
    assert read(output / "process-exit.json")["command_exit_code"] == (7 if state == "bad_exit" else 0)
    assert "run/events.jsonl" in read(output / "ci-artifact-index.json")
    if state in ("regression", "invalid", "insufficient_evidence"):
        assert result["raw_state"] == state


@pytest.mark.parametrize("termination,exit_code", [("timeout", 124), ("interrupted", 130)])
def test_live_child_timeout_interrupt_reap_and_partial_archive(harness, termination, exit_code):
    import _thread

    root, script = harness
    output = root / termination
    timer = None
    if termination == "interrupted":
        timer = threading.Timer(0.7, _thread.interrupt_main)
        timer.start()
    try:
        result = ci.supervise(output, [sys.executable, str(script), str(output / "run"), termination],
                              "daily", platform.system().lower(), 2,
                              auditor=lambda *_: {"ok": False, "errors": ["partial_evidence"]})
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert result["exit_code"] == exit_code
    assert result["termination"] == termination
    assert result["quality_passed"] is False
    pid = read(output / "process-start.json")["pid"]
    assert not psutil.pid_exists(pid)
    assert read(output / "process-exit.json")["cleanup"]["ok"]
    assert "run/events.jsonl" in read(output / "ci-artifact-index.json")
    assert not read(output / "offline-audit.json")["ok"]


def test_replay_rejects_stale_success_and_retains_raw_evidence(harness):
    root, script = harness
    output = root / "bad_replay"
    result = ci.supervise(output, [sys.executable, str(script), str(output / "run"), "passed_diagnostic"],
                          "daily", platform.system().lower(), 10,
                          auditor=lambda *_: {"ok": False, "errors": ["raw_record_replay"]})
    assert result["exit_code"] == 1
    assert result["raw_state"] == "passed_diagnostic"
    assert "offline_replay_failed" in result["errors"]
    assert json.loads((output / "run/run-result.json").read_text())["state"] == "passed_diagnostic"


def test_platform_mismatch_never_launches_and_records_no_remote_link(harness):
    root, script = harness
    output = root / "other_os"
    other = "linux" if platform.system() == "Windows" else "windows"
    result = ci.supervise(output, [sys.executable, str(script)], "daily", other, 10,
                          auditor=lambda *_: {"ok": False})
    assert result["exit_code"] == 1
    assert result["termination"] == "platform_mismatch"
    assert not (output / "process-start.json").exists()
    assert not (output / "run").exists()
