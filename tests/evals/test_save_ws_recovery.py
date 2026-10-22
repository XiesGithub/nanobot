"""Real WS save denial must terminate as failure and allow a later persisted turn."""
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("boundary", ["SAVE", "checkpoint"])
async def test_save_failure_terminal_and_recovery(tmp_path, boundary):
    script = Path(__file__).resolve().parents[2] / "docs/agent-evaluation-save-fix-20261016/reproduce_ws_save.py"
    spec = importlib.util.spec_from_file_location("save_ws_control", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await module.execute(tmp_path, True, boundary)
    assert result["injection_reached"]
    assert result["old_session_unchanged_on_failure"]
    assert result["failure_turn"]["terminal"] == "terminal_received"
    assert result["failure_turn"]["frames"][-1]["outcome"] == "error"
    assert any(row.get("text") == "Sorry, I encountered an error."
               for row in result["failure_turn"]["frames"])
    assert result["recovery_turn"]["terminal"] == "terminal_received"
    assert result["recovery_turn"]["frames"][-1].get("outcome") != "error"
    assert result["recovery_file"]["jsonl_valid"] and result["recovery_file"]["new_turn_persisted"]
    assert result["cleanup"]["remaining_owned_temps"] == 0
    stage = "SAVE" if boundary == "SAVE" else "RUN"
    assert any(row["type"] == "stage" and row["phase"] == "error"
               and row["payload"].get("stage_name") == stage for row in result["events"])
