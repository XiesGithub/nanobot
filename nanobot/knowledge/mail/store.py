"""Private SQLite facts and atomic input publication.

All connections belong to the calling thread. Expensive extraction never runs
inside transactions. File locks coordinate writers, snapshots and collection
across Gateway and independent query processes on a local filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from filelock import FileLock

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.contracts import AcceptReceipt, ArchiveEnvelope, MailError

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
    instance_id TEXT NOT NULL, privacy_activated INTEGER NOT NULL DEFAULT 1,
    active_index_revision TEXT NOT NULL DEFAULT 'text-v1', mode_revision INTEGER NOT NULL DEFAULT 1,
    archive_enabled INTEGER NOT NULL DEFAULT 0, trigger_agent INTEGER NOT NULL DEFAULT 1,
    clean_shutdown INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
    server_name TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
    UNIQUE(account_id,server_name,uidvalidity)
);
CREATE TABLE IF NOT EXISTS mail_items (
    mail_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id), uid INTEGER NOT NULL,
    owner_id TEXT NOT NULL, message_id_header TEXT NOT NULL, raw_hash TEXT NOT NULL,
    sender_address TEXT NOT NULL, sender_name TEXT NOT NULL, subject TEXT NOT NULL,
    received_at TEXT, received_timezone TEXT, sent_at TEXT, received_time_basis TEXT NOT NULL,
    archived_at REAL NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    deleted_at REAL, UNIQUE(mailbox_id,uid)
);
CREATE INDEX IF NOT EXISTS mail_scope ON mail_items(owner_id,deleted_at,received_at,sender_address);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY, mail_id TEXT NOT NULL REFERENCES mail_items(mail_id),
    kind TEXT NOT NULL, part_id TEXT NOT NULL, mime_section TEXT NOT NULL,
    part_descriptor TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
    size INTEGER NOT NULL, object_ref TEXT, sha256 TEXT,
    extract_status TEXT NOT NULL, index_status TEXT NOT NULL DEFAULT 'pending',
    active_generation INTEGER NOT NULL DEFAULT 0, coverage_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT, UNIQUE(mail_id,part_id)
);
CREATE TABLE IF NOT EXISTS document_blocks (
    block_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
    generation INTEGER NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
    locator_json TEXT NOT NULL, content_role TEXT NOT NULL, role_basis TEXT NOT NULL,
    UNIQUE(document_id,generation,ordinal)
);
CREATE TABLE IF NOT EXISTS document_versions (
    document_id TEXT NOT NULL REFERENCES documents(document_id), generation INTEGER NOT NULL,
    coverage_json TEXT NOT NULL, PRIMARY KEY(document_id,generation)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
    generation INTEGER NOT NULL, chunking_revision TEXT NOT NULL, ordinal INTEGER NOT NULL,
    text TEXT NOT NULL, block_spans_json TEXT NOT NULL, token_count INTEGER NOT NULL,
    text_hash TEXT NOT NULL, content_role TEXT NOT NULL, role_basis TEXT NOT NULL,
    UNIQUE(document_id,generation,chunking_revision,ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id UNINDEXED,terms,header_terms);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    embedding_revision TEXT NOT NULL, dimension INTEGER NOT NULL CHECK(dimension>0),
    vector_blob BLOB NOT NULL, normalized INTEGER NOT NULL CHECK(normalized=1),
    PRIMARY KEY(chunk_id,embedding_revision), CHECK(length(vector_blob)=dimension*4)
);
CREATE TABLE IF NOT EXISTS ingest_jobs (
    job_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
    mail_id TEXT NOT NULL REFERENCES mail_items(mail_id), stage TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0, next_run_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
    error_code TEXT, spool_ref TEXT NOT NULL, input_bytes INTEGER NOT NULL,
    pending_bytes INTEGER NOT NULL, mail_revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
    bytes INTEGER NOT NULL CHECK(bytes>=0), lease_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admission_waits (
    source_key TEXT PRIMARY KEY, source_json TEXT NOT NULL,
    first_at REAL NOT NULL, next_attempt_at REAL NOT NULL, error_code TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'waiting' CHECK(state IN ('waiting','source_unavailable'))
);
CREATE TABLE IF NOT EXISTS source_tombstones (
    source_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, old_mail_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL REFERENCES accounts(account_id), mailbox_server_name TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, removed_at REAL NOT NULL,
    restore_revision INTEGER NOT NULL, cleanup_pending INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS interaction_dispatches (
    source_key TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, mode_revision INTEGER NOT NULL,
    state TEXT NOT NULL, input_json TEXT, attempt_id TEXT NOT NULL, updated_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS interaction_deliveries (
    attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
    error_code TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    binding_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    channel TEXT NOT NULL, scopes_json TEXT NOT NULL, policy_revision INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0, UNIQUE(channel,principal_id)
);
CREATE TABLE IF NOT EXISTS chat_bindings (
    channel TEXT NOT NULL, chat_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    binding_id TEXT, session_key TEXT NOT NULL, PRIMARY KEY(channel,chat_id)
);
CREATE TABLE IF NOT EXISTS web_requests (
    binding_id TEXT NOT NULL, session_key TEXT NOT NULL, request_id TEXT NOT NULL,
    action TEXT NOT NULL, payload_hash TEXT NOT NULL, response_json TEXT NOT NULL,
    PRIMARY KEY(binding_id,session_key,request_id)
);
CREATE TABLE IF NOT EXISTS confirmations (
    token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    policy_revision INTEGER NOT NULL, session_key TEXT NOT NULL, action TEXT NOT NULL,
    target_ids TEXT NOT NULL, target_revisions TEXT NOT NULL,
    expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS management_actions (
    action_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, request_id TEXT NOT NULL,
    action TEXT NOT NULL, payload_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(binding_id,request_id)
);
CREATE TABLE IF NOT EXISTS maintenance_effects (
    action_id TEXT NOT NULL REFERENCES management_actions(action_id), target_id TEXT NOT NULL,
    mail_id TEXT NOT NULL REFERENCES mail_items(mail_id), PRIMARY KEY(action_id,target_id)
);
CREATE TABLE IF NOT EXISTS health_events (
    code TEXT NOT NULL, scope_id TEXT NOT NULL, first_at REAL NOT NULL, last_at REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 1, resolved_at REAL, PRIMARY KEY(code,scope_id)
);
CREATE TABLE IF NOT EXISTS index_manifests (
    revision TEXT PRIMARY KEY, embedding_revision TEXT, chunking_revision TEXT NOT NULL,
    status TEXT NOT NULL, manifest_json TEXT NOT NULL
);
"""

QUERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_runs (
    query_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    binding_id TEXT NOT NULL, session_key TEXT NOT NULL, filters_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS query_expiry ON query_runs(expires_at);
CREATE TABLE IF NOT EXISTS summary_progress (
    owner_id TEXT NOT NULL, binding_id TEXT NOT NULL, session_key TEXT NOT NULL,
    query_id TEXT NOT NULL REFERENCES query_runs(query_id) ON DELETE CASCADE,
    cursor_json TEXT NOT NULL,
    PRIMARY KEY(owner_id,binding_id,session_key)
);
CREATE TABLE IF NOT EXISTS query_evidence (
    evidence_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES query_runs(query_id)
    ON DELETE CASCADE, mail_id TEXT NOT NULL, document_id TEXT NOT NULL,
    generation INTEGER NOT NULL, index_revision TEXT NOT NULL, chunk_id TEXT NOT NULL,
    mail_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, exact_hits_json TEXT NOT NULL,
    UNIQUE(query_id,ordinal)
);
CREATE INDEX IF NOT EXISTS query_mail_ordinal ON query_evidence(query_id,mail_id,ordinal);
CREATE TABLE IF NOT EXISTS mail_focus (
    owner_id TEXT NOT NULL, binding_id TEXT NOT NULL, session_key TEXT NOT NULL,
    filters_json TEXT NOT NULL, last_query TEXT NOT NULL, expires_at REAL NOT NULL,
    PRIMARY KEY(owner_id,binding_id,session_key)
);
"""


def identifier(prefix: str) -> str:
    return prefix + uuid4().hex


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def utc_text(value: str | None) -> str | None:
    return datetime.fromisoformat(value).astimezone(UTC).isoformat() if value else None


def atomic_write(path: Path, data: bytes) -> None:
    """Publish on the same volume; no successful receipt before fsync + replace."""
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class MailStore:
    def __init__(self, root: Path, config: MailKnowledgeConfig, *, readonly: bool = False,
                 initial_trigger_agent: bool = True):
        self.root = root.resolve()
        self.config = config
        self.readonly = readonly
        self.initial_trigger_agent = initial_trigger_agent
        self.path = self.root / "knowledge.sqlite3"
        self.query_path = self.root / "query_state.sqlite3"
        self.lock = FileLock(str(self.root / "maintenance.lock"), timeout=10)
        self.writer_lock = FileLock(str(self.root / "worker.lock"), timeout=0)

    def safe_path(self, relative: str) -> Path:
        candidate = self.root / relative
        # Resolve directories first: Windows cannot GetFinalPathNameByHandle on
        # a regular file currently opened exclusively by FileLock.
        resolved = Path(os.path.abspath(candidate.parent.resolve() / candidate.name))
        try:
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                resolved = candidate.resolve()
        except FileNotFoundError:
            pass
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise MailError("invalid_object_path")
        # resolve() follows symlinks and Windows directory junctions.
        return resolved

    @contextmanager
    def connect(self, *, write: bool = False, query_state: bool = False) -> Iterator[sqlite3.Connection]:
        if (self.root / "restore_pending.json").exists():
            raise MailError("restore_recovery_required")
        if write and self.readonly and not query_state:
            raise MailError("knowledge_readonly")
        path = self.query_path if query_state else self.path
        if not path.is_file():
            raise MailError("query_state_unavailable" if query_state else "index_unavailable")
        connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                     uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if (self.root / "restore_pending.json").exists():
                    raise MailError("restore_recovery_required")
                if not query_state:
                    self._check_version(connection)
            else:
                connection.execute("PRAGMA query_only=ON")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if (self.root / "restore_pending.json").exists():
            raise MailError("restore_recovery_required")
        if self.readonly:
            with self.connect() as db:
                self._check_version(db)
            if not self.query_path.is_file():
                raise MailError("query_state_unavailable")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                has_meta = db.execute("SELECT name FROM sqlite_master WHERE name='schema_meta'").fetchone()
                row = db.execute("SELECT schema_version FROM schema_meta WHERE singleton=1").fetchone() if has_meta else None
            if row and row[0] == 1:
                from nanobot.knowledge.mail.migrations import upgrade_v1
                upgrade_v1(self)
        with self.lock:
            with closing(sqlite3.connect(self.path)) as db, db:
                if db.execute("SELECT name FROM sqlite_master WHERE name='schema_meta'").fetchone():
                    self._check_version(db)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript(SCHEMA)
                db.execute("INSERT OR IGNORE INTO schema_meta(singleton,schema_version,instance_id,archive_enabled,trigger_agent) VALUES(1,?,?,?,?)",
                           (SCHEMA_VERSION, identifier("instance_"), self.config.archive_enabled, self.initial_trigger_agent))
                db.execute("INSERT OR IGNORE INTO index_manifests VALUES('text-v1',NULL,'text-v1','ready','{}')")
            with closing(sqlite3.connect(self.query_path)) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript(QUERY_SCHEMA)
            for directory in ("spool", "objects", "maintenance"):
                self.safe_path(directory).mkdir(exist_ok=True)

    @staticmethod
    def _check_version(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT schema_version FROM schema_meta WHERE singleton=1").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise MailError("schema_version_unsupported")

    def pending_bytes(self, db: sqlite3.Connection) -> int:
        jobs = db.execute("SELECT COALESCE(SUM(pending_bytes),0) FROM ingest_jobs").fetchone()[0]
        reservations = db.execute("SELECT COALESCE(SUM(bytes),0) FROM reservations").fetchone()[0]
        return jobs + reservations

    def source_receipt(self, source_key: str, db=None) -> AcceptReceipt | None:
        if db is None:
            with self.connect() as connection:
                return self.source_receipt(source_key, connection)
        row = db.execute("SELECT old_mail_id FROM source_tombstones WHERE source_key=?", (source_key,)).fetchone()
        if row:
            return AcceptReceipt(status="removed", mail_id=row[0], durable=True)
        row = db.execute("SELECT mail_id,job_id FROM ingest_jobs WHERE source_key=?", (source_key,)).fetchone()
        if row:
            return AcceptReceipt(status="duplicate", mail_id=row[0], job_id=row[1], durable=True)
        return None

    def reserve(self, source_key: str, size: int) -> str:
        if size < 0 or size > self.config.max_message_bytes:
            raise MailError("size_limit")
        with self.lock, self.connect(write=True) as db:
            if self.pending_bytes(db) + size > self.config.max_pending_bytes:
                raise MailError("capacity_backpressure")
            if shutil.disk_usage(self.root).free < size * 2 + 1024**2:
                raise MailError("disk_full")
            reservation = identifier("reserve_")
            try:
                db.execute("INSERT INTO reservations VALUES(?,?,?,?)",
                           (reservation, source_key, size, time.time() + 120))
            except sqlite3.IntegrityError:
                raise MailError("capacity_backpressure") from None
            return reservation

    def release_reservation(self, reservation: str) -> None:
        with self.lock, self.connect(write=True) as db:
            db.execute("DELETE FROM reservations WHERE reservation_id=?", (reservation,))

    def release_unpublished_reservation(self, reservation: str) -> None:
        with self.lock:
            for manifest in self.safe_path("spool").glob("*/manifest.json"):
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("reservation") == reservation:
                    return  # Recovery still owns these published bytes.
            self.release_reservation(reservation)

    def accept(self, envelope: ArchiveEnvelope, reservation: str | None = None) -> AcceptReceipt:
        """Only local durable work here. Parsing and embeddings belong to Worker."""
        with self.lock:
            existing = self.source_receipt(envelope.source.key)
            if existing:
                if reservation:
                    self.release_reservation(reservation)
                return existing
            size = len(envelope.body.encode("utf-8")) + sum(len(p.content or b"") for p in envelope.parts)
            with self.connect() as db:
                account = db.execute("SELECT * FROM accounts WHERE account_id=?", (str(envelope.source.account_id),)).fetchone()
                if account and (account["source_fingerprint"] != envelope.source_fingerprint
                                or account["owner_id"] != envelope.owner_id):
                    raise MailError("source_identity_changed")
            reservation = reservation or self.reserve(envelope.source.key, size)
            job_id, mail_id = identifier("job_"), identifier("mail_")
            spool = self.safe_path("spool/" + job_id)
            published = False
            try:
                with self.connect(write=True) as db:
                    reserved = db.execute("SELECT * FROM reservations WHERE reservation_id=? AND source_key=?",
                                          (reservation, envelope.source.key)).fetchone()
                    if reserved is None or reserved["lease_until"] <= time.time():
                        raise MailError("capacity_backpressure")
                    if size > self.config.max_message_bytes:
                        raise MailError("size_limit")
                    if self.pending_bytes(db) - reserved["bytes"] + size > self.config.max_pending_bytes:
                        raise MailError("capacity_backpressure")
                    db.execute("UPDATE reservations SET bytes=? WHERE reservation_id=?", (size, reservation))
                spool.mkdir()
                data = envelope.model_dump(mode="json")
                data.update(job_id=job_id, mail_id=mail_id, input_bytes=size, reservation=reservation)
                body_bytes = envelope.body.encode("utf-8")
                data["body_sha256"] = hashlib.sha256(body_bytes).hexdigest()
                atomic_write(spool / "body.bin", body_bytes)
                data["body"] = ""  # Body is a separately checked private object.
                for info, part in zip(data["parts"], envelope.parts):
                    if part.content is not None:
                        name = identifier("part_") + ".bin"
                        atomic_write(spool / name, part.content)
                        info["file"] = name
                        info["sha256"] = hashlib.sha256(part.content).hexdigest()
                atomic_write(spool / "manifest.json", json_text(data).encode("utf-8"))
                published = True
                self._register_manifest(spool)
                return AcceptReceipt(status="accepted", mail_id=mail_id, job_id=job_id, durable=True)
            finally:
                if not published:
                    self.release_reservation(reservation)
                    if spool.exists():
                        shutil.rmtree(spool)

    def _register_manifest(self, spool: Path) -> None:
        data = json.loads((spool / "manifest.json").read_text(encoding="utf-8"))
        source = data["source"]
        from nanobot.knowledge.mail.contracts import SourceIdentity
        identity = SourceIdentity.model_validate(source)
        with self.connect(write=True) as db:
            if self.source_receipt(identity.key, db):
                db.execute("DELETE FROM reservations WHERE reservation_id=?", (data["reservation"],))
                return
            account = db.execute("SELECT * FROM accounts WHERE account_id=?", (source["account_id"],)).fetchone()
            if account and (account["source_fingerprint"] != data["source_fingerprint"] or account["owner_id"] != data["owner_id"]):
                raise MailError("source_identity_changed")
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?)",
                       (source["account_id"], data["owner_id"], data["source_fingerprint"], time.time()))
            db.execute("INSERT OR IGNORE INTO mailboxes VALUES(?,?,?,?)",
                       (identifier("box_"), source["account_id"], source["mailbox"], source["uidvalidity"]))
            box = db.execute("SELECT mailbox_id FROM mailboxes WHERE account_id=? AND server_name=? AND uidvalidity=?",
                             (source["account_id"], source["mailbox"], source["uidvalidity"])).fetchone()[0]
            db.execute("""INSERT INTO mail_items(mail_id,source_key,mailbox_id,uid,owner_id,
                message_id_header,raw_hash,sender_address,sender_name,subject,received_at,
                received_timezone,sent_at,received_time_basis,archived_at,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""", (
                data["mail_id"], identity.key, box, source["uid"], data["owner_id"],
                data["message_id_header"], data["body_sha256"], data["sender_address"],
                data["sender_name"], data["subject"], utc_text(data["received_at"]), data["received_timezone"],
                utc_text(data["sent_at"]), data["received_time_basis"], time.time(),
            ))
            parts = [{"part_id": "body", "filename": "", "mime": "text/" + data["body_format"],
                      "size": (spool / "body.bin").stat().st_size, "file": "body.bin",
                      "sha256": data["body_sha256"]}, *data["parts"]]
            for part in parts:
                ref = str((spool / part["file"]).relative_to(self.root)) if part.get("file") else None
                if ref:
                    content = self.safe_path(ref).read_bytes()
                    if hashlib.sha256(content).hexdigest() != part["sha256"]:
                        raise MailError("input_integrity_failed")
                db.execute("""INSERT INTO documents(document_id,mail_id,kind,part_id,mime_section,
                    part_descriptor,filename,mime,size,object_ref,sha256,extract_status,error_code)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    identifier("doc_"), data["mail_id"], "body" if part["part_id"] == "body" else "attachment",
                    part["part_id"], part.get("mime_section", ""), part.get("part_descriptor", ""),
                    part["filename"], part["mime"], part["size"], ref, part.get("sha256"),
                    "queued" if ref else "failed", part.get("error_code"),
                ))
            db.execute("""INSERT INTO ingest_jobs(job_id,source_key,mail_id,stage,spool_ref,input_bytes,pending_bytes)
                VALUES(?,?,?,'accepted',?,?,?)""", (data["job_id"], identity.key, data["mail_id"],
                str(spool.relative_to(self.root)), data["input_bytes"], data["input_bytes"]))
            if data.get("interaction_input") is not None:
                db.execute("INSERT OR IGNORE INTO interaction_dispatches VALUES(?,?,?,'pending',?,?,?,1)", (
                    identity.key, identifier("task_"), data["mode_revision"],
                    json_text(data["interaction_input"]), identifier("attempt_"), time.time(),
                ))
            else:
                db.execute("INSERT OR IGNORE INTO interaction_dispatches VALUES(?,?,?,'not_requested',NULL,?,?,1)", (
                    identity.key, identifier("task_"), data["mode_revision"], identifier("attempt_"), time.time(),
                ))
            db.execute("DELETE FROM reservations WHERE reservation_id=?", (data["reservation"],))
            if data.get("rejection_code"):
                db.execute("UPDATE documents SET extract_status='failed',error_code=? WHERE mail_id=?",
                           (data["rejection_code"], data["mail_id"]))
                db.execute("UPDATE ingest_jobs SET stage='failed',pending_bytes=0,error_code=? WHERE mail_id=?",
                           (data["rejection_code"], data["mail_id"]))
                db.execute("UPDATE mail_items SET status='failed' WHERE mail_id=?", (data["mail_id"],))

    def recover(self) -> None:
        """Called only after obtaining the exclusive Worker lock."""
        with self.lock:
            for manifest in self.safe_path("spool").glob("*/manifest.json"):
                self._register_manifest(self.safe_path(str(manifest.parent.relative_to(self.root))))
            with self.connect(write=True) as db:
                db.execute("DELETE FROM reservations WHERE lease_until<=?", (time.time(),))
                db.execute("UPDATE ingest_jobs SET stage='accepted',generation=generation+1,lease_until=0 WHERE stage='extracting'")
                db.execute("UPDATE interaction_dispatches SET state='uncertain',revision=revision+1 WHERE state IN ('dispatching','running')")
                db.execute("UPDATE interaction_deliveries SET status='uncertain',error_code='restart_before_delivery_proof',updated_at=? WHERE status IN ('queued','sending')", (time.time(),))
                # Only the exclusive Worker performs shadow builds. A building
                # revision found at startup has no surviving publisher.
                db.execute("UPDATE index_manifests SET status='failed' WHERE status='building'")
                retained = {r[0] for r in db.execute("SELECT spool_ref FROM ingest_jobs")}
                retained.update(str(Path(r[0]).parent) for r in db.execute("SELECT object_ref FROM documents WHERE object_ref IS NOT NULL"))
            for directory in self.safe_path("spool").glob("job_*"):
                relative = str(directory.relative_to(self.root))
                resolved = self.safe_path(relative)
                if (resolved.parent == self.safe_path("spool") and resolved.is_dir()
                    and relative not in retained and not (resolved / "manifest.json").exists()):
                    # accept holds this maintenance lock through publication;
                    # an unpublished orphan cannot belong to a concurrent writer.
                    shutil.rmtree(resolved)
            for directory in self.safe_path("maintenance").glob("input_*"):
                relative = str(directory.relative_to(self.root))
                if directory.is_dir() and relative not in retained:
                    shutil.rmtree(self.safe_path(relative))

    def claim(self, lease_seconds: float = 90) -> dict | None:
        with self.connect(write=True) as db:
            row = db.execute("""SELECT j.* FROM ingest_jobs j JOIN mail_items m USING(mail_id)
                WHERE m.deleted_at IS NULL AND j.stage IN ('accepted','retry_wait')
                AND j.next_run_at<=? ORDER BY j.next_run_at,j.rowid LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE ingest_jobs SET stage='extracting',generation=generation+1,lease_until=? WHERE job_id=?",
                       (time.time() + lease_seconds, row["job_id"]))
            db.execute("UPDATE mail_items SET status='processing' WHERE mail_id=?", (row["mail_id"],))
            result = dict(row)
            result["generation"] += 1
            return result

    def check_fence(self, db: sqlite3.Connection, job: dict) -> None:
        row = db.execute("""SELECT j.generation,j.stage,j.lease_until,m.revision,m.deleted_at
            FROM ingest_jobs j JOIN mail_items m USING(mail_id) WHERE job_id=?""", (job["job_id"],)).fetchone()
        if not row or row["deleted_at"] is not None or row["stage"] != "extracting" or (
            row["generation"] != job["generation"] or row["revision"] != job["mail_revision"]
            or row["lease_until"] <= time.time()
        ):
            raise MailError("revision_conflict")

    def renew(self, job: dict, lease_seconds: float = 90) -> None:
        with self.connect(write=True) as db:
            self.check_fence(db, job)
            db.execute("UPDATE ingest_jobs SET lease_until=? WHERE job_id=?",
                       (time.time() + lease_seconds, job["job_id"]))

    def finish(self, job: dict) -> None:
        with self.connect(write=True) as db:
            self.check_fence(db, job)
            states = [r[0] for r in db.execute("SELECT extract_status FROM documents WHERE mail_id=?", (job["mail_id"],))]
            status = "ready" if all(s == "ready" for s in states) else (
                "partial" if any(s in {"ready", "partial"} for s in states) else "failed")
            db.execute("UPDATE mail_items SET status=? WHERE mail_id=?", (status, job["mail_id"]))
            db.execute("UPDATE ingest_jobs SET stage='finished',pending_bytes=0,lease_until=0 WHERE job_id=?", (job["job_id"],))

    def publish_document(self, job: dict, document_id: str, result) -> None:
        from nanobot.knowledge.mail.chunking import chunks, terms
        # Prepare outside the transaction. Each document switches as one unit.
        prepared = list(chunks(result.blocks))
        with self.lock, self.connect(write=True) as db:
            self.check_fence(db, job)
            index_revision = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
            document = db.execute("SELECT * FROM documents WHERE document_id=? AND mail_id=?",
                                  (document_id, job["mail_id"])).fetchone()
            if document is None:
                raise MailError("source_removed")
            generation = document["active_generation"] + 1
            if result.blocks:
                db.execute("INSERT INTO document_versions VALUES(?,?,?)", (
                    document_id, generation, result.coverage.model_dump_json(),
                ))
            for block in result.blocks:
                db.execute("INSERT INTO document_blocks VALUES(?,?,?,?,?,?,?,?)", (
                    identifier("block_"), document_id, generation, block.ordinal, block.text,
                    json_text(block.locator), block.content_role, block.role_basis,
                ))
            mail = db.execute("SELECT subject,sender_address,sender_name FROM mail_items WHERE mail_id=?",
                              (job["mail_id"],)).fetchone()
            header = terms(" ".join([*mail, document["filename"]]))
            for chunk in prepared:
                chunk_id = identifier("chunk_")
                db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    chunk_id, document_id, generation, index_revision, chunk["ordinal"],
                    chunk["text"], json_text(chunk["spans"]), len(chunk["text"].encode()),
                    chunk["text_hash"], chunk["content_role"], chunk["role_basis"],
                ))
                db.execute("INSERT INTO chunk_fts(chunk_id,terms,header_terms) VALUES(?,?,?)",
                           (chunk_id, terms(chunk["text"]), header))
            # Failed reparse preserves the previously readable generation, but exposes failure.
            db.execute("""UPDATE documents SET active_generation=?,extract_status=?,
                coverage_json=?,error_code=?,index_status=? WHERE document_id=?""", (
                generation if result.blocks else document["active_generation"], result.status,
                result.coverage.model_dump_json(), result.error_code,
                "pending" if self.config.semantic_enabled else "disabled", document_id,
            ))

    def preserve_objects(self, job: dict) -> None:
        """Copy + switch refs + remove spool; crash at any point leaves an input copy."""
        with self.lock:
            with self.connect() as db:
                self.check_fence(db, job)
                documents = list(db.execute("SELECT document_id,object_ref FROM documents WHERE mail_id=? AND object_ref IS NOT NULL", (job["mail_id"],)))
            directory = self.safe_path("objects/" + job["mail_id"])
            directory.mkdir(exist_ok=True)
            refs = []
            obsolete_inputs = {self.safe_path(job["spool_ref"])}
            for document in documents:
                old = self.safe_path(document["object_ref"])
                new = directory / (document["document_id"] + ".bin")
                if old != new:
                    atomic_write(new, old.read_bytes())
                    if old.parent.parent in {self.safe_path("spool"), self.safe_path("maintenance")}:
                        obsolete_inputs.add(old.parent)
                refs.append((str(new.relative_to(self.root)), document["document_id"]))
            with self.connect(write=True) as db:
                self.check_fence(db, job)
                db.executemany("UPDATE documents SET object_ref=? WHERE document_id=?", refs)
            # A multi-part refetch publishes one staging directory per target.
            # The job retains only its last spool_ref, so remove every old input
            # directory after all document references switch, not just the last.
            # Each generated staging directory belongs to this mail only; all
            # its input references were captured and switched above under lock.
            for old_directory in obsolete_inputs:
                if old_directory.exists():
                    shutil.rmtree(old_directory)

    def fail(self, job: dict, code: str, *, permanent: bool = False) -> None:
        with self.connect(write=True) as db:
            self.check_fence(db, job)
            attempt = job["attempt"] + 1
            terminal = permanent or attempt >= 3
            db.execute("""UPDATE ingest_jobs SET stage=?,attempt=?,next_run_at=?,error_code=?,
                pending_bytes=CASE WHEN ? THEN 0 ELSE pending_bytes END,lease_until=0 WHERE job_id=?""",
                ("failed" if terminal else "retry_wait", attempt,
                 time.time() + (30, 120, 600)[min(attempt - 1, 2)], code, terminal, job["job_id"]))
            db.execute("UPDATE mail_items SET status=? WHERE mail_id=?",
                       ("failed" if terminal else "queued", job["mail_id"]))

    def remove(self, mail_id: str, expected_revision: int) -> None:
        """Caller has consumed owner confirmation. Logical removal precedes cleanup."""
        with self.lock, self.connect(write=True) as db:
            mail = db.execute("""SELECT m.*,b.account_id,b.server_name,b.uidvalidity FROM mail_items m
                JOIN mailboxes b USING(mailbox_id) WHERE mail_id=?""", (mail_id,)).fetchone()
            if mail is None or mail["revision"] != expected_revision or mail["deleted_at"] is not None:
                raise MailError("revision_conflict")
            db.execute("INSERT INTO source_tombstones VALUES(?,?,?,?,?,?,?,?,?,1)", (
                mail["source_key"], mail["owner_id"], mail_id, mail["account_id"], mail["server_name"],
                mail["uidvalidity"], mail["uid"], time.time(), expected_revision + 1,
            ))
            db.execute("UPDATE mail_items SET deleted_at=?,status='removed',revision=revision+1 WHERE mail_id=?", (time.time(), mail_id))
            db.execute("UPDATE ingest_jobs SET stage='removed',generation=generation+1,pending_bytes=0 WHERE mail_id=?", (mail_id,))
            db.execute("UPDATE interaction_dispatches SET state='cancelled',input_json=NULL,revision=revision+1 WHERE source_key=?", (mail["source_key"],))
        self.cleanup_removed(mail_id)

    def cleanup_removed(self, mail_id: str) -> None:
        with self.lock:
            with self.connect() as db:
                tombstone = db.execute("SELECT * FROM source_tombstones WHERE old_mail_id=?", (mail_id,)).fetchone()
                if not tombstone or not tombstone["cleanup_pending"]:
                    return
                refs = [r[0] for r in db.execute("SELECT spool_ref FROM ingest_jobs WHERE mail_id=?", (mail_id,))]
                document_ids = {r[0] for r in db.execute("SELECT document_id FROM documents WHERE mail_id=?", (mail_id,))}
                refs.extend(str(Path(r[0]).parent) for r in db.execute("SELECT object_ref FROM documents WHERE mail_id=? AND object_ref IS NOT NULL", (mail_id,)))
            for ref in [*refs, "objects/" + mail_id]:
                path = self.safe_path(ref)
                if path.exists():
                    shutil.rmtree(path)
            # Remove query text/focus and match diagnostics referring to the
            # removed source. Keep only opaque evidence identities so an old link
            # reports source_removed, including after the mail ID is restored.
            with self.connect(write=True, query_state=True) as state:
                state.execute("DELETE FROM mail_focus WHERE owner_id=?", (tombstone["owner_id"],))
                runs = list(state.execute("SELECT * FROM query_runs WHERE owner_id=?", (tombstone["owner_id"],)))
                for run in runs:
                    snapshot = json.loads(run["snapshot_json"])
                    if mail_id not in snapshot["mails"]:
                        continue
                    snapshot["mails"].pop(mail_id, None)
                    snapshot["documents"] = {k: v for k, v in snapshot["documents"].items() if k not in document_ids}
                    coverage = json.loads(run["coverage_json"])
                    coverage["matching_mail_count"] = len(snapshot["mails"])
                    coverage["warnings"] = list(dict.fromkeys([*coverage["warnings"], "source_removed"]))
                    state.execute("UPDATE query_runs SET filters_json='{}',snapshot_json=?,coverage_json=? WHERE query_id=?", (json_text(snapshot), json_text(coverage), run["query_id"]))
                state.execute("UPDATE query_evidence SET exact_hits_json='[]' WHERE mail_id=?", (mail_id,))
            with self.connect(write=True) as db:
                removed_ids = document_ids | {mail_id}
                for row in db.execute("SELECT * FROM confirmations").fetchall():
                    if set(json.loads(row["target_ids"])) & removed_ids:
                        db.execute("DELETE FROM confirmations WHERE token_hash=?", (row["token_hash"],))
                for row in db.execute("SELECT * FROM management_actions").fetchall():
                    if row["action"] == "request_restore":
                        continue  # Already contains only minimal recovery identity.
                    payload, receipt = json.loads(row["payload_json"]), json.loads(row["receipt_json"])
                    if not set(receipt["target_ids"]) & removed_ids:
                        continue
                    payload["targets"] = [{k: v for k, v in t.items() if k in {"id", "revision"}} for t in payload["targets"]]
                    receipt["preview"] = []
                    receipt["confirmation_token"] = None
                    status = row["status"]
                    if row["action"] != "prepare_remove" and status not in {"completed", "failed"}:
                        status, receipt["status"], receipt["error_code"] = "failed", "failed", "source_removed"
                    db.execute("UPDATE management_actions SET payload_json=?,receipt_json=?,status=? WHERE action_id=?",
                               (json_text(payload), json_text(receipt), status, row["action_id"]))
                db.execute("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT c.chunk_id FROM chunks c JOIN documents d USING(document_id) WHERE d.mail_id=?)", (mail_id,))
                db.execute("DELETE FROM chunks WHERE document_id IN (SELECT document_id FROM documents WHERE mail_id=?)", (mail_id,))
                db.execute("DELETE FROM document_blocks WHERE document_id IN (SELECT document_id FROM documents WHERE mail_id=?)", (mail_id,))
                db.execute("DELETE FROM document_versions WHERE document_id IN (SELECT document_id FROM documents WHERE mail_id=?)", (mail_id,))
                db.execute("DELETE FROM documents WHERE mail_id=?", (mail_id,))
                db.execute("UPDATE mail_items SET subject='',sender_address='',sender_name='',message_id_header='',raw_hash='',received_at=NULL,sent_at=NULL,received_timezone=NULL WHERE mail_id=?", (mail_id,))
                db.execute("UPDATE source_tombstones SET cleanup_pending=0 WHERE old_mail_id=?", (mail_id,))

    def backup(self, target: Path) -> dict:
        """Offline private backup; snapshots/history are explicitly excluded."""
        from nanobot.knowledge.mail.backup import create_backup
        return create_backup(self, target)
