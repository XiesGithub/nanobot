"""Task-local content suppression for protected model and runner operations."""

from contextlib import asynccontextmanager
from contextvars import ContextVar

from loguru import logger

_private_content = ContextVar("private_log_content", default=False)


def _redact(record):
    if _private_content.get():
        record["message"] = "Private operation event; details omitted"
        record["exception"] = None
        record["extra"] = {}


private_logger = logger.patch(_redact)


@asynccontextmanager
async def private_logging():
    """Nested/concurrent calls restore their own context, including cancellation."""
    token = _private_content.set(True)
    try:
        yield
    finally:
        _private_content.reset(token)
