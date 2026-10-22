"""Explicit forward migrations, offline under the backup's writer/mode locks."""

import sqlite3
from contextlib import closing

from nanobot.knowledge.mail.backup import create_backup
from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import identifier


def _apply_v2(db):
    db.execute("""ALTER TABLE admission_waits ADD COLUMN state TEXT NOT NULL
        DEFAULT 'waiting' CHECK(state IN ('waiting','source_unavailable'))""")
    db.execute("""UPDATE admission_waits SET state='source_unavailable'
        WHERE error_code='source_no_longer_unseen'""")
    db.execute("UPDATE schema_meta SET schema_version=2 WHERE singleton=1")


def upgrade_v1(store):
    def migrate():
        # Backup has been published durably; all locks remain held. Do not use
        # executescript, which implicitly commits before executing its script.
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT schema_version FROM schema_meta WHERE singleton=1").fetchone()
            if row and row[0] == 2:
                return  # Another initializer completed before we acquired locks.
            if not row or row[0] != 1:
                raise MailError("schema_version_unsupported")
            _apply_v2(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise MailError("migration_integrity_failed")

    create_backup(store, store.root / "backups" / identifier("pre_schema_2_"),
                  _after_publish=migrate)
