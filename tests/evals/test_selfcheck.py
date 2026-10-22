"""Self-check artifacts are independently analyzable and never overwrite evidence."""

import json

import pytest

from evals.common import digest
from evals.selfcheck import CONTROLS, run_selfcheck
from evals.trace import analyze_trace


async def test_selfcheck_emits_replayable_evidence_and_exclusive_output(tmp_path):
    output = tmp_path / "controls"
    summary = await run_selfcheck(output)
    assert summary["ok"]
    assert summary["real_model_calls"] == 0
    assert summary["full_task_validation"] is False
    assert len(summary["controls"]) == len(CONTROLS) == 11
    for control in summary["controls"]:
        path = output / control["evidence"]["path"]
        assert digest(path.read_bytes()) == control["evidence"]["sha256"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        analysis = analyze_trace(events, execution_status=control["actual"]["outcome"])
        assert analysis["valid"] == (control["control"] != "collector_failure")
        assert analysis["model_attempts"] == 0
        assert all(event["payload"]["data_kind"] == "deterministic_control" for event in events)
    before = (output / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        await run_selfcheck(output)
    assert (output / "summary.json").read_bytes() == before
    assert (output / "summary.md").is_file()
