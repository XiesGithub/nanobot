"""Provider observations describe attempts without changing retry decisions."""

import asyncio

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.observability import observation_context, span, usage_payload


class SequenceProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_default_model(self):
        return "deepseek-v4-flash"


def sink_for(events):
    def sink(kind, **payload):
        events.append({"type": kind, **payload})
    return sink


def terminal(events, kind):
    return [e for e in events if e["type"] == kind and e["phase"] != "start"]


def test_missing_usage_fields_remain_unknown_and_cached_usage_is_not_invented():
    assert usage_payload({}) == {
        "usage": None,
        "usage_status": "unknown",
        "usage_source": "provider_usage",
    }
    partial = usage_payload({"prompt_tokens": 0})
    assert partial["usage_status"] == "partial"
    assert partial["usage"] == {
        "input_tokens": 0,
        "output_tokens": None,
        "cached_input_tokens": None,
    }
    assert usage_payload({"prompt_tokens": 10, "completion_tokens": 0})[
        "usage_status"
    ] == "known"


@pytest.mark.asyncio
async def test_transient_attempts_wait_and_usage_are_independent(monkeypatch):
    events = []
    provider = SequenceProvider([
        LLMResponse("unavailable", finish_reason="error", error_status_code=503),
        LLMResponse("done", usage={"prompt_tokens": 7, "completion_tokens": 2}),
    ])
    waits = []

    async def fake_sleep(delay, **kwargs):
        assert events[-1]["type"] == "retry_wait"
        assert events[-1]["phase"] == "start"
        waits.append(delay)

    monkeypatch.setattr(provider, "_sleep_with_heartbeat", fake_sleep)
    with observation_context(sink_for(events), "task-1"):
        with span("model_request") as request:
            result = await provider.chat_with_retry(
                [{"role": "user", "content": "secret prompt"}], model="deepseek-v4-flash"
            )
    attempts = terminal(events, "model_attempt")
    assert result.content == "done"
    assert waits == [1]
    assert len(attempts) == 2
    assert [e["phase"] for e in attempts] == ["error", "end"]
    assert [e["attempt_index"] for e in attempts] == [1, 2]
    assert attempts[1]["retry_of"] == attempts[0]["span"]
    assert all(e["parent_span_id"] == request.span_id for e in attempts)
    assert all(e["task_id"] == "task-1" for e in events)
    assert attempts[0]["usage"] is None
    assert attempts[0]["usage_status"] == "unknown"
    assert all(e["sdk_retries"] is None for e in attempts)
    assert attempts[1]["usage"]["input_tokens"] == 7
    assert terminal(events, "retry_wait")[0]["retry_of"] == attempts[0]["span"]
    assert "secret prompt" not in str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_direct_retry_series_has_one_request_and_one_transport_attempt_per_call(
    monkeypatch, streaming,
):
    provider = SequenceProvider([
        LLMResponse("unavailable", finish_reason="error", error_status_code=503),
        LLMResponse("done", usage={"prompt_tokens": 4, "completion_tokens": 1}),
    ])
    waits = []

    async def fake_sleep(delay, **_kwargs):
        waits.append(delay)

    monkeypatch.setattr(provider, "_sleep_with_heartbeat", fake_sleep)
    events = []
    call = provider.chat_stream_with_retry if streaming else provider.chat_with_retry
    with observation_context(sink_for(events), "direct-request"):
        result = await call([{"role": "user", "content": "hello"}], model="deepseek-v4-flash")

    starts = [event for event in events
              if event["type"] == "model_request" and event["phase"] == "start"]
    attempts = [event for event in events
                if event["type"] == "model_attempt" and event["phase"] == "start"]
    request_terminals = terminal(events, "model_request")
    attempt_terminals = terminal(events, "model_attempt")
    assert result.content == "done"
    assert waits == [1]
    assert len(provider.calls) == len(attempts) == len(attempt_terminals) == 2
    assert len(starts) == len(request_terminals) == 1
    assert all(event["parent_span_id"] == starts[0]["span"] for event in attempts)
    assert request_terminals[0]["usage_status"] == "known"
    assert starts[0]["streaming"] is streaming


@pytest.mark.asyncio
async def test_image_fallback_records_changed_input_and_preserves_mutation():
    provider = SequenceProvider([
        LLMResponse("unsupported image", finish_reason="error"), LLMResponse("done"),
    ])
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}},
    ]}]
    events = []
    with observation_context(sink_for(events), "image-task"):
        result = await provider.chat_with_retry(messages)
    attempts = terminal(events, "model_attempt")
    assert result.content == "done"
    assert len(provider.calls) == 2
    assert messages[0]["content"][0]["type"] == "text"
    assert attempts[1]["cause"] == "image_fallback"
    assert attempts[1]["attempt_index"] == 2
    assert attempts[1]["retry_of"] == attempts[0]["span"]
    assert attempts[1]["input_sha256"] != attempts[0]["input_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_handled_exception_keeps_response_and_records_type(streaming):
    provider = SequenceProvider([ValueError("private failure details")])
    events = []
    call = provider.chat_stream_with_retry if streaming else provider.chat_with_retry
    with observation_context(sink_for(events), "error-task"):
        result = await call([{"role": "user", "content": "hello"}])
    assert result.content == "Error calling LLM: private failure details"
    assert result.finish_reason == "error"
    exceptions = terminal(events, "provider_exception")
    assert len(exceptions) == 1
    assert exceptions[0]["error_kind"] == "ValueError"
    attempts = terminal(events, "model_attempt")
    assert len(attempts) == 1 and attempts[0]["phase"] == "error"
    assert exceptions[0]["parent_span_id"] == attempts[0]["span"]
    assert "private failure details" not in str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_wait", [False, True])
async def test_cancellation_has_one_terminal_and_propagates(monkeypatch, during_wait):
    provider = SequenceProvider([
        LLMResponse("503", finish_reason="error") if during_wait else asyncio.CancelledError(),
    ])

    async def cancel_sleep(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(provider, "_sleep_with_heartbeat", cancel_sleep)
    events = []
    with observation_context(sink_for(events), "cancel-task"):
        with pytest.raises(asyncio.CancelledError):
            await provider.chat_with_retry([{"role": "user", "content": "hello"}])
    kind = "retry_wait" if during_wait else "model_attempt"
    endings = terminal(events, kind)
    assert len(endings) == 1 and endings[0]["phase"] == "cancelled"
    assert len(provider.calls) == 1
    request_endings = terminal(events, "model_request")
    assert len(request_endings) == 1 and request_endings[0]["phase"] == "cancelled"
    if not during_wait:
        assert endings[0]["usage"] is None
        assert endings[0]["usage_status"] == "unknown"


@pytest.mark.asyncio
async def test_scripted_usage_does_not_become_model_attempt():
    provider = SequenceProvider([LLMResponse("done", usage={"prompt_tokens": 0})])
    provider.observation_mode = "scripted"
    events = []
    with observation_context(sink_for(events), "scripted-task"):
        await provider.chat_with_retry([])
    assert not terminal(events, "model_attempt")
    endings = terminal(events, "scripted_response")
    assert len(endings) == 1
    assert endings[0]["usage_status"] == "partial"
    assert endings[0]["usage"]["input_tokens"] == 0
    assert endings[0]["usage"]["output_tokens"] is None


@pytest.mark.asyncio
async def test_collector_failure_does_not_change_result():
    response = LLMResponse("done")
    provider = SequenceProvider([response])

    def broken_sink(*args, **kwargs):
        raise OSError("collector unavailable")

    with observation_context(broken_sink, "collector-task") as observation:
        result = await provider.chat_with_retry([])
    assert result is response
    assert len(provider.calls) == 1
    assert [(error["type"], error["phase"]) for error in observation.collector_errors] == [
        ("model_request", "start"),
        ("model_attempt", "start"),
        ("model_attempt", "end"),
        ("model_request", "end"),
    ]


@pytest.mark.asyncio
async def test_trusted_sdk_retry_metadata_is_recorded_without_changing_calls():
    provider = SequenceProvider([LLMResponse("done")])
    provider.observation_sdk_retries = 0
    events = []
    with observation_context(sink_for(events), "sdk-task"):
        await provider.chat_with_retry([])
    endings = terminal(events, "model_attempt")
    assert len(endings) == 1 and endings[0]["sdk_retries"] == 0
    assert len(provider.calls) == 1
