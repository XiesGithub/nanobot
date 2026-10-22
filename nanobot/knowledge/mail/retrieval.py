"""Authorized local retrieval with immutable 24-hour query snapshots."""

from __future__ import annotations

import json
import time
from datetime import UTC

from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.chunking import exact_match, normalize, terms
from nanobot.knowledge.mail.contracts import (
    ARCHIVE_COVERAGE,
    QUERY_TTL_SECONDS,
    AccessContext,
    Coverage,
    Evidence,
    MailError,
    MailQuery,
    SearchPage,
    SourcePage,
    SourceRead,
)
from nanobot.knowledge.mail.store import MailStore, identifier, json_text


class MailRetrieval:
    def __init__(self, store: MailStore, access: MailAccess, encoder=None):
        self.store = store
        self.access = access
        self.encoder = encoder

    @staticmethod
    def _diversify(db, ranked, documents):
        """Limit each mail to two hits per round; retain deferred context for paging."""
        if not ranked:
            return ranked
        rows = {row["chunk_id"]: dict(row) for row in db.execute(
            "SELECT chunk_id,document_id,text_hash,content_role,block_spans_json FROM chunks WHERE chunk_id IN (SELECT value FROM json_each(?))",
            (json_text(ranked),))}
        remaining, seen = [], set()
        for cid in ranked:
            row = rows[cid]
            signature = (row["document_id"], row["content_role"], row["text_hash"])
            if signature not in seen:
                seen.add(signature)
                remaining.append(cid)
        result = []
        while remaining:
            counts, spans, deferred = {}, {}, []
            for cid in remaining:
                row = rows[cid]
                document = row["document_id"]
                mail = documents[document]["mail_id"]
                current = json.loads(row["block_spans_json"])
                overlap = any(a["block_ordinal"] == b["block_ordinal"] and
                    max(0, min(a["end"], b["end"]) - max(a["start"], b["start"])) >=
                    .7 * min(a["end"] - a["start"], b["end"] - b["start"])
                    for a in current for b in spans.get(document, []))
                if counts.get(mail, 0) >= 2 or overlap:
                    deferred.append(cid)
                    continue
                result.append(cid)
                counts[mail] = counts.get(mail, 0) + 1
                spans.setdefault(document, []).extend(current)
            remaining = deferred
        return result

    def _candidates(self, db, access: AccessContext, query: MailQuery):
        sql = "SELECT * FROM mail_items WHERE owner_id=? AND deleted_at IS NULL"
        args: list = [access.owner_id]
        if query.mail_ids is not None:
            sql += " AND mail_id IN (SELECT value FROM json_each(?))"
            args.append(json_text(query.mail_ids))
        if query.sender_addresses is not None:
            sql += " AND sender_address IN (SELECT value FROM json_each(?))"
            args.append(json_text([s.casefold() for s in query.sender_addresses]))
        excluded_unknown = 0
        if query.received_from or query.received_before:
            excluded_unknown = db.execute("SELECT COUNT(*) FROM (" + sql + ") WHERE received_at IS NULL", args).fetchone()[0]
            field = "COALESCE(received_at,sent_at)" if query.include_sent_time_fallback else "received_at"
            if query.received_from:
                sql += f" AND {field}>=?"
                args.append(query.received_from.astimezone(UTC).isoformat())
            if query.received_before:
                sql += f" AND {field}<?"
                args.append(query.received_before.astimezone(UTC).isoformat())
        mails = {}
        for row in db.execute(sql, args):
            if query.sender_name_hint and normalize(query.sender_name_hint) not in normalize(row["sender_name"]):
                continue
            if query.subject_terms is not None and (not query.subject_terms or not all(
                exact_match(term, row["subject"]) for term in query.subject_terms
            )):
                continue
            mails[row["mail_id"]] = dict(row)
        return mails, excluded_unknown

    def search(self, access: AccessContext, query: MailQuery, *, page_chars=9000) -> SearchPage:
        self.access.require(access)
        if query.cursor:
            return self.page(access, query.cursor, query.limit, max_chars=page_chars)
        # Lock spans fact read and retention registration. No LLM/network work here.
        with self.store.lock, self.store.connect() as db:
            self.access.require(access)
            query = self._focus_query(access, query)
            mails, unknown = self._candidates(db, access, query)
            documents = {}
            for row in db.execute("SELECT * FROM documents WHERE mail_id IN (SELECT value FROM json_each(?))",
                                  (json_text(list(mails)),)):
                if query.document_ids is not None and row["document_id"] not in query.document_ids:
                    continue
                documents[row["document_id"]] = dict(row)
            exact_hits = {mail_id: [] for mail_id in mails}
            if query.exact_terms:
                hit_terms = {mail_id: set() for mail_id in mails}
                for mail_id, mail in mails.items():
                    for term in query.exact_terms:
                        for field in ("subject", "sender_address", "sender_name"):
                            if exact_match(term, mail[field]):
                                hit_terms[mail_id].add(term)
                                exact_hits[mail_id].append({"term": term, "field": field, "content_role": "header"})
                for document_id, doc in documents.items():
                    for term in query.exact_terms:
                        if exact_match(term, doc["filename"]):
                            hit_terms[doc["mail_id"]].add(term)
                            exact_hits[doc["mail_id"]].append({"term": term, "document_id": document_id, "field": "filename"})
                    for block in db.execute("SELECT * FROM document_blocks WHERE document_id=? AND generation=?",
                                            (document_id, doc["active_generation"])):
                        for term in query.exact_terms:
                            if exact_match(term, block["text"]):
                                hit_terms[doc["mail_id"]].add(term)
                                # Bound diagnostic positions without removing match constraints.
                                if len(exact_hits[doc["mail_id"]]) < 40:
                                    exact_hits[doc["mail_id"]].append({
                                        "term": term, "document_id": document_id,
                                        "block_id": block["block_id"], "content_role": block["content_role"],
                                    })
                needed = set(query.exact_terms)
                mails = {mid: mail for mid, mail in mails.items() if (
                    needed <= hit_terms[mid] if query.exact_operator == "all" else bool(hit_terms[mid])
                )}
                documents = {did: doc for did, doc in documents.items() if doc["mail_id"] in mails}
            if query.document_ids is not None:
                visible_mails = {doc["mail_id"] for doc in documents.values()}
                mails = {mid: mail for mid, mail in mails.items() if mid in visible_mails}
            manifest = {did: doc["active_generation"] for did, doc in documents.items()}
            index_revision = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
            snapshot_sql = """SELECT c.* FROM chunks c JOIN json_each(?) s
                ON c.document_id=s.key AND c.generation=s.value WHERE c.chunking_revision=?"""
            parameters = (json_text(manifest), index_revision)
            keyword = []
            query_terms = list(dict.fromkeys(terms(query.query).split()))[:128]
            if query_terms and query.mode != "enumerate":
                expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in query_terms)
                keyword = [row[0] for row in db.execute("""WITH snapshot AS MATERIALIZED (
                    SELECT key,value FROM json_each(?)) SELECT c.chunk_id FROM chunk_fts f
                    JOIN chunks c ON c.chunk_id=f.chunk_id JOIN snapshot s
                    ON c.document_id=s.key AND c.generation=s.value
                    WHERE chunk_fts MATCH ? AND c.chunking_revision=?
                    ORDER BY bm25(chunk_fts,0,1,2) LIMIT 50""", (parameters[0], expression, index_revision))]
            semantic_status = "disabled" if not self.store.config.semantic_enabled else "semantic_unavailable"
            semantic = []
            if self.encoder and query.mode == "hybrid" and query.query:
                try:
                    semantic = self.encoder.search(db, manifest, index_revision, query.query, limit=50)
                    missing = db.execute("""SELECT COUNT(*) FROM chunks c JOIN json_each(?) s
                        ON c.document_id=s.key AND c.generation=s.value LEFT JOIN chunk_vectors v
                        ON c.chunk_id=v.chunk_id AND v.embedding_revision=?
                        WHERE c.chunking_revision=? AND v.chunk_id IS NULL""",
                        (parameters[0], self.encoder.revision, index_revision)).fetchone()[0]
                    semantic_status = "semantic_partial" if missing else "ready"
                except Exception:
                    semantic_status = "semantic_unavailable"
            ranked = []
            if query.mode == "enumerate" or not query.query:
                ranked = [row["chunk_id"] for row in db.execute(snapshot_sql + " ORDER BY c.document_id,c.ordinal", parameters)]
            else:
                scores = {}
                for route in (keyword, semantic):
                    for rank, chunk_id in enumerate(route, 1):
                        scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
                ranked = sorted(scores, key=lambda cid: (-scores[cid], cid))
            # A query with no keyword overlap still exposes exact-mail candidates via
            # evidence from that mail, never unrelated near-ID matches.
            if not ranked and query.exact_terms:
                ranked = [row["chunk_id"] for row in db.execute(snapshot_sql + " ORDER BY c.document_id,c.ordinal", parameters)]
            if query.query and query.mode != "enumerate":
                ranked = self._diversify(db, ranked, documents)
            query_id = identifier("query_")
            created = time.time()
            snapshot = {"mails": {mid: mail["revision"] for mid, mail in mails.items()},
                        "documents": manifest, "index_revision": index_revision,
                        "document_statuses": {did: {"mail_id": doc["mail_id"],
                            "status": doc["extract_status"]} for did, doc in documents.items()}}
            warnings = []
            if query.mode != "enumerate" and query.query:
                warnings.append("ranked_evidence_is_not_complete_scope")
            incomplete = sum(doc["extract_status"] != "ready" for doc in documents.values())
            if incomplete:
                warnings.append(f"incomplete_documents:{incomplete}")
            coverage = {"matching_mail_count": len(mails), "excluded_unknown_time": unknown,
                        "semantic_status": semantic_status, "warnings": warnings}
            try:
                with self.store.connect(write=True, query_state=True) as state:
                    instance = db.execute("SELECT instance_id FROM schema_meta").fetchone()[0]
                    state.execute("INSERT INTO query_runs VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        query_id, instance, access.owner_id, access.binding_id, access.session_key,
                        query.model_dump_json(), json_text(snapshot), json_text(coverage), created, created + QUERY_TTL_SECONDS,
                    ))
                    for ordinal, cid in enumerate(ranked):
                        chunk = db.execute("SELECT document_id,generation FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
                        mail_id = documents[chunk["document_id"]]["mail_id"]
                        state.execute("INSERT INTO query_evidence VALUES(?,?,?,?,?,?,?,?,?,?)", (
                            identifier("ev_"), query_id, mail_id, chunk["document_id"], chunk["generation"],
                            index_revision, cid, mails[mail_id]["revision"], ordinal, json_text(exact_hits[mail_id]),
                        ))
                    focus = query.model_copy(update={"mail_ids": list(mails), "cursor": None})
                    state.execute("INSERT OR REPLACE INTO mail_focus VALUES(?,?,?,?,?,?)", (
                        access.owner_id, access.binding_id, access.session_key, focus.model_dump_json(),
                        query.query, created + QUERY_TTL_SECONDS,
                    ))
            except Exception:
                raise MailError("query_state_unavailable") from None
        return self.page(access, query_id + ":0", query.limit, max_chars=page_chars)

    def _focus_query(self, access, query):
        if query.scope_action == "replace":
            return query
        try:
            with self.store.connect(query_state=True) as db:
                row = db.execute("SELECT * FROM mail_focus WHERE owner_id=? AND binding_id=? AND session_key=? AND expires_at>?",
                                 (access.owner_id, access.binding_id, access.session_key, time.time())).fetchone()
                if row:
                    previous = MailQuery.model_validate_json(row["filters_json"])
                    updates = query.model_dump(exclude_unset=True)
                    previous = previous.model_copy(update=updates)
                    if query.scope_action == "expand":
                        previous = previous.model_copy(update={"mail_ids": None, "document_ids": None,
                                                               "received_from": None, "received_before": None})
                    return previous
        except Exception:
            raise MailError("query_state_unavailable") from None
        return query

    def _run(self, access, query_id, *, source=False):
        self.access.require(access)
        with self.store.connect(query_state=True) as state:
            row = state.execute("SELECT * FROM query_runs WHERE query_id=? AND owner_id=? AND binding_id=? AND session_key=?",
                                (query_id, access.owner_id, access.binding_id, access.session_key)).fetchone()
        if row is None:
            raise MailError("source_not_found")
        if row["expires_at"] <= time.time():
            raise MailError("source_version_expired" if source else "query_expired")
        with self.store.connect() as db:
            if row["instance_id"] != db.execute("SELECT instance_id FROM schema_meta").fetchone()[0]:
                raise MailError("source_version_expired")
        return row

    def _evidence(self, access, row) -> Evidence:
        self.access.require(access)
        with self.store.connect() as db:
            mail = db.execute("SELECT * FROM mail_items WHERE mail_id=? AND owner_id=?", (row["mail_id"], access.owner_id)).fetchone()
            if not mail or mail["deleted_at"] is not None or mail["revision"] != row["mail_revision"]:
                raise MailError("source_removed")
            chunk = db.execute("SELECT * FROM chunks WHERE chunk_id=? AND generation=? AND chunking_revision=?",
                               (row["chunk_id"], row["generation"], row["index_revision"])).fetchone()
            doc = db.execute("SELECT * FROM documents WHERE document_id=?", (row["document_id"],)).fetchone()
            if not chunk or not doc:
                raise MailError("source_version_expired")
            version = db.execute("SELECT coverage_json FROM document_versions WHERE document_id=? AND generation=?",
                                 (row["document_id"], row["generation"])).fetchone()
            if not version:
                raise MailError("source_version_expired")
            coverage = Coverage.model_validate_json(version[0])
            # Page/block locations are carried by the exact chunk spans. A full
            # document's locator inventory must not consume every evidence budget.
            coverage.locators = []
            coverage.warnings = coverage.warnings[:20]
            return Evidence(
                evidence_id=row["evidence_id"], query_id=row["query_id"], mail_id=row["mail_id"],
                document_id=row["document_id"], generation=row["generation"], revision=row["mail_revision"],
                index_revision=row["index_revision"], chunk_id=row["chunk_id"],
                sender_address=mail["sender_address"], sender_name=mail["sender_name"], subject=mail["subject"],
                received_at=mail["received_at"], time_basis=mail["received_time_basis"], filename=doc["filename"],
                locator={"spans": json.loads(chunk["block_spans_json"])}, excerpt=chunk["text"],
                match_kind="exact_mail" if json.loads(row["exact_hits_json"]) else "retrieved",
                exact_hits=json.loads(row["exact_hits_json"])[:8], content_role=chunk["content_role"], role_basis=chunk["role_basis"],
                coverage=coverage,
            )

    def page(self, access: AccessContext, cursor: str, limit: int = 8, *, max_chars=9000) -> SearchPage:
        try:
            query_id, position = cursor.rsplit(":", 1)
            position = int(position)
            if position < 0 or not 1 <= limit <= 20:
                raise ValueError()
        except ValueError:
            raise MailError("invalid_cursor") from None
        run = self._run(access, query_id)
        coverage = json.loads(run["coverage_json"])
        matches, chars, next_position = [], 0, position
        with self.store.connect(query_state=True) as state:
            rows = list(state.execute("SELECT * FROM query_evidence WHERE query_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?",
                                      (query_id, position, limit + 1)))
        for row in rows[:limit]:
            try:
                evidence = self._evidence(access, row)
            except MailError as error:
                if error.code != "source_removed":
                    raise
                coverage["warnings"].append("source_removed")
                next_position = row["ordinal"] + 1
                continue
            size = len(evidence.model_dump_json())
            if chars + size > min(max_chars, 9000):
                if not matches:
                    raise MailError("evidence_budget_exceeded")
                break
            matches.append(evidence)
            chars += size
            next_position = row["ordinal"] + 1
        remaining = bool(rows and next_position <= rows[-1]["ordinal"])
        with self.store.connect(query_state=True) as state:
            processed = {r[0] for r in state.execute("SELECT DISTINCT mail_id FROM query_evidence WHERE query_id=? AND ordinal<?", (query_id, next_position))}
        with self.store.connect() as db:
            processed_count = db.execute("SELECT COUNT(*) FROM mail_items WHERE mail_id IN (SELECT value FROM json_each(?)) AND deleted_at IS NULL", (json_text(list(processed)),)).fetchone()[0]
        self.access.require(access)
        return SearchPage(
            query_id=query_id, applied_filters=json.loads(run["filters_json"]), matches=matches,
            next_cursor=f"{query_id}:{next_position}" if remaining else None,
            matching_mail_count=coverage["matching_mail_count"], processed_mail_count=processed_count,
            excluded_unknown_time=coverage["excluded_unknown_time"], archive_coverage=ARCHIVE_COVERAGE,
            semantic_status=coverage["semantic_status"], warnings=coverage["warnings"],
        )

    def read_source(self, access: AccessContext, request: SourceRead) -> SourcePage:
        self.access.require(access)
        if request.evidence_id:
            with self.store.connect(query_state=True) as state:
                row = state.execute("SELECT * FROM query_evidence WHERE evidence_id=?", (request.evidence_id,)).fetchone()
            if row is None:
                raise MailError("source_not_found")
            self._run(access, row["query_id"], source=True)
            evidence = self._evidence(access, row)
            if len(evidence.model_dump_json()) > request.limit:
                raise MailError("evidence_budget_exceeded")
            return SourcePage(query_id=row["query_id"], sources=[evidence])
        with self.store.connect() as db:
            if request.mail_id:
                source = db.execute("SELECT deleted_at FROM mail_items WHERE mail_id=? AND owner_id=?", (request.mail_id, access.owner_id)).fetchone()
            else:
                source = db.execute("SELECT deleted_at FROM documents JOIN mail_items USING(mail_id) WHERE document_id=? AND owner_id=?", (request.document_id, access.owner_id)).fetchone()
            if source is None:
                raise MailError("source_not_found")
            if source[0] is not None:
                raise MailError("source_removed")
        if request.cursor:
            run = self._run(access, request.cursor.rsplit(":", 1)[0], source=True)
            filters = json.loads(run["filters_json"])
            if (request.mail_id and filters.get("mail_ids") != [request.mail_id]) or (
                request.document_id and filters.get("document_ids") != [request.document_id]
            ):
                raise MailError("source_scope_mismatch")
        query = MailQuery(mode="enumerate", mail_ids=[request.mail_id] if request.mail_id else None,
                          document_ids=[request.document_id] if request.document_id else None, cursor=request.cursor, limit=20)
        page = self.search(access, query, page_chars=request.limit)
        return SourcePage(query_id=page.query_id, sources=page.matches, next_cursor=page.next_cursor)
