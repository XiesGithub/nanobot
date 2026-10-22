"""Mail domain contracts, independent of channels and the agent runtime."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanobot.security.context import SecurityContext

GENERATION_MODEL = "deepseek-v4-flash"
QUERY_TTL_SECONDS = 24 * 60 * 60
ARCHIVE_COVERAGE = "Only successfully fetched and admitted mail; no historical backfill."


class MailError(Exception):
    """Safe public error; underlying exceptions must not leak content or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MailStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    REMOVED = "removed"


class IndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


class InteractionState(StrEnum):
    NOT_REQUESTED = "not_requested"
    CANCELLED = "cancelled"
    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


class SourceIdentity(Record):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID
    mailbox: str = Field(min_length=1, max_length=1024)
    uidvalidity: int = Field(gt=0, le=2**32 - 1)
    uid: int = Field(gt=0, le=2**32 - 1)

    @property
    def key(self) -> str:
        # Structured encoding avoids ambiguous delimiter concatenation.
        data = [str(self.account_id), self.mailbox, self.uidvalidity, self.uid]
        return hashlib.sha256(json.dumps(data, ensure_ascii=True).encode()).hexdigest()


class ArchivePart(Record):
    part_id: str = Field(min_length=1, max_length=128)
    mime_section: str = ""
    part_descriptor: str = ""
    filename: str = Field(default="", max_length=1024)
    mime: str = "text/plain"
    size: int = Field(default=0, ge=0)
    # Trusted ingress supplies bytes; neither model nor API supplies filesystem paths.
    content: bytes | None = Field(default=None, repr=False, exclude=True)
    error_code: str | None = None


class ArchiveEnvelope(Record):
    source: SourceIdentity
    owner_id: str = "local-owner"
    source_fingerprint: str
    sender_address: str = Field(max_length=320)
    sender_name: str = Field(default="", max_length=512)
    subject: str = Field(default="", max_length=1024)
    message_id_header: str = ""
    received_at: datetime | None = None
    received_timezone: str | None = None
    sent_at: datetime | None = None
    received_time_basis: Literal["internaldate", "unknown"] = "unknown"
    body: str = Field(default="", repr=False)
    body_format: Literal["plain", "html"] = "plain"
    parts: list[ArchivePart] = Field(default_factory=list)
    policy_revision: int = 0
    mode_revision: int = 0
    interaction_input: dict[str, Any] | None = Field(default=None, repr=False)
    rejection_code: Literal["size_limit"] | None = None

    @model_validator(mode="after")
    def validate_envelope(self) -> ArchiveEnvelope:
        for value in (self.received_at, self.sent_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Mail timestamps must include a timezone")
        if (self.received_at is not None) != (self.received_time_basis == "internaldate"):
            raise ValueError("received_at requires INTERNALDATE provenance")
        ids = [p.part_id for p in self.parts]
        if "body" in ids or len(ids) != len(set(ids)):
            raise ValueError("Attachment part IDs must be unique and not body")
        return self


class AcceptReceipt(Record):
    status: Literal["accepted", "duplicate", "removed", "rejected", "retryable_failure"]
    mail_id: str | None = None
    job_id: str | None = None
    durable: bool = False
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_durability(self) -> AcceptReceipt:
        if self.status in {"accepted", "duplicate"} and not self.durable:
            raise ValueError("Successful archive receipts must be durable")
        if self.error_code in {"capacity_backpressure", "disk_full"} and (
            self.status != "retryable_failure" or self.durable
        ):
            raise ValueError("Temporary capacity failure must remain retryable")
        return self


class AccessContext(Record):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_id: str
    principal_id: str
    binding_id: str
    scopes: frozenset[Literal["read", "manage"]]
    origin: str
    policy_revision: int
    session_key: str

    @classmethod
    def from_security(cls, security: SecurityContext | None, session_key: str) -> AccessContext:
        if not security or not security.authenticated or not security.owner_id or not security.binding_id:
            raise MailError("unauthorized")
        return cls(
            owner_id=security.owner_id, principal_id=security.principal_id,
            binding_id=security.binding_id, scopes=security.scopes,
            origin=security.origin_kind, policy_revision=security.policy_revision,
            session_key=session_key,
        )


class Coverage(Record):
    complete: bool = True
    truncated: bool = False
    total_known: bool = True
    processed_chars: int = 0
    locators: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extraction_revision: str = "mail-extract-v1"


class TextBlock(Record):
    ordinal: int
    text: str
    locator: dict[str, Any]
    content_role: Literal["current", "quoted", "signature", "unknown"] = "unknown"
    role_basis: str = "unclassified"


class ExtractionResult(Record):
    blocks: list[TextBlock] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    status: MailStatus = MailStatus.READY
    error_code: str | None = None


class MailQuery(Record):
    query: str = Field(default="", max_length=2000)
    exact_terms: list[str] = Field(default_factory=list, max_length=20)
    exact_operator: Literal["all", "any"] = "all"
    sender_addresses: list[str] | None = None
    sender_name_hint: str | None = None
    subject_terms: list[str] | None = None
    received_from: datetime | None = None
    received_before: datetime | None = None
    include_sent_time_fallback: bool = False
    mail_ids: list[str] | None = None
    document_ids: list[str] | None = None
    scope_action: Literal["inherit", "replace", "expand"] = "replace"
    mode: Literal["hybrid", "keyword", "enumerate"] = "hybrid"
    limit: int = Field(default=8, ge=1, le=20)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> MailQuery:
        for value in (self.received_from, self.received_before):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Query timestamps must include a timezone")
        if self.received_from and self.received_before and self.received_from >= self.received_before:
            raise ValueError("Query date interval must be nonempty")
        if any(not t.strip() or len(t) > 256 for t in self.exact_terms):
            raise ValueError("Exact terms must be nonempty and <= 256 characters")
        return self


class Evidence(Record):
    evidence_id: str
    query_id: str
    mail_id: str
    document_id: str
    generation: int
    revision: int
    index_revision: str
    chunk_id: str
    sender_address: str
    sender_name: str = ""
    subject: str
    received_at: str | None = None
    time_basis: str
    filename: str = ""
    locator: dict[str, Any]
    excerpt: str
    match_kind: str
    exact_hits: list[dict[str, Any]] = Field(default_factory=list)
    content_role: str
    role_basis: str
    coverage: Coverage


class SearchPage(Record):
    query_id: str
    applied_filters: dict[str, Any]
    matches: list[Evidence]
    next_cursor: str | None = None
    matching_mail_count: int = 0
    processed_mail_count: int = 0
    excluded_unknown_time: int = 0
    archive_coverage: str = ARCHIVE_COVERAGE
    semantic_status: str = "semantic_unavailable"
    warnings: list[str] = Field(default_factory=list)


class SourceRead(Record):
    evidence_id: str | None = None
    mail_id: str | None = None
    document_id: str | None = None
    cursor: str | None = None
    limit: int = Field(default=6000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_target(self) -> SourceRead:
        if sum(bool(x) for x in (self.evidence_id, self.mail_id, self.document_id)) != 1:
            raise ValueError("Specify exactly one source ID")
        return self


class SourcePage(Record):
    query_id: str
    sources: list[Evidence]
    next_cursor: str | None = None


class HealthSnapshot(Record):
    mode_revision: int = 0
    privacy_activated: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    delivery_counts: dict[str, int] = Field(default_factory=dict)
    admission_waiting_count: int = 0
    admission_waiting_reasons: dict[str, int] = Field(default_factory=dict)
    ingest_counts: dict[str, int] = Field(default_factory=dict)
    index_pending_documents: int = 0
    pending_bytes: int = 0
    max_pending_bytes: int = 0
    semantic_status: str = "semantic_unavailable"
    archive_coverage: str = ARCHIVE_COVERAGE
    errors: list[dict[str, Any]] = Field(default_factory=list)
    worker_online: bool = False


class ManageAction(Record):
    action: Literal[
        "retry", "reparse", "reindex", "prepare_remove", "request_restore",
        "prepare_refetch", "dismiss_interaction", "prepare_interaction_retry",
    ]
    target_ids: list[str] = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=128)


class ManageReceipt(Record):
    status: Literal[
        "accepted", "accepted_waiting_service", "completed", "needs_confirmation", "failed",
    ]
    action_id: str
    target_ids: list[str]
    error_code: str | None = None
    confirmation_token: str | None = None
    preview: list[dict[str, Any]] = Field(default_factory=list)


class MailKnowledgeService(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def accept(self, envelope: ArchiveEnvelope) -> AcceptReceipt: ...
    async def search(self, access: AccessContext, query: MailQuery) -> SearchPage: ...
    async def read_source(self, access: AccessContext, request: SourceRead) -> SourcePage: ...
    async def status(self, access: AccessContext) -> HealthSnapshot: ...
    async def manage(self, access: AccessContext, action: ManageAction) -> ManageReceipt: ...
