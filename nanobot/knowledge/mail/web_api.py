"""Owner-only read HTTP and bounded management WebSocket envelopes."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal
from urllib.parse import quote, unquote

from pydantic import Field

from nanobot.knowledge.mail.contracts import (
    AccessContext,
    MailError,
    ManageAction,
    Record,
    SourceRead,
)
from nanobot.knowledge.mail.delivery import async_delivery
from nanobot.knowledge.mail.management import MailManagement


class WebRequest(Record):
    type: Literal["mail_knowledge"]
    request_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


def settings(service, access):
    service.access.require(access)
    return {"enabled": service.config.enabled, "mode": service.mode(),
            "attachment_types": service.config.attachment_types,
            "max_attachment_bytes": service.config.max_attachment_bytes,
            "max_attachments": service.config.max_attachments,
            "owner_binding": access.binding_id, "scopes": sorted(access.scopes),
            "status": service.status_sync(access).model_dump(mode="json"),
            "privacy_notice": "Private evidence may be sent to the configured deepseek-v4-flash provider. External conversations use restricted tools; no historical mailbox backfill."}


async def private_http(channel, connection, request, path, query):
    from nanobot.channels.websocket import _http_error, _http_json_response, _http_response
    service, web = channel.mail_service, channel._mail_access
    try:
        chat_id = (query.get("chat_id") or ["management"])[0]
        if len(chat_id) > 128:
            raise MailError("invalid_request")
        access = web.http_access(request, scope="manage" if path.endswith("/update") or path.endswith("/delete") else "read", chat_id=chat_id)
        connection._mail_pending_http = (access, None)
        if path == "/api/sessions":
            return True, _http_json_response(await asyncio.to_thread(web.session_list, access))
        match = re.fullmatch(r"/api/sessions/([^/]+)/(messages|delete)", path)
        if match:
            key = unquote(match[1])
            if not key.startswith("websocket:") or len(key) > 138:
                raise MailError("source_not_found")
            def read_session():
                with service.access.delivery(access, "manage" if match[2] == "delete" else "read"):
                    return web.session(access, key.removeprefix("websocket:"), delete=match[2] == "delete")

            result = await asyncio.to_thread(read_session)
            return True, _http_json_response(result)
        if path == "/api/mail-knowledge/status":
            return True, _http_json_response((await service.status(access)).model_dump(mode="json"))
        if path == "/api/mail-knowledge/inventory":
            kind = (query.get("kind") or ["documents"])[0]
            cursor = (query.get("cursor") or [""])[0]
            if len(cursor) > 100 or kind not in {"documents", "interactions", "deliveries", "actions"}:
                raise MailError("invalid_request")
            with service.store.connect() as db:
                if kind == "documents":
                    rows = [dict(r) for r in db.execute("""SELECT d.document_id AS id,
                        d.mail_id,d.filename,m.subject,d.extract_status,d.error_code,d.coverage_json,
                        CASE WHEN d.object_ref IS NULL THEN 0 ELSE 1 END AS local_input
                        FROM documents d JOIN mail_items m USING(mail_id)
                        WHERE m.owner_id=? AND m.deleted_at IS NULL AND d.document_id>?
                        AND (d.extract_status!='ready' OR json_extract(d.coverage_json,'$.complete')=0)
                        ORDER BY d.document_id LIMIT 51""", (access.owner_id, cursor))]
                elif kind == "interactions":
                    rows = [dict(r) for r in db.execute("""SELECT i.task_id AS id,i.state,m.subject
                        FROM interaction_dispatches i JOIN mail_items m USING(source_key)
                        WHERE m.owner_id=? AND m.deleted_at IS NULL AND i.state='uncertain'
                        AND i.task_id>? ORDER BY i.task_id LIMIT 51""", (access.owner_id, cursor))]
                elif kind == "deliveries":
                    rows = [dict(r) for r in db.execute("""SELECT d.attempt_id AS id,d.task_id,
                        d.status,d.error_code,d.updated_at FROM interaction_deliveries d
                        WHERE d.attempt_id>? ORDER BY d.attempt_id LIMIT 51""", (cursor,))]
                else:
                    rows = [dict(r) for r in db.execute("""SELECT action_id AS id,action,status,receipt_json
                        FROM management_actions WHERE binding_id=? AND action_id>?
                        ORDER BY action_id LIMIT 51""", (access.binding_id, cursor))]
                    for row in rows:
                        receipt = json.loads(row.pop("receipt_json"))
                        row["error_code"] = receipt.get("error_code")
            return True, _http_json_response({"items": rows[:50], "next_cursor": rows[49]["id"] if len(rows) > 50 else None})
        if path == "/api/mail-knowledge/removed":
            cursor = (query.get("cursor") or [""])[0]
            if len(cursor) > 100:
                raise MailError("invalid_cursor")
            with service.store.connect() as db:
                rows = [dict(r) for r in db.execute("SELECT old_mail_id,account_id,mailbox_server_name,removed_at,cleanup_pending FROM source_tombstones WHERE owner_id=? AND old_mail_id>? ORDER BY old_mail_id LIMIT 51", (access.owner_id, cursor))]
            return True, _http_json_response({"sources": rows[:50], "next_cursor": rows[49]["old_mail_id"] if len(rows) > 50 else None})
        match = re.fullmatch(r"/api/mail-knowledge/sources/([a-z]+_[a-f0-9]+)", path)
        if match:
            identity = match[1]
            key = "evidence_id" if identity.startswith("ev_") else ("document_id" if identity.startswith("doc_") else "mail_id")
            value = await service.read_source(access, SourceRead(**{key: identity}, cursor=(query.get("cursor") or [None])[0], limit=8000))

            def validate():
                for evidence in value.sources:
                    service.retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))

            connection._mail_pending_http = (access, validate)
            return True, _http_json_response(value.model_dump(mode="json"))
        match = re.fullmatch(r"/api/mail-knowledge/attachments/(doc_[a-f0-9]+)", path)
        if match:
            document_id = match[1]
            with service.store.connect() as db:
                doc = db.execute("SELECT d.*,m.revision,m.deleted_at FROM documents d JOIN mail_items m USING(mail_id) WHERE document_id=? AND owner_id=? AND kind='attachment'", (document_id, access.owner_id)).fetchone()
            if not doc or doc["deleted_at"] is not None:
                raise MailError("source_removed")
            if not doc["object_ref"]:
                raise MailError("local_input_missing")
            target = service.store.safe_path(doc["object_ref"])
            total = target.stat().st_size
            part = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("Range", "bytes=0-"))
            if not part:
                raise MailError("invalid_range")
            start = int(part[1])
            end = min(total - 1, start + 65535, int(part[2]) if part[2] else total - 1)
            if start > end or start >= total:
                return True, _http_error(416, "invalid_range")
            with target.open("rb") as stream:
                stream.seek(start)
                content = stream.read(end - start + 1)

            def validate_attachment():
                with service.store.connect() as db:
                    current = db.execute("SELECT d.sha256,m.revision,m.deleted_at FROM documents d JOIN mail_items m USING(mail_id) WHERE document_id=? AND owner_id=?", (document_id, access.owner_id)).fetchone()
                if not current or current[2] is not None or (current[0], current[1]) != (doc["sha256"], doc["revision"]):
                    raise MailError("source_removed")

            connection._mail_pending_http = (access, validate_attachment)
            return True, _http_response(content, status=206, content_type="application/octet-stream", extra_headers=[
                ("Content-Disposition", "attachment; filename=attachment.bin; filename*=UTF-8''" + quote(doc["filename"], safe="")),
                ("Content-Range", f"bytes {start}-{end}/{total}"), ("Accept-Ranges", "bytes"),
                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ])
        if path.startswith("/api/mail-knowledge/"):
            return True, _http_error(404, "source_not_found")
        if path == "/api/settings/update" and (query.get("model") or ["deepseek-v4-flash"])[0] != "deepseek-v4-flash":
            raise MailError("model_not_allowed")
        # Existing ordinary settings routes still run, behind owner auth and a
        # final transport delivery gate instead of the old shared-token boundary.
        return False, None
    except MailError as error:
        return True, _http_error(403 if error.code in {"unauthorized", "authorization_revoked", "origin_denied"} else 400, error.code)
    except (ValueError, OSError):
        return True, _http_error(400, "invalid_request")


async def private_envelope(channel, connection, envelope):
    service, web = channel.mail_service, channel._mail_access
    request_id = envelope.get("request_id", "")
    try:
        request = WebRequest.model_validate(envelope)
        payload = dict(request.payload)
        chat_id = payload.pop("chat_id", "management")
        if not isinstance(chat_id, str) or len(chat_id) > 128:
            raise MailError("invalid_request")
        security = web.security(connection)
        access = AccessContext.from_security(security, web.session_key(security, chat_id))
        service.access.require(access, "read" if request.action in {"get_settings", "select_source"} else "manage")
        if request.action == "get_settings":
            if payload:
                raise MailError("invalid_request")
            result = await asyncio.to_thread(settings, service, access)
        elif request.action == "set_mode":
            if set(payload) != {"archive_enabled", "trigger_agent", "expected_revision"} or any(type(payload[k]) is not bool for k in ("archive_enabled", "trigger_agent")) or type(payload["expected_revision"]) is not int:
                raise MailError("invalid_request")
            if payload["archive_enabled"] and not service.config.enabled:
                raise MailError("knowledge_disabled")
            result = await asyncio.to_thread(service.set_mode, **payload, access=access, request_id=request.request_id)
        elif request.action == "select_source":
            result = await service.read_source(access, SourceRead.model_validate(payload))
        elif request.action.startswith("confirm_"):
            allowed = {"confirm_remove", "confirm_restore", "confirm_refetch", "confirm_interaction_retry"}
            if request.action not in allowed or set(payload) != {"token"} or not isinstance(payload["token"], str) or len(payload["token"]) > 128:
                raise MailError("invalid_request")
            expected = {"confirm_remove": "prepare_remove", "confirm_restore": "request_restore",
                        "confirm_refetch": "prepare_refetch", "confirm_interaction_retry": "prepare_interaction_retry"}[request.action]
            result = await asyncio.to_thread(MailManagement(service.store, service.access,
                worker_online=lambda: service._online and service.worker is not None).confirm, access, payload["token"],
                expected_action=expected, request_id=request.request_id)
        elif request.action in {"retry", "prepare_remove", "request_restore", "prepare_refetch", "dismiss_interaction", "prepare_interaction_retry"}:
            if set(payload) != {"target_ids"}:
                raise MailError("invalid_request")
            result = await service.manage(access, ManageAction(action=request.action, target_ids=payload["target_ids"], request_id=request.request_id))
        else:
            raise MailError("unsupported_action")
        data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        async with async_delivery(service, access):
            for evidence in getattr(result, "sources", []):
                service.retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))
            await connection.send(json.dumps({"type": "mail_knowledge_result", "event": "mail_knowledge_result",
                "request_id": request.request_id, "status": data.get("status", "completed"), "data": data, "error": None}, ensure_ascii=False))
    except (MailError, ValueError) as error:
        code = error.code if isinstance(error, MailError) else "invalid_request"
        await connection.send(json.dumps({"type": "mail_knowledge_result", "event": "mail_knowledge_result",
            "request_id": str(request_id)[:128], "status": "failed", "data": None, "error": code}))
