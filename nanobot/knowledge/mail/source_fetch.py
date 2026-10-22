"""Explicit one-source PEEK retrieval and atomic, fenced local publication.

This adapter never searches UNSEEN, marks mail read, emits bus events or sends
mail. Only the maintenance queue invokes it after user confirmation.
"""

from __future__ import annotations

import hashlib
import imaplib
import time
from contextlib import suppress
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from nanobot.knowledge.mail.contracts import MailError, SourceIdentity
from nanobot.knowledge.mail.store import atomic_write, identifier, utc_text


class SourceFetcher:
    def __init__(self, ingress):
        self.ingress = ingress
        self.service = ingress.service
        self.store = self.service.store

    def __call__(self, action, access, payload):
        for target in payload["targets"]:
            self._check_running()
            self._one(action, access, payload, target)

    def _check_running(self):
        worker = self.service.worker
        if worker is not None and worker._stopping:
            raise MailError("service_stopping")

    def _source(self, action, target):
        with self.store.connect() as db:
            if action == "request_restore":
                row = db.execute("SELECT * FROM source_tombstones WHERE old_mail_id=?", (target["id"],)).fetchone()
                if not row:
                    raise MailError("source_not_found")
                if row["cleanup_pending"]:
                    raise MailError("cleanup_pending")
                values = (row["account_id"], row["mailbox_server_name"], row["uidvalidity"], row["uid"])
            else:
                row = db.execute("SELECT b.account_id,b.server_name,b.uidvalidity,m.uid FROM documents d JOIN mail_items m USING(mail_id) JOIN mailboxes b USING(mailbox_id) WHERE document_id=? AND m.deleted_at IS NULL", (target["id"],)).fetchone()
                if not row:
                    raise MailError("source_removed")
                values = tuple(row)
        return SourceIdentity(account_id=values[0], mailbox=values[1], uidvalidity=values[2], uid=values[3])

    def _download(self, source):
        cfg = self.ingress.channel.config
        if not self.service.config.enabled or not cfg.enabled or not cfg.consent_granted:
            raise MailError("source_connection_unavailable")
        if source.account_id != self.service.config.account_id or source.mailbox != (cfg.imap_mailbox or "INBOX"):
            raise MailError("source_identity_changed")
        self._check_running()
        client = None
        try:
            client = (imaplib.IMAP4_SSL if cfg.imap_use_ssl else imaplib.IMAP4)(cfg.imap_host, cfg.imap_port, timeout=10)
            client.login(cfg.imap_username, cfg.imap_password)
            self._check_running()
            status, _ = client.select(source.mailbox, readonly=True)
            if status != "OK":
                raise MailError("source_unrecoverable")
            _, values = client.response("UIDVALIDITY")
            if not values or int(values[0]) != source.uidvalidity:
                raise MailError("source_unrecoverable")
            status, fetched = client.uid("FETCH", str(source.uid), "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER])")
            channel = self.ingress.channel
            raw = channel._extract_message_bytes(fetched)
            if status != "OK" or raw is None or channel._extract_uid(fetched) != str(source.uid):
                raise MailError("source_unrecoverable")
            response = b" ".join(r[0] for r in fetched if isinstance(r, tuple))
            import re
            size = re.search(rb"RFC822.SIZE\s+(\d+)", response)
            if not size or int(size[1]) > self.service.config.max_message_bytes:
                raise MailError("size_limit")
            header = BytesParser(policy=policy.default).parsebytes(raw)
            sender = parseaddr(header.get("From", ""))[1].strip().lower()
            spf, dkim = channel._check_authentication_results(header)
            if not sender or channel._is_self_address(sender) or not channel.is_allowed(sender) or (cfg.verify_spf and not spf) or (cfg.verify_dkim and not dkim):
                raise MailError("source_policy_denied")
            self._check_running()
            status, fetched = client.uid("FETCH", str(source.uid), "(UID INTERNALDATE BODY.PEEK[])")
            raw = channel._extract_message_bytes(fetched)
            if status != "OK" or raw is None or channel._extract_uid(fetched) != str(source.uid):
                raise MailError("source_unrecoverable")
            if len(raw) > self.service.config.max_message_bytes:
                raise MailError("size_limit")
            message = BytesParser(policy=policy.default).parsebytes(raw)
            return self.ingress._envelope(message, source, channel._extract_text_body(message), response,
                                          self.service.mode(), None)
        except MailError:
            raise
        except Exception:
            raise MailError("source_connection_unavailable") from None
        finally:
            if client is not None:
                with suppress(Exception):
                    client.logout()

    def _one(self, action, access, payload, target):
        self.service.access.require(access, "manage")
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM maintenance_effects WHERE action_id=? AND target_id=?", (payload["action_id"], target["id"])).fetchone():
                return
        source = self._source(action, target)
        # Explicit retrieval has no interaction branch. Reserve bounded capacity
        # before fetching content; failure leaves the source untouched.
        reservation = self.store.reserve("maintenance:" + source.key, self.service.config.max_message_bytes)
        try:
            envelope = self._download(source)
            self._check_running()
            self._publish(action, access, payload, target, source, envelope, reservation)
        finally:
            self.store.release_reservation(reservation)

    def _publish(self, action, access, payload, target, source, envelope, reservation):
        # Restrict bytes to explicitly selected parts when refetching. Never
        # augment other skipped attachments merely because policy has expanded.
        if action == "prepare_refetch":
            with self.store.connect() as db:
                doc = db.execute("SELECT * FROM documents WHERE document_id=?", (target["id"],)).fetchone()
            if doc["kind"] == "body":
                if doc["active_generation"] and doc["sha256"] != hashlib.sha256(envelope.body.encode()).hexdigest():
                    raise MailError("source_identity_changed")
                parts = [{"part_id": "body", "content": envelope.body.encode(), "mime": "text/" + envelope.body_format,
                          "mime_section": "", "part_descriptor": "", "filename": "", "error_code": None}]
            else:
                part = next((p for p in envelope.parts if p.part_id == doc["part_id"]), None)
                if not part or part.mime_section != doc["mime_section"] or part.part_descriptor != doc["part_descriptor"]:
                    raise MailError("source_identity_changed")
                if part.content is None:
                    raise MailError(part.error_code or "source_policy_denied")
                parts = [{**part.model_dump(), "content": part.content}]
        else:
            parts = [{"part_id": "body", "content": envelope.body.encode(), "mime": "text/" + envelope.body_format,
                      "mime_section": "", "part_descriptor": "", "filename": "", "error_code": None},
                     *[{**p.model_dump(), "content": p.content} for p in envelope.parts]]
        total = sum(len(p["content"] or b"") for p in parts)
        directory = self.store.safe_path("maintenance/" + identifier("input_"))
        published = False
        try:
            directory.mkdir()
            for part in parts:
                content = part.pop("content")
                part["size"] = len(content) if content is not None else part.get("size", 0)
                part["sha256"] = hashlib.sha256(content).hexdigest() if content is not None else None
                part["object_ref"] = None
                if content is not None:
                    path = directory / (identifier("part_") + ".bin")
                    atomic_write(path, content)
                    part["object_ref"] = str(path.relative_to(self.store.root))
            with self.service.access.delivery(access, "manage"), self.store.lock, self.store.connect(write=True) as db:
                if payload["policy"] != self.service.config.model_dump(mode="json"):
                    raise MailError("revision_conflict")
                account = db.execute("SELECT * FROM accounts WHERE account_id=?", (str(source.account_id),)).fetchone()
                if not account or account["source_fingerprint"] != envelope.source_fingerprint or account["owner_id"] != access.owner_id:
                    raise MailError("source_identity_changed")
                if action == "request_restore":
                    mail_id = target["id"]
                    old = db.execute("SELECT * FROM source_tombstones WHERE old_mail_id=?", (mail_id,)).fetchone()
                    if not old or old["restore_revision"] != target["revision"] or old["cleanup_pending"]:
                        raise MailError("revision_conflict")
                    db.execute("DELETE FROM source_tombstones WHERE old_mail_id=?", (mail_id,))
                    db.execute("UPDATE mail_items SET deleted_at=NULL,revision=revision+1,status='queued',sender_address=?,sender_name=?,subject=?,message_id_header=?,raw_hash=?,received_at=?,received_timezone=?,received_time_basis=?,sent_at=?,archived_at=? WHERE mail_id=?", (
                        envelope.sender_address, envelope.sender_name, envelope.subject, envelope.message_id_header,
                        hashlib.sha256(envelope.body.encode()).hexdigest(), utc_text(envelope.received_at.isoformat()) if envelope.received_at else None,
                        envelope.received_timezone, envelope.received_time_basis, envelope.sent_at.isoformat() if envelope.sent_at else None, time.time(), mail_id,
                    ))
                    for part in parts:
                        db.execute("INSERT INTO documents(document_id,mail_id,kind,part_id,mime_section,part_descriptor,filename,mime,size,object_ref,sha256,extract_status,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                            identifier("doc_"), mail_id, "body" if part["part_id"] == "body" else "attachment", part["part_id"], part["mime_section"], part["part_descriptor"],
                            part["filename"], part["mime"], part["size"], part["object_ref"], part["sha256"], "queued" if part["object_ref"] else "failed", part["error_code"],
                        ))
                else:
                    mail_id = target["mail_id"]
                    current = db.execute("SELECT m.revision,m.deleted_at,d.active_generation,j.stage,j.pending_bytes FROM documents d JOIN mail_items m USING(mail_id) JOIN ingest_jobs j USING(mail_id) WHERE document_id=?", (target["id"],)).fetchone()
                    if not current or current["deleted_at"] is not None:
                        raise MailError("source_removed")
                    if current["revision"] != target["revision"] or current["active_generation"] != target["generation"]:
                        raise MailError("revision_conflict")
                    same_batch = db.execute("SELECT 1 FROM maintenance_effects WHERE action_id=? AND mail_id=?", (payload["action_id"], mail_id)).fetchone()
                    if current["stage"] not in {"finished", "failed"} and not (same_batch and current["stage"] == "accepted"):
                        raise MailError("source_processing")
                    total += current["pending_bytes"]
                    part = parts[0]
                    db.execute("UPDATE documents SET object_ref=?,sha256=?,size=?,extract_status='queued',error_code=NULL WHERE document_id=?", (part["object_ref"], part["sha256"], part["size"], target["id"]))
                    db.execute("UPDATE mail_items SET status='queued' WHERE mail_id=?", (mail_id,))
                db.execute("UPDATE ingest_jobs SET spool_ref=?,stage='accepted',attempt=0,next_run_at=0,lease_until=0,generation=generation+1,pending_bytes=?,input_bytes=?,mail_revision=(SELECT revision FROM mail_items WHERE mail_id=?) WHERE mail_id=?", (
                    str(directory.relative_to(self.store.root)), total, total, mail_id, mail_id,
                ))
                db.execute("DELETE FROM reservations WHERE reservation_id=?", (reservation,))
                db.execute("INSERT INTO maintenance_effects VALUES(?,?,?)", (payload["action_id"], target["id"], mail_id))
            published = True
        finally:
            if not published:
                import shutil
                # Only this freshly generated, boundary-checked staging directory.
                if directory.exists():
                    shutil.rmtree(self.store.safe_path(str(directory.relative_to(self.store.root))))
                self.store.release_reservation(reservation)
