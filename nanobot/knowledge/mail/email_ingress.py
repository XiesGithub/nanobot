"""UID-based mail admission adapter. Historical reads do not pass through this path."""

from __future__ import annotations

import hashlib
import imaplib
import json
import random
import re
import time
from contextlib import suppress
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from nanobot.knowledge.mail.contracts import ArchiveEnvelope, ArchivePart, MailError, SourceIdentity
from nanobot.knowledge.mail.dispatch import InteractionDispatch
from nanobot.security.context import SecurityContext

MAX_DEGRADED_DISPATCHES = 100000


class MailIngress:
    def __init__(self, channel, service):
        self.channel = channel
        self.service = service
        self.dispatch = InteractionDispatch(service.store)
        self._cursor = 0
        self._epoch = None
        self._waiting: dict[int, tuple[float, float]] = {}
        self._attempts: dict[int, int] = {}
        self._dispatched: set[str] = set()
        self.last_error: str | None = None
        self._capacity_head: int | None = None
        self._recovery_baseline: set[int] | None = None

    def _identity(self, uidvalidity, uid):
        if self.service.config.account_id is None:
            raise MailError("identity_unavailable")
        return SourceIdentity(account_id=self.service.config.account_id,
                              mailbox=self.channel.config.imap_mailbox or "INBOX",
                              uidvalidity=uidvalidity, uid=uid)

    def _health(self, code):
        self.last_error = code
        with suppress(Exception), self.service.store.connect(write=True) as db:
            db.execute("""INSERT INTO health_events(code,scope_id,first_at,last_at) VALUES(?,'email',?,?)
                ON CONFLICT(code,scope_id) DO UPDATE SET last_at=excluded.last_at,count=count+1,resolved_at=NULL""",
                (code, time.time(), time.time()))

    def _waiting_for(self, uid, code):
        first, _ = self._waiting.get(uid, (time.time(), 0))
        attempt = min(self._attempts.get(uid, 0) + 1, 8)
        next_at = time.time() + min(300, 5 * 2 ** (attempt - 1)) * random.uniform(.8, 1.2)
        with suppress(Exception), self.service.store.connect(write=True) as db:
            source = self._identity(self._epoch, uid)
            if uid in self._waiting or db.execute("SELECT COUNT(*) FROM admission_waits WHERE state='waiting'").fetchone()[0] < 1000:
                db.execute("""INSERT INTO admission_waits
                    (source_key,source_json,first_at,next_attempt_at,error_code,attempt) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(source_key) DO UPDATE SET next_attempt_at=excluded.next_attempt_at,
                    error_code=excluded.error_code,attempt=excluded.attempt,state='waiting'""", (
                    source.key, source.model_dump_json(), first, next_at, code, attempt,
                ))
        if len(self._waiting) < 1000 or uid in self._waiting:
            self._waiting[uid] = (first, next_at)
            self._attempts[uid] = attempt
        self._health(code)

    def _schedule(self, uids, mode):
        if not uids:
            return []
        # Rotating bounded scan avoids markSeen=false records monopolizing batches.
        start = next((i for i, uid in enumerate(uids) if uid > self._cursor), 0)
        scan = (uids[start:] + uids[:start])[:200]
        self._cursor = scan[-1]
        eligible = []
        for uid in scan:
            source = self._identity(self._epoch, uid)
            receipt = self.service.store.source_receipt(source.key)
            dispatch = self.dispatch.get(source.key)
            if receipt and not self.channel.config.mark_seen and (not mode["trigger_agent"] or dispatch is not None):
                continue
            eligible.append(uid)
        scan = eligible
        now = time.time()
        due = sorted((uid for uid in scan if uid in self._waiting and self._waiting[uid][1] <= now),
                     key=lambda uid: self._waiting[uid][0])
        fresh = [uid for uid in scan if uid not in self._waiting]
        selected = due[:10] + fresh[:10]
        selected += (due[10:] + fresh[10:])[:20 - len(selected)]
        return selected

    def capacity_available(self):
        """Resume only temporary capacity waits; other retry schedules stay intact."""
        with self.service.store.connect() as db:
            for row in db.execute("SELECT source_json FROM admission_waits WHERE error_code IN ('capacity_backpressure','disk_full')"):
                source = SourceIdentity.model_validate_json(row[0])
                if (source.account_id == self.service.config.account_id and source.uidvalidity == self._epoch
                    and source.mailbox == (self.channel.config.imap_mailbox or "INBOX") and source.uid in self._waiting):
                    first, _ = self._waiting[source.uid]
                    self._waiting[source.uid] = (first, 0)

    def poll(self):
        mode = self.service.mode()
        if not mode["archive_enabled"] and not mode["trigger_agent"]:
            return []
        cfg = self.channel.config
        client = (imaplib.IMAP4_SSL if cfg.imap_use_ssl else imaplib.IMAP4)(cfg.imap_host, cfg.imap_port, timeout=10)
        items = []
        try:
            client.login(cfg.imap_username, cfg.imap_password)
            status, _ = client.select(cfg.imap_mailbox or "INBOX")
            if status != "OK":
                raise MailError("imap_unavailable")
            _, validity_data = client.response("UIDVALIDITY")
            if not validity_data or not validity_data[0]:
                raise MailError("identity_unavailable")
            uidvalidity = int(validity_data[0])
            if self._epoch != uidvalidity:
                self._cursor, self._waiting = 0, {}
                self._attempts = {}
                self._capacity_head = None
                self._recovery_baseline = None
                self._epoch = uidvalidity
                with self.service.store.connect() as db:
                    for waiting in db.execute("SELECT * FROM admission_waits WHERE state='waiting' ORDER BY first_at LIMIT 1000"):
                        identity = SourceIdentity.model_validate_json(waiting["source_json"])
                        if identity.account_id == self.service.config.account_id and identity.uidvalidity == uidvalidity and identity.mailbox == (cfg.imap_mailbox or "INBOX"):
                            self._waiting[identity.uid] = (waiting["first_at"], waiting["next_attempt_at"])
                            self._attempts[identity.uid] = waiting["attempt"]
                            if self._capacity_head is None and waiting["error_code"] in {"capacity_backpressure", "disk_full"}:
                                self._capacity_head = identity.uid
            status, data = client.uid("SEARCH", None, "UNSEEN")
            if status != "OK" or not data:
                return []
            uids = sorted(int(value) for value in data[0].split())
            missing = set(self._waiting) - set(uids)
            if missing:
                # The UNSEEN poll cannot distinguish another client's Seen flag
                # from deletion. Preserve an explicit coverage gap, never imply
                # acceptance or silently turn this into whole-mailbox backfill.
                with self.service.store.connect(write=True) as db:
                    for uid in missing:
                        db.execute("UPDATE admission_waits SET error_code='source_no_longer_unseen',state='source_unavailable' WHERE source_key=?",
                                   (self._identity(uidvalidity, uid).key,))
                for uid in missing:
                    self._waiting.pop(uid, None)
                    self._attempts.pop(uid, None)
                self._health("admission_source_no_longer_unseen")
            if self._recovery_baseline is None:
                self._recovery_baseline = set(uids) if self.service.unclean_start else set()
            if self._capacity_head not in uids:
                if self._capacity_head is not None:
                    self._health("admission_source_no_longer_unseen")
                self._capacity_head = None
            # Persistent pending intentions can recover even after an ACK/server deletion.
            if mode["trigger_agent"]:
                for pending in self.dispatch.pending():
                    item = pending["input"]
                    item["_security"] = self._security(pending)
                    items.append(item)
            for uid in self._schedule(uids, mode):
                try:
                    item = self._receive(client, self._identity(uidvalidity, uid), mode)
                    if item:
                        items.append(item)
                except Exception:
                    self._waiting_for(uid, "ingress_failed")
            return items
        finally:
            with suppress(Exception):
                client.logout()

    @staticmethod
    def _security(dispatch):
        return SecurityContext(origin_kind="email_ingress", source_key=dispatch["source_key"],
                               task_id=dispatch["task_id"], attempt_id=dispatch["attempt_id"],
                               mode_revision=dispatch["mode_revision"])

    @staticmethod
    def _ack(client, uid):
        status, _ = client.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        if status != "OK":
            raise MailError("imap_ack_failed")

    def _clear_wait(self, source):
        if self._capacity_head == source.uid:
            self._capacity_head = None
        self._waiting.pop(source.uid, None)
        self._attempts.pop(source.uid, None)
        with self.service.store.connect(write=True) as db:
            db.execute("DELETE FROM admission_waits WHERE source_key=?", (source.key,))

    def _receive(self, client, source, mode):
        store, cfg = self.service.store, self.channel.config
        existing = store.source_receipt(source.key)
        dispatch = self.dispatch.get(source.key)
        if mode["trigger_agent"] and dispatch is None and source.uid in (self._recovery_baseline or set()):
            with store.connect(write=True) as db:
                from nanobot.knowledge.mail.store import identifier
                db.execute("INSERT OR IGNORE INTO interaction_dispatches VALUES(?,?,?,'uncertain',NULL,?,?,1)", (
                    source.key, identifier("task_"), mode["mode_revision"], identifier("attempt_"), time.time(),
                ))
            dispatch = self.dispatch.get(source.key)
            self._health("interaction_uncertain")
        if existing and (not mode["trigger_agent"] or dispatch is not None):
            if cfg.mark_seen and existing.durable:
                self._ack(client, source.uid)
            if existing.durable:
                self._clear_wait(source)
            return None
        needs_archive = bool(mode["archive_enabled"] and existing is None)
        needs_interaction = bool(mode["trigger_agent"] and dispatch is None and source.key not in self._dispatched)
        if not needs_archive and not needs_interaction:
            return None
        status, fetched = client.uid("FETCH", str(source.uid), "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER])")
        if status != "OK":
            raise MailError("imap_unavailable")
        raw_header = self.channel._extract_message_bytes(fetched)
        if raw_header is None:
            raise MailError("identity_unavailable")
        headers = BytesParser(policy=policy.default).parsebytes(raw_header)
        sender = parseaddr(headers.get("From", ""))[1].strip().lower()
        if not sender or self.channel._is_self_address(sender) or not self.channel.is_allowed(sender):
            return None
        spf, dkim = self.channel._check_authentication_results(headers)
        if (cfg.verify_spf and not spf) or (cfg.verify_dkim and not dkim):
            return None
        response = b" ".join(item[0] for item in fetched if isinstance(item, tuple))
        if self.channel._extract_uid(fetched) != str(source.uid):
            raise MailError("identity_unavailable")
        size_match = re.search(rb"RFC822.SIZE\s+(\d+)", response)
        if not size_match:
            raise MailError("identity_unavailable")
        size = int(size_match[1])
        if size > self.service.config.max_message_bytes:
            # Persist a header-only, explicitly failed source before policy ACK.
            if needs_archive:
                envelope = self._envelope(headers, source, "", response, mode, None)
                envelope.rejection_code = "size_limit"
                receipt = self.service.accept_sync(envelope)
                if receipt.durable:
                    if cfg.mark_seen:
                        self._ack(client, source.uid)
                else:
                    self._waiting_for(source.uid, receipt.error_code or "storage_unavailable")
            self._health("size_limit")
            return None
        reservation = None
        if needs_archive and self._capacity_head is not None and self._capacity_head != source.uid:
            self._waiting_for(source.uid, "capacity_backpressure")
            needs_archive = False
            if not needs_interaction:
                return None
        if needs_archive:
            try:
                reservation = store.reserve(source.key, size)
            except MailError as error:
                if error.code in {"capacity_backpressure", "disk_full"} and self._capacity_head is None:
                    self._capacity_head = source.uid
                self._waiting_for(source.uid, error.code)
                needs_archive = False
                if not needs_interaction:
                    return None
        try:
            status, fetched = client.uid("FETCH", str(source.uid), "(UID INTERNALDATE BODY.PEEK[])")
            raw = self.channel._extract_message_bytes(fetched)
            if status != "OK" or raw is None:
                raise MailError("imap_unavailable")
            if self.channel._extract_uid(fetched) != str(source.uid):
                raise MailError("identity_unavailable")
            if len(raw) > self.service.config.max_message_bytes:
                raise MailError("size_limit")
            message = BytesParser(policy=policy.default).parsebytes(raw)
            body = self.channel._extract_text_body(message)
            subject = self.channel._decode_header_value(message.get("Subject", ""))
            item = None
            if needs_interaction:
                item = self._projection(message, sender, subject, body, source)
            receipt = existing
            if needs_archive:
                envelope = self._envelope(message, source, body, response, mode, item)
                receipt = self.service.accept_sync(envelope, reservation)
                reservation = None
                if not receipt.durable:
                    self._waiting_for(source.uid, receipt.error_code or "storage_unavailable")
            if item is not None:
                try:
                    dispatch = self.dispatch.intent(source.key, item, mode["mode_revision"])
                    item["_security"] = self._security(dispatch)
                except Exception:
                    # Current-process delivery remains possible, but no false durable claim.
                    self._health("dispatch_tracking_degraded")
                    item["_security"] = SecurityContext(origin_kind="email_ingress", source_key=source.key,
                                                         mode_revision=mode["mode_revision"])
                    if len(self._dispatched) >= MAX_DEGRADED_DISPATCHES:
                        # Never evict uncertainty fences to admit more effects.
                        # Durable intents remain available when storage recovers.
                        item = None
                        self._health("dispatch_tracking_capacity")
                    else:
                        self._dispatched.add(source.key)
            if receipt and receipt.durable:
                if cfg.mark_seen:
                    self._ack(client, source.uid)
                self._clear_wait(source)
            elif not mode["archive_enabled"] and dispatch and cfg.mark_seen:
                self._ack(client, source.uid)
            return item
        finally:
            if reservation:
                store.release_reservation(reservation)

    def _projection(self, message, sender, subject, body, source):
        cfg = self.channel.config
        content = f"[EMAIL-CONTEXT] Email received.\nFrom: {sender}\nSubject: {subject}\nDate: {message.get('Date', '')}\n\n{body[:cfg.max_body_chars]}"
        paths = []
        if cfg.allowed_attachment_types:
            paths = [str(p) for p in self.channel._extract_attachments(
                message, source.key, allowed_types=cfg.allowed_attachment_types,
                max_size=cfg.max_attachment_size, max_count=cfg.max_attachments_per_email,
            )]
        return {"sender": sender, "subject": subject, "message_id": str(message.get("Message-ID", "")),
                "content": content, "media": paths, "metadata": {"uid": str(source.uid)}}

    def _envelope(self, message, source, body, response, mode, item):
        cfg = self.service.config
        parts, accepted = [], 0
        for section, part in mime_parts(message):
            if not (part.get_filename() or part.get_content_disposition() == "attachment"):
                continue
            content = part.as_bytes(policy=policy.SMTP) if part.is_multipart() else (part.get_payload(decode=True) or b"")
            mime = part.get_content_type()
            error = None
            if part.is_multipart() or mime not in cfg.attachment_types:
                error = "unsupported_type"
            elif len(content) > cfg.max_attachment_bytes:
                error = "size_limit"
            elif accepted >= cfg.max_attachments:
                error = "attachment_count_limit"
            else:
                accepted += 1
            descriptor = json.dumps([section, mime, part.get_filename(), len(content),
                                     str(part.get("Content-ID", "")), str(part.get("Content-Transfer-Encoding", "")),
                                     hashlib.sha256(content).hexdigest()])
            parts.append(ArchivePart(part_id=section, mime_section=section,
                                     part_descriptor=hashlib.sha256(descriptor.encode()).hexdigest(),
                                     filename=str(part.get_filename() or "attachment")[:1024], mime=mime,
                                     size=len(content), content=None if error else content, error_code=error))
        received = None
        match = re.search(rb'INTERNALDATE "([^"]+)"', response)
        if match:
            with suppress(Exception):
                # RFC-style parse supports IMAP's day-month-year date with zone.
                received = parsedate_to_datetime(match[1].decode().replace("-", " ", 2))
        sent = None
        with suppress(Exception):
            sent = parsedate_to_datetime(str(message.get("Date", "")))
            if sent.utcoffset() is None:
                sent = None
        sender_name, sender = parseaddr(message.get("From", ""))
        fingerprint = hashlib.sha256(json.dumps([
            self.channel.config.imap_host.casefold(), self.channel.config.imap_username,
        ]).encode()).hexdigest()
        body_format = "plain"
        best_body = message.get_body(preferencelist=("plain", "html"))
        if best_body is not None and best_body.get_content_type() == "text/html":
            body, body_format = best_body.get_content(), "html"
        owners = {b.owner_id for b in cfg.owner_bindings}
        return ArchiveEnvelope(source=source, source_fingerprint=fingerprint,
            owner_id=next(iter(owners), "local-owner"),
            sender_address=sender.lower(), sender_name=sender_name[:512],
            subject=self.channel._decode_header_value(message.get("Subject", ""))[:1024],
            message_id_header=str(message.get("Message-ID", "")), body=body, body_format=body_format, parts=parts,
            received_at=received, received_timezone=match[1].decode() if match else None,
            received_time_basis="internaldate" if received else "unknown", sent_at=sent,
            interaction_input=item, mode_revision=mode["mode_revision"])


def mime_parts(message, prefix=""):
    """Stable hierarchical MIME section identity, including nested multiparts."""
    if prefix and (message.get_filename() or message.get_content_disposition() == "attachment"):
        yield prefix, message
        return
    if message.is_multipart():
        for index, part in enumerate(message.iter_parts(), 1):
            section = f"{prefix}.{index}" if prefix else str(index)
            if part.get_content_type() == "message/rfc822":
                # Retain the encapsulated attachment identity; never merge its
                # inner attachments into this message's first-level archive.
                yield section, part
            else:
                yield from mime_parts(part, section)
    else:
        yield prefix or "1", message
