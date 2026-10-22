"""Shadow index publication and query-retention-aware collection under one lock."""

from __future__ import annotations

import json
import time
from contextlib import nullcontext

from nanobot.knowledge.mail.chunking import CHUNKING_REVISION, chunks, terms
from nanobot.knowledge.mail.contracts import MailError, TextBlock
from nanobot.knowledge.mail.store import MailStore, identifier, json_text


def active_documents(db):
    return {row["document_id"]: (row["active_generation"], row["revision"]) for row in db.execute(
        """SELECT document_id,active_generation,revision FROM documents d JOIN mail_items m USING(mail_id)
        WHERE m.deleted_at IS NULL AND d.active_generation>0""")}


def rebuild(store: MailStore, encoder=None, *, before_switch=None, document_ids=None,
            publication_guard=nullcontext) -> str:
    """Build alongside the old index. Concurrent content changes reject the switch.

    A retry rebuilds the current active set. Neither missing raw inputs nor new
    model revisions cause implicit server downloads.
    """
    revision = identifier("index_")
    with store.lock, store.connect(write=True) as db:
        expected = active_documents(db)
        if document_ids is not None and not set(document_ids) <= set(expected):
            raise MailError("retained_text_missing")
        previous = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
        db.execute("INSERT INTO index_manifests VALUES(?,?,?,'building',?)", (
            revision, encoder.revision if encoder else None, CHUNKING_REVISION,
            json_text({"documents": expected}),
        ))
    try:
        for document_id, (generation, _) in expected.items():
            with store.connect() as db:
                doc = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
                if doc is None:
                    raise MailError("revision_conflict")
                mail = db.execute("SELECT subject,sender_address,sender_name FROM mail_items WHERE mail_id=?", (doc["mail_id"],)).fetchone()
                blocks = [TextBlock(ordinal=r["ordinal"], text=r["text"], locator=json.loads(r["locator_json"]),
                                    content_role=r["content_role"], role_basis=r["role_basis"])
                          for r in db.execute("SELECT * FROM document_blocks WHERE document_id=? AND generation=? ORDER BY ordinal", (document_id, generation))]
                header = terms(" ".join([*mail, doc["filename"]]))
                if document_ids is not None and document_id not in document_ids:
                    prepared = [{"ordinal": r["ordinal"], "text": r["text"],
                                 "spans": json.loads(r["block_spans_json"]), "text_hash": r["text_hash"],
                                 "content_role": r["content_role"], "role_basis": r["role_basis"]}
                                for r in db.execute("SELECT * FROM chunks WHERE document_id=? AND generation=? AND chunking_revision=? ORDER BY ordinal", (document_id, generation, previous))]
                else:
                    prepared = chunks(blocks)
            batch = []
            for chunk in prepared:
                batch.append(chunk)
                if len(batch) >= 32:
                    _insert_batch(store, revision, document_id, generation, header, batch, encoder)
                    batch = []
            if batch:
                _insert_batch(store, revision, document_id, generation, header, batch, encoder)
        if before_switch:
            before_switch()
        with publication_guard(), store.lock, store.connect(write=True) as db:
            if active_documents(db) != expected or db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] != previous:
                raise MailError("revision_conflict")
            represented = {r[0] for r in db.execute("SELECT DISTINCT document_id FROM chunks WHERE chunking_revision=?", (revision,))}
            if represented != set(expected):
                raise MailError("index_coverage_incomplete")
            db.execute("UPDATE index_manifests SET status='ready' WHERE revision=?", (revision,))
            db.execute("UPDATE schema_meta SET active_index_revision=?", (revision,))
        return revision
    except BaseException:
        with store.lock, store.connect(write=True) as db:
            db.execute("UPDATE index_manifests SET status='failed' WHERE revision=?", (revision,))
        raise


def _insert_batch(store, revision, document_id, generation, header, batch, encoder):
    vectors = encoder.encode([chunk["text"] for chunk in batch]) if encoder else None
    with store.lock, store.connect(write=True) as db:
        if not db.execute("SELECT 1 FROM documents d JOIN mail_items m USING(mail_id) WHERE document_id=? AND m.deleted_at IS NULL", (document_id,)).fetchone():
            raise MailError("source_removed")
        for i, chunk in enumerate(batch):
            chunk_id = identifier("chunk_")
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                chunk_id, document_id, generation, revision, chunk["ordinal"], chunk["text"],
                json_text(chunk["spans"]), len(chunk["text"].encode()), chunk["text_hash"],
                chunk["content_role"], chunk["role_basis"],
            ))
            db.execute("INSERT INTO chunk_fts VALUES(?,?,?)", (chunk_id, terms(chunk["text"]), header))
            if vectors is not None:
                db.execute("INSERT INTO chunk_vectors VALUES(?,?,?,?,1)",
                           (chunk_id, encoder.revision, encoder.dimension, vectors[i].tobytes()))


def collect(store: MailStore) -> dict[str, int]:
    """No collection if query state is unavailable; deletion always overrides retention."""
    with store.lock:
        try:
            with store.connect(write=True, query_state=True) as state:
                state.execute("DELETE FROM query_runs WHERE expires_at<=?", (time.time(),))
                snapshots = [json.loads(r[0]) for r in state.execute("SELECT snapshot_json FROM query_runs")]
                state.execute("DELETE FROM mail_focus WHERE expires_at<=?", (time.time(),))
        except Exception:
            raise MailError("query_state_unavailable") from None
        with store.connect(write=True) as db:
            active = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
            keep = {(did, generation, active) for did, (generation, _) in active_documents(db).items()}
            for snapshot in snapshots:
                keep.update((did, generation, snapshot["index_revision"]) for did, generation in snapshot["documents"].items())
            # A maintenance build not yet switched must not be collected.
            building = {r[0] for r in db.execute("SELECT revision FROM index_manifests WHERE status='building'")}
            db.execute("CREATE TEMP TABLE keep_version(document_id TEXT,generation INTEGER,revision TEXT,PRIMARY KEY(document_id,generation,revision))")
            db.executemany("INSERT OR IGNORE INTO keep_version VALUES(?,?,?)", keep)
            predicate = """NOT EXISTS(SELECT 1 FROM keep_version k WHERE k.document_id=chunks.document_id
                AND k.generation=chunks.generation AND k.revision=chunks.chunking_revision)
                AND chunking_revision NOT IN (SELECT value FROM json_each(?))"""
            args = (json_text(list(building)),)
            db.execute("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE " + predicate + ")", args)
            removed_chunks = db.execute("DELETE FROM chunks WHERE " + predicate, args).rowcount
            # Blocks/coverage live as long as any active or retained chunk generation.
            removed_blocks = db.execute("""DELETE FROM document_blocks WHERE NOT EXISTS(
                SELECT 1 FROM chunks c WHERE c.document_id=document_blocks.document_id AND c.generation=document_blocks.generation)""").rowcount
            db.execute("""DELETE FROM document_versions WHERE NOT EXISTS(
                SELECT 1 FROM chunks c WHERE c.document_id=document_versions.document_id AND c.generation=document_versions.generation)""")
            retained_indexes = {k[2] for k in keep} | building | {active}
            db.execute("DELETE FROM index_manifests WHERE revision NOT IN (SELECT value FROM json_each(?))", (json_text(list(retained_indexes)),))
        return {"chunks": removed_chunks, "blocks": removed_blocks}
