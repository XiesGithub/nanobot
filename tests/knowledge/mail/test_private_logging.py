import asyncio

from loguru import logger

from nanobot.providers.base import LLMResponse
from nanobot.security.private_logging import private_logger, private_logging
from tests.providers.test_provider_retry import ScriptedProvider


async def test_provider_retry_redacts_private_error_body_and_keeps_normal_diagnostics(monkeypatch):
    from unittest.mock import AsyncMock

    records = []
    # CLI logging tests can disable this namespace process-wide. Make the sink
    # observable here, then restore its cached activation state for other tests.
    previously_enabled = logger._core.enabled.get("nanobot.providers.base", True)
    logger.enable("nanobot.providers.base")
    sink = logger.add(lambda message: records.append(message.record))
    monkeypatch.setattr(ScriptedProvider, "_sleep_with_heartbeat", AsyncMock())
    provider = ScriptedProvider([
        LLMResponse(content="429 fictional_private_source_with_credentials", finish_reason="error"),
        LLMResponse(content="safe result"),
    ])
    try:
        async with private_logging():
            response = await provider.chat_with_retry(messages=[{"role": "user", "content": "fictional"}],
                model="deepseek-v4-flash")
        private_logger.info("ordinary diagnostic remains available")
    finally:
        logger.remove(sink)
        if not previously_enabled:
            logger.disable("nanobot.providers.base")
    assert response.content == "safe result"
    assert records[0]["message"] == "Private operation event; details omitted"
    assert records[-1]["message"] == "ordinary diagnostic remains available"
    assert "fictional_private_source" not in str(records)


async def test_nested_cancelled_private_logging_is_isolated_from_concurrent_normal_task():
    entered, ordinary_finished = asyncio.Event(), asyncio.Event()
    records = []
    sink = logger.add(lambda message: records.append(message.record))

    @private_logging()
    async def protected():
        async with private_logging():
            entered.set()
            await ordinary_finished.wait()
            try:
                raise RuntimeError("FICTIONAL_PRIVATE_EXCEPTION")
            except RuntimeError:
                private_logger.exception("FICTIONAL_PRIVATE_BODY")
            raise asyncio.CancelledError()

    async def ordinary():
        await entered.wait()
        private_logger.info("ordinary concurrent event")
        ordinary_finished.set()

    try:
        results = await asyncio.gather(protected(), ordinary(), return_exceptions=True)
        private_logger.info("ordinary after cancellation")
    finally:
        logger.remove(sink)
    assert isinstance(results[0], asyncio.CancelledError)
    assert [record["message"] for record in records] == [
        "ordinary concurrent event", "Private operation event; details omitted",
        "ordinary after cancellation",
    ]
    assert all(record["exception"] is None for record in records)
