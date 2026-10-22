"""V118 uses actual abrupt exit and a new AgentLoop process."""

from evals.batch_checkpoint import cases, execute
from evals.batches import score


async def test_durable_tool_checkpoint_survives_abrupt_process_exit(tmp_path):
    spec = cases()["V118"]
    for path, text in spec["fixtures"].items():
        (tmp_path / path).write_text(text, encoding="utf-8")
    observed = await execute("V118", tmp_path)
    verdict = score(spec, {"status": "finished", "observed": observed,
                           "collector_errors": [], "trace": {"valid": True}})
    assert verdict["verdict"] == "pass", observed
    assert observed["crash_exit"] == 61
    assert observed["tool_write_count"] == 1
