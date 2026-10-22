-- Frozen pre-migration schema 1; no credentials or mail content.

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
    attempt INTEGER NOT NULL DEFAULT 0
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
