"""All network probes run against controlled DNS and in-memory HTTP."""

import pytest

from evals.batch_security import cases, execute
from evals.batches import score
from evals.trace import analyze_trace
from nanobot.utils.observability import observation_context, span


@pytest.mark.parametrize("case_id", ["V099", "V100", "V102", "V109", "V110"])
async def test_safe_boundary_cases(case_id, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = cases()[case_id]
    for path, text in spec["fixtures"].items():
        (workspace / path).write_text(text, encoding="utf-8")
    observed = await execute(case_id, workspace)
    verdict = score(spec, {"status": "finished", "observed": observed,
                           "collector_errors": [], "trace": {"valid": True}})
    assert verdict["verdict"] == "pass"


async def test_redirect_is_blocked_before_simulated_private_connection(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed = await execute("V103", workspace)
    assert observed["redirect_blocked"]
    assert observed["dns_blocked"]
    assert observed["dns_transport_requests"] == 0
    assert observed["private_connections"] == 0
    assert observed["request_ledger"] == [
        {"url": "https://a.example.test/page", "host": "a.example.test"}]
    verdict = score(cases()["V103"], {"status": "finished", "observed": observed,
                                     "collector_errors": [], "trace": {"valid": True}})
    assert verdict["verdict"] == "pass"


async def test_link_without_platform_privilege_is_unknown(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def unavailable(*args, **kwargs):
        raise PermissionError("synthetic unavailable symlink capability")

    monkeypatch.setattr(type(workspace), "symlink_to", unavailable)
    observed = await execute("V101", workspace)
    assert "__environment_unverified__" in observed
    assert not (workspace / "link").exists()


async def test_execution_event_is_counted_once(tmp_path):
    from evals.worker import Emitter

    events = []
    sink = type("Sink", (), {"send": lambda _, message: events.append(message["event"])})()
    emitter = Emitter(sink, "run", "trial")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with observation_context(emitter.event, "trial") as state, span("task"):
        await execute("V099", workspace)
    trace = analyze_trace(events, collector_errors=state.collector_errors)
    assert trace["valid"], trace["errors"]
    assert trace["tool_requested"] == trace["tool_executed"] == 1
