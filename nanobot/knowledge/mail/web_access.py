"""Server-owned WebSocket principals and private chat subscriptions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlparse

from nanobot.knowledge.mail.contracts import AccessContext, MailError
from nanobot.security.context import SecurityContext


class MailWebAccess:
    def __init__(self, service):
        self.service = service
        self.tokens = {}
        self.connections = {}

    @staticmethod
    def origin_allowed(request):
        origin = request.headers.get("Origin")
        if not origin:
            return True  # Native authenticated clients have no browser Origin.
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == request.headers.get("Host", "").casefold()

    def issue(self, token, ttl, *, owner_principal=None):
        now = time.monotonic()
        self.tokens = {key: value for key, value in self.tokens.items() if value[1] > now}
        if owner_principal:
            security = self.service.access.authenticate("websocket", owner_principal, private_chat=True)
            if not security.authenticated:
                raise MailError("owner_binding_required")
        else:
            security = SecurityContext(principal_id="web-anon:" + secrets.token_hex(16), origin_kind="websocket")
        self.tokens[token] = (security, now + ttl)
        return security

    def token(self, value):
        item = self.tokens.get(value)
        if not item or item[1] <= time.monotonic():
            raise MailError("unauthorized")
        security = item[0]
        if security.authenticated:
            self.service.access.require(AccessContext.from_security(security, "web:auth"))
        return security

    def bind(self, connection, token):
        if token in self.tokens:
            security = self.token(token)
        else:
            # A shared static WS token authenticates transport only. It cannot
            # acquire an owner role or borrow another client's principal.
            security = SecurityContext(principal_id="web-anon:" + secrets.token_hex(16), origin_kind="websocket")
        self.connections[connection] = security

    def security(self, connection):
        security = self.connections.get(connection)
        if security is None:
            raise MailError("unauthorized")
        if security.authenticated:
            self.service.access.require(AccessContext.from_security(security, "web:auth"))
        return security

    @staticmethod
    def session_key(security, chat_id):
        principal = security.binding_id if security.authenticated else "external:websocket:" + security.principal_id
        return "mail-private:" + hashlib.sha256(f"{principal}\0websocket\0{chat_id}".encode()).hexdigest()

    def attach(self, connection, chat_id):
        security = self.security(connection)
        principal = security.binding_id if security.authenticated else security.principal_id
        with self.service.store.connect(write=True) as db:
            row = db.execute("SELECT principal_id,binding_id FROM chat_bindings WHERE channel='websocket' AND chat_id=?", (chat_id,)).fetchone()
            if row and (row[0], row[1]) != (principal, security.binding_id):
                raise MailError("unauthorized")
            db.execute("INSERT OR IGNORE INTO chat_bindings VALUES('websocket',?,?,?,?)", (
                chat_id, principal, security.binding_id, self.session_key(security, chat_id),
            ))
        return security

    def http_access(self, request, *, scope="read", chat_id="management"):
        if not self.origin_allowed(request):
            raise MailError("origin_denied")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise MailError("unauthorized")
        security = self.token(header[7:])
        access = AccessContext.from_security(security, self.session_key(security, chat_id))
        self.service.access.require(access, scope)
        return access

    def session_list(self, access):
        manager = self.service.private_sessions
        with self.service.store.connect() as db:
            rows = list(db.execute("SELECT chat_id,session_key FROM chat_bindings WHERE channel='websocket' AND binding_id=?", (access.binding_id,)))
        sessions = []
        for row in rows:
            data = manager.read_session_file(row["session_key"])
            if data and data["metadata"].get("binding_id") == access.binding_id:
                sessions.append({"key": "websocket:" + row["chat_id"],
                                 "created_at": data["created_at"], "updated_at": data["updated_at"],
                                 "title": data["metadata"].get("title", "")})
        return {"sessions": sorted(sessions, key=lambda s: s["updated_at"] or "", reverse=True)}

    def session(self, access, chat_id, *, delete=False):
        with self.service.store.connect() as db:
            row = db.execute("SELECT session_key FROM chat_bindings WHERE channel='websocket' AND chat_id=? AND binding_id=?", (chat_id, access.binding_id)).fetchone()
        if not row:
            raise MailError("source_not_found")
        manager = self.service.private_sessions
        if delete:
            return {"deleted": manager.delete_session(row[0])}
        data = manager.read_session_file(row[0])
        if not data or data["metadata"].get("binding_id") != access.binding_id:
            raise MailError("source_not_found")
        messages = [{k: v for k, v in m.items() if k in {"role", "content", "timestamp", "citations"}} for m in data["messages"]]
        # No local paths, internal metadata or checkpoints in HTTP history.
        return {"key": "websocket:" + chat_id, "created_at": data["created_at"],
                "updated_at": data["updated_at"], "messages": messages}

    async def send(self, channel, message):
        from nanobot.knowledge.mail.contracts import SourceRead
        from nanobot.knowledge.mail.delivery import async_delivery
        for connection in list(channel._subs.get(message.chat_id, ())):
            try:
                security = self.security(connection)
                expected = message.security
                control = message.metadata.get("_turn_end") or message.metadata.get("_session_updated")
                if not control and (expected is None or security != expected):
                    continue
                if security.authenticated:
                    access = AccessContext.from_security(security, self.session_key(security, message.chat_id))
                    async with async_delivery(self.service, access):
                        for citation in message.metadata.get("citations", []):
                            self.service.retrieval.read_source(access, SourceRead(evidence_id=citation["evidence_id"]))
                        await connection.send(self._payload(message))
                else:
                    await connection.send(self._payload(message))
            except Exception:
                channel._cleanup_connection(connection)

    @staticmethod
    def _payload(message):
        event = "turn_end" if message.metadata.get("_turn_end") else (
            "session_updated" if message.metadata.get("_session_updated") else "message")
        payload = {"event": event, "chat_id": message.chat_id,
                   "text": message.content, "citations": message.metadata.get("citations", [])}
        if event == "turn_end" and message.metadata.get("_turn_error") is True:
            payload["outcome"] = "error"
        return json.dumps(payload, ensure_ascii=False)
