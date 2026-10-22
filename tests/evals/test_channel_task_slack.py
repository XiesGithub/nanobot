"""Contract for local SDK envelopes through the real Slack/Agent/session path.

The adapter is deliberately not added while another evaluation's source snapshot
is frozen. This module skips only until that adapter exists; it uses no LLM or
Slack platform connection when enabled.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.util
import json
import re

import pytest

from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.observability import observation_context, span

if importlib.util.find_spec("evals.channel_task_slack") is None:
    pytest.skip("Slack full-task adapter awaits the active source freeze", allow_module_level=True)

run_slack = importlib.import_module("evals.channel_task_slack").run_slack

TURN_IDS = ("seed_A", "seed_B", "ask_B", "ask_A")
GOLD = {"A": "A17", "B": "B23"}


class Connection:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class HistoryProvider(LLMProvider):
    """D response depends solely on the real messages, never the actor label."""

    observation_mode = "scripted"

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.generation = GenerationSettings(temperature=0, max_tokens=128)

    def get_default_model(self):
        return "deepseek-v4-flash"

    async def chat(self, messages, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        user_texts = [message.get("content", "") for message in messages
                      if message.get("role") == "user"]
        latest = json.dumps(user_texts[-1], ensure_ascii=False) if user_texts else ""
        if "本线程代号是什么？" not in latest:
            return LLMResponse(content="已记录。", finish_reason="stop")
        history = json.dumps(user_texts, ensure_ascii=False)
        codes = re.findall(r"本线程代号(A17|B23)", history)
        return LLMResponse(content=codes[-1] if codes else "未获得代号。", finish_reason="stop")


def _run(tmp_path, control=None, *, default_provider=False, supplied_provider=None):
    workspace, runtime = tmp_path / "workspace", tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    provider = None if default_provider else supplied_provider or HistoryProvider()
    connection = Connection()
    emitter = Emitter(connection, "slack-contract-D", "slack-contract-trial")
    with observation_context(emitter.event, "slack-contract-trial"):
        with span("task", case_id="V063", role="foreground"):
            result = asyncio.run(run_slack(runtime, workspace, provider, emitter, control=control))
    events = [row["event"] for row in connection.messages if row["message"] == "event"]
    return result, provider, events


def _ask_deliveries(result):
    return [row for row in result["deliveries"] if row["turn_id"].startswith("ask_")]


def _codes(value):
    return set(re.findall(r"A17|B23", json.dumps(value, ensure_ascii=False)))


def _correct_delivery(result, row):
    actor = result["actors"][row["thread"]]
    return (row["channel"] == actor["channel"] and row["thread_ts"] == actor["thread_ts"]
            and _codes(row["text"]) == {GOLD[row["thread"]]})


def test_sdk_envelopes_ack_and_real_inbound_sequence(tmp_path):
    result, _, _ = _run(tmp_path)
    requests = [row for row in result["events"] if row["kind"] == "socket_request"]
    acknowledgements = [row for row in result["events"] if row["kind"] == "socket_ack"]
    inbound = [row for row in result["events"] if row["kind"] == "inbound"]
    assert [row["turn_id"] for row in requests] == list(TURN_IDS)
    assert [row["turn_id"] for row in inbound] == list(TURN_IDS)
    assert len(acknowledgements) == 4
    assert {row["response"]["envelope_id"] for row in acknowledgements} == {
        row["envelope"]["envelope_id"] for row in requests
    }
    channel_ids, user_ids = set(), set()
    for request, accepted in zip(requests, inbound, strict=True):
        envelope = request["envelope"]
        assert envelope["type"] == "events_api"
        assert request["sdk_class"] == "slack_sdk.socket_mode.request.SocketModeRequest"
        event = envelope["payload"]["event"]
        assert event["type"] == "message" and event["channel_type"] == "channel"
        assert not event.get("subtype")
        channel_ids.add(event["channel"])
        user_ids.add(event["user"])
        actor = result["actors"][request["thread"]]
        assert event["thread_ts"] == actor["thread_ts"]
        assert event["ts"] != event["thread_ts"]  # Genuine replies, not DM/channel roots.
        assert accepted["session_key"] == actor["session_key"]
    assert len(channel_ids) == len(user_ids) == 1


def test_interleaved_answers_use_actual_thread_history_and_wire_target(tmp_path):
    result, provider, events = _run(tmp_path)
    assert len(provider.inputs) == len(result["model_inputs"]) == 4
    assert [row["messages"] for row in result["model_inputs"]] == provider.inputs
    assert result["actors"]["A"]["session_key"] != result["actors"]["B"]["session_key"]
    for row in result["model_inputs"]:
        assert _codes(row["messages"]) == {GOLD[row["thread"]]}
        assert row["session_key"] == result["actors"][row["thread"]]["session_key"]
    replies = _ask_deliveries(result)
    assert [row["turn_id"] for row in replies] == ["ask_B", "ask_A"]
    assert all(_correct_delivery(result, row) for row in replies)
    posts = [row for row in result["events"] if row["kind"] == "sdk_http"
             and row["method"] == "chat.postMessage"]
    assert len(posts) == len(result["deliveries"]) == 4
    for post, delivery in zip(posts, result["deliveries"], strict=True):
        assert {key: post["parameters"][key] for key in ("channel", "thread_ts", "text")} == {
            key: delivery[key] for key in ("channel", "thread_ts", "text")
        }
        assert post["metadata"] == delivery["metadata"]
        assert delivery["metadata"]["slack"]["thread_ts"] == delivery["thread_ts"]
        assert not any(delivery["metadata"].get(key) for key in ("_progress", "_tool_hint", "_retry_wait"))
    for thread, state in result["session_state"].items():
        assert state["session_key"] == result["actors"][thread]["session_key"]
        assert _codes(state["messages"]) == {GOLD[thread]}
        assert state["relative_path"].startswith("sessions/") and state["sha256"]
    assert all(row["empty"] for rows in result["memory_state"].values() for row in rows)
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 0
    assert trace["event_counts"]["scripted_response"] == 8
    assert result["SDK_version"]
    assert result["platform_scope"]["real_platform_connected"] is False
    assert result["platform_scope"]["sdk_http_transport"] == "loopback"
    assert result["platform_scope"]["tools"] == []


def test_default_provider_runs_only_scripted_actual_history(tmp_path):
    result, provider, events = _run(tmp_path, default_provider=True)
    assert provider is None
    assert all(_correct_delivery(result, row) for row in _ask_deliveries(result))
    assert len(result["model_inputs"]) == 4
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"]
    assert trace["model_attempts"] == 0
    assert trace["event_counts"]["scripted_response"] == 8


def test_fault_controls_cannot_change_a_real_provider_response(tmp_path):
    provider = HistoryProvider()
    provider.observation_mode = "real"
    connection = Connection()
    emitter = Emitter(connection, "slack-contract-D", "slack-contract-trial")
    with pytest.raises(ValueError, match="slack_fault_controls_require_scripted_provider"):
        asyncio.run(run_slack(tmp_path / "runtime", tmp_path / "workspace", provider, emitter,
                              control="wrong_answer"))
    assert not provider.inputs and not connection.messages


def test_real_retry_callback_keeps_every_sdk_delivery_and_exact_progress_metadata(tmp_path):
    class RetryProvider(HistoryProvider):
        _CHAT_RETRY_DELAYS = (0.001,)

        async def chat(self, messages, **kwargs):
            if not self.inputs:
                self.inputs.append(copy.deepcopy(messages))
                raise RuntimeError("503 unavailable (synthetic D provider failure)")
            return await super().chat(messages, **kwargs)

    result, provider, events = _run(tmp_path, supplied_provider=RetryProvider())
    assert len(provider.inputs) == len(result["model_inputs"]) == 5
    assert [row["turn_id"] for row in result["model_inputs"]] == ["seed_A", *TURN_IDS]
    assert [row["messages"] for row in result["model_inputs"]] == provider.inputs
    posts = [row for row in result["events"] if row["kind"] == "sdk_http"
             and row["method"] == "chat.postMessage"]
    assert len(posts) == len(result["deliveries"]) > 4
    progress = [row for row in result["deliveries"] if row["metadata"].get("_retry_wait") is True]
    assert progress
    assert all(row["metadata"]["_progress"] is True and row["thread"] == "A"
               and row["thread_ts"] == result["actors"]["A"]["thread_ts"] for row in progress)
    final = [row for row in result["deliveries"] if not any(row["metadata"].get(key)
             for key in ("_progress", "_tool_hint", "_retry_wait"))]
    assert len(final) == 4
    assert all(_correct_delivery(result, row) for row in final if row["turn_id"].startswith("ask_"))
    for post, delivery in zip(posts, result["deliveries"], strict=True):
        assert post["metadata"] == delivery["metadata"]
        assert post["parameters"]["text"] == delivery["text"]
        assert post["parameters"]["thread_ts"] == delivery["thread_ts"]
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 0
    assert trace["event_counts"]["scripted_response"] == 10
    assert any(row["type"] == "provider_exception" for row in events)


def test_wrong_answer_is_visible_and_cannot_satisfy_both_threads(tmp_path):
    result, _, _ = _run(tmp_path, "wrong_answer")
    replies = _ask_deliveries(result)
    assert len(replies) == 2
    assert any(not _correct_delivery(result, row) for row in replies)


def test_cross_session_fault_is_detectable_in_real_model_or_saved_history(tmp_path):
    result, _, _ = _run(tmp_path, "cross_session")
    observed = [row["messages"] for row in result["model_inputs"]]
    observed.extend(row["messages"] for row in result["session_state"].values())
    assert any(_codes(value) == {"A17", "B23"} for value in observed)


def test_wrong_reply_thread_fault_remains_in_actual_sdk_wire(tmp_path):
    result, _, _ = _run(tmp_path, "wrong_reply_thread")
    replies = _ask_deliveries(result)
    assert len(replies) == 2
    assert any(row["thread_ts"] != result["actors"][row["thread"]]["thread_ts"] for row in replies)
    posts = [row for row in result["events"] if row["kind"] == "sdk_http"
             and row["method"] == "chat.postMessage" and row["turn_id"].startswith("ask_")]
    assert [row["parameters"]["thread_ts"] for row in posts] == [row["thread_ts"] for row in replies]


def test_illegal_sdk_events_are_acknowledged_without_agent_or_business_delivery(tmp_path):
    result, provider, events = _run(tmp_path, "illegal_event")
    requests = [row for row in result["events"] if row["kind"] == "socket_request"]
    assert len(requests) == 4
    assert all(row["envelope"]["payload"]["event"]["subtype"] == "message_changed" for row in requests)
    assert len([row for row in result["events"] if row["kind"] == "socket_ack"]) == 4
    assert not [row for row in result["events"] if row["kind"] == "inbound"]
    assert not provider.inputs and not result["model_inputs"] and not result["deliveries"]
    assert not [row for row in result["events"] if row["kind"] == "sdk_http"
                and row["method"] == "chat.postMessage"]
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"]
    assert trace["model_attempts"] == trace["event_counts"].get("scripted_response", 0) == 0
