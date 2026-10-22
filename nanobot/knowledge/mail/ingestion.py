"""Durable Worker lifecycle and document processing, with no IMAP dependencies."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from threading import Event

from filelock import Timeout

from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.extractors import extract_isolated
from nanobot.knowledge.mail.maintenance import MailMaintenance
from nanobot.knowledge.mail.store import MailStore
from nanobot.utils.observability import (
    current_observation,
    current_span_id,
    observation_context,
    span,
)


class MailWorker:
    def __init__(self, store: MailStore):
        self.store = store
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.wakeup = asyncio.Event()
        self.capacity_changed = asyncio.Event()
        self._cancel_parser = Event()
        self.encoder = None
        self.semantic_error: str | None = None
        self._semantic_attempt = 0
        self._next_semantic_try = 0.0
        self._next_collection = 0.0
        self.maintenance = MailMaintenance(store)

    def _background_task(self, task_key: str, purpose: str, *, causal_parent: bool):
        """Bind read-only worker spans to a causal task or explicit service overhead."""
        state = current_observation()
        if state is None:
            return span("background_task", detached=True, purpose=purpose,
                        accounting="unobserved" if causal_parent else "unassigned_periodic_overhead")
        if causal_parent:
            task_id = f"{state.task_id}:mail:{task_key}"
            parent_task_id = state.task_id
            parent_span_id = current_span_id()
        else:
            task_id = f"mail-worker:{id(self)}"
            parent_task_id = None
            parent_span_id = None

        class BoundSpan:
            def __enter__(self):
                self.context = observation_context(
                    state.sink, task_id, parent_task_id=parent_task_id,
                    parent_span_id=parent_span_id,
                )
                self.context.__enter__()
                self.observation = span(
                    "background_task", detached=True, purpose=purpose,
                    accounting="causal_child" if causal_parent else "unassigned_periodic_overhead",
                )
                return self.observation.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                try:
                    return self.observation.__exit__(exc_type, exc, traceback)
                finally:
                    self.context.__exit__(exc_type, exc, traceback)

        return BoundSpan()

    async def start(self) -> None:
        if self.store.readonly:
            raise MailError("knowledge_readonly")
        if self._task:
            return
        try:
            self.store.writer_lock.acquire()
        except Timeout:
            raise MailError("worker_already_running") from None
        try:
            await asyncio.to_thread(self.store.recover)
            await asyncio.to_thread(self.maintenance.recover)
            self._stopping = False
            self._cancel_parser.clear()
            self._task = asyncio.create_task(self._run())
        except BaseException:
            self.store.writer_lock.release()
            raise

    async def close(self, *, release_lock=True) -> None:
        self._stopping = True
        self._cancel_parser.set()
        self.wakeup.set()
        if self._task:
            # Parsing owns a terminable subprocess. Do not release exclusive ownership
            # while a to_thread worker might still commit under the old lease.
            try:
                cancelled = False
                while not self._task.done():
                    try:
                        await asyncio.shield(self._task)
                    except asyncio.CancelledError:
                        # Cancelling an asyncio wrapper cannot terminate its OS
                        # thread. Keep ownership until all publication ends.
                        cancelled = True
                self._task.result()
                await asyncio.to_thread(self._invalidate_running)
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                self._task = None
                if release_lock:
                    self.store.writer_lock.release()

    def _invalidate_running(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE ingest_jobs SET stage='accepted',generation=generation+1 WHERE stage='extracting'")

    async def _run(self):
        while not self._stopping:
            self.maintenance.encoder = self.encoder
            try:
                with self._background_task("maintenance", "mail_management_queue", causal_parent=False):
                    await asyncio.to_thread(self.maintenance.process_one)
                with self._background_task("cleanup", "mail_local_cleanup", causal_parent=False):
                    cleaned = await asyncio.to_thread(self.maintenance.cleanup_one)
                if cleaned:
                    self.capacity_changed.set()
            except Exception:
                # A cleanup failure must not starve unrelated accepted inputs.
                pass
            if not self._stopping and time.monotonic() >= self._next_collection:
                # Registration and collection share the cross-process maintenance
                # lock. Run infrequently even under continuous ingestion; readers
                # and independent query services never take over maintenance.
                self._next_collection = time.monotonic() + 300
                with self._background_task("collection", "mail_version_collection", causal_parent=False):
                    await asyncio.to_thread(self._collect_versions)
            jobs = []
            for _ in range(self.store.config.worker_concurrency):
                if self._stopping:
                    break
                job = await asyncio.to_thread(self.store.claim)
                if not job:
                    break
                jobs.append(job)
            if jobs:
                await asyncio.gather(*(self._process_safe(job) for job in jobs))
                self.capacity_changed.set()
            if self._stopping:
                break
            if self.encoder is not None and time.monotonic() >= self._next_semantic_try:
                try:
                    count = await asyncio.to_thread(self.encoder.index_pending, self.store)
                    self.semantic_error = None
                    self._semantic_attempt = 0
                    if count:
                        continue
                except Exception:
                    self.semantic_error = "semantic_unavailable"
                    self._semantic_attempt = min(self._semantic_attempt + 1, 6)
                    self._next_semantic_try = time.monotonic() + min(60, 2 ** self._semantic_attempt)
            if jobs:
                continue
            self.wakeup.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)

    def _collect_versions(self):
        from nanobot.knowledge.mail.versioning import collect
        try:
            collect(self.store)
        except Exception:
            # A failed state-store read must retain every old version. Expose a
            # bounded health event instead of retrying every loop or hiding it.
            with suppress(Exception), self.store.connect(write=True) as db:
                db.execute("""INSERT INTO health_events(code,scope_id,first_at,last_at)
                    VALUES('version_collection_unavailable','worker',?,?)
                    ON CONFLICT(code,scope_id) DO UPDATE SET last_at=excluded.last_at,
                    count=count+1,resolved_at=NULL""", (time.time(), time.time()))
        else:
            with suppress(Exception), self.store.connect(write=True) as db:
                db.execute("UPDATE health_events SET resolved_at=? WHERE code='version_collection_unavailable' AND scope_id='worker'",
                           (time.time(),))

    async def _process_safe(self, job):
        with self._background_task(job["job_id"], "mail_ingest", causal_parent=True) as task_span:
            try:
                await self.process(job)
            except MailError as error:
                task_span.update(status="failed", failure_code=error.code)
                task_span.close("error", error_kind="MailError")
                if error.code != "revision_conflict" and not self._stopping:
                    with suppress(MailError):
                        await asyncio.to_thread(self.store.fail, job, error.code)
            except Exception as error:
                task_span.update(status="failed", failure_code="processing_failed")
                task_span.close("error", error_kind=type(error).__name__)
                if not self._stopping:
                    with suppress(MailError):
                        await asyncio.to_thread(self.store.fail, job, "processing_failed")
            else:
                task_span.update(status="finished", job_id=job["job_id"])

    async def process(self, job: dict) -> None:
        with self.store.connect() as db:
            documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE mail_id=?", (job["mail_id"],))]
        for document in documents:
            if self._stopping:
                return
            if not document["object_ref"] or document["extract_status"] == "ready":
                continue
            await asyncio.to_thread(self.store.renew, job)
            path = self.store.safe_path(document["object_ref"])
            if not path.is_file():
                raise MailError("local_input_missing")
            result = await asyncio.to_thread(
                extract_isolated, path, document["mime"], body=document["kind"] == "body",
                max_chars=self.store.config.max_extracted_chars,
                cancel_event=self._cancel_parser,
            )
            if self._stopping:
                return
            await asyncio.to_thread(self.store.publish_document, job, document["document_id"], result)
        await asyncio.to_thread(self.store.preserve_objects, job)
        await asyncio.to_thread(self.store.finish, job)
