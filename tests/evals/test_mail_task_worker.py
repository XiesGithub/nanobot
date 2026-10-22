"""D-only integration tests for the protected full-task mail worker."""

import asyncio
import multiprocessing as mp

import pytest

from evals.common import read
from evals.mail_task_cases import IDS, load_cases
from evals.mail_task_worker import _observe_mail_model_requests
from evals.run import run_trial
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.observability import observation_context, span


class _EventConnection:
    def __init__(self, events):
        self.events = events

    def send(self, message):
        if message.get("message") == "event":
            self.events.append(message["event"])


class _OfflineProvider(LLMProvider):
    """Base-provider retry and observation behavior with no network access."""

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls = 0
        self.generation = GenerationSettings(temperature=0, max_tokens=64)
        self._CHAT_RETRY_DELAYS = (0, 0, 0)

    def get_default_model(self):
        return "offline-test-model"

    async def chat(self, messages, **kwargs):
        self.calls += 1
        result = self.outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


async def _record_provider_call(provider, events, *, existing_request=False):
    connection = _EventConnection(events)
    emitter = Emitter(connection, "offline-run", "offline-trial")
    with observation_context(emitter.event, "offline-trial"):
        with span("task"):
            _observe_mail_model_requests(provider)
            if existing_request:
                with span("model_request", model="offline-test-model",
                          purpose="iteration", streaming=False):
                    return await provider.chat_with_retry(
                        messages=[{"role": "user", "content": "SYNTHETIC_PRIVATE_MAIL_BODY"}],
                        model="offline-test-model",
                    )
            return await provider.chat_with_retry(
                messages=[{"role": "user", "content": "SYNTHETIC_PRIVATE_MAIL_BODY"}],
                model="offline-test-model",
            )


def _events_for(events, event_type, phase=None):
    return [event for event in events
            if event["type"] == event_type and (phase is None or event["phase"] == phase)]


@pytest.mark.asyncio
async def test_mail_provider_request_span_contains_retry_attempts_and_usage():
    events = []
    provider = _OfflineProvider([
        LLMResponse(content="503 unavailable", finish_reason="error", usage={
            "input_tokens": 2, "output_tokens": 1,
        }, error_status_code=503, error_should_retry=True, retry_after=0.001),
        LLMResponse(content="ok", finish_reason="stop", usage={
            "input_tokens": 3, "output_tokens": 2,
        }),
    ])

    result = await _record_provider_call(provider, events)

    assert result.content == "ok"
    assert provider.calls == 2
    requests = _events_for(events, "model_request", "start")
    attempts = _events_for(events, "model_attempt", "start")
    assert len(requests) == 1
    assert len(attempts) == 2
    assert all(event["parent_span_id"] == requests[0]["span_id"] for event in attempts)
    terminal = next(event for event in _events_for(events, "model_request")
                    if event["phase"] == "end")
    assert terminal["payload"]["finish_reason"] == "stop"
    assert terminal["payload"]["usage"] == {
        "input_tokens": 3, "output_tokens": 2, "cached_input_tokens": None,
    }
    assert "SYNTHETIC_PRIVATE_MAIL_BODY" not in str(events)
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 2
    assert all(row["request_span_id"] == requests[0]["span_id"] for row in trace["ledger"])


@pytest.mark.asyncio
async def test_mail_provider_permanent_error_closes_request_without_retry():
    events = []
    provider = _OfflineProvider([
        LLMResponse(content="bad request", finish_reason="error", usage={
            "input_tokens": 1, "output_tokens": 1,
        }, error_status_code=400, error_should_retry=False),
    ])

    response = await _record_provider_call(provider, events)

    assert response.finish_reason == "error"
    assert provider.calls == 1
    request_end = _events_for(events, "model_request", "error")
    assert len(request_end) == 1
    assert request_end[0]["payload"]["error_status_code"] == 400
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 1


@pytest.mark.asyncio
async def test_mail_provider_cancellation_closes_request_and_attempt_spans():
    events = []
    provider = _OfflineProvider([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await _record_provider_call(provider, events)

    assert len(_events_for(events, "model_request", "cancelled")) == 1
    assert len(_events_for(events, "model_attempt", "cancelled")) == 1
    trace = analyze_trace(events, execution_status="cancelled")
    assert trace["valid"] and trace["closed"], trace["errors"]


@pytest.mark.asyncio
async def test_mail_provider_reuses_agent_runner_model_request_boundary():
    events = []
    provider = _OfflineProvider([
        LLMResponse(content="ok", finish_reason="stop", usage={
            "input_tokens": 1, "output_tokens": 1,
        }),
    ])

    await _record_provider_call(provider, events, existing_request=True)

    requests = _events_for(events, "model_request", "start")
    assert len(requests) == 1
    assert requests[0]["payload"]["purpose"] == "iteration"
    attempt = _events_for(events, "model_attempt", "start")[0]
    assert attempt["parent_span_id"] == requests[0]["span_id"]
    trace = analyze_trace(events)
    assert trace["valid"] and trace["closed"], trace["errors"]


@pytest.mark.parametrize("case_id", IDS)
def test_positive_mail_task_runs_real_protected_turn(tmp_path, case_id):
    case = next(item for item in load_cases() if item["case_id"] == case_id)
    run_dir = tmp_path / "run"
    (run_dir / "trials").mkdir(parents=True)
    isolation_root = run_dir / "instances"
    isolation_root.mkdir()
    plan = {
        "trial_id": case_id + "-positive",
        "case_id": case_id,
        "case_version": case["case_version"],
        "purpose": "primary",
        "profile": "D-mail-full-task/1.0",
        "fault": None,
        "product_timeout_s": 30,
    }

    result = run_trial(case, plan, run_dir, isolation_root, "D", mp.get_context("spawn"))

    assert result["status"] == "pass", result["reasons"]
    assert result["execution_status"] == "finished"
    evidence = read(run_dir / "trials" / plan["trial_id"] / "artifacts" / "evidence.json")
    assert evidence["ready"]["tools"] == []
    assert not evidence["runtime_violations"]
    history = next(event["payload"] for event in evidence["events"]
                   if event["type"] == "private_history")
    assert history["session_file_sha256"]
    assert history["session_file"].startswith("mail/private/sessions/")
    assert history["messages"][-1]["content"] == next(
        event["payload"]["response"] for event in evidence["events"]
        if event["type"] == "delivery"
    )


def test_no_sources_control_uses_product_insufficient_answer(tmp_path):
    case = next(item for item in load_cases() if item["case_id"] == "V088")
    run_dir = tmp_path / "run"
    (run_dir / "trials").mkdir(parents=True)
    isolation_root = run_dir / "instances"
    isolation_root.mkdir()
    plan = {
        "trial_id": "V088-no-sources",
        "case_id": "V088",
        "case_version": case["case_version"],
        "purpose": "diagnostic",
        "profile": "D-mail-full-task/1.0",
        "fault": "no_sources",
        "product_timeout_s": 30,
    }

    result = run_trial(case, plan, run_dir, isolation_root, "D", mp.get_context("spawn"))

    assert result["status"] == "product_fail"
    evidence = read(run_dir / "trials" / plan["trial_id"] / "artifacts" / "evidence.json")
    route = next(event["payload"]["plan"] for event in evidence["events"]
                 if event["type"] == "mail_route")
    answer = next(event["payload"]["result"] for event in evidence["events"]
                  if event["type"] == "mail_answer")
    assert route["intent"] == "mail_fact"
    assert answer["status"] == "insufficient"
    assert not any(event["type"] == "mail_read" for event in evidence["events"])
