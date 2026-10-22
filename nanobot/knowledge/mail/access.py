"""Durable owner authorization and cross-process delivery/revocation gates."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, contextmanager
from typing import Iterator

from filelock import FileLock

from nanobot.config.schema import MailOwnerBinding
from nanobot.knowledge.mail.contracts import AccessContext, MailError
from nanobot.knowledge.mail.store import MailStore, json_text
from nanobot.security.context import SecurityContext


class MailAccess:
    def __init__(self, store: MailStore):
        self.store = store

    def _gate(self, binding_id: str) -> FileLock:
        # Independent gates avoid revocation of A blocking delivery to B.
        name = hashlib.sha256(binding_id.encode()).hexdigest()
        return FileLock(str(self.store.safe_path("auth-" + name + ".lock")), timeout=10)

    def initialize_bindings(self, bindings: list[MailOwnerBinding]) -> None:
        """Bootstrap once only: stale config must never restore revoked bindings."""
        with self.store.lock, self.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM bindings LIMIT 1").fetchone():
                return
            for binding in bindings:
                db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,1,0)", (
                    binding.binding_id, binding.owner_id, binding.principal_id,
                    binding.channel, json_text(sorted(binding.scopes)),
                ))

    def authenticate(self, channel: str, principal_id: str, *, private_chat: bool) -> SecurityContext:
        """Called only with principal information verified by the entry point."""
        if not private_chat or channel in {"email", "cron", "subagent", "system"}:
            return SecurityContext(principal_id=principal_id, origin_kind=channel)
        try:
            with self.store.connect() as db:
                binding = db.execute("SELECT * FROM bindings WHERE channel=? AND principal_id=? AND revoked=0",
                                     (channel, principal_id)).fetchone()
                if not binding:
                    return SecurityContext(principal_id=principal_id, origin_kind=channel)
                return SecurityContext(
                    principal_id=principal_id, binding_id=binding["binding_id"],
                    owner_id=binding["owner_id"], authenticated=True,
                    scopes=frozenset(json.loads(binding["scopes_json"])), origin_kind=channel,
                    policy_revision=binding["policy_revision"],
                )
        except Exception:
            raise MailError("authorization_unavailable") from None

    def require(self, access: AccessContext, scope: str = "read") -> None:
        try:
            with self.store.connect() as db:
                binding = db.execute("SELECT * FROM bindings WHERE binding_id=?", (access.binding_id,)).fetchone()
                if not binding or binding["revoked"] or binding["policy_revision"] != access.policy_revision:
                    raise MailError("authorization_revoked")
                if (binding["owner_id"] != access.owner_id or binding["principal_id"] != access.principal_id
                    or binding["channel"] != access.origin or scope not in json.loads(binding["scopes_json"])):
                    raise MailError("unauthorized")
        except MailError:
            raise
        except Exception:
            raise MailError("authorization_unavailable") from None

    @contextmanager
    def delivery(self, access: AccessContext, scope: str = "read") -> Iterator[None]:
        """Hold only while committing a response/chunk, never during generation."""
        with self._gate(access.binding_id):
            self.require(access, scope)
            yield

    def _management_gates(self, binding_id: str, access: AccessContext | None):
        @contextmanager
        def gates():
            with ExitStack() as stack:
                for identity in sorted({binding_id, *([access.binding_id] if access else [])}):
                    stack.enter_context(self._gate(identity))
                if access:
                    self.require(access, "manage")
                yield
        return gates()

    def revoke(self, binding_id: str, *, access: AccessContext | None = None) -> int:
        """Trusted local management or an already-authorized management call only."""
        with self._management_gates(binding_id, access), self.store.connect(write=True) as db:
            binding = db.execute("SELECT policy_revision FROM bindings WHERE binding_id=?", (binding_id,)).fetchone()
            if not binding:
                raise MailError("unauthorized")
            revision = binding[0] + 1
            db.execute("UPDATE bindings SET revoked=1,policy_revision=? WHERE binding_id=?", (revision, binding_id))
            db.execute("DELETE FROM confirmations WHERE binding_id=?", (binding_id,))
        return revision

    def grant(self, binding: MailOwnerBinding, *, access: AccessContext | None = None) -> None:
        """Explicit local management; never called by config reload or a model tool."""
        with self._management_gates(binding.binding_id, access), self.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM bindings WHERE owner_id<>? LIMIT 1", (binding.owner_id,)).fetchone():
                raise MailError("owner_mismatch")
            db.execute("""INSERT INTO bindings VALUES(?,?,?,?,?,1,0)
                ON CONFLICT(binding_id) DO UPDATE SET owner_id=excluded.owner_id,
                principal_id=excluded.principal_id,channel=excluded.channel,
                scopes_json=excluded.scopes_json,policy_revision=bindings.policy_revision+1,revoked=0""",
                (binding.binding_id, binding.owner_id, binding.principal_id,
                 binding.channel, json_text(sorted(binding.scopes))))
