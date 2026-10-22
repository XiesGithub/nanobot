"""Offline mathematical proof of a pinned, genuinely called local encoder.

This verifier never loads model weights into a model, generates vectors or calls
a provider. Vectors must come from the trusted worker's actual encode/index/search
observations. Synthetic vectors used in tests are rejection tests, not capability
evidence.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import numpy as np

from evals.common import ROOT, digest, inside, read


def _points(evidence, kind):
    return [event for event in evidence.get("events", [])
            if event.get("type") == kind and event.get("phase") == "point"]


def _weights(pin):
    """Validate local files and reconstruct LocalEncoder's content-derived space."""
    if pin != read(ROOT / "evals/mail_semantic_encoder.json"):
        return False
    root = inside(ROOT, pin["path"])
    files = {path.relative_to(root).as_posix(): path for path in root.rglob("*")
             if path.is_file() and ".cache" not in path.parts
             and path.suffix in {".json", ".txt", ".safetensors"}}
    if set(files) != set(pin["files"]):
        return False
    revision = hashlib.sha256(pin["query_instruction"].encode("utf-8"))
    for name, path in sorted(files.items()):
        file_hash = hashlib.sha256()
        # Match LocalEncoder's platform-specific relative path bytes exactly.
        revision.update(str(Path(name)).encode("utf-8"))
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(1024**2), b""):
                file_hash.update(data)
                revision.update(data)
        if file_hash.hexdigest() != pin["files"][name]:
            return False
    return "bge-" + revision.hexdigest() == pin["embedding_revision"]


def _vector(blob_hex, dimension):
    if not isinstance(blob_hex, str):
        raise ValueError("missing_vector")
    blob = bytes.fromhex(blob_hex)
    if len(blob) != dimension * 4:
        raise ValueError("vector_dimension")
    vector = np.frombuffer(blob, dtype="<f4")
    if not np.isfinite(vector).all() or not np.allclose(np.linalg.norm(vector), 1, atol=1e-4):
        raise ValueError("vector_not_normalized_or_finite")
    return vector, digest(blob)


def _encoder(state, pin):
    encoder = state.get("encoder", {})
    expected = {"available": True, "model": pin["model"], "pin_revision": pin["revision"],
                "revision": pin["embedding_revision"], "dimension": pin["dimension"],
                "device": pin["device"],
                "threads": pin["threads"], "package_versions": pin["package_versions"]}
    frozen = {key: pin[key] for key in
              ("path", "model", "revision", "embedding_revision", "dimension", "files")}
    return (all(encoder.get(key) == value for key, value in expected.items())
            and state.get("pin") == frozen and encoder.get("load_error") is None
            and encoder.get("pin_error") is None)


def _indexed(state, pin):
    chunks = state.get("chunks", [])
    vectors = state.get("vectors", [])
    index = state.get("index_manifest") or {}
    revision = index.get("revision")
    if (not chunks or len({c["chunk_id"] for c in chunks}) != len(chunks)
            or len({v["chunk_id"] for v in vectors}) != len(vectors)
            or {c["chunk_id"] for c in chunks} != {v["chunk_id"] for v in vectors}
            or index.get("embedding_revision") != pin["embedding_revision"]
            or index.get("status") != "ready" or index.get("chunking_revision") != revision):
        raise ValueError("index_space_or_inventory")
    by_chunk = {v["chunk_id"]: v for v in vectors}
    decoded = {}
    for chunk in chunks:
        stored = by_chunk[chunk["chunk_id"]]
        vector, sha = _vector(stored.get("blob_hex"), pin["dimension"])
        if (stored.get("blob_sha256") != sha or stored.get("normalized") != 1
                or stored.get("dimension") != pin["dimension"]
                or stored.get("embedding_revision") != pin["embedding_revision"]
                or chunk.get("chunking_revision") != revision
                or chunk.get("text_sha256") != digest(chunk["text"].encode("utf-8"))
                or chunk.get("stored_text_hash") != chunk.get("text_sha256")):
            raise ValueError("indexed_vector_binding")
        decoded[chunk["chunk_id"]] = vector
    return decoded


def _encodes(events, pin):
    index_rows, query_rows = [], []
    for event in events:
        payload = event["payload"]
        texts = payload.get("texts", [])
        hashes = payload.get("text_sha256", [])
        vectors = payload.get("vectors_hex", [])
        query = payload.get("query")
        if (payload.get("revision") != pin["embedding_revision"]
                or payload.get("dimension") != pin["dimension"]
                or payload.get("device") != pin["device"] or payload.get("threads") != pin["threads"]
                or type(query) is not bool or payload.get("role") != ("query" if query else "index")
                or not texts or len(texts) != len(hashes) or len(texts) != len(vectors)):
            raise ValueError("encode_identity")
        for text, sha, blob in zip(texts, hashes, vectors, strict=True):
            if not isinstance(text, str) or sha != digest(text.encode("utf-8")):
                raise ValueError("encoded_text_hash")
            _vector(blob, pin["dimension"])
            row = (text, sha, blob, event)
            (query_rows if query else index_rows).append(row)
    return index_rows, query_rows


def semantic_binding(case, evidence):
    """Return hard checks plus Hit@8 without deriving anything from answer quality."""
    checks = {name: False for name in (
        "semantic_environment", "pinned_weights", "encoder_ready", "index_vectors", "index_unchanged",
        "actual_document_encoding", "actual_query_encoding", "rank_replay",
        "hybrid_search_ready", "rank_search_binding", "fixture_chunk_binding",
        "synonym_filter_safe", "gold_hit_at_8", "gold_source_read_and_cited")}
    metrics = {"hit_at_8": None, "semantic_only_hit_at_8": None,
               "gold_mail_count": None, "rankings": [], "errors": []}
    try:
        pin = case["encoder_pin"]
        checks["pinned_weights"] = _weights(pin)
        if (pin.get("dtype") != "<f4" or pin.get("dimension") != 512
                or pin.get("device") != "cpu"):
            raise ValueError("unsupported_embedding_profile")
        state_events = _points(evidence, "semantic_state")
        before = [e["payload"] for e in state_events if e["payload"].get("moment") == "before"]
        after = [e["payload"] for e in state_events if e["payload"].get("moment") == "after"]
        if len(before) != 1 or len(after) != 1:
            raise ValueError("semantic_state_missing")
        before, after = before[0], after[0]
        loads = _points(evidence, "semantic_encoder_load")
        loaded = {"status": "ready", "operation": "LocalEncoder.__init__", "path_relative": pin["path"],
                  "model": pin["model"], "revision": pin["embedding_revision"],
                  "dimension": pin["dimension"], "device": pin["device"], "threads": pin["threads"]}
        checks["encoder_ready"] = (len(loads) == 1 and loads[0]["payload"] == loaded
                                   and all(_encoder(s, pin)
                                      and s.get("health", {}).get("semantic_status") == "ready"
                                      and s.get("health", {}).get("index_pending_documents") == 0
                                      for s in (before, after)))
        matrix = _indexed(before, pin)
        _indexed(after, pin)
        checks["index_vectors"] = True
        checks["index_unchanged"] = all(before.get(key) == after.get(key)
                                        for key in ("encoder", "index_manifest", "chunks", "vectors"))
        checks["semantic_environment"] = all(checks[name] for name in (
            "pinned_weights", "encoder_ready", "index_vectors", "index_unchanged"))
        index_rows, query_rows = _encodes(_points(evidence, "semantic_encode"), pin)
        vector_rows = {row["chunk_id"]: row for row in before["vectors"]}
        chunks = {row["chunk_id"]: row for row in before["chunks"]}
        checks["actual_document_encoding"] = all(
            any(text == chunk["text"] and sha == chunk["text_sha256"]
                and blob == vector_rows[cid]["blob_hex"] for text, sha, blob, _ in index_rows)
            for cid, chunk in chunks.items())
        mail_states = _points(evidence, "mail_state")
        sources = next((e["payload"].get("sources", []) for e in mail_states
                        if e["payload"].get("moment") == "before"), [])
        documents = next((e["payload"].get("state", {}).get("documents", []) for e in mail_states
                          if e["payload"].get("moment") == "before"), [])
        active_documents = {row["document_id"]: row for row in documents}
        by_document = {row["document_id"]: row for row in sources}
        checks["fixture_chunk_binding"] = bool(sources) and all(
            chunk["document_id"] in by_document
            and chunk["text"] in by_document[chunk["document_id"]]["text"]
            and chunk["document_id"] in active_documents
            and chunk["generation"] == active_documents[chunk["document_id"]]["active_generation"]
            and chunk.get("uid") == by_document[chunk["document_id"]]["uid"]
            and chunk.get("part_id") == by_document[chunk["document_id"]]["part_id"]
            for chunk in chunks.values())
        checks["semantic_environment"] = checks["semantic_environment"] and checks["fixture_chunk_binding"]
        ranks = _points(evidence, "semantic_rank")
        searches = _points(evidence, "mail_search")
        checks["hybrid_search_ready"] = bool(searches) and all(
            e["payload"].get("status") == "ok"
            and e["payload"].get("request", {}).get("mode") == "hybrid"
            and e["payload"].get("request", {}).get("query")
            and (e["payload"].get("result") or {}).get("semantic_status") == "ready"
            for e in searches)
        replay_ok = encode_ok = bool(ranks)
        for event in ranks:
            payload = event["payload"]
            query = payload["query"]
            query_encode = payload.get("query_encode") or {}
            query_hash = digest(query.encode("utf-8"))
            query_vector, _ = _vector(query_encode.get("vector_hex"), pin["dimension"])
            encode_ok = encode_ok and query_encode.get("text_sha256") == query_hash and any(
                text == query and sha == query_hash and blob == query_encode["vector_hex"]
                for text, sha, blob, _ in query_rows)
            manifest = payload["manifest"]
            candidates = [cid for cid, chunk in chunks.items()
                          if manifest.get(chunk["document_id"]) == chunk["generation"]
                          and chunk["chunking_revision"] == payload["index_revision"]]
            if (payload.get("status") != "ok" or not candidates
                    or not manifest or any(doc not in active_documents
                                           or generation != active_documents[doc]["active_generation"]
                                           for doc, generation in manifest.items())
                    or payload["index_revision"] != before["index_manifest"]["revision"]
                    or payload["embedding_revision"] != pin["embedding_revision"]
                    or type(payload.get("limit")) is not int or not 1 <= payload["limit"] <= 50):
                replay_ok = False
                continue
            values = np.vstack([matrix[cid] for cid in candidates]) @ query_vector
            ordered = [cid for _, cid in sorted(zip(map(float, values), candidates), reverse=True)]
            ordered = ordered[:payload["limit"]]
            expected_results = [{"rank": i, "chunk_id": cid, "text": chunks[cid]["text"]}
                                for i, cid in enumerate(ordered, 1)]
            replay_ok = replay_ok and payload.get("returned_chunk_ids") == ordered and payload.get("results") == expected_results
            metrics["rankings"].append({"query": query, "chunk_ids": ordered,
                                        "scores": [{"chunk_id": cid, "score": float(value)}
                                                   for cid, value in zip(candidates, values, strict=True)]})
        checks["actual_query_encoding"], checks["rank_replay"] = encode_ok, replay_ok
        rank_queries = Counter(e["payload"].get("query") for e in ranks)
        search_queries = Counter(e["payload"].get("request", {}).get("query") for e in searches)
        checks["rank_search_binding"] = bool(ranks) and rank_queries == search_queries and all(
            any(rank["payload"].get("query") == e["payload"]["request"]["query"]
                and all(item.get("chunk_id") in rank["payload"].get("returned_chunk_ids", [])
                        and rank["payload"].get("manifest", {}).get(item.get("document_id")) == item.get("generation")
                        for item in e["payload"]["result"]["matches"])
                for rank in ranks) for e in searches)
        is_v085 = case.get("case_id") == "V085"
        checks["synonym_filter_safe"] = checks["gold_hit_at_8"] = checks["gold_source_read_and_cited"] = not is_v085
        if is_v085:
            gold_sources = [row for row in sources if row.get("uid") == 1 and row.get("kind") == "body"]
            gold_docs = {row["document_id"] for row in gold_sources}
            gold_text = "\n".join(row["text"] for row in gold_sources)
            checks["synonym_filter_safe"] = all(
                all(term in gold_text or term in case["mail_fixture"]["mails"][0]["subject"]
                    for term in e["payload"]["request"].get("exact_terms", [])) for e in searches)
            gold_matches = [item for e in searches for item in e["payload"]["result"]["matches"][:8]
                            if item.get("document_id") in gold_docs]
            checks["gold_hit_at_8"] = bool(gold_docs and gold_matches)
            metrics["hit_at_8"] = int(checks["gold_hit_at_8"])
            metrics["semantic_only_hit_at_8"] = int(any(
                chunks[cid]["document_id"] in gold_docs
                for rank in metrics["rankings"] for cid in rank["chunk_ids"][:8]))
            metrics["gold_mail_count"] = len({s["mail_id"] for s in gold_sources})
            deliveries = _points(evidence, "delivery")
            citations = deliveries[0]["payload"].get("metadata", {}).get("citations", []) if len(deliveries) == 1 else []
            reads = [item for e in _points(evidence, "mail_read")
                     if e["payload"].get("status") == "ok"
                     for item in (e["payload"].get("result") or {}).get("sources", [])]
            checks["gold_source_read_and_cited"] = any(item in reads and item in citations for item in gold_matches)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        metrics["errors"].append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    return {"checks": checks, "metrics": metrics}
