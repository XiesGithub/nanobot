"""Vector store wrapper around ChromaDB."""

from __future__ import annotations

import time
from pathlib import Path

import chromadb


class VectorStore:
    """Persistent ChromaDB collection for memory chunk retrieval.

    Stores pre-computed embeddings (from ``EmbeddingProvider``) and provides
    cosine-similarity search at query time.
    """

    _COLLECTION_NAME = "memory_chunks"
    _MTIME_KEY = "indexed_mtime"

    def __init__(self, persist_path: Path) -> None:
        persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(persist_path),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self._COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    # -- write ----------------------------------------------------------------

    def add(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict] | None = None,
    ) -> None:
        """Add documents with pre-computed embeddings."""
        if not ids:
            return
        self._collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def clear(self) -> None:
        """Remove all documents from the collection."""
        self._client.delete_collection(self._COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=self._COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    # -- read -----------------------------------------------------------------

    def query(
        self, query_embedding: list[float], top_k: int = 5
    ) -> list[dict]:
        """Retrieve top-K most similar documents.

        Returns a list of dicts with keys: ``id``, ``document``, ``metadata``,
        ``distance``.
        """
        if self.count == 0:
            return []
        result = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.count),
            include=["documents", "metadatas", "distances"],
        )
        hits: list[dict] = []
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for i in range(len(ids)):
            hits.append({
                "id": ids[i],
                "document": documents[i] if documents else "",
                "metadata": metadatas[i] if metadatas else {},
                "distance": distances[i] if distances else 0.0,
            })
        return hits

    @property
    def count(self) -> int:
        return self._collection.count()

    # -- mtime tracking -------------------------------------------------------

    def get_index_mtime(self) -> float | None:
        """Return the mtime of MEMORY.md at the time of the last index."""
        md = self._collection.metadata or {}
        raw = md.get(self._MTIME_KEY)
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None
        return None

    def set_index_mtime(self, mtime: float) -> None:
        existing = {k: v for k, v in (self._collection.metadata or {}).items()
                    if k != "hnsw:space"}
        existing[self._MTIME_KEY] = str(mtime)
        self._collection.modify(metadata=existing)
