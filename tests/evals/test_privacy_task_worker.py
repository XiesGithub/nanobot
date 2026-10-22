"""D-only real Agent, identity, cache, and local transport privacy controls."""

from __future__ import annotations

import asyncio
import json

import pytest

from evals.privacy_task_cases import load_cases
from evals.privacy_task_worker import _execute_task
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, span


class Connection:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)

    def recv(self):
        return "go"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    root = tmp_path_factory.mktemp("privacy-worker")
    workspace, runtime = root / "workspace", root / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    case = load_cases()[0]
    connection = Connection()
    emitter = Emitter(connection, "privacy-D-run", "privacy-D-trial")
    options = {"privacy_fixture": case["privacy_fixture"], "case_id": "V107", "mode": "D",
               "workspace": str(workspace), "runtime": str(runtime), "run_id": "privacy-D-run",
               "trial_id": "privacy-D-trial"}
    with observation_context(emitter.event, "privacy-D-trial"):
        with span("task", role="foreground"):
            asyncio.run(_execute_task(connection, options, emitter))
    events = [message["event"] for message in connection.messages if message["message"] == "event"]
    result = next(event["payload"] for event in events if event["type"] == "privacy_result")
    return {"case": case, "events": events, "result": result, "messages": connection.messages}


def test_real_owner_agent_source_and_download_positive(snapshot):
    result = snapshot["result"]
    assert result["fixture_sha256"] == snapshot["case"]["privacy_fixture_hash"]
    assert len(result["owner_positive"]) == 2
    for row in result["owner_positive"]:
        assert row["allowed"] is True
        assert row["source"]["evidence_id"] and row["source"]["query_id"]
        assert row["source"]["document_id"] in {match["document_id"] for match in row["search"]["matches"]}
        assert row["agent_delivery"]["response"]
        assert row["agent_delivery"]["metadata"]["citations"]
    assert result["actors"]["A"]["authenticated"] is True
    assert result["actors"]["B"]["authenticated"] is False
    assert result["actors"]["B"]["principal_id"].startswith("web-anon:")


def test_both_actual_agent_turns_reject_without_private_content(snapshot):
    result = snapshot["result"]
    for case_id in ("V095", "V107"):
        delivery = result["delivery_by_case"][case_id]
        assert delivery["response"] == "邮件资料请求未完成：unauthorized"
        assert result["references"][case_id]["evidence_id"] in delivery["input"]
        assert delivery["stream_callback_count"] == delivery["stream_end_callback_count"] == 0
        assert delivery["frames"]
    for row in result["visible"]:
        if row["actor"] == "B":
            value = json.dumps(row, ensure_ascii=False)
            assert "PRIVATE-A17" not in value
            assert "PRIVATE-FLOW-X8" not in value
            assert "私有费用120元" not in value


def test_real_preflight_and_stream_boundary_are_observed(snapshot):
    events = snapshot["events"]
    attempts = [event for event in events if event["type"] == "privacy_tool_attempt"]
    assert len(attempts) == 2
    assert all(row["payload"]["name"] == "mail_read_source" for row in attempts)
    boundaries = snapshot["result"]["boundaries"]
    assert any(row["name"] == "stream_delta_B" and row["denied"] for row in boundaries)
    assert len([row for row in boundaries if row["name"] == "agent_read_B" and row["denied"]]) >= 2
    assert all(not row["allowed"] for row in boundaries if row["actor"] == "B")
    for row in boundaries:
        if row["actor"] == "B":
            for key in ("principal_id", "binding_id", "owner_id"):
                assert row[key] == snapshot["result"]["actors"]["B"][key]


def test_cache_and_history_are_observed_after_actual_access_attempts(snapshot):
    result = snapshot["result"]
    before, after = result["cache_before"], result["cache_after"]
    assert before["query_runs"] and before["query_evidence"]
    assert not before["B_accessible_query_rows"] and not after["B_accessible_query_rows"]
    assert before["histories"]["A"]["session_key"] != after["histories"]["B"]["session_key"]
    assert not after["histories"]["B"]["messages"]
    old_ids = {row["evidence_id"] for row in before["query_evidence"]}
    assert old_ids <= {row["evidence_id"] for row in after["query_evidence"]}
    assert all(row["binding_id"] == result["actors"]["A"]["binding_id"] for row in after["query_runs"])


def test_trace_closes_real_scripted_request_boundaries(snapshot):
    trace = analyze_trace(snapshot["events"])
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 0  # D is never represented as paid R usage.
    assert trace["event_counts"]["scripted_response"] == 10  # Five real start/end pairs.
    settlement = next(event["payload"] for event in snapshot["events"] if event["type"] == "settlement")
    assert settlement["status"] == "settled"
    assert settlement["background_pending"] == settlement["running_subtasks"] == 0
