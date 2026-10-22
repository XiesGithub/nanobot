"""Focused regressions for background and subtask observation boundaries."""

import asyncio
import hashlib
import json

import pytest

from evals.batch_services import cases, execute
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.heartbeat.service import HeartbeatService
from nanobot.utils.observability import observation_context, span


class _CaptureConnection:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _events(connection):
    return [message["event"] for message in connection.messages
            if message.get("message") == "event"]


async def test_heartbeat_swallowed_exception_is_observed_as_error(tmp_path):
    (tmp_path / "HEARTBEAT.md").write_text("run the controlled task", encoding="utf-8")

    async def fail_execution(_tasks):
        raise RuntimeError("controlled failure")

    service = HeartbeatService(tmp_path, provider=None, model="test", on_execute=fail_execution)

    async def decide(_content):
        return "run", "controlled task"

    service._decide = decide
    connection = _CaptureConnection()
    emitter = Emitter(connection, "run-heartbeat-error", "trial-heartbeat-error")
    with observation_context(emitter.event, "trial-heartbeat-error") as observation:
        with span("task", role="evaluation"):
            # Heartbeat deliberately swallows execution exceptions; the telemetry
            # must still distinguish this tick from a successful one.
            await service._tick()

    events = _events(connection)
    actions = [event["payload"]["action"] for event in events
               if event["type"] == "background_job"]
    errors = [event for event in events if event["type"] == "background_job"
              and event["payload"]["action"] == "error"]
    task_end = next(event for event in events if event["type"] == "task"
                    and event["phase"] == "end"
                    and event["payload"].get("background_kind") == "heartbeat_tick")
    trace = analyze_trace(events, collector_errors=observation.collector_errors)
    assert actions == ["started", "error"]
    assert errors[0]["payload"]["error_kind"] == "RuntimeError"
    assert task_end["payload"]["outcome"] == "error"
    assert trace["valid"] and trace["closed"]
    assert observation.collector_errors == []


async def test_cron_cancellation_is_observed_and_propagates(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_cancel(_job):
        entered.set()
        await release.wait()

    service = CronService(tmp_path / "jobs.json", on_job=wait_for_cancel)
    job = CronJob(
        id="controlled-job",
        name="controlled job",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(message="controlled task"),
    )
    connection = _CaptureConnection()
    emitter = Emitter(connection, "run-cron-cancel", "trial-cron-cancel")
    with observation_context(emitter.event, "trial-cron-cancel") as observation:
        with span("task", role="evaluation"):
            execution = asyncio.create_task(service._execute_job(job))
            await entered.wait()
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution

    events = _events(connection)
    actions = [event["payload"]["action"] for event in events
               if event["type"] == "background_job"]
    trace = analyze_trace(events, "cancelled", observation.collector_errors)
    assert actions == ["started", "cancelled"]
    assert trace["valid"] and trace["closed"]
    assert observation.collector_errors == []


async def test_subagent_observation_hashes_session_and_chat_identifiers(tmp_path):
    spec = cases()["V041"]
    for relative, content in spec["fixtures"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    connection = _CaptureConnection()
    emitter = Emitter(connection, "run-subagent-privacy", "trial-subagent-privacy")
    with observation_context(emitter.event, "trial-subagent-privacy") as observation:
        with span("task", role="evaluation"):
            observed = await execute("V041", tmp_path)

    assert observed["announcement_count"] == 1
    events = _events(connection)
    serialized = json.dumps(events, ensure_ascii=False)
    session_key = "cli:case-v041"
    chat_id = "direct"
    assert session_key not in serialized
    assert chat_id not in serialized

    payloads = [event["payload"] for event in events]
    assert all("origin_session_key" not in payload for payload in payloads)
    assert all("origin_chat_id" not in payload for payload in payloads)
    assert all("target_chat_id" not in payload for payload in payloads)

    spawn = next(payload for payload in payloads if "origin_chat_id_hash" in payload)
    delivery = next(payload for payload in payloads if "target_chat_id_hash" in payload)
    assert spawn["origin_chat_id_hash"] == hashlib.sha256(chat_id.encode()).hexdigest()
    assert spawn["origin_session_hash"] == hashlib.sha256(session_key.encode()).hexdigest()
    assert delivery["target_chat_id_hash"] == hashlib.sha256(chat_id.encode()).hexdigest()
    assert delivery["origin_session_hash"] == hashlib.sha256(session_key.encode()).hexdigest()
    assert analyze_trace(events, collector_errors=observation.collector_errors)["closed"]
    assert observation.collector_errors == []


async def test_subagent_announce_does_not_put_plaintext_routing_ids_in_events():
    connection = _CaptureConnection()
    emitter = Emitter(connection, "run-subagent-delivery-privacy", "trial-delivery-privacy")
    manager = SubagentManager.__new__(SubagentManager)
    manager.bus = MessageBus()
    origin = {"channel": "test", "chat_id": "user-123", "session_key": "test:user-123"}

    with observation_context(emitter.event, "trial-delivery-privacy") as observation:
        with span("task", role="evaluation"):
            await manager._announce_result(
                "child-1", "label", "task", "result", origin, "ok",
            )

    events = _events(connection)
    delivery = next(event for event in events if event["type"] == "subtask_delivery")
    assert delivery["payload"]["origin_session_hash"] == hashlib.sha256(
        origin["session_key"].encode(),
    ).hexdigest()
    assert delivery["payload"]["target_chat_id_hash"] == hashlib.sha256(
        origin["chat_id"].encode(),
    ).hexdigest()
    assert origin["session_key"] not in json.dumps(events)
    assert origin["chat_id"] not in json.dumps(events)
    assert observation.collector_errors == []
