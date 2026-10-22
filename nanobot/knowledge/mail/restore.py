"""Offline backup restore with a durable fail-closed recovery marker."""

import json
import os
import re
import shutil
import sqlite3
from contextlib import closing, suppress

from filelock import FileLock, Timeout

from nanobot.knowledge.mail.backup import ASSET_DIRECTORIES, file_digest, verify_backup
from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import MailStore, atomic_write, identifier, json_text


def _insert(db, table, row):
    columns = ",".join(row.keys())
    placeholders = ",".join("?" for _ in row)
    db.execute(f"INSERT OR REPLACE INTO {table}({columns}) VALUES({placeholders})", tuple(row.values()))


def _prepare(store, backup, stage, current):
    manifest = verify_backup(store, backup)
    stage.mkdir()
    for name in manifest["files"]:
        source, target = backup / name, stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    restored = MailStore(stage, store.config)
    restored.initialize()
    mode = dict(current.execute("SELECT * FROM schema_meta").fetchone())
    bindings = [dict(r) for r in current.execute("SELECT * FROM bindings")]
    if not bindings:
        raise MailError("owner_binding_required")
    with restored.connect(write=True) as db:
        owners = {r[0] for r in db.execute("SELECT DISTINCT owner_id FROM mail_items")}
        if owners - {row["owner_id"] for row in bindings}:
            raise MailError("backup_owner_mismatch")
        # Retain current authorization, never resurrect an older backup binding.
        old_revisions = dict(db.execute("SELECT binding_id,policy_revision FROM bindings"))
        db.execute("DELETE FROM bindings")
        for row in bindings:
            row["policy_revision"] = max(row["policy_revision"], old_revisions.get(row["binding_id"], 0)) + 1
            _insert(db, "bindings", row)
        db.execute("UPDATE schema_meta SET instance_id=?,privacy_activated=1,mode_revision=MAX(mode_revision,?)+1,archive_enabled=?,trigger_agent=?,clean_shutdown=1",
            (identifier("instance_"), mode["mode_revision"], mode["archive_enabled"], mode["trigger_agent"]))
        db.execute("DELETE FROM confirmations")
        db.execute("DELETE FROM web_requests")
        db.execute("DELETE FROM chat_bindings")
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM admission_waits")
        db.execute("UPDATE interaction_dispatches SET state='uncertain',revision=revision+1 WHERE state IN ('pending','dispatching','running')")
        db.execute("UPDATE interaction_deliveries SET status='uncertain',error_code='backup_restored' WHERE status IN ('queued','sending')")
        for row in db.execute("SELECT action_id,receipt_json FROM management_actions WHERE status NOT IN ('completed','failed')").fetchall():
            receipt = json.loads(row["receipt_json"])
            receipt.update(status="failed", error_code="backup_restored", confirmation_token=None)
            db.execute("UPDATE management_actions SET status='failed',receipt_json=? WHERE action_id=?", (json_text(receipt), row["action_id"]))
    # Deletions made after the backup override its earlier copy of the same mail.
    for tombstone in current.execute("SELECT * FROM source_tombstones"):
        tombstone = dict(tombstone)
        with restored.connect() as db:
            existing = db.execute("SELECT mail_id,revision,deleted_at FROM mail_items WHERE source_key=?", (tombstone["source_key"],)).fetchone()
        if existing and existing["mail_id"] != tombstone["old_mail_id"]:
            raise MailError("backup_source_identity_mismatch")
        if existing and existing["deleted_at"] is None:
            restored.remove(existing["mail_id"], existing["revision"])
        with restored.connect(write=True) as db:
            account = dict(current.execute("SELECT * FROM accounts WHERE account_id=?", (tombstone["account_id"],)).fetchone())
            if not db.execute("SELECT 1 FROM accounts WHERE account_id=?", (account["account_id"],)).fetchone():
                _insert(db, "accounts", account)
            if not existing:
                mail = dict(current.execute("SELECT * FROM mail_items WHERE mail_id=?", (tombstone["old_mail_id"],)).fetchone())
                box = dict(current.execute("SELECT * FROM mailboxes WHERE mailbox_id=?", (mail["mailbox_id"],)).fetchone())
                if not db.execute("SELECT 1 FROM mailboxes WHERE mailbox_id=?", (box["mailbox_id"],)).fetchone():
                    _insert(db, "mailboxes", box)
                _insert(db, "mail_items", mail)
            tombstone["cleanup_pending"] = 1
            _insert(db, "source_tombstones", tombstone)
        restored.cleanup_removed(tombstone["old_mail_id"])
    # Both snapshots and retained-version registrations start empty after restore.
    with restored.connect(write=True, query_state=True) as db:
        db.execute("DELETE FROM query_runs")
        db.execute("DELETE FROM mail_focus")
    files = {}
    for path in stage.rglob("*"):
        if path.is_file() and (path.name in {"knowledge.sqlite3", "query_state.sqlite3"}
                              or path.relative_to(stage).parts[0] in ASSET_DIRECTORIES):
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
            files[path.relative_to(stage).as_posix()] = file_digest(path)
    if os.name != "nt":
        for folder in [p for p in stage.rglob("*") if p.is_dir()] + [stage]:
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    atomic_write(stage / "restore_manifest.json", json_text(files).encode())


def _restore_database(source, target):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
        with closing(sqlite3.connect(target)) as outgoing:
            incoming.backup(outgoing)


def _install(store, marker):
    name = marker.get("stage", "")
    if not re.fullmatch(r"restore_[a-f0-9]{32}", name):
        raise MailError("restore_integrity_failed")
    stage = store.safe_path(name)
    files = json.loads((stage / "restore_manifest.json").read_text(encoding="utf-8"))
    if not {"knowledge.sqlite3", "query_state.sqlite3"} <= files.keys():
        raise MailError("restore_integrity_failed")
    for name, digest in files.items():
        path = (stage / name).resolve()
        if not path.is_relative_to(stage) or file_digest(path) != digest:
            raise MailError("restore_integrity_failed")
    for name in ASSET_DIRECTORIES:
        target = store.safe_path(name)
        # Every recursive replacement resolves to a fixed child of the private root.
        if target.parent != store.root:
            raise MailError("invalid_object_path")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(stage / name, target)
        for path in target.rglob("*"):
            if path.is_file():
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
        if os.name != "nt":
            for folder in [p for p in target.rglob("*") if p.is_dir()] + [target]:
                descriptor = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    _restore_database(stage / "knowledge.sqlite3", store.path)
    _restore_database(stage / "query_state.sqlite3", store.query_path)
    (store.root / "restore_pending.json").unlink()
    if os.name != "nt":
        descriptor = os.open(store.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    with suppress(OSError):
        shutil.rmtree(store.safe_path(str(stage.relative_to(store.root))))
    return stage.exists()


def restore_backup(store, backup_id, *, principal_id, resume=False):
    """Caller is an explicitly authenticated local administrator; resume uses its OS ID."""
    if store.readonly:
        raise MailError("knowledge_readonly")
    worker = FileLock(str(store.root / "worker.lock"), timeout=0)
    try:
        worker.acquire()
    except Timeout:
        raise MailError("offline_maintenance_required") from None
    marker_path = store.safe_path("restore_pending.json")
    stage = None
    try:
        with FileLock(str(store.root / "mode.lock"), timeout=35), store.lock:
            if resume:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker.get("principal_id") != principal_id:
                    raise MailError("unauthorized")
            else:
                if marker_path.exists():
                    raise MailError("restore_recovery_required")
                if not re.fullmatch(r"backup_[a-f0-9]{32}", backup_id):
                    raise MailError("invalid_backup_path")
                stage = store.safe_path(identifier("restore_"))
                # BEGIN IMMEDIATE fences concurrent revocation/grant and mode writes.
                with store.connect(write=True) as current:
                    _prepare(store, store.safe_path("backups/" + backup_id), stage, current)
                    marker = {"stage": stage.name, "principal_id": principal_id}
                    atomic_write(marker_path, json_text(marker).encode())
            cleanup_pending = _install(store, marker)
        return {"status": "completed", "query_snapshots_invalidated": True,
                "old_authorization_tokens_invalidated": True, "current_mode_preserved": True,
                "staging_cleanup_pending": cleanup_pending}
    except MailError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise MailError("restore_recovery_required" if marker_path.exists() else "restore_failed") from None
    finally:
        try:
            if stage is not None and stage.exists() and not marker_path.exists():
                with suppress(OSError):
                    shutil.rmtree(store.safe_path(str(stage.relative_to(store.root))))
        finally:
            worker.release()
