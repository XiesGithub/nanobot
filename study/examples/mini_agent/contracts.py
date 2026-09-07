"""The transport-neutral contract shared by providers and the runner."""
from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]


@dataclass
class Call:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    content: str = ""
    calls: list[Call] = field(default_factory=list)


class Provider(Protocol):
    async def complete(self, messages: list[Message], schemas: list[dict]) -> Reply:
        """Return final text or tool calls; never execute a tool here."""
        ...
