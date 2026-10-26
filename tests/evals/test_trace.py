"""Trace integrity, honest unknowns, and backward-compatible evidence scoring."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from evals.common import ROOT, load_cases, read
from evals.scorer import score
from evals.trace import EvalHook, analyze_trace
from evals.worker import Emitter
from nanobot.agent.hook import AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.observability import (
    current_span_id,
    observation_context,
    point,
    span,
    usage_payload,
)


class MemorySink:
    def __init__(self):
        self.events = []

    def send(self, message):
        self.events.append(copy.deepcopy(message["event"]))


def example_trace(usage=None, status="known", sdk_retries=0):
    sink = MemorySink()
    emit = Emitter(sink, "fixture-run", "task-1")
    with observation_context(emit.event, "task-1"):
        with span("task"):
            with span("model_request", **usage_payload({"prompt_tokens": 999, "completion_tokens": 999})):
                with span("model_attempt", source="synthetic_fixture", sdk_retries=sdk_retries) as attempt:
                    attempt.update(usage=usage or {"input_tokens": 10, "output_tokens": 2},
                                   usage_status=status)
    return sink.events


def test_only_attempts_are_charged_once():
    events = example_trace()
    result = analyze_trace(events)
    assert result["valid"] and result["closed"]
    assert result["model_attempts"] == 1
    assert result["known_tokens"] == result["actual_tokens"] == 12
    assert result["amount"] is None


@pytest.mark.parametrize("change", ["event", "conflict", "terminal"])
def test_duplicate_records_invalidate_without_double_charging(change):
    events = example_trace()
    terminal = next(e for e in events if e["type"] == "model_attempt" and e["phase"] == "end")
    duplicate = copy.deepcopy(terminal)
    if change == "conflict":
        duplicate["payload"]["usage"]["input_tokens"] = 50
    elif change == "terminal":
        duplicate["event_id"] = "duplicate-terminal"
        duplicate["seq"] = len(events) + 1
    events.append(duplicate)
    result = analyze_trace(events)
    assert not result["valid"]
    assert result["known_tokens"] == 12
    assert result["model_attempts"] == 1
    assert result["actual_tokens"] is None


@pytest.mark.parametrize("usage,status,expected", [
    ({"input_tokens": 10, "output_tokens": None}, "partial", 10),
    ({"input_tokens": None, "output_tokens": None}, "unknown", 0),
    ({"input_tokens": 10, "output_tokens": 2}, "partial", 12),
])
def test_unknown_usage_is_not_a_zero_bill(usage, status, expected):
    result = analyze_trace(example_trace(usage, status))
    assert result["valid"]
    assert result["known_tokens"] == expected
    assert result["unknown_usage_attempts"] == 1
    assert result["actual_tokens"] is None


def test_sdk_coverage_unknown_is_distinct_from_known_returned_usage():
    result = analyze_trace(example_trace(sdk_retries=None))
    assert result["valid"]
    assert result["known_tokens"] == 12
    assert result["unknown_sdk_coverage_calls"] == 1
    assert result["actual_tokens"] is None


def test_raw_transport_field_presence_overrides_parser_zero_defaults():
    events = example_trace({"input_tokens": 0, "output_tokens": 0})
    attempt = next(e for e in events if e["type"] == "model_attempt" and e["phase"] == "start")
    transport = copy.deepcopy(attempt)
    transport.update(type="model_transport", phase="point", span_id="raw-sdk", event_id="raw-sdk",
                     parent_span_id=attempt["span_id"])
    transport["payload"] = {"usage": None, "usage_status": "unknown",
                            "usage_source": "sdk_response_usage", "sdk_retries": 0}
    events.insert(3, transport)
    for i, event in enumerate(events, 1):
        event["seq"] = i
    result = analyze_trace(events)
    assert result["valid"]
    assert result["unknown_usage_attempts"] == 1
    assert result["actual_tokens"] is None


def test_provider_call_without_raw_transport_keeps_usage_unknown():
    events = example_trace()
    attempt = next(event for event in events if event["type"] == "model_attempt"
                   and event["phase"] == "start")
    attempt["payload"]["source"] = "provider_call"
    result = analyze_trace(events)
    assert result["valid"]
    assert result["unknown_usage_attempts"] == 1
    assert result["unknown_sdk_coverage_calls"] == 1
    assert result["ledger"][0]["usage_status"] == "unknown"
    assert result["actual_tokens"] is None


def test_duplicate_raw_transports_invalidate_provider_call_trace():
    events = example_trace()
    attempt = next(event for event in events if event["type"] == "model_attempt"
                   and event["phase"] == "start")
    attempt["payload"]["source"] = "provider_call"
    transport = copy.deepcopy(attempt)
    transport.update(type="model_transport", phase="point", span_id="raw-sdk",
                     event_id="raw-sdk", parent_span_id=attempt["span_id"])
    transport["payload"] = {"usage": {"input_tokens": 10, "output_tokens": 2},
                            "usage_status": "known", "usage_source": "sdk_response_usage",
                            "sdk_retries": 0}
    duplicate = copy.deepcopy(transport)
    duplicate["event_id"] = "raw-sdk-duplicate"
    events.insert(3, transport)
    events.append(duplicate)
    for i, event in enumerate(events, 1):
        event["seq"] = i
    result = analyze_trace(events)
    assert not result["valid"]
    assert "duplicate_model_transport" in result["errors"]
    assert result["actual_tokens"] is None


@pytest.mark.parametrize("bad", [True, -1, "10", float("nan")])
def test_invalid_usage_cannot_be_known(bad):
    result = analyze_trace(example_trace({"input_tokens": bad, "output_tokens": 2}))
    assert not result["valid"]
    assert result["unknown_usage_attempts"] == 1
    assert usage_payload({"prompt_tokens": bad, "completion_tokens": 2})["usage_status"] == "partial"


@pytest.mark.parametrize("change", ["gap", "missing_parent", "task", "trial", "missing_terminal"])
def test_incomplete_or_misassociated_trace_fails(change):
    events = example_trace()
    if change == "gap":
        events[2]["seq"] += 50
    elif change == "missing_parent":
        events[2]["parent_span_id"] = "nonexistent"
    elif change == "task":
        events[2]["task_id"] = "foreign"
    elif change == "trial":
        events[2]["trial_id"] = "foreign"
    else:
        events.pop(3)
    assert not analyze_trace(events)["valid"]


def test_orphan_tool_execution_is_invalid():
    sink = MemorySink()
    with observation_context(Emitter(sink, "r", "t").event, "t"):
        with span("task"):
            point("tool_execution", name="write_file", call_id="orphan")
    result = analyze_trace(sink.events)
    assert not result["valid"]
    assert "tool_request_missing" in result["errors"]


@pytest.mark.asyncio
async def test_hook_cancelled_before_after_iteration_preserves_exception():
    sink = MemorySink()
    hook = EvalHook()
    with observation_context(Emitter(sink, "r", "t").event, "t") as state:
        with pytest.raises(asyncio.CancelledError):
            with span("task"):
                with span("stage", stage_name="RUN"):
                    await hook.before_iteration(AgentHookContext(1, []))
                    raise asyncio.CancelledError()
        assert current_span_id() is None
        assert not state.collector_errors
    result = analyze_trace(sink.events)
    assert result["valid"] and result["closed"]
    assert [e["phase"] for e in sink.events if e["type"] == "iteration"] == ["start", "cancelled"]
    assert hook.wants_streaming() is False
    assert hook.finalize_content(AgentHookContext(1, []), "unchanged") == "unchanged"


@pytest.mark.asyncio
async def test_collector_failure_never_replaces_task_result():
    def broken(kind, **kwargs):
        raise OSError("sink unavailable")

    with observation_context(broken, "task") as state:
        with span("task"):
            result = "normal response"
    assert result == "normal response"
    assert len(state.collector_errors) == 2
    assert not analyze_trace([], collector_errors=state.collector_errors)["valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["extension", "system", "exception", "cancelled"])
async def test_root_task_exists_before_early_branches(branch):
    sink = MemorySink()
    result = OutboundMessage("cli", "eval", "unchanged")
    calls = []

    async def intercept(loop, message):
        calls.append("extension")
        if branch == "exception":
            raise ValueError("private failure")
        if branch == "cancelled":
            raise asyncio.CancelledError()
        return True, result

    async def system(*args, **kwargs):
        calls.append("system")
        return result

    # Only test the entry branch: no constructors or real services needed.
    loop = SimpleNamespace(_refresh_provider_snapshot=lambda: None,
                           turn_extension=SimpleNamespace(intercept=intercept) if branch != "system" else None,
                           _process_system_message=system)
    message = InboundMessage("system" if branch == "system" else "cli", "user", "eval", "input")
    with observation_context(Emitter(sink, "r", "t").event, "t"):
        if branch in ("exception", "cancelled"):
            with pytest.raises(ValueError if branch == "exception" else asyncio.CancelledError):
                await AgentLoop._process_message(loop, message)
        else:
            assert await AgentLoop._process_message(loop, message) is result
    assert len(calls) == 1
    assert sink.events[0]["type"] == "task"
    assert sink.events[-1]["phase"] == {"exception": "error", "cancelled": "cancelled"}.get(branch, "end")
    assert "private failure" not in str(sink.events)
    assert analyze_trace(sink.events)["valid"]


def test_all_phase2_evidence_keeps_its_original_verdict(history_tree):
    cases = {case["case_id"]: case for case in load_cases()}
    count = 0
    for mode in ("D", "R"):
        root = history_tree / "docs/agent-evaluation-phase2-runs" / (mode + "-20261009/trials")
        for trial in root.iterdir():
            original = read(trial / "verdicts/1.json")
            case_id = read(trial / "trial.json")["case_id"]
            actual = score(cases[case_id], read(trial / "artifacts/evidence.json"))
            assert actual["status"] == original["status"]
            count += 1
    assert count == 19


def test_phase3_scorer_keeps_unknown_sdk_coverage_undetermined():
    # Synthetic transport metadata; no actual model or archived tmp dependency.
    events = example_trace(sdk_retries=None)
    sink = MemorySink()
    with observation_context(Emitter(sink, "fixture-run", "task-1").event, "task-1"):
        with span("iteration"):
            pass
        point("delivery", response='{"total":330}', outbox_count=0)
    evidence = {"events": events + sink.events, "observation_version": "phase3/1.0",
                "trial_id": "task-1", "before": {}, "after": {}, "workspace": str(ROOT),
                "execution_status": "finished", "ready": True, "cleanup": {"ok": True}}
    for event in evidence["events"]:
        if event["type"] == "model_attempt" and event["phase"] == "end":
            event["payload"].update(usage={"input_tokens": 5, "output_tokens": 1}, usage_status="known")
    result = score(load_cases()[0], evidence)
    assert result["status"] == "undetermined"
    assert result["extensions"]["known_tokens"] == 6
    assert result["extensions"]["unknown_sdk_coverage_calls"] == 1


def cancellation_evidence(operator=True, changed=False):
    sink = MemorySink()
    emit = Emitter(sink, "fixture-run", "task-1")
    if operator:
        task_id = emit.event("task", "start")
        parent = Emitter(sink, "fixture-run", "task-1")
        parent.producer = "parent-" + parent.producer
        parent.event("watchdog", open_span_ids=[task_id], product_terminals_synthesized=False)
    else:
        with observation_context(emit.event, "task-1"):
            with pytest.raises(asyncio.CancelledError):
                with span("task"):
                    raise asyncio.CancelledError()
    return {"events": sink.events, "observation_version": "phase3/1.0",
            "trial_id": "task-1", "before": {},
            "after": {"unauthorized.txt": {"sha256": "changed"}} if changed else {},
            "workspace": str(ROOT), "execution_status": "cancelled",
            "cancel_reason": "operator_interrupted" if operator else None,
            "ready": True, "cleanup": {"ok": True}}


@pytest.mark.parametrize("operator,changed,status", [
    (True, False, "undetermined"), (True, True, "product_fail"),
    (False, False, "product_fail"),
])
def test_cancelled_trial_distinguishes_operator_from_product(operator, changed, status):
    evidence = cancellation_evidence(operator, changed)
    result = score(load_cases()[0], evidence)
    assert result["status"] == status
    assert result["extensions"]["trace_valid"]
    if operator:
        assert result["requirements"][0]["label"] == "unknown"
        assert analyze_trace(evidence["events"], "cancelled")["actual_tokens"] is None
    else:
        assert {r["reason_code"] for r in result["reasons"]} >= {"task_cancelled"}


def test_operator_cancellation_does_not_hide_delivered_wrong_answer():
    evidence = cancellation_evidence()
    sink = MemorySink()
    with observation_context(Emitter(sink, "fixture-run", "task-1").event, "task-1"):
        with span("iteration"):
            pass
        point("delivery", response='{"total":360}', outbox_count=0)
    evidence["events"].extend(sink.events)
    result = score(load_cases()[0], evidence)
    assert result["status"] == "product_fail"
    assert result["requirements"][0]["label"] == "fail"


@pytest.mark.parametrize("cancel_at", [1, 5])
def test_run_operator_cancellation_stops_plan_even_on_last_trial(tmp_path, monkeypatch, cancel_at):
    from evals import run as runner

    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setattr(runner, "phase1_audit", lambda: {})
    monkeypatch.setattr(runner, "manifest", lambda *args: {})
    calls = []

    def trial(case, plan, *args):
        calls.append(case["case_id"])
        cancelled = len(calls) == cancel_at
        return {**plan, "status": "undetermined" if cancelled else "pass",
                "cancel_reason": "operator_interrupted" if cancelled else None,
                "elapsed_ms": 1, "initial_state_sha256": "fixture",
                "cleanup": {"ok": True, "instance_removed": True}}

    monkeypatch.setattr(runner, "run_trial", trial)
    summary = runner.run("D", tmp_path / "run")
    assert len(calls) == cancel_at
    assert summary["overall_status"] == "incomplete"
    assert len(summary["missing_ids"]) == 5 - cancel_at
