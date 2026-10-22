import asyncio
from threading import Event

import pytest
from filelock import FileLock, Timeout

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.ingestion import MailWorker
from nanobot.knowledge.mail.store import MailStore
from tests.knowledge.mail.test_store import envelope


@pytest.mark.asyncio
async def test_close_cancellation_keeps_writer_lock_until_thread_finishes(tmp_path, monkeypatch):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig(semantic_enabled=False))
    store.initialize()
    worker = MailWorker(store)
    started, release, finished = Event(), Event(), Event()

    def in_flight():
        started.set()
        assert release.wait(5)
        finished.set()
        return False

    monkeypatch.setattr(worker.maintenance, "process_one", in_flight)
    await worker.start()
    try:
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(.02)
        closing.cancel()
        await asyncio.sleep(.02)
        assert not closing.done()
        contender = FileLock(str(store.root / "worker.lock"), timeout=0)
        with pytest.raises(Timeout):
            contender.acquire()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)
        assert finished.is_set()
        with contender:
            pass
    finally:
        release.set()
        await worker.close()


@pytest.mark.asyncio
async def test_configured_parser_concurrency_is_bounded_and_jobs_distinct(tmp_path, monkeypatch):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig(semantic_enabled=False, worker_concurrency=2))
    store.initialize()
    for _ in range(5):
        store.accept(envelope())
    worker = MailWorker(store)
    active = set()
    seen = set()
    peak = 0
    completed = asyncio.Event()

    async def process(job):
        nonlocal peak
        assert job["job_id"] not in seen
        seen.add(job["job_id"])
        active.add(job["job_id"])
        peak = max(peak, len(active))
        await asyncio.sleep(.02)
        active.remove(job["job_id"])
        if len(seen) == 5:
            completed.set()

    monkeypatch.setattr(worker, "process", process)
    await worker.start()
    try:
        await asyncio.wait_for(completed.wait(), 3)
    finally:
        await worker.close()
    assert peak == 2 and len(seen) == 5 and not active
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ingest_jobs WHERE stage='extracting'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_transient_encoder_failure_retries_after_backoff(tmp_path):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig(semantic_enabled=False))
    store.initialize()
    worker = MailWorker(store)
    success = Event()

    class Encoder:
        calls = 0

        def index_pending(self, store):
            self.calls += 1
            if self.calls == 1:
                raise OSError("fictional transient write failure")
            success.set()
            return 0

    worker.encoder = Encoder()
    await worker.start()
    try:
        for _ in range(100):
            if worker.semantic_error:
                break
            await asyncio.sleep(.01)
        assert worker.semantic_error == "semantic_unavailable"
        await asyncio.sleep(.03)
        assert worker.encoder.calls == 1
        worker._next_semantic_try = 0  # Advance retry schedule without a slow test.
        worker.wakeup.set()
        assert await asyncio.to_thread(success.wait, 2)
    finally:
        await worker.close()
    assert worker.semantic_error is None


async def test_worker_collects_expired_snapshot_versions_and_reports_state_failure(tmp_path, monkeypatch):
    from nanobot.knowledge.mail.contracts import MailQuery
    from nanobot.knowledge.mail.retrieval import MailRetrieval
    from nanobot.knowledge.mail.versioning import rebuild
    from tests.knowledge.mail.test_access import make_access
    from tests.knowledge.mail.test_retrieval import ingest

    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    MailRetrieval(store, auth).search(access, MailQuery())
    rebuild(store)
    worker = MailWorker(store)
    completed = Event()
    original = worker._collect_versions

    def observe():
        original()
        completed.set()

    monkeypatch.setattr(worker, "_collect_versions", observe)
    await worker.start()
    try:
        assert await asyncio.to_thread(completed.wait, 3)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(DISTINCT chunking_revision) FROM chunks").fetchone()[0] == 2
        state_backup = store.root / "unavailable-query-state.sqlite3"
        store.query_path.rename(state_backup)
        completed.clear()
        worker._next_collection = 0
        worker.wakeup.set()
        assert await asyncio.to_thread(completed.wait, 3)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(DISTINCT chunking_revision) FROM chunks").fetchone()[0] == 2
            event = db.execute("SELECT count,resolved_at FROM health_events WHERE code='version_collection_unavailable'").fetchone()
            assert tuple(event) == (1, None)
        state_backup.rename(store.query_path)
        with store.connect(write=True, query_state=True) as state:
            state.execute("UPDATE query_runs SET expires_at=0")
        completed.clear()
        worker._next_collection = 0
        worker.wakeup.set()
        assert await asyncio.to_thread(completed.wait, 3)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(DISTINCT chunking_revision) FROM chunks").fetchone()[0] == 1
            assert db.execute("SELECT resolved_at FROM health_events WHERE code='version_collection_unavailable'").fetchone()[0]
    finally:
        await worker.close()
