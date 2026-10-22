"""No-network controls for native SDK streaming admission and raw usage receipts."""
from types import SimpleNamespace

import pytest

from evals.channel_task_worker import native_provider
from evals.common import MODEL
from evals.worker import BudgetExceeded, Emitter
from nanobot.utils.observability import observation_context, span


class Connection:
    def __init__(self):
        self.messages = []

    def send(self, value):
        self.messages.append(value)


class Stream:
    def __init__(self, usage):
        self.rows = iter([SimpleNamespace(model="service-returned-model", usage=None),
                          SimpleNamespace(model="service-returned-model", usage=usage)])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.rows)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        return None


def setup_provider(tmp_path, monkeypatch, usage=None):
    import nanobot.providers.openai_compat_provider as module

    calls = []

    class FakeProvider:
        def __init__(self, **kwargs):
            async def create(**options):
                calls.append(options)
                return Stream(usage)
            self._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(module, "OpenAICompatProvider", FakeProvider)
    credentials = tmp_path / "synthetic-credential.txt"
    credentials.write_text("sk-SYNTHETIC-DO-NOT-SEND", encoding="utf-8")
    connection = Connection()
    emit = Emitter(connection, "native-control", "native-control")
    return native_provider(credentials, emit), connection, calls, emit


@pytest.mark.parametrize("model,stream", [("wrong-model", True), (MODEL, False), (MODEL, None)])
async def test_invalid_native_profile_never_creates_sdk_request(tmp_path, monkeypatch, model, stream):
    provider, connection, calls, emit = setup_provider(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        await provider._client.chat.completions.create(model=model, stream=stream)
    assert calls == [] and connection.messages == []


@pytest.mark.parametrize("raw,expected", [(None, "unknown"),
    (SimpleNamespace(prompt_tokens=None, completion_tokens=7), "partial"),
    (SimpleNamespace(prompt_tokens=11, completion_tokens=7), "known")])
async def test_sdk_receipt_keeps_raw_field_presence_and_actual_span(tmp_path, monkeypatch, raw, expected):
    provider, connection, calls, emit = setup_provider(tmp_path, monkeypatch, raw)
    with observation_context(emit.event, "native-control"):
        with span("task"):
            with span("model_request"):
                with span("model_attempt", sdk_retries=0) as attempt:
                    stream = await provider._client.chat.completions.create(model=MODEL, stream=True)
                    chunks = [chunk async for chunk in stream]
    points = [row["event"] for row in connection.messages if row["event"]["type"] == "model_transport"]
    assert len(points) == 1 and len(chunks) == 2
    assert points[0]["parent_span_id"] == attempt.span_id
    payload = points[0]["payload"]
    assert payload["usage_status"] == expected
    assert payload["usage_source"] == "sdk_response_usage"
    assert payload["native_stream"] and payload["wire_stream"] and payload["complete"]
    assert payload["actual_model"] == "service-returned-model"
    assert calls == [{"model": MODEL, "stream": True}]


async def test_stream_request_cap_stops_before_ninth_sdk_request(tmp_path, monkeypatch):
    provider, connection, calls, emit = setup_provider(tmp_path, monkeypatch)
    for _ in range(8):
        result = await provider._client.chat.completions.create(model=MODEL, stream=True)
        assert len([row async for row in result]) == 2
    with pytest.raises(BudgetExceeded):
        await provider._client.chat.completions.create(model=MODEL, stream=True)
    assert len(calls) == 8


async def test_completed_over_budget_stream_retains_paid_receipt_before_stopping(tmp_path, monkeypatch):
    provider, connection, calls, emit = setup_provider(tmp_path, monkeypatch,
        SimpleNamespace(prompt_tokens=32001, completion_tokens=1))
    with observation_context(emit.event, "native-control"):
        result = await provider._client.chat.completions.create(model=MODEL, stream=True)
        with pytest.raises(BudgetExceeded):
            _ = [row async for row in result]
    types = [row["event"]["type"] for row in connection.messages]
    assert types.index("model_transport") < types.index("budget")
    with pytest.raises(BudgetExceeded):
        await provider._client.chat.completions.create(model=MODEL, stream=True)
    assert len(calls) == 1
