"""Shared service facade. Independent readers never start a Worker or write facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import nullcontext
from pathlib import Path

from filelock import FileLock

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import (
    AcceptReceipt,
    AccessContext,
    ArchiveEnvelope,
    HealthSnapshot,
    MailError,
    MailQuery,
    SourceRead,
)
from nanobot.knowledge.mail.ingestion import MailWorker
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import MailStore


class MailService:
    def __init__(self, root: Path, config: MailKnowledgeConfig, *, readonly=False, worker_enabled=True,
                 initial_trigger_agent=True):
        self.config = config
        self.store = MailStore(root, config, readonly=readonly, initial_trigger_agent=initial_trigger_agent)
        self.access = MailAccess(self.store)
        self.retrieval = MailRetrieval(self.store, self.access)
        self.worker = None if readonly or not worker_enabled else MailWorker(self.store)
        self.capacity_changed = asyncio.Event()
        if self.worker:
            self.worker.capacity_changed = self.capacity_changed
        self.mode_gate = FileLock(str(self.store.root / "mode.lock"), timeout=35)
        self._online = False
        self.unclean_start = False
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if not self._online:
                await self._start()

    async def _start(self) -> None:
        await asyncio.to_thread(self.store.initialize)
        if not self.store.readonly:
            await asyncio.to_thread(self.access.initialize_bindings, self.config.owner_bindings)
        if self.config.enabled and self.config.semantic_enabled and self.config.embedding_model_path:
            try:
                from nanobot.knowledge.mail.embeddings import LocalEncoder
                encoder = await asyncio.to_thread(LocalEncoder, self.config.embedding_model_path)
                self.retrieval.encoder = encoder
                if self.worker:
                    self.worker.encoder = encoder
            except Exception:
                self.retrieval.encoder = None
        if self.worker:
            await self.worker.start()
            with self.store.connect(write=True) as db:
                self.unclean_start = not bool(db.execute("SELECT clean_shutdown FROM schema_meta").fetchone()[0])
                db.execute("UPDATE schema_meta SET clean_shutdown=0")
        self._online = True

    async def close(self) -> None:
        if not self._online:
            return
        if self.worker:
            try:
                await self.worker.close(release_lock=False)
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE schema_meta SET clean_shutdown=1")
            finally:
                self.store.writer_lock.release()
        self._online = False

    def mode(self) -> dict:
        with self.store.connect() as db:
            mode = dict(db.execute("SELECT * FROM schema_meta").fetchone())
        if not self.config.enabled:
            mode["archive_enabled"] = 0
        return mode

    def set_mode(self, *, archive_enabled: bool, trigger_agent: bool, expected_revision: int,
                 access=None, request_id=None):
        payload_hash = hashlib.sha256(json.dumps([archive_enabled, trigger_agent, expected_revision]).encode()).hexdigest()
        guard = self.access.delivery(access, "manage") if access else nullcontext()
        with guard, self.mode_gate, self.store.connect(write=True) as db:
            if access and request_id:
                old = db.execute("SELECT * FROM web_requests WHERE binding_id=? AND session_key=? AND request_id=?", (access.binding_id, access.session_key, request_id)).fetchone()
                if old:
                    if old["action"] != "set_mode" or old["payload_hash"] != payload_hash:
                        raise MailError("request_id_conflict")
                    return json.loads(old["response_json"])
            if access and archive_enabled:
                email = getattr(self, "email_config", None)
                if not self.config.enabled or not self.config.account_id or not email or not email.enabled or not email.consent_granted:
                    raise MailError("source_connection_unavailable")
            row = db.execute("SELECT * FROM schema_meta").fetchone()
            if row["mode_revision"] != expected_revision:
                raise MailError("revision_conflict")
            db.execute("UPDATE schema_meta SET mode_revision=mode_revision+1,archive_enabled=?,trigger_agent=?",
                       (archive_enabled, trigger_agent))
            cancelling = 0
            if not trigger_agent:
                cancelling = db.execute("SELECT COUNT(*) FROM interaction_dispatches WHERE state='running'").fetchone()[0]
                db.execute("UPDATE interaction_dispatches SET state='cancelled',revision=revision+1 WHERE state IN ('pending','dispatching','running')")
                db.execute("UPDATE interaction_deliveries SET status='suppressed',error_code='interaction_disabled' WHERE status='queued'")
            result = {"status": "applied", "mode_revision": expected_revision + 1}
            if cancelling:
                result["cancellation_requested"] = cancelling
            if access and request_id:
                db.execute("INSERT INTO web_requests VALUES(?,?,?,'set_mode',?,?)", (access.binding_id, access.session_key, request_id, payload_hash, json.dumps(result)))
        return result

    def accept_sync(self, envelope: ArchiveEnvelope, reservation: str | None = None):
        try:
            # The receipt is truth about persistence, not extraction/dispatch success.
            return self.store.accept(envelope, reservation)
        except MailError as error:
            if reservation:
                self.store.release_unpublished_reservation(reservation)
            return AcceptReceipt(status="retryable_failure", error_code=error.code)
        except (OSError, sqlite3.Error):
            if reservation:
                try:
                    self.store.release_unpublished_reservation(reservation)
                except (OSError, sqlite3.Error):
                    pass
            return AcceptReceipt(status="retryable_failure", error_code="storage_unavailable")

    async def accept(self, envelope: ArchiveEnvelope):
        return await asyncio.to_thread(self.accept_sync, envelope)

    async def search(self, access: AccessContext, query: MailQuery):
        return await asyncio.to_thread(self.retrieval.search, access, query)

    async def read_source(self, access: AccessContext, request: SourceRead):
        return await asyncio.to_thread(self.retrieval.read_source, access, request)

    async def status(self, access: AccessContext):
        return await asyncio.to_thread(self.status_sync, access)

    def management(self):
        """Explicit commands queue writes through a separate management handle.

        The query handle remains SQLite read-only and this never initializes a
        database, reloads bindings, or starts a Worker.
        """
        from nanobot.knowledge.mail.management import MailManagement
        store = MailStore(self.store.root, self.config) if self.store.readonly else self.store
        return MailManagement(store, MailAccess(store),
            worker_online=lambda: self._online and self.worker is not None)

    async def manage(self, access, action):
        return await asyncio.to_thread(self.management().manage, access, action)

    async def confirm(self, access, token):
        return await asyncio.to_thread(self.management().confirm, access, token)

    async def change_mode(self, **kwargs):
        target = self
        if self.store.readonly:
            target = MailService(self.store.root, self.config, worker_enabled=False)
            target.email_config = getattr(self, "email_config", None)
        return await asyncio.to_thread(target.set_mode, **kwargs)

    def status_sync(self, access: AccessContext):
        self.access.require(access)
        with self.store.connect() as db:
            mode = db.execute("SELECT * FROM schema_meta").fetchone()
            counts = dict(db.execute("SELECT status,COUNT(*) FROM mail_items WHERE owner_id=? GROUP BY status", (access.owner_id,)))
            deliveries = dict(db.execute("SELECT status,COUNT(*) FROM interaction_deliveries GROUP BY status"))
            pending = self.store.pending_bytes(db)
            waiting = dict(db.execute("""SELECT w.error_code,COUNT(*) FROM admission_waits w
                WHERE w.state='waiting'
                AND NOT EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.source_key=w.source_key)
                AND NOT EXISTS(SELECT 1 FROM source_tombstones t WHERE t.source_key=w.source_key)
                GROUP BY w.error_code"""))
            ingest = dict(db.execute("""SELECT j.stage,COUNT(*) FROM ingest_jobs j
                JOIN mail_items m USING(mail_id) WHERE m.owner_id=? AND m.deleted_at IS NULL
                GROUP BY j.stage""", (access.owner_id,)))
            index_pending = db.execute("""SELECT COUNT(*) FROM documents d JOIN mail_items m USING(mail_id)
                WHERE m.owner_id=? AND m.deleted_at IS NULL AND d.index_status='pending'
                AND d.active_generation>0""",
                (access.owner_id,)).fetchone()[0]
            errors = [dict(r) for r in db.execute("SELECT code,scope_id,count FROM health_events WHERE resolved_at IS NULL")]
            index = db.execute("SELECT embedding_revision FROM index_manifests WHERE revision=?", (mode["active_index_revision"],)).fetchone()
            semantic = "semantic_unavailable"
            if self.retrieval.encoder and index and index[0] == self.retrieval.encoder.revision:
                missing = db.execute("""SELECT COUNT(*) FROM chunks c JOIN documents d USING(document_id)
                    JOIN mail_items m USING(mail_id) LEFT JOIN chunk_vectors v
                    ON v.chunk_id=c.chunk_id AND v.embedding_revision=?
                    WHERE m.owner_id=? AND m.deleted_at IS NULL AND c.generation=d.active_generation
                    AND c.chunking_revision=? AND v.chunk_id IS NULL""",
                    (index[0], access.owner_id, mode["active_index_revision"])).fetchone()[0]
                semantic = "semantic_partial" if missing else "ready"
        return HealthSnapshot(mode_revision=mode["mode_revision"], privacy_activated=bool(mode["privacy_activated"]),
                              counts=counts, pending_bytes=pending, max_pending_bytes=self.config.max_pending_bytes,
                              errors=errors, worker_online=self.worker is not None and self._online,
                              semantic_status=semantic, delivery_counts=deliveries,
                              admission_waiting_count=sum(waiting.values()),
                              admission_waiting_reasons=waiting, ingest_counts=ingest,
                              index_pending_documents=index_pending)
