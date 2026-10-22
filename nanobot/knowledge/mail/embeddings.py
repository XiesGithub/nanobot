"""Pinned local BGE encoding and bounded float32 exact vector scan."""

from __future__ import annotations

import hashlib
import heapq
import json
from pathlib import Path
from threading import Lock

from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import json_text

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class LocalEncoder:
    def __init__(self, path: str):
        root = Path(path)
        manifest = root / "nanobot-model-revision.json"
        if not manifest.is_file():
            raise MailError("semantic_unavailable")
        pin = json.loads(manifest.read_text(encoding="utf-8"))
        if pin.get("model") != "BAAI/bge-small-zh-v1.5" or not pin.get("revision"):
            raise MailError("embedding_revision_invalid")
        import numpy as np
        from sentence_transformers import SentenceTransformer
        self.np = np
        self.model = SentenceTransformer(str(root.resolve()), device="cpu", local_files_only=True,
                                         trust_remote_code=False)
        self.model.max_seq_length = min(self.model.max_seq_length, self.model.tokenizer.model_max_length)
        self.dimension = self.model.get_sentence_embedding_dimension()
        # Include actual file contents; editing a local tokenizer changes the space.
        digest = hashlib.sha256(QUERY_INSTRUCTION.encode())
        for file in sorted(root.rglob("*")):
            if file.is_file() and ".cache" not in file.parts and file.suffix in {".json", ".txt", ".safetensors"}:
                digest.update(str(file.relative_to(root)).encode())
                with file.open("rb") as stream:
                    for data in iter(lambda: stream.read(1024**2), b""):
                        digest.update(data)
        self.revision = "bge-" + digest.hexdigest()
        self._lock = Lock()

    def encode(self, texts: list[str], *, query=False):
        encoded_text = [QUERY_INSTRUCTION + text if query else text for text in texts]
        if any(len(self.model.tokenizer.encode(text, truncation=False)) > self.model.max_seq_length for text in encoded_text):
            raise MailError("embedding_input_limit")
        with self._lock:
            vectors = self.model.encode(encoded_text, normalize_embeddings=True, convert_to_numpy=True,
                                        batch_size=32, show_progress_bar=False).astype("<f4")
        if vectors.shape != (len(texts), self.dimension) or not self.np.isfinite(vectors).all():
            raise MailError("embedding_invalid")
        if not self.np.allclose(self.np.linalg.norm(vectors, axis=1), 1, atol=1e-4):
            raise MailError("embedding_invalid")
        return vectors

    def index_pending(self, store, *, batch_size=32):
        """Encoding is outside DB transactions; publication rechecks active/deleted state."""
        with store.connect() as db:
            active = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
            current = db.execute("SELECT embedding_revision FROM index_manifests WHERE revision=?", (active,)).fetchone()
            if not current or current[0] not in (None, self.revision):
                raise MailError("embedding_revision_migration_required")
            rows = list(db.execute("""SELECT c.* FROM chunks c JOIN documents d USING(document_id)
                JOIN mail_items m USING(mail_id) LEFT JOIN chunk_vectors v
                ON v.chunk_id=c.chunk_id AND v.embedding_revision=?
                WHERE m.deleted_at IS NULL AND d.active_generation=c.generation AND v.chunk_id IS NULL
                AND c.chunking_revision=? LIMIT ?""",
                (self.revision, active, batch_size)))
        if not rows:
            return 0
        vectors = self.encode([row["text"] for row in rows])
        with store.lock, store.connect(write=True) as db:
            if db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] != active:
                return 0
            for row, vector in zip(rows, vectors):
                valid = db.execute("""SELECT 1 FROM chunks c JOIN documents d USING(document_id)
                    JOIN mail_items m USING(mail_id) WHERE c.chunk_id=? AND m.deleted_at IS NULL
                    AND d.active_generation=c.generation""", (row["chunk_id"],)).fetchone()
                if valid:
                    db.execute("INSERT OR REPLACE INTO chunk_vectors VALUES(?,?,?,?,1)",
                               (row["chunk_id"], self.revision, self.dimension, vector.tobytes()))
            db.execute("""UPDATE documents SET index_status='ready' WHERE active_generation>0 AND NOT EXISTS(
                SELECT 1 FROM chunks c LEFT JOIN chunk_vectors v ON v.chunk_id=c.chunk_id AND v.embedding_revision=?
                WHERE c.document_id=documents.document_id AND c.generation=documents.active_generation
                AND c.chunking_revision=? AND v.chunk_id IS NULL)""",
                (self.revision, active))
            db.execute("UPDATE index_manifests SET embedding_revision=? WHERE revision=(SELECT active_index_revision FROM schema_meta)", (self.revision,))
        return len(rows)

    def search(self, db, manifest, index_revision, question, *, limit):
        index = db.execute("SELECT embedding_revision FROM index_manifests WHERE revision=?", (index_revision,)).fetchone()
        if not index or index[0] != self.revision:
            raise MailError("semantic_unavailable")
        query = self.encode([question], query=True)[0]
        cursor = db.execute("""SELECT c.chunk_id,v.dimension,v.vector_blob FROM chunks c
            JOIN json_each(?) s ON c.document_id=s.key AND c.generation=s.value
            JOIN chunk_vectors v ON c.chunk_id=v.chunk_id
            WHERE c.chunking_revision=? AND v.embedding_revision=?""",
            (json_text(manifest), index_revision, self.revision))
        best = []
        while rows := cursor.fetchmany(512):
            if any(row["dimension"] != self.dimension or len(row["vector_blob"]) != self.dimension * 4 for row in rows):
                raise MailError("embedding_invalid")
            matrix = self.np.vstack([self.np.frombuffer(row["vector_blob"], dtype="<f4") for row in rows])
            if not self.np.isfinite(matrix).all():
                raise MailError("embedding_invalid")
            for row, score in zip(rows, matrix @ query):
                item = (float(score), row["chunk_id"])
                if len(best) < limit:
                    heapq.heappush(best, item)
                elif item > best[0]:
                    heapq.heapreplace(best, item)
        return [cid for _, cid in sorted(best, reverse=True)]
