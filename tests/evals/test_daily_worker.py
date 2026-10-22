"""Pre-dispatch request budgets with fake SDK responses; no real model calls."""

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.common import MODEL
from evals.daily_worker import (
    _agent_budget_record,
    _safe_json_bytes,
    _zip_common,
    install_request_budget,
)
from evals.scorer import score
from evals.worker import BudgetExceeded


class FakeEmitter:
    def __init__(self):
        self.events = []

    def event(self, kind, **payload):
        self.events.append({"type": kind, "payload": payload})


def fake_provider(create):
    return SimpleNamespace(_client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))


def request_kwargs():
    return {"model": MODEL, "messages": [{"role": "user", "content": "固定测试"}],
            "max_tokens": 16, "stream": False}


def test_request_budget_allows_exact_pre_reservation_boundary():
    kwargs = request_kwargs()
    request_bytes = len(_safe_json_bytes(kwargs))
    calls = []

    async def create(**request):
        calls.append(request)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3))

    provider = fake_provider(create)
    emit = FakeEmitter()
    state = install_request_budget(provider, {"max_attempts": 1,
                                               "max_tokens": request_bytes + 16}, emit)
    response = asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert response.usage.prompt_tokens == 4
    assert state["attempts_dispatched"] == 1
    assert state["known_tokens"] == 7
    assert emit.events[0]["payload"]["action"] == "dispatch_reserved"
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert len(calls) == 1


def test_successful_request_budget_records_do_not_trigger_scorer_failure():
    kwargs = request_kwargs()

    async def create(**request):
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3))

    emit = FakeEmitter()
    provider = fake_provider(create)
    install_request_budget(provider, {"max_attempts": 1, "max_tokens": 10000}, emit)
    asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert {event["type"] for event in emit.events} == {"request_budget"}

    events = []

    def add_event(kind, phase="point", span_id=None, payload=None):
        sequence = len(events) + 1
        events.append({"event_id": f"test:{sequence}", "producer_id": "test",
                       "seq": sequence, "span_id": span_id or f"test:{sequence}",
                       "phase": phase, "type": kind, "payload": payload or {}})

    for item in emit.events:
        add_event(item["type"], payload=item["payload"])
    add_event("task", "start", "task")
    add_event("iteration", "start", "iteration")
    add_event("iteration", "end", "iteration")
    add_event("task", "end", "task", {"response": "答案完成"})
    verdict = score({"case_id": "V001", "case_version": "test-v1", "requirements": [],
                     "file_policy": {"business_write": []}}, {
        "events": events, "before": {}, "after": {}, "cleanup": {"ok": True},
        "execution_status": "finished", "ready": True, "trial_id": "V001-test",
        "workspace": ".",
    })
    assert verdict["status"] == "pass"
    assert "budget_exhausted" not in {reason["reason_code"] for reason in verdict["reasons"]}


def test_token_budget_rejects_before_dispatch_when_reservation_exceeds_limit():
    kwargs = request_kwargs()
    request_bytes = len(_safe_json_bytes(kwargs))
    calls = []

    async def create(**request):
        calls.append(request)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    provider = fake_provider(create)
    emit = FakeEmitter()
    state = install_request_budget(provider, {"max_attempts": 3,
                                               "max_tokens": request_bytes + 15}, emit)
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert state["attempts_dispatched"] == 0
    assert not calls
    assert emit.events[-1]["payload"]["reason"] == "token_budget_exhausted"


def test_reported_usage_overrun_is_latched_and_reported():
    kwargs = request_kwargs()
    request_bytes = len(_safe_json_bytes(kwargs))
    calls = []

    async def create(**request):
        calls.append(request)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=request_bytes + 1,
                                                      completion_tokens=16))

    provider = fake_provider(create)
    emit = FakeEmitter()
    budget = {"max_attempts": 3, "max_tokens": request_bytes + 16}
    state = install_request_budget(provider, budget, emit)
    asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert state["halted_reason"] == "token_budget_overrun"
    assert emit.events[-1]["payload"]["action"] == "usage_overrun"
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    record = _agent_budget_record({"events": emit.events}, budget)
    assert record["known_tokens"] == request_bytes + 17
    assert record["token_budget_overrun"] is True
    assert record["overruns"][0]["reason"] == "token_budget_overrun"
    assert len(calls) == 1


@pytest.mark.parametrize("response", [
    SimpleNamespace(usage=None),
    SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=None)),
])
def test_unknown_usage_stops_all_following_dispatches(response):
    kwargs = request_kwargs()
    calls = []

    async def create(**request):
        calls.append(request)
        return response

    provider = fake_provider(create)
    emit = FakeEmitter()
    state = install_request_budget(provider, {"max_attempts": 3, "max_tokens": 10000}, emit)
    asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert state["usage_complete"] is False
    assert state["known_tokens"] == 0
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert len(calls) == 1
    assert _agent_budget_record({"events": [
        {"type": event["type"], **event}
        for event in emit.events
    ]}, {"max_attempts": 3, "max_tokens": 10000})["known_tokens"] is None


def test_sdk_exception_is_counted_and_blocks_retry():
    kwargs = request_kwargs()
    calls = []

    async def create(**request):
        calls.append(request)
        raise TimeoutError("private provider details are not serialized")

    provider = fake_provider(create)
    emit = FakeEmitter()
    state = install_request_budget(provider, {"max_attempts": 3, "max_tokens": 10000}, emit)
    with pytest.raises(TimeoutError):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert state["attempts_dispatched"] == 1
    assert state["usage_complete"] is False
    with pytest.raises(BudgetExceeded):
        asyncio.run(provider._client.chat.completions.create(**kwargs))
    assert len(calls) == 1
    assert "private provider" not in json.dumps(emit.events)


def test_dispatch_without_usage_terminal_is_unknown_not_zero():
    budget = {"max_attempts": 3, "max_tokens": 10000}
    evidence = {"events": [{"type": "budget", "payload": {
        "action": "dispatch_reserved", "attempt": 1,
    }}]}
    record = _agent_budget_record(evidence, budget)
    assert record["attempts_dispatched"] == 1
    assert record["known_tokens"] is None
    assert record["usage_complete"] is False
    assert record["unknown_usage_attempts"] == 1
    assert record["trace_accounting_errors"] == ["dispatch_missing_usage_terminal"]


def test_duplicate_dispatch_attempt_identity_is_reported():
    budget = {"max_attempts": 3, "max_tokens": 10000}
    evidence = {"events": [{"type": "budget", "payload": {
        "action": "dispatch_reserved", "attempt": 1,
    }}] * 2}
    record = _agent_budget_record(evidence, budget)
    assert record["attempts_dispatched"] == 2
    assert record["known_tokens"] is None
    assert record["usage_complete"] is False
    assert "duplicate_reservation_attempt" in record["trace_accounting_errors"]


def test_source_snapshot_archive_contains_only_common_module():
    common = Path(__file__).parents[2] / "evals" / "common.py"
    with zipfile.ZipFile(io.BytesIO(_zip_common(common))) as archive:
        assert archive.namelist() == ["evals/common.py"]
        assert archive.read("evals/common.py") == common.read_bytes()
