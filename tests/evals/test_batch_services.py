"""Executable checks for the Stage 6 service and transport adapters."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.batch_services import cases, execute
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, span


def _workspace(tmp_path: Path, spec):
    for relative, content in spec["fixtures"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _assertions(spec, observed):
    for row in spec["assertions"]:
        actual = observed[row["key"]]
        expected = row["expected"]
        if row["op"] == "equal":
            assert type(actual) is type(expected)
            assert actual == expected
        elif row["op"] == "contains":
            assert expected in actual
        else:
            assert actual is True


def _event_sink(events):
    def sink(kind, **fields):
        events.append({"type": kind, **fields})
    return sink


def test_catalog_covers_v041_to_v080_and_explains_browser_gaps():
    catalog = cases()
    assert set(catalog) == {f"V{i:03}" for i in range(41, 81)}
    assert all(spec["scope"] and spec["entry"] and isinstance(spec["fixtures"], dict)
               for spec in catalog.values())
    assert all(spec["limitations"] for spec in catalog.values())
    assert not catalog["V073"]["supported"]
    assert "Playwright" in catalog["V073"]["limitations"][0]
    assert not catalog["V079"]["supported"]
    assert "邮件专项脚本" in catalog["V079"]["limitations"][0]


SUPPORTED = [case_id for case_id, spec in cases().items()
             if spec["supported"] and case_id != "V048"]


@pytest.mark.parametrize("case_id", SUPPORTED)
async def test_supported_service_contracts(case_id, tmp_path):
    spec = cases()[case_id]
    workspace = _workspace(tmp_path, spec)
    observed = await execute(case_id, workspace)
    _assertions(spec, observed)


async def test_v048_rejects_invalid_execution_before_mcp_call(tmp_path):
    spec = cases()["V048"]
    observed = await execute("V048", _workspace(tmp_path, spec))
    _assertions(spec, observed)
    assert "order_id should be string" in observed["invalid_result"]
    assert observed["invalid_server_calls"] == []
    assert observed["valid_server_calls"] == ["A17"]
    assert observed["server_calls"] == ["A17"]


async def test_subagent_and_background_observability_carries_parent_task(tmp_path):
    class CaptureConnection:
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    connection = CaptureConnection()
    trial_id = "trial-parent"
    emitter = Emitter(connection, "run-subagent-observation", trial_id)
    spec = cases()["V041"]
    workspace = _workspace(tmp_path, spec)
    with observation_context(emitter.event, trial_id) as observation:
        with span("task", case_id="V041", scope=spec["scope"]):
            observed = await execute("V041", workspace)
    _assertions(spec, observed)

    events = [message["event"] for message in connection.messages
              if message.get("message") == "event"]
    lifecycle = [event for event in events if event["type"] == "subtask_lifecycle"]
    trace = analyze_trace(events, collector_errors=observation.collector_errors)
    spawn = next(event for event in events if event["type"] == "subtask_spawn")
    child_task = next(event for event in events if event["type"] == "task"
                      and event["payload"].get("role") == "subagent")
    delivered = next(event for event in events if event["type"] == "subtask_delivery")
    assert trace["valid"] and trace["closed"]
    assert observation.collector_errors == []
    assert len(lifecycle) >= 2
    assert all(event["task_id"] == child_task["task_id"] for event in lifecycle)
    assert all(event["parent_task_id"] == trial_id for event in lifecycle)
    assert spawn["task_id"] == trial_id
    assert child_task["task_id"] != trial_id
    assert child_task["parent_task_id"] == trial_id
    assert delivered["task_id"] == child_task["task_id"]


async def test_cron_and_heartbeat_emit_background_task_events(tmp_path):
    for case_id in ("V053", "V059"):
        events = []
        sink = _event_sink(events)
        spec = cases()[case_id]
        workspace = _workspace(tmp_path / case_id, spec)
        with observation_context(sink, f"trial-{case_id}"):
            observed = await execute(case_id, workspace)
        _assertions(spec, observed)
        task = next(event for event in events if event["type"] == "task"
                    and event.get("role") == "background")
        assert task["task_id"] != f"trial-{case_id}"
        assert task["parent_task_id"] == f"trial-{case_id}"
        assert any(event["type"] == "background_job" for event in events)


async def test_channel_delivery_attempt_is_observed_without_payload(tmp_path):
    from nanobot.bus.events import OutboundMessage
    from nanobot.channels.manager import ChannelManager

    events = []
    sink = _event_sink(events)
    manager = ChannelManager.__new__(ChannelManager)
    manager.config = SimpleNamespace(channels=SimpleNamespace(send_max_retries=1))
    sent = []

    class Channel:
        async def send(self, message):
            sent.append(message.content)

    with observation_context(sink, "trial-delivery"):
        await manager._send_with_retry(Channel(), OutboundMessage("test", "user-private", "fixed payload"))

    attempts = [event for event in events if event["type"] == "delivery_attempt"]
    writes = [event for event in events if event["type"] == "delivery_write"]
    assert len(attempts) == 2  # start/end of one attempt
    assert len(writes) == 2
    assert attempts[-1]["phase"] == "end"
    assert attempts[-1]["status"] == "adapter_send_returned"
    assert sent == ["fixed payload"]
    assert all("payload" not in event for event in attempts + writes)


def test_supported_contracts_do_not_claim_full_agent_tasks():
    for spec in cases().values():
        if spec["supported"]:
            assert spec["scope"] in {"module_contract", "transport"}
            assert spec["limitations"]
