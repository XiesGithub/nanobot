"""Optional request-domain extensions, evaluated before session/history restoration."""

from typing import Protocol

from nanobot.bus.events import InboundMessage, OutboundMessage


class TurnExtension(Protocol):
    def session_key(self, message: InboundMessage) -> str: ...
    async def intercept(self, loop, message: InboundMessage) -> tuple[bool, OutboundMessage | None]: ...
