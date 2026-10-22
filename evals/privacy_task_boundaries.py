"""Real local transport and domain authorization probes for privacy evals.

This helper is observation-only. It uses the existing MailService and a real
loopback WebSocketChannel server; it never invokes IMAP or external hosts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import time
from urllib.parse import quote

import httpx
import websockets


def _identity(security, access=None):
    return {
        "principal_id": getattr(security, "principal_id", None),
        "owner_id": getattr(access, "owner_id", None) if access else getattr(security, "owner_id", None),
        "binding_id": getattr(access, "binding_id", None) if access else getattr(security, "binding_id", None),
        "authenticated": bool(getattr(security, "authenticated", False)),
    }


def _access_identity(access):
    if access is None:
        return None
    return {
        "principal_id": access.principal_id,
        "owner_id": access.owner_id,
        "binding_id": access.binding_id,
        "session_key_sha256": hashlib.sha256(access.session_key.encode()).hexdigest(),
    }


def _wire_response(response):
    body = response.content
    result = {
        "status": response.status_code,
        "headers": {
            name: response.headers[name]
            for name in ("content-type", "cache-control", "content-range", "x-content-type-options")
            if name in response.headers
        },
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "byte_length": len(body),
    }
    try:
        result["body_text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        result["body_base64"] = base64.b64encode(body).decode("ascii")
    return result


def _wire_frame(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    return {
        "body_text": raw,
        "body_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "value": value,
    }


def _query_identity_rows(service):
    """Return opaque query/cache identities without query text or source text."""
    # bindings live in the main database, not query_state.sqlite3.
    with service.store.connect() as db:
        principals = {
            row["binding_id"]: row["principal_id"]
            for row in db.execute("SELECT binding_id,principal_id FROM bindings")
        }
    with service.store.connect(query_state=True) as db:
        runs = [dict(row) for row in db.execute("""
            SELECT query_id,instance_id,owner_id,binding_id,session_key
            FROM query_runs ORDER BY query_id
        """)]
        evidence = [dict(row) for row in db.execute("""
            SELECT e.evidence_id,e.query_id,e.mail_id,e.document_id,e.ordinal,
                   q.owner_id,q.binding_id,q.session_key
            FROM query_evidence e JOIN query_runs q USING(query_id)
            ORDER BY e.query_id,e.ordinal
        """)]
        focus = [dict(row) for row in db.execute("""
            SELECT owner_id,binding_id,session_key FROM mail_focus
            ORDER BY owner_id,binding_id,session_key
        """)]

    def scrub(rows):
        for row in rows:
            row["principal_id"] = principals.get(row.get("binding_id"))
            session_key = row.pop("session_key", None)
            row["session_key_sha256"] = hashlib.sha256((session_key or "").encode()).hexdigest()
        return rows

    return {"query_runs": scrub(runs), "query_evidence": scrub(evidence), "mail_focus": scrub(focus)}


def _case_refs(refs):
    if isinstance(refs, dict):
        cases = {key: value for key, value in refs.items()
                 if key in {"V095", "V107"} and isinstance(value, dict)}
        if cases:
            return cases
        case_id = str(refs.get("case_id", "case"))
        return {case_id: refs}
    return {"case": {}}


def _sources_for_case(source_state, case_id):
    if isinstance(source_state, dict):
        if isinstance(source_state.get(case_id), list):
            return source_state[case_id]
        source_state = source_state.get("sources", [])
    if isinstance(source_state, list):
        return source_state
    return []


def _error_code(error):
    return getattr(error, "code", None) or type(error).__name__


async def _wait_server(channel, task):
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if task.done():
            error = task.exception()
            if error:
                raise error
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", channel.config.port), timeout=0.25
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.025)
    raise TimeoutError("loopback WebSocket server did not start")


async def _probe_case(service, web, channel, actor_a, actor_b, source_state, case_id, ref, runtime):
    from nanobot.knowledge.mail.contracts import AccessContext, MailError, MailQuery, SourceRead

    sources = _sources_for_case(source_state, case_id)
    security_a, security_b = actor_a["security"], actor_b["security"]
    access_a = actor_a.get("access")
    chat_a, chat_b = actor_a["chat_id"], actor_b["chat_id"]
    session_a, session_b = web.session_key(security_a, chat_a), web.session_key(security_b, chat_b)
    identity_a, identity_b = _identity(security_a, access_a), _identity(security_b, actor_b.get("access"))

    evidence_id = ref.get("evidence_id") or next((row.get("evidence_id") for row in sources if row.get("evidence_id")), None)
    mail_id = ref.get("mail_id") or next((row.get("mail_id") for row in sources if row.get("mail_id")), None)
    document_id = ref.get("document_id") or next((row.get("document_id") for row in sources if row.get("kind") == "body"), None)
    attachment_id = ref.get("attachment_document_id") or next(
        (row.get("document_id") for row in sources if row.get("kind") == "attachment"), None
    )
    query_id = ref.get("query_id") or next((row.get("query_id") for row in sources if row.get("query_id")), None)
    cursor = ref.get("cursor") or (f"{query_id}:0" if query_id else None)
    source_ids = {key: value for key, value in {
        "evidence_id": evidence_id, "query_id": query_id, "mail_id": mail_id,
        "document_id": document_id, "attachment_document_id": attachment_id, "cursor": cursor,
    }.items() if value}

    boundaries, visible, coverage, probe_errors = [], [], [], []

    def boundary(name, actor, *, allowed=False, denied=False, code=None, stage=None,
                 request_ref=None, checks=None, result=None, attack=None):
        identity = identity_a if actor == "A" else identity_b
        normalized_checks = []
        for check in checks or []:
            check = dict(check)
            check.setdefault("actor", actor)
            check.setdefault("identity", identity)
            normalized_checks.append(check)
        item = {
            "name": name, "actor": actor,
            "principal_id": identity["principal_id"], "owner_id": identity["owner_id"],
            "binding_id": identity["binding_id"], "allowed": bool(allowed),
            "denied": bool(denied), "code": code, "stage": stage,
            "request_ref": request_ref, "source_ids": source_ids,
            "checks": normalized_checks, "result": result,
        }
        if attack:
            item["attack"] = attack
        boundaries.append(item)
        return item

    try:
        AccessContext.from_security(security_b, session_b)
        auth_denied, auth_code = False, "unexpectedly_authenticated"
    except MailError as error:
        auth_denied, auth_code = error.code == "unauthorized", error.code
    boundary("auth_B", "B", denied=auth_denied, code=auth_code,
             stage="AccessContext.from_security", request_ref="anonymous_server_identity",
             result={"security_authenticated": bool(security_b.authenticated)})
    coverage.append("auth_B")

    cache_before = _query_identity_rows(service)
    cache_checks = []
    session_checks = []
    forged_access = None
    if access_a is not None:
        # Explicitly model untrusted owner/binding claims combined with B's
        # actual principal and server-derived B session.
        forged_access = access_a.model_copy(update={
            "principal_id": security_b.principal_id,
            "session_key": session_b,
        })

    async def domain_attempt(operation, label, request, access, expected_codes=("unauthorized",)):
        try:
            value = (await service.search(access, request) if operation == "search"
                     else await service.read_source(access, request))
            return {"operation": label, "surface": "domain", "allowed": True,
                    "denied": False, "code": None,
                    "result_count": len(getattr(value, "sources", getattr(value, "matches", [])))}
        except Exception as error:
            code = _error_code(error)
            return {"operation": label, "surface": "domain", "allowed": False,
                    "denied": code in expected_codes, "code": code,
                    "error_type": type(error).__name__}

    if forged_access is not None:
        if query_id:
            attempt = await domain_attempt("search", "search_cursor", MailQuery(cursor=cursor), forged_access)
            attempt.update({"request_ref": "A_query_cursor", "identity": identity_b,
                           "submitted_claims": _access_identity(forged_access),
                           "attack": "synthetic_forged_access_context"})
            cache_checks.append(attempt)
            other_session = web.session_key(security_a, chat_a + "-other-session")
            other_access = access_a.model_copy(update={"session_key": other_session})
            attempt = await domain_attempt("search", "same_owner_other_session_cursor",
                                           MailQuery(cursor=cursor), other_access,
                                           expected_codes=("source_not_found",))
            attempt.update({"request_ref": "A_query_cursor_from_other_session", "actor": "A",
                           "identity": identity_a, "submitted_claims": _access_identity(other_access),
                           "attack": "same_owner_different_server_session"})
            session_checks.append(attempt)
        for label, value, key in (("read_evidence", evidence_id, "evidence_id"),
                                  ("read_document", document_id, "document_id"),
                                  ("read_mail", mail_id, "mail_id")):
            if not value:
                continue
            request = SourceRead(**{key: value}, cursor=cursor if key != "evidence_id" else None)
            attempt = await domain_attempt("read_source", label, request, forged_access)
            attempt.update({"request_ref": "A_source_id", "source_id_type": key,
                           "identity": identity_b, "submitted_claims": _access_identity(forged_access),
                           "attack": "synthetic_forged_access_context"})
            cache_checks.append(attempt)

    cache_after_domain = _query_identity_rows(service)
    cache_result = {
        "before": cache_before,
        "after_domain": cache_after_domain,
        "after_all_probes": None,
        "checks": cache_checks,
        "session_isolation_checks": session_checks,
        "rows_unchanged_by_B_domain_attempts": cache_before == cache_after_domain,
        "actor_session_keys": {
            "A_sha256": hashlib.sha256(session_a.encode()).hexdigest(),
            "B_sha256": hashlib.sha256(session_b.encode()).hexdigest(),
            "distinct": session_a != session_b,
        },
        "owner_session_keys": sorted(set(
            [access_a.session_key] if access_a is not None else []
        ) | {item for item in actor_a.get("owner_session_keys", []) if isinstance(item, str)}),
    }
    cache_denied = bool(cache_checks) and all(item["denied"] for item in cache_checks)
    boundary("cache_B", "B", denied=cache_denied,
             code="unauthorized" if cache_denied else "cache_probe_incomplete",
             stage="cursor_focus_and_session_binding", request_ref="A_query_cursor",
             checks=cache_checks, result=cache_result,
             attack="anonymous_identity_and_explicit_forged_access_payload")
    boundary("search_B", "B",
             denied=any(item.get("operation") == "search_cursor" and item["denied"] for item in cache_checks),
             code="unauthorized", stage="MailService.search", request_ref="A_query_cursor",
             checks=[item for item in cache_checks if item.get("operation") == "search_cursor"],
             attack="synthetic_forged_access_context")

    owner_checks = []
    if access_a is not None:
        owner_reads = [("owner_domain_evidence", SourceRead(evidence_id=evidence_id))] if evidence_id else []
        owner_reads.extend((label, SourceRead(**{key: value})) for label, key, value in (
            ("owner_domain_document", "document_id", document_id),
            ("owner_domain_mail", "mail_id", mail_id),
            ("owner_domain_attachment_document", "document_id", attachment_id),
        ) if value)
        for label, request in owner_reads:
            attempt = await domain_attempt("read_source", label, request, access_a, expected_codes=())
            attempt["identity"] = identity_a
            attempt["access_identity"] = _access_identity(access_a)
            owner_checks.append(attempt)
    boundary("actor_owner_read_A", "A", allowed=bool(owner_checks and owner_checks[0]["allowed"]),
             code=owner_checks[0]["code"] if owner_checks else "owner_access_unavailable",
             stage="MailService.read_source", request_ref="A_evidence_id", checks=owner_checks)
    if owner_checks:
        coverage.append("actor_owner_read_A")

    # Exercise SourceFetcher._one's real manage-scope preflight. Instrument
    # IMAP constructors so the local test proves that denial precedes transport.
    source_fetch = {"operation": "prepare_refetch", "surface": "SourceFetcher._one",
                    "identity": identity_b, "allowed": False, "denied": False,
                    "code": "probe_not_completed", "stage": "not_reached",
                    "imap_constructor_calls": 0}
    if forged_access is not None and (document_id or attachment_id):
        from types import SimpleNamespace
        from unittest.mock import patch

        from nanobot.knowledge.mail.source_fetch import SourceFetcher

        constructor_calls = {"plain": 0, "ssl": 0}

        def imap_plain(*_args, **_kwargs):
            constructor_calls["plain"] += 1
            raise AssertionError("offline probe must not reach IMAP")

        def imap_ssl(*_args, **_kwargs):
            constructor_calls["ssl"] += 1
            raise AssertionError("offline probe must not reach IMAP")

        fetcher = SourceFetcher(SimpleNamespace(service=service))
        with patch("nanobot.knowledge.mail.source_fetch.imaplib.IMAP4", side_effect=imap_plain), \
             patch("nanobot.knowledge.mail.source_fetch.imaplib.IMAP4_SSL", side_effect=imap_ssl):
            try:
                fetcher._one("prepare_refetch", forged_access,
                             {"action_id": "privacy-boundary-preflight"},
                             {"id": attachment_id or document_id})
                source_fetch.update({"code": "unexpectedly_accepted", "stage": "after_preflight"})
            except Exception as error:
                code = _error_code(error)
                source_fetch.update({"code": code, "stage": "SourceFetcher._one.require_manage",
                                     "denied": code == "unauthorized"})
        source_fetch["imap_constructor_calls"] = sum(constructor_calls.values())
        source_fetch["imap_constructor_counts"] = constructor_calls
        source_fetch["submitted_claims"] = _access_identity(forged_access)
    boundary("source_fetch_B", "B", denied=source_fetch["denied"], code=source_fetch["code"],
             stage=source_fetch["stage"], request_ref="forged_manage_refetch", checks=[source_fetch],
             result={"tested": source_fetch["stage"] == "SourceFetcher._one.require_manage",
                     "imap_constructor_calls": source_fetch["imap_constructor_calls"]})
    coverage.append("source_fetch_B")
    if cache_checks:
        coverage.extend(["search_B", "cache_B"])

    old_host, old_port = channel.config.host, channel.config.port
    owned_server = not (channel._server_task and not channel._server_task.done())
    server_task = None
    if owned_server:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            channel.config.host = "127.0.0.1"
            channel.config.port = probe.getsockname()[1]
        server_task = asyncio.create_task(channel.start())
    base = f"http://127.0.0.1:{channel.config.port}"
    ws_base = f"ws://127.0.0.1:{channel.config.port}{channel.config.path}"

    def record_visible(surface, actor, response, path):
        visible.append({"surface": surface, "actor": actor, "path": path,
                        "identity": identity_a if actor == "A" else identity_b, **response})

    try:
        if owned_server:
            await _wait_server(channel, server_task)
        owner_http_checks, guest_http_checks = [], []

        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            async def http_get(actor, path, *, range_header=False):
                token = actor_a["token"] if actor == "A" else actor_b["token"]
                headers = {"Authorization": "Bearer " + token}
                if range_header:
                    headers["Range"] = "bytes=0-"
                response = await client.get(base + path, headers=headers)
                result = _wire_response(response)
                record_visible("http", actor, result, path)
                return result

            chat_query = "?chat_id=" + quote(chat_a, safe="")
            if evidence_id:
                path = f"/api/mail-knowledge/sources/{quote(evidence_id, safe='_')}" + chat_query
                for actor, target in (("A", owner_http_checks), ("B", guest_http_checks)):
                    result = await http_get(actor, path)
                target.append({"operation": "read_evidence", "identity": identity_a if actor == "A" else identity_b,
                               "allowed": actor == "A" and result["status"] == 200,
                                   "denied": actor == "B" and result["status"] == 403,
                                   "code": result.get("body_text") if actor == "B" else None,
                                   "result": result, "source_ids": {"evidence_id": evidence_id}})
            if document_id:
                path = f"/api/mail-knowledge/sources/{quote(document_id, safe='_')}" + chat_query
                for actor, target in (("A", owner_http_checks), ("B", guest_http_checks)):
                    result = await http_get(actor, path)
                    target.append({"operation": "read_document", "identity": identity_a if actor == "A" else identity_b,
                                   "allowed": actor == "A" and result["status"] == 200,
                                   "denied": actor == "B" and result["status"] == 403,
                                   "code": result.get("body_text") if actor == "B" else None,
                                   "result": result, "source_ids": {"document_id": document_id}})
                if cursor:
                    path = f"/api/mail-knowledge/sources/{quote(document_id, safe='_')}?cursor={quote(cursor, safe=':_')}&chat_id={quote(chat_a, safe='')}"
                    result = await http_get("B", path)
                    guest_http_checks.append({"operation": "expand_cursor", "identity": identity_b, "allowed": False,
                                             "denied": result["status"] == 403, "code": result.get("body_text"),
                                             "result": result, "source_ids": {"document_id": document_id, "cursor": cursor}})
            if mail_id:
                path = f"/api/mail-knowledge/sources/{quote(mail_id, safe='_')}" + chat_query
                for actor, target in (("A", owner_http_checks), ("B", guest_http_checks)):
                    result = await http_get(actor, path)
                    target.append({"operation": "read_mail", "identity": identity_a if actor == "A" else identity_b,
                                   "allowed": actor == "A" and result["status"] == 200,
                                   "denied": actor == "B" and result["status"] == 403,
                                   "code": result.get("body_text") if actor == "B" else None,
                                   "result": result, "source_ids": {"mail_id": mail_id}})
            if attachment_id:
                path = f"/api/mail-knowledge/attachments/{quote(attachment_id, safe='_')}" + chat_query
                for actor, target in (("A", owner_http_checks), ("B", guest_http_checks)):
                    result = await http_get(actor, path, range_header=True)
                    target.append({"operation": "read_attachment", "identity": identity_a if actor == "A" else identity_b,
                                   "allowed": actor == "A" and result["status"] == 206,
                                   "denied": actor == "B" and result["status"] == 403,
                                   "code": result.get("body_text") if actor == "B" else None,
                                   "result": result, "source_ids": {"attachment_document_id": attachment_id}})

            history_path = "/api/sessions/" + quote("websocket:" + chat_a, safe="") + "/messages" + chat_query
            a_history = await http_get("A", history_path)
            b_history = await http_get("B", history_path)
            owner_http_checks.append({"operation": "read_owner_history", "identity": identity_a,
                                      "allowed": a_history["status"] == 200,
                                      "denied": False, "result": a_history})
            guest_http_checks.append({"operation": "read_owner_history", "identity": identity_b, "allowed": False,
                                      "denied": b_history["status"] == 403,
                                      "code": b_history.get("body_text"), "result": b_history})

        owner_checks.extend(owner_http_checks)
        for name, operation in (("read_evidence_B", "read_evidence"),
                                ("read_document_B", "read_document"),
                                ("read_mail_B", "read_mail"),
                                ("expand_http_B", "expand_cursor"),
                                ("attachment_http_B", "read_attachment"),
                                ("history_http_B", "read_owner_history")):
            checks = [item for item in guest_http_checks if item["operation"] == operation]
            if name in {"read_evidence_B", "read_document_B", "read_mail_B"}:
                checks += [item for item in cache_checks if item.get("operation") == operation]
            boundary(name, "B", denied=bool(checks) and all(item["denied"] for item in checks),
                     code="unauthorized", stage="real_loopback_http", request_ref=operation,
                     checks=checks)
        coverage.extend(["owner_http_positive", "read_evidence_B", "read_document_B", "read_mail_B",
                         "expand_http_B", "attachment_http_B", "history_http_B"])

        async def select_source(actor, label):
            token = actor["token"]
            # The channel's transport ticket and MailWebAccess token are the
            # same synthetic secret, but only the ticket is single-use.
            channel._issued_tokens[token] = time.monotonic() + 60
            uri = ws_base + "?token=" + quote(token, safe="") + "&client_id=" + label
            request_id = "privacy-select-" + label
            async with websockets.connect(uri, open_timeout=5, close_timeout=2, max_size=1_000_000) as ws:
                ready = _wire_frame(await asyncio.wait_for(ws.recv(), 5))
                target = {"evidence_id": evidence_id} if evidence_id else {"mail_id": mail_id}
                target["chat_id"] = chat_a
                await ws.send(json.dumps({"type": "mail_knowledge", "request_id": request_id,
                                          "action": "select_source", "payload": target}, ensure_ascii=False))
                while True:
                    frame = _wire_frame(await asyncio.wait_for(ws.recv(), 5))
                    value = frame["value"]
                    if isinstance(value, dict) and value.get("request_id") == request_id:
                        return {"ready": ready, "response": frame}

        if evidence_id or mail_id:
            ws_a = await select_source(actor_a, "owner-A")
            ws_b = await select_source(actor_b, "visitor-B")
            record_visible("websocket", "A", ws_a["response"], "mail_knowledge/select_source")
            record_visible("websocket", "B", ws_b["response"], "mail_knowledge/select_source")
            a_value, b_value = ws_a["response"]["value"], ws_b["response"]["value"]
            owner_checks.append({"operation": "owner_websocket_select_source", "surface": "websocket",
                                 "identity": identity_a,
                                 "allowed": bool(a_value and a_value.get("status") == "completed"),
                                 "denied": False, "result": ws_a})
            boundary("envelope_source_B", "B",
                     denied=bool(b_value and b_value.get("status") == "failed" and b_value.get("error") == "unauthorized"),
                     code=(b_value or {}).get("error"), stage="real_loopback_websocket_select_source",
                     request_ref="A_evidence_id", checks=[{"operation": "select_source", "surface": "websocket",
                     "allowed": False, "denied": bool(b_value and b_value.get("error") == "unauthorized"),
                     "result": ws_b}])
        coverage.extend(["actor_owner_read_A_websocket", "envelope_source_B"])

        # Retain A's successful checks only in A-scoped evidence; B checks above
        # contain only B's real responses/errors.
        owner_boundary = next(item for item in boundaries if item["name"] == "actor_owner_read_A")
        owner_boundary["checks"].extend(owner_checks)
        owner_boundary["allowed"] = bool(owner_boundary["allowed"] or any(x.get("allowed") for x in owner_checks))
        final_cache = _query_identity_rows(service)
        cache_result["after_all_probes"] = final_cache
        b_principal = security_b.principal_id
        cache_result["B_identity_rows_absent"] = not any(
            row.get("principal_id") == b_principal
            for group in final_cache.values() for row in group
        )
        cache_result["A_original_query_preserved"] = bool(query_id and any(
            row.get("query_id") == query_id for row in final_cache["query_runs"]
        ))
        cache_result["owner_session_rows"] = [
            row for group in final_cache.values() for row in group
            if row.get("session_key_sha256") in {
                hashlib.sha256(value.encode()).hexdigest() for value in cache_result["owner_session_keys"]
            }
        ]
        cache_boundary = next(item for item in boundaries if item["name"] == "cache_B")
        cache_boundary["denied"] = bool(cache_boundary["denied"] and cache_result["B_identity_rows_absent"])
        cache_boundary["result"] = cache_result
        if not cache_result["B_identity_rows_absent"]:
            cache_boundary["code"] = "unexpected_B_cache_identity"
    except Exception as error:
        # Never serialize exception text: it could contain private request data.
        probe_errors.append({"stage": "loopback_transport", "error_type": type(error).__name__})
    finally:
        if owned_server:
            await channel.stop()
            await asyncio.gather(server_task, return_exceptions=True)
            channel.config.host, channel.config.port = old_host, old_port

    required = ("search_B", "read_evidence_B", "read_document_B", "read_mail_B", "expand_http_B",
                "attachment_http_B", "envelope_source_B", "history_http_B", "source_fetch_B",
                "cache_B", "actor_owner_read_A")
    present = {item["name"] for item in boundaries}
    for name in required:
        if name not in present:
            actor = "A" if name == "actor_owner_read_A" else "B"
            identity = identity_a if actor == "A" else identity_b
            boundaries.append({"name": name, "actor": actor,
                               "principal_id": identity["principal_id"], "owner_id": identity["owner_id"],
                               "binding_id": identity["binding_id"], "allowed": False, "denied": False,
                               "code": "probe_not_completed", "stage": "not_reached",
                               "request_ref": None, "source_ids": source_ids, "checks": [], "result": None})
    return {"boundaries": boundaries, "visible": visible, "coverage": coverage, "errors": probe_errors}


async def probe_boundaries(service, web, channel, actor_a, actor_b, source_state, refs, runtime):
    """Probe one/multiple frozen case refs, aggregating by boundary name."""
    runs = []
    for case_id, case_ref in _case_refs(refs).items():
        run = await _probe_case(service, web, channel, actor_a, actor_b,
                                source_state, case_id, case_ref, runtime)
        for item in run["visible"]:
            item["case_id"] = case_id
        runs.append((case_id, run))

    grouped = {}
    for case_id, run in runs:
        for item in run["boundaries"]:
            name = item["name"]
            aggregate = grouped.get(name)
            if aggregate is None:
                aggregate = {key: value for key, value in item.items()
                             if key not in {"checks", "result", "source_ids"}}
                aggregate["checks"], aggregate["cases"], aggregate["source_ids_by_case"] = [], {}, {}
                grouped[name] = aggregate
            aggregate["checks"].extend(item.get("checks", []))
            aggregate["cases"][case_id] = {
                key: item.get(key) for key in ("allowed", "denied", "code", "stage", "result", "attack")
            }
            aggregate["source_ids_by_case"][case_id] = item.get("source_ids", {})
            aggregate["allowed"] = bool(aggregate.get("allowed", True) and item.get("allowed", False))
            aggregate["denied"] = bool(aggregate.get("denied", True) and item.get("denied", False))
            if aggregate.get("code") is None:
                aggregate["code"] = item.get("code")

    for name, item in grouped.items():
        if name == "cache_B":
            item["result"] = {
                "cases": {case_id: run["result"] for case_id, run in item["cases"].items()},
                "B_identity_rows_absent": all(bool(run.get("result", {}).get("B_identity_rows_absent"))
                                               for run in item["cases"].values()),
                "A_original_query_preserved": all(bool(run.get("result", {}).get("A_original_query_preserved"))
                                                   for run in item["cases"].values()),
                "owner_session_keys": sorted({key for run in item["cases"].values()
                                               for key in run.get("result", {}).get("owner_session_keys", [])}),
            }
        elif len(item["cases"]) == 1:
            item["result"] = next(iter(item["cases"].values())).get("result")
        else:
            item["result"] = {"cases": item["cases"]}
        if name in {"search_B", "read_evidence_B", "read_document_B", "read_mail_B",
                    "expand_http_B", "attachment_http_B", "envelope_source_B", "history_http_B",
                    "source_fetch_B", "cache_B"}:
            item["denied"] = bool(item["cases"]) and all(value["denied"] for value in item["cases"].values())

    return {
        "boundaries": list(grouped.values()),
        "visible": [visible for _, run in runs for visible in run["visible"]],
        "coverage": list(dict.fromkeys(event for _, run in runs for event in run["coverage"])),
        "errors": [{"case_id": case_id, **error} for case_id, run in runs for error in run.get("errors", [])],
        "owner_session_keys": sorted({
            value for _, run in runs
            for item in run["boundaries"] if item["name"] == "cache_B"
            for value in item.get("result", {}).get("owner_session_keys", [])
        }),
    }
