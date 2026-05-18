"""Embedding provider using OpenAI-compatible embedding API.

Reuses the same ``api_key`` / ``api_base`` credentials from the configured chat
provider so users don't need a separate provider config for embeddings.
"""

from __future__ import annotations

from openai import AsyncOpenAI, OpenAI

_MAX_BATCH_SIZE = 2048  # OpenAI embedding API limit per request


class EmbeddingProvider:
    """Thin wrapper around the OpenAI embedding API.

    Supports both sync (for context-building) and async (for batch re-indexing).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        model: str = "text-embedding-3-small",
    ) -> None:
        base_url = (api_base.rstrip("/") + "/v1") if api_base else None
        self._model = model
        self._sync_client = OpenAI(api_key=api_key, base_url=base_url or None)
        self._async_client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    @property
    def model(self) -> str:
        return self._model

    # -- sync interface (used during context building) -------------------------

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts synchronously."""
        if not texts:
            return []
        result = self._sync_client.embeddings.create(model=self._model, input=texts)
        # Sort by index to preserve input order
        sorted_data = sorted(result.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]

    def embed_query_sync(self, text: str) -> list[float]:
        """Embed a single query text synchronously."""
        embeddings = self.embed_sync([text])
        return embeddings[0] if embeddings else []

    # -- async interface (used during re-indexing) ----------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts asynchronously."""
        if not texts:
            return []
        result = await self._async_client.embeddings.create(
            model=self._model, input=texts
        )
        sorted_data = sorted(result.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query text asynchronously."""
        embeddings = await self.embed([text])
        return embeddings[0] if embeddings else []
