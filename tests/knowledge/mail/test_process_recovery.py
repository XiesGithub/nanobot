"""Real abrupt child-process exits, rather than exceptions imitating crashes."""

import os
import subprocess
import sys
import textwrap

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.store import MailStore
from tests.knowledge.mail.test_access import make_access


def test_real_exit_after_private_preflight_restores_restricted_checkpoint(tmp_path):
    store, _, _, _ = make_access(tmp_path)
    checkpoint = tmp_path / 'checkpoint.json'
    script = """
        import asyncio, json, os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore, atomic_write
        from nanobot.knowledge.mail.access import MailAccess
        from nanobot.knowledge.mail.contracts import AccessContext
        from nanobot.knowledge.mail.tool_policy import MailToolView
        from nanobot.agent.runner import AgentRunner, AgentRunSpec
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.providers.base import ToolCallRequest
        from tests.knowledge.mail.test_tool_policy import CounterTool
        store = MailStore(Path(sys.argv[1]), MailKnowledgeConfig(), readonly=True)
        auth = MailAccess(store)
        access = AccessContext.from_security(auth.authenticate('cli', 'alice', private_chat=True), 'cli:test')
        counters, registry = {}, ToolRegistry()
        registry.register(CounterTool('mail_search', counters, 'mail_read'))
        registry.register(CounterTool('message', counters))
        path = Path(sys.argv[2])
        async def run():
            if not path.exists():
                view = MailToolView(registry, auth, access)
                async def save(payload):
                    assert not counters
                    atomic_write(path, json.dumps(payload).encode())
                    os._exit(54)
                spec = AgentRunSpec(initial_messages=[], tools=view, model='deepseek-v4-flash',
                    max_iterations=2, max_tool_result_chars=10000, checkpoint_callback=save)
                await AgentRunner(None)._execute_tools(spec, [ToolCallRequest('1','message',{}),
                    ToolCallRequest('2','mail_search',{})], {}, {})
            else:
                state = json.loads(path.read_text())
                assert state['phase'] == 'restricted_preflight'
                view = MailToolView(registry, auth, access, restored=state['tool_policy'])
                assert 'tool_blocked' in await view.execute('message', {})
                assert counters == {}
        asyncio.run(run())
    """
    stopped = child(script, store.root, checkpoint)
    assert stopped.returncode == 54, stopped.stderr
    resumed = child(script, store.root, checkpoint)
    assert resumed.returncode == 0, resumed.stderr


def test_real_exit_halfway_through_unpublished_input_is_never_admitted(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        import nanobot.knowledge.mail.store as module
        from tests.knowledge.mail.test_store import envelope
        store = module.MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        store.initialize()
        def partial(path, data):
            with path.with_suffix('.tmp').open('wb') as stream:
                stream.write(data[:5])
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(47)
        module.atomic_write = partial
        store.accept(envelope())
    """, root)
    assert result.returncode == 47, result.stderr
    store = MailStore(root, MailKnowledgeConfig())
    assert list((root / "spool").rglob("*.tmp"))
    with store.connect(write=True) as db:
        db.execute("UPDATE reservations SET lease_until=0")
    store.recover()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    assert list((root / "spool").iterdir()) == []


def test_real_exit_after_admission_before_ack_deduplicates_refetch(tmp_path):
    from tests.knowledge.mail.test_store import envelope
    root = tmp_path / "mail"
    mail = envelope()
    fixture = tmp_path / "fictional-envelope.json"
    fixture.write_text(mail.model_dump_json(), encoding="utf-8")
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.contracts import ArchiveEnvelope
        from nanobot.knowledge.mail.store import MailStore
        store = MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        store.initialize()
        mail = ArchiveEnvelope.model_validate_json(Path(sys.argv[2]).read_text(encoding='utf-8'))
        assert store.accept(mail).durable
        os._exit(48)
    """, root, fixture)
    assert result.returncode == 48, result.stderr
    store = MailStore(root, MailKnowledgeConfig())
    store.recover()
    assert store.accept(mail).status == "duplicate"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 1


def test_real_exit_after_text_before_vectors_preserves_keyword_readiness(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from tests.knowledge.mail.test_access import make_access
        from tests.knowledge.mail.test_retrieval import ingest
        from tests.knowledge.mail.test_store import envelope
        store, _, _, _ = make_access(Path(sys.argv[1]))
        ingest(store, envelope())
        os._exit(49)
    """, tmp_path)
    assert result.returncode == 49, result.stderr
    from nanobot.knowledge.mail.access import MailAccess
    from nanobot.knowledge.mail.contracts import AccessContext, MailQuery
    from nanobot.knowledge.mail.retrieval import MailRetrieval
    store = MailStore(root, MailKnowledgeConfig())
    store.recover()
    auth = MailAccess(store)
    access = AccessContext.from_security(auth.authenticate("cli", "alice", private_chat=True), "cli:recovered")
    page = MailRetrieval(store, auth).search(access, MailQuery(query="September"))
    assert page.matches and page.semantic_status == "semantic_unavailable"


def test_real_exit_during_shadow_index_leaves_old_active_and_collects_abandoned_build(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from tests.knowledge.mail.test_access import make_access
        from tests.knowledge.mail.test_retrieval import ingest
        from tests.knowledge.mail.test_store import envelope
        from nanobot.knowledge.mail.versioning import rebuild
        store, _, _, _ = make_access(Path(sys.argv[1]))
        ingest(store, envelope())
        rebuild(store, before_switch=lambda: os._exit(50))
    """, tmp_path)
    assert result.returncode == 50, result.stderr
    from nanobot.knowledge.mail.versioning import collect
    store = MailStore(root, MailKnowledgeConfig())
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM index_manifests WHERE status='building'").fetchone()[0] == 1
        assert db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] == "text-v1"
    store.recover()
    assert collect(store)["chunks"] > 0
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM index_manifests WHERE status='building'").fetchone()[0] == 0
        assert [row[0] for row in db.execute("SELECT DISTINCT chunking_revision FROM chunks")] == ["text-v1"]


def child(code, *args):
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code), *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8",
                            env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
    return result


def test_real_exit_after_manifest_before_database(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from tests.knowledge.mail.test_store import envelope
        store=MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        store.initialize()
        store._register_manifest=lambda path: os._exit(41)
        store.accept(envelope(interaction_input={'content':'fictional'}))
    """, root)
    assert result.returncode == 41, result.stderr
    store = MailStore(root, MailKnowledgeConfig())
    store.initialize()
    store.recover()
    store.recover()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "pending"


def test_real_exit_in_uncommitted_generation_leaves_no_half_version(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from tests.knowledge.mail.test_store import envelope
        from tests.knowledge.mail.test_retrieval import ingest
        store=MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        store.initialize()
        ingest(store,envelope())
        with store.connect(write=True) as db:
            doc=db.execute('SELECT document_id FROM documents').fetchone()[0]
            db.execute('INSERT INTO document_blocks VALUES(?,?,?,?,?,?,?,?)',
                       ('half',doc,2,0,'not committed','{}','current','test'))
            db.execute('UPDATE documents SET active_generation=2')
            os._exit(42)
    """, root)
    assert result.returncode == 42, result.stderr
    store = MailStore(root, MailKnowledgeConfig())
    with store.connect() as db:
        assert db.execute("SELECT active_generation FROM documents").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM document_blocks WHERE generation=2").fetchone()[0] == 0


def test_second_process_cannot_own_worker(tmp_path):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig())
    store.initialize()
    with store.writer_lock:
        result = child("""
            import sys
            from pathlib import Path
            from filelock import Timeout
            from nanobot.config.schema import MailKnowledgeConfig
            from nanobot.knowledge.mail.store import MailStore
            store=MailStore(Path(sys.argv[1]),MailKnowledgeConfig())
            try:
                store.writer_lock.acquire()
            except Timeout:
                sys.exit(43)
            sys.exit(1)
        """, store.root)
    assert result.returncode == 43, result.stderr


def test_cross_process_stale_access_is_revoked(tmp_path):
    store, auth, _, access = make_access(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(access.model_dump_json(), encoding="utf-8")
    auth.revoke(access.binding_id)
    result = child("""
        import sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from nanobot.knowledge.mail.access import MailAccess
        from nanobot.knowledge.mail.contracts import AccessContext,MailError
        store=MailStore(Path(sys.argv[1]),MailKnowledgeConfig(),readonly=True)
        snapshot=AccessContext.model_validate_json(Path(sys.argv[2]).read_text())
        try:
            MailAccess(store).require(snapshot)
        except MailError as error:
            sys.exit(44 if error.code=='authorization_revoked' else 1)
        sys.exit(2)
    """, store.root, snapshot)
    assert result.returncode == 44, result.stderr


def test_real_exit_after_smtp_start_preserves_agent_completion_but_not_delivery_proof(tmp_path):
    root = tmp_path / "mail"
    result = child("""
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        from nanobot.knowledge.mail.dispatch import InteractionDispatch
        store = MailStore(Path(sys.argv[1]), MailKnowledgeConfig())
        store.initialize()
        dispatch = InteractionDispatch(store)
        task = dispatch.intent('fictional-source', {'content': 'fictional'}, 1)['task_id']
        dispatch.transition(task, 'pending', 'dispatching')
        dispatch.transition(task, 'dispatching', 'running')
        dispatch.transition(task, 'running', 'completed')
        with store.connect(write=True) as db:
            db.execute("UPDATE interaction_deliveries SET status='sending'")
        os._exit(45)
    """, root)
    assert result.returncode == 45, result.stderr
    store = MailStore(root, MailKnowledgeConfig())
    store.recover()
    with store.connect() as db:
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "completed"
        assert db.execute("SELECT status FROM interaction_deliveries").fetchone()[0] == "uncertain"
