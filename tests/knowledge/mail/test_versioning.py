import pytest

from nanobot.knowledge.mail.contracts import MailError, MailQuery, SourceRead
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.versioning import collect, rebuild
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope


def test_encoder_upgrade_never_mixes_spaces_and_old_page_needs_no_reencoding(tmp_path):
    import numpy as np

    from nanobot.knowledge.mail.embeddings import LocalEncoder

    def encoder(revision, dimension):
        value = object.__new__(LocalEncoder)
        value.revision, value.dimension, value.np = revision, dimension, np
        value.encode = lambda texts, **kwargs: np.tile(
            np.array([1] + [0] * (dimension - 1), dtype='<f4'), (len(texts), 1))
        return value

    store, auth, _, access = make_access(tmp_path)
    for _ in range(3):
        ingest(store, envelope())
    old_encoder, new_encoder = encoder('fictional-old', 2), encoder('fictional-new', 3)
    old_index = rebuild(store, old_encoder)
    retrieval = MailRetrieval(store, auth, old_encoder)
    old = retrieval.search(access, MailQuery(query='onboarding', limit=1))
    assert old.semantic_status == 'ready' and old.next_cursor
    new_index = rebuild(store, new_encoder)
    collect(store)
    with store.connect() as db:
        for index, revision, dimension in [(old_index, old_encoder.revision, 2),
                                           (new_index, new_encoder.revision, 3)]:
            assert {tuple(r) for r in db.execute('''SELECT v.embedding_revision,v.dimension
                FROM chunk_vectors v JOIN chunks c USING(chunk_id) WHERE c.chunking_revision=?''',
                (index,))} == {(revision, dimension)}
    # An already-running reader with the old encoder explicitly degrades for
    # fresh queries; it cannot compare a new-space vector with the old model.
    assert retrieval.search(access, MailQuery(query='onboarding')).semantic_status == 'semantic_unavailable'
    retrieval.encoder = new_encoder
    assert retrieval.search(access, MailQuery(query='onboarding')).semantic_status == 'ready'
    new_encoder.encode = lambda *args, **kwargs: pytest.fail('old fixed ranking must not be encoded again')
    next_page = retrieval.search(access, MailQuery(cursor=old.next_cursor, limit=1))
    assert next_page.query_id == old.query_id
    assert retrieval.read_source(access, SourceRead(evidence_id=old.matches[0].evidence_id)).sources[0] == old.matches[0]


def test_index_switch_preserves_old_evidence_until_fixed_expiry(tmp_path, monkeypatch):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    retrieval = MailRetrieval(store, auth)
    page = retrieval.search(access, MailQuery())
    evidence = page.matches[0]
    revision = rebuild(store)
    collect(store)
    assert retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id)).sources[0] == evidence
    fresh = retrieval.search(access, MailQuery())
    assert fresh.matches[0].excerpt == evidence.excerpt
    with store.connect() as db:
        assert db.execute("SELECT COUNT(DISTINCT chunking_revision) FROM chunks").fetchone()[0] == 2
    with store.connect(query_state=True) as db:
        expiry = db.execute("SELECT MAX(expires_at) FROM query_runs").fetchone()[0]
    monkeypatch.setattr("nanobot.knowledge.mail.versioning.time.time", lambda: expiry + 1)
    assert collect(store)["chunks"] > 0
    with store.connect() as db:
        assert [r[0] for r in db.execute("SELECT DISTINCT chunking_revision FROM chunks")] == [revision]
    with pytest.raises(MailError, match="source_not_found|source_version_expired"):
        retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))


def test_new_mail_after_switch_uses_current_index(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    revision = rebuild(store)
    new = envelope()
    new.body = "New mail published after index switch"
    receipt = ingest(store, new)
    page = MailRetrieval(store, auth).search(access, MailQuery(mail_ids=[receipt.mail_id]))
    assert page.matches[0].excerpt == new.body
    with store.connect() as db:
        assert db.execute("SELECT DISTINCT chunking_revision FROM chunks c JOIN documents d USING(document_id) WHERE mail_id=?", (receipt.mail_id,)).fetchone()[0] == revision


def test_concurrent_content_change_aborts_shadow_publication(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    ingest(store, envelope())
    with pytest.raises(MailError, match="revision_conflict"):
        rebuild(store, before_switch=lambda: ingest(store, envelope()))
    with store.connect() as db:
        assert db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] == "text-v1"
        assert db.execute("SELECT COUNT(*) FROM index_manifests WHERE status='failed'").fetchone()[0] == 1
    assert MailRetrieval(store, auth).search(access, MailQuery()).matching_mail_count == 2


def test_query_state_failure_stops_collection(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    ingest(store, envelope())
    rebuild(store)
    store.query_path.rename(store.root / "temporarily-unavailable.sqlite3")
    with pytest.raises(MailError, match="query_state_unavailable"):
        collect(store)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(DISTINCT chunking_revision) FROM chunks").fetchone()[0] == 2


def test_private_object_path_rejects_parent_and_absolute_escape(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    for value in ("objects/../..", str(tmp_path.parent / "outside.txt"), "../outside"):
        with pytest.raises(MailError, match="invalid_object_path"):
            store.safe_path(value)


def test_reader_process_registers_snapshot_before_concurrent_rebuild_and_gc(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap
    import time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    store, _, _, _ = make_access(tmp_path)
    ingest(store, envelope())
    barrier = tmp_path / "snapshot-read"
    registered = tmp_path / "snapshot-registered"
    script = """
        import sys
        from contextlib import contextmanager
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from nanobot.knowledge.mail.access import MailAccess
        from nanobot.knowledge.mail.contracts import AccessContext, MailQuery, SourceRead
        from nanobot.knowledge.mail.retrieval import MailRetrieval
        store = MailStore(Path(sys.argv[1]), MailKnowledgeConfig(), readonly=True)
        auth = MailAccess(store)
        access = AccessContext.from_security(auth.authenticate('cli', 'alice', private_chat=True), 'cli:child')
        original = store.connect
        @contextmanager
        def connect(**kwargs):
            if kwargs.get('query_state') and kwargs.get('write'):
                Path(sys.argv[2]).write_text('facts read; retention not registered')
                assert sys.stdin.readline().strip() == 'register'
            with original(**kwargs) as db:
                yield db
        store.connect = connect
        retrieval = MailRetrieval(store, auth)
        page = retrieval.search(access, MailQuery())
        Path(sys.argv[3]).write_text('registered')
        assert sys.stdin.readline().strip() == 'verify'
        assert retrieval.read_source(access, SourceRead(evidence_id=page.matches[0].evidence_id)).sources[0] == page.matches[0]
    """
    process = subprocess.Popen([sys.executable, "-c", textwrap.dedent(script),
        str(store.root), str(barrier), str(registered)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"})

    def await_file(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            assert process.poll() is None, "reader exited before barrier"
            assert time.monotonic() < deadline, "reader barrier timeout"
            time.sleep(.01)

    def rebuild_and_collect():
        rebuild(store)
        return collect(store)

    try:
        await_file(barrier)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(rebuild_and_collect)
            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=.15)
            finally:
                process.stdin.write("register\n")
                process.stdin.flush()
            assert future.result(timeout=10)["chunks"] == 0
        await_file(registered)
        stdout, stderr = process.communicate("verify\n", timeout=10)
        assert process.returncode == 0, stderr + stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
