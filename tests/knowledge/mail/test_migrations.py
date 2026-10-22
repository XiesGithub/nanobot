import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from filelock import FileLock

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import MailStore


def legacy_store(tmp_path):
    store = MailStore(tmp_path / 'mail', MailKnowledgeConfig())
    store.root.mkdir()
    with closing(sqlite3.connect(store.path)) as db, db:
        db.executescript(Path(__file__).with_name('schema_v1.sql').read_text(encoding='utf-8'))
        db.execute("INSERT INTO schema_meta(singleton,schema_version,instance_id) VALUES(1,1,'original')")
        for key, reason in [('pending', 'capacity_backpressure'), ('gap', 'source_no_longer_unseen')]:
            db.execute('INSERT INTO admission_waits VALUES(?,?,?,?,?,?)', (key, '{}', 1, 2, reason, 1))
    for folder in ('objects', 'spool', 'maintenance'):
        (store.root / folder).mkdir()
    (store.root / 'objects' / 'fixture.bin').write_bytes(b'fictional retained input')
    return store


def version(store):
    with closing(sqlite3.connect(store.path)) as db:
        return db.execute('SELECT schema_version FROM schema_meta').fetchone()[0]


def test_upgrade_preserves_inputs_and_publishes_matching_backup_first(tmp_path):
    store = legacy_store(tmp_path)
    store.initialize()
    assert version(store) == 2
    with store.connect() as db:
        assert dict(db.execute('SELECT source_key,state FROM admission_waits')) == {
            'pending': 'waiting', 'gap': 'source_unavailable'}
        assert db.execute('SELECT instance_id FROM schema_meta').fetchone()[0] == 'original'
    backup, = (store.root / 'backups').glob('pre_schema_2_*')
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['schema_version'] == 1
    assert (backup / 'objects' / 'fixture.bin').read_bytes() == b'fictional retained input'
    with closing(sqlite3.connect(backup / 'knowledge.sqlite3')) as db:
        assert db.execute('SELECT schema_version FROM schema_meta').fetchone()[0] == 1
        assert 'state' not in [r[1] for r in db.execute('PRAGMA table_info(admission_waits)')]
    store.initialize()
    assert len(list((store.root / 'backups').iterdir())) == 1


def test_readonly_and_live_worker_never_upgrade(tmp_path):
    store = legacy_store(tmp_path)
    with pytest.raises(MailError, match='schema_version_unsupported'):
        MailStore(store.root, store.config, readonly=True).initialize()
    with FileLock(str(store.root / 'worker.lock'), timeout=0):
        with pytest.raises(MailError, match='offline_maintenance_required'):
            store.initialize()
    assert version(store) == 1
    assert not (store.root / 'backups').exists()


@pytest.mark.parametrize('failure', ['backup', 'migration'])
def test_failed_upgrade_leaves_v1_usable_and_retryable(tmp_path, monkeypatch, failure):
    store = legacy_store(tmp_path)
    if failure == 'backup':
        def fail(_):
            raise OSError('fictional backup failure')
        monkeypatch.setattr('nanobot.knowledge.mail.backup.file_digest', fail)
    else:
        def fail(db):
            db.execute("ALTER TABLE admission_waits ADD COLUMN state TEXT DEFAULT 'waiting'")
            raise OSError('fictional migration failure')
        monkeypatch.setattr('nanobot.knowledge.mail.migrations._apply_v2', fail)
    with pytest.raises(OSError):
        store.initialize()
    assert version(store) == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert 'state' not in [r[1] for r in db.execute('PRAGMA table_info(admission_waits)')]
        assert db.execute('SELECT COUNT(*) FROM admission_waits').fetchone()[0] == 2
    monkeypatch.undo()
    store.initialize()
    assert version(store) == 2


def test_initialized_writer_rechecks_schema_before_every_write(tmp_path):
    store = MailStore(tmp_path / 'mail', MailKnowledgeConfig())
    store.initialize()
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute('UPDATE schema_meta SET schema_version=999')
    with pytest.raises(MailError, match='schema_version_unsupported'):
        with store.connect(write=True) as db:
            db.execute('DELETE FROM schema_meta')
    assert version(store) == 999


def test_process_exit_mid_migration_rolls_back_and_retains_backup(tmp_path):
    import subprocess
    import sys
    import textwrap

    store = legacy_store(tmp_path)
    script = """
        import os, sys
        from pathlib import Path
        from nanobot.config.schema import MailKnowledgeConfig
        from nanobot.knowledge.mail.store import MailStore
        import nanobot.knowledge.mail.migrations as migration
        def crash(db):
            db.execute("ALTER TABLE admission_waits ADD COLUMN state TEXT DEFAULT 'waiting'")
            db.execute('UPDATE schema_meta SET schema_version=2')
            os._exit(53)
        migration._apply_v2 = crash
        MailStore(Path(sys.argv[1]), MailKnowledgeConfig()).initialize()
    """
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(script), str(store.root)],
                            capture_output=True, timeout=20)
    assert result.returncode == 53, result.stderr.decode(errors='replace')
    assert version(store) == 1
    assert len(list((store.root / 'backups').glob('pre_schema_2_*'))) == 1
    store.initialize()
    assert version(store) == 2
