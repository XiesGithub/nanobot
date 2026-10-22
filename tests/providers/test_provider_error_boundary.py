"""Provider-level regression coverage for terminal errors and stream callbacks."""

from __future__ import annotations

import copy

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse

SENSITIVE_SENTINEL = "credential-fragment-7f3a9d"
IMAGE_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,synthetic"},
            },
        ],
    },
]


class SequencedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls.append(copy.deepcopy(messages))
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "synthetic-provider-model"


def permanent_error(*, status_code: int | None = 400) -> LLMResponse:
    return LLMResponse(
        content=SENSITIVE_SENTINEL,
        finish_reason="error",
        error_status_code=status_code,
        error_should_retry=False,
    )


@pytest.mark.parametrize("retry_hint", [None, False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_400_cannot_be_retried_by_text_or_retry_hints(retry_hint, streaming):
    provider = SequencedProvider([LLMResponse(
        content=f"timeout connection 503 {SENSITIVE_SENTINEL}", finish_reason="error",
        error_status_code=400, error_kind="connection", error_should_retry=retry_hint,
    )])
    messages = copy.deepcopy(IMAGE_MESSAGES)
    original = copy.deepcopy(messages)
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    call = provider.chat_stream_with_retry if streaming else provider.chat_with_retry
    kwargs = {"on_content_delta": on_delta} if streaming else {}
    result = await call(messages, retry_mode="persistent", **kwargs)
    assert result.finish_reason == "error" and result.error_status_code == 400
    assert len(provider.calls) == 1 and messages == original and not deltas


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_mode", ["standard", "persistent"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_permanent_error_preserves_diagnostics_without_streaming_them(
    retry_mode: str,
    streaming: bool,
) -> None:
    provider = SequencedProvider([permanent_error()])
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    messages = [{"role": "user", "content": "synthetic request"}]
    if streaming:
        response = await provider.chat_stream_with_retry(
            messages,
            retry_mode=retry_mode,
            on_content_delta=on_delta,
        )
    else:
        response = await provider.chat_with_retry(messages, retry_mode=retry_mode)

    assert response.finish_reason == "error"
    assert response.content == SENSITIVE_SENTINEL
    assert response.error_status_code == 400
    assert response.error_should_retry is False
    assert len(provider.calls) == 1
    assert deltas == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_mode", ["standard", "persistent"])
@pytest.mark.parametrize("status_code", [400, None])
async def test_permanent_image_error_does_not_retry_or_mutate_messages(
    retry_mode: str,
    status_code: int | None,
) -> None:
    messages = copy.deepcopy(IMAGE_MESSAGES)
    original_messages = copy.deepcopy(messages)
    provider = SequencedProvider([permanent_error(status_code=status_code)])

    response = await provider.chat_with_retry(messages, retry_mode=retry_mode)

    assert response.finish_reason == "error"
    assert response.content == SENSITIVE_SENTINEL
    assert len(provider.calls) == 1
    assert provider.calls[0] == original_messages
    assert messages == original_messages


@pytest.mark.asyncio
async def test_default_stream_fallback_emits_normal_content_delta() -> None:
    provider = SequencedProvider([LLMResponse(content="normal response")])
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    response = await provider.chat_stream_with_retry(
        [{"role": "user", "content": "synthetic request"}],
        on_content_delta=on_delta,
    )

    assert response.finish_reason == "stop"
    assert response.content == "normal response"
    assert deltas == ["normal response"]
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_mode", ["standard", "persistent"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_transient_error_still_retries_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    retry_mode: str,
    streaming: bool,
) -> None:
    provider = SequencedProvider([
        LLMResponse(
            content="synthetic unavailable",
            finish_reason="error",
            error_status_code=503,
            error_should_retry=True,
        ),
        LLMResponse(content="recovered response"),
    ])
    wait_delays: list[float] = []

    async def no_wait(delay: float, **kwargs) -> None:
        wait_delays.append(delay)

    monkeypatch.setattr(provider, "_sleep_with_heartbeat", no_wait)
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    messages = [{"role": "user", "content": "synthetic request"}]
    if streaming:
        response = await provider.chat_stream_with_retry(
            messages,
            retry_mode=retry_mode,
            on_content_delta=on_delta,
        )
    else:
        response = await provider.chat_with_retry(messages, retry_mode=retry_mode)

    assert response.finish_reason == "stop"
    assert response.content == "recovered response"
    assert len(provider.calls) == 2
    assert wait_delays == [1]
    assert deltas == (["recovered response"] if streaming else [])
