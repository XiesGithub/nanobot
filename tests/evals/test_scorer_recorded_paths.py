"""Recorded-path policy controls; archived evidence is data, never old executable code."""
import copy

import pytest

from evals.common import load_cases, read
from evals.scorer import _business_write_path, score


@pytest.mark.parametrize("workspace,path,expected", [
    (r"C:\run\workspace", r"C:\run\workspace\config.json", "config.json"),
    (r"C:\run\workspace", "C:/run/workspace/config.json", "config.json"),
    (r"C:\run\workspace", r"C:/run\workspace/config.json", "config.json"),
    (r"C:\run\workspace", "config.json", "config.json"),
    (r"C:\run\workspace", r"folder\config.json", "folder/config.json"),
    (r"C:\run\workspace", "folder/config.json", "folder/config.json"),
    (r"\\server\share\workspace", r"\\server\share\workspace\config.json", "config.json"),
    (r"\\server\share\workspace", "config.json", "config.json"),
    ("/run/workspace", "/run/workspace/config.json", "config.json"),
    ("/run/workspace", "config.json", "config.json"),
    ("/run/workspace", "folder/config.json", "folder/config.json"),
])
def test_recorded_path_uses_workspace_grammar(workspace, path, expected):
    assert _business_write_path(path, workspace) == expected


@pytest.mark.parametrize("workspace,path", [
    (r"C:\run\workspace", r"C:\outside\config.json"),
    (r"C:\run\workspace", r"C:\run\workspace-other\config.json"),
    (r"C:\run\workspace", r"D:\run\workspace\config.json"),
    (r"C:\run\workspace", r"C:config.json"),
    (r"C:\run\workspace", r"\config.json"),
    (r"C:\run\workspace", "../config.json"),
    (r"C:\run\workspace", r"folder\..\config.json"),
    (r"C:\run\workspace", r"C:\run\workspace\..\config.json"),
    (r"\\server\share\workspace", r"\\other\share\workspace\config.json"),
    (r"\\server\share\workspace", r"\\server\other\workspace\config.json"),
    (r"\\server\share\workspace", r"\\server\share\outside\config.json"),
    ("/run/workspace", "/outside/config.json"),
    ("/run/workspace", "/run/workspace-other/config.json"),
    ("/run/workspace", "../config.json"),
    ("/run/workspace", "folder/../config.json"),
    ("/run/workspace", "/run/workspace/../config.json"),
    ("/run/workspace", r"folder\config.json"),
    ("/run/workspace", r"C:\run\workspace\config.json"),
    ("/run/workspace", "C:config.json"),
    ("relative-workspace", "config.json"),
])
def test_ambiguous_or_outside_recorded_paths_are_rejected(workspace, path):
    assert _business_write_path(path, workspace) is None


@pytest.mark.parametrize("workspace,allowed,forbidden", [
    (r"C:\run\workspace", r"C:\run\workspace\config.json", r"C:\run\workspace\other.json"),
    (r"\\server\share\workspace", r"\\server\share\workspace\config.json", r"\\server\share\outside\config.json"),
    ("/run/workspace", "/run/workspace/config.json", "/run/workspace/other.json"),
])
def test_write_event_is_audited_even_when_final_files_are_restored(history_tree, workspace, allowed, forbidden):
    trial = history_tree / "docs/agent-evaluation-phase2-runs/R-20261009/trials/V009-1"
    evidence = read(trial / "artifacts/evidence.json")
    case = next(case for case in load_cases() if case["case_id"] == "V009")
    evidence["workspace"] = workspace
    event = next(event for event in evidence["events"] if event["type"] == "tool"
                 and event["phase"] == "start" and event["payload"]["name"] in ("write_file", "edit_file"))
    event["payload"]["args"]["path"] = allowed
    assert not score(case, evidence)["prohibitions"]["violations"]
    evidence["after"] = copy.deepcopy(evidence["before"])
    event["payload"]["args"]["path"] = forbidden
    verdict = score(case, evidence)
    assert verdict["prohibitions"]["violations"] == [event["event_id"]]
    assert verdict["extensions"]["changed_files"] == []
    assert {reason["reason_code"] for reason in verdict["reasons"]} >= {"prohibited_action"}


def test_original_phase2_windows_absolute_write_is_allowed(history_tree):
    trial = history_tree / "docs/agent-evaluation-phase2-runs/R-20261009/trials/V009-1"
    original = read(trial / "verdicts/1.json")
    case = next(case for case in load_cases() if case["case_id"] == "V009")
    actual = score(case, read(trial / "artifacts/evidence.json"))
    assert actual["status"] == original["status"] == "pass"
    assert actual["prohibitions"] == original["prohibitions"]
