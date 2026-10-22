from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from evals.browser_task_run import (
    _capture_process_tree,
    _remaining_observed_processes,
    _select_frozen_trial,
    process_identity_is_live,
)


def _plan():
    parent = Path(__file__).resolve().parent / ".browser-task-run-test-fixture"
    return parent / "frozen-real-plan.json", {
        "runs": [
            {"case_id": "V073", "theme": None, "directory": "B-real-V073-01", "index": 1},
            {"case_id": "V073", "theme": None, "directory": "B-real-V073-02", "index": 2},
            {"case_id": "V073", "theme": None, "directory": "B-real-V073-03", "index": 3},
            {"case_id": "V080", "theme": "light", "directory": "B-real-V080-light", "index": 4},
        ]
    }


def test_frozen_trial_selection_uses_unique_exact_directory():
    plan_path, plan = _plan()
    selected = _select_frozen_trial(
        plan, plan_path, plan_path.parent / "B-real-V073-02", "V073", None)
    assert selected["index"] == 2


def test_frozen_trial_selection_rejects_missing_directory():
    plan_path, plan = _plan()
    with pytest.raises(ValueError, match="frozen_real_directory_missing_or_ambiguous"):
        _select_frozen_trial(plan, plan_path, plan_path.parent / "B-real-V073", "V073", None)


def test_frozen_trial_selection_rejects_duplicate_directory():
    plan_path, plan = _plan()
    plan["runs"].append({"case_id": "V073", "theme": None,
                         "directory": "B-real-V073-02", "index": 5})
    with pytest.raises(ValueError, match="frozen_real_directory_missing_or_ambiguous"):
        _select_frozen_trial(plan, plan_path, plan_path.parent / "B-real-V073-02", "V073", None)


def test_frozen_trial_selection_rejects_case_or_theme_mismatch():
    plan_path, plan = _plan()
    with pytest.raises(ValueError, match="frozen_real_case_or_theme_mismatch"):
        _select_frozen_trial(plan, plan_path, plan_path.parent / "B-real-V073-02", "V080", None)
    with pytest.raises(ValueError, match="frozen_real_case_or_theme_mismatch"):
        _select_frozen_trial(plan, plan_path, plan_path.parent / "B-real-V080-light", "V080", "dark")


def test_frozen_trial_selection_rejects_plan_from_another_directory():
    plan_path, plan = _plan()
    with pytest.raises(ValueError, match="frozen_real_plan_parent_mismatch"):
        _select_frozen_trial(plan, plan_path, plan_path.parent / "trials/B-real-V073-01",
                             "V073", None)


def test_observed_child_still_alive_is_cleanup_residual():
    recorded = {"pid": 4182, "create_time": 1234.5}
    assert process_identity_is_live(recorded, 1234.504, True)


def test_observed_child_that_exited_is_not_cleanup_residual():
    recorded = {"pid": 4182, "create_time": 1234.5}
    assert not process_identity_is_live(recorded, 1234.5, False)


def test_reused_pid_with_different_creation_time_is_not_misclassified():
    recorded = {"pid": 4182, "create_time": 1234.5}
    assert not process_identity_is_live(recorded, 1240.0, True)


def test_tracked_live_frontend_child_blocks_cleanup_then_clears_on_exit():
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code, child_code])
    observed = {}
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _capture_process_tree(parent.pid, "frontend_build", observed)
            children = psutil.Process(parent.pid).children(recursive=True)
            if children:
                child_pid = children[0].pid
                break
            time.sleep(0.05)
        assert child_pid is not None
        live, unknown = _remaining_observed_processes(observed)
        assert not unknown
        assert {row["pid"] for row in live} >= {parent.pid, child_pid}

        parent.terminate()
        parent.wait(timeout=5)
        live, unknown = _remaining_observed_processes(observed)
        assert not unknown
        assert child_pid in {row["pid"] for row in live}

        child = psutil.Process(child_pid)
        child.terminate()
        child.wait(timeout=5)
        live, unknown = _remaining_observed_processes(observed)
        assert not unknown
        assert live == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        try:
            child = psutil.Process(child_pid)
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                child.terminate()
                child.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            pass
