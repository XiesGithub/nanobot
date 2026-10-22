"""Last-boundary validation of private answers, shared by trusted entry points."""

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager

from filelock import FileLock, Timeout

from nanobot.knowledge.mail.contracts import AccessContext, MailError, SourceRead


@contextmanager
def delivery(service, message):
    security = message.security if message is not None else None
    if security is None or not security.authenticated or not security.binding_id:
        yield
        return
    access = AccessContext.from_security(security, message.metadata.get("mail_session_key", ""))
    with service.access.delivery(access), service.store.lock:
        for citation in message.metadata.get("citations", []):
            service.retrieval.read_source(access, SourceRead(evidence_id=citation["evidence_id"]))
        yield


async def acquire_delivery(service, access):
    """Avoid blocking the event loop while a different process commits revocation."""
    locks = [service.access._gate(access.binding_id),
             FileLock(str(service.store.root / "maintenance.lock"), thread_local=False)]
    acquired = []
    deadline = time.monotonic() + 10
    try:
        for lock in locks:
            while True:
                try:
                    lock.acquire(timeout=0)
                    acquired.append(lock)
                    break
                except Timeout:
                    if time.monotonic() >= deadline:
                        raise MailError("delivery_busy") from None
                    await asyncio.sleep(.02)
        service.access.require(access)
        return acquired
    except BaseException:
        for lock in reversed(acquired):
            lock.release()
        raise


@asynccontextmanager
async def async_delivery(service, access):
    locks = await acquire_delivery(service, access)
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()
