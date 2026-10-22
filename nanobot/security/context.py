"""Immutable identity snapshots supplied by trusted server entry points.

Snapshots are not credentials: protected operations must reauthorize against
the durable binding store. Never construct these from free-form metadata.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SecurityContext:
    principal_id: str = ""
    binding_id: str | None = None
    owner_id: str | None = None
    authenticated: bool = False
    scopes: frozenset[str] = frozenset()
    origin_kind: str = "untrusted"
    policy_revision: int = 0
    source_key: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    mode_revision: int = 0
    reply_channel: str | None = None
    reply_chat_id: str | None = None
