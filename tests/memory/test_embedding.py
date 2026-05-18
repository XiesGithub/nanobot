"""Unit tests for EmbeddingProvider (mocked OpenAI client)."""

from unittest.mock import MagicMock, patch

import pytest
from nanobot.memory.embedding import EmbeddingProvider


@pytest.fixture
def mock_openai():
    with patch("nanobot.memory.embedding.OpenAI") as mock_sync, \
         patch("nanobot.memory.embedding.AsyncOpenAI") as mock_async:
        # Set up sync client mock
        sync_client = MagicMock()
        mock_sync.return_value = sync_client
        # Set up async client mock
        mock_async.return_value = MagicMock()
        yield sync_client


class TestEmbeddingProvider:
    def test_construction_strips_api_base_slash(self):
        p = EmbeddingProvider(api_key="sk-test", api_base="https://api.example.com/")
        assert p.model == "text-embedding-3-small"

    def test_embed_sync_single_text(self, mock_openai):
        mock_openai.embeddings.create.return_value = MagicMock(
            data=[MagicMock(index=0, embedding=[0.1, 0.2, 0.3])]
        )
        p = EmbeddingProvider(api_key="sk-test")
        result = p.embed_sync(["hello world"])
        assert len(result) == 1
        assert result[0] == [0.1, 0.2, 0.3]

    def test_embed_sync_multiple_texts(self, mock_openai):
        mock_openai.embeddings.create.return_value = MagicMock(
            data=[
                MagicMock(index=0, embedding=[1.0, 0.0]),
                MagicMock(index=1, embedding=[0.0, 1.0]),
            ]
        )
        p = EmbeddingProvider(api_key="sk-test")
        result = p.embed_sync(["first", "second"])
        assert len(result) == 2

    def test_embed_sync_preserves_order(self, mock_openai):
        # Return out of order to verify sorting
        mock_openai.embeddings.create.return_value = MagicMock(
            data=[
                MagicMock(index=1, embedding=[0.0, 1.0]),
                MagicMock(index=0, embedding=[1.0, 0.0]),
            ]
        )
        p = EmbeddingProvider(api_key="sk-test")
        result = p.embed_sync(["a", "b"])
        assert result[0] == [1.0, 0.0]
        assert result[1] == [0.0, 1.0]

    def test_embed_sync_empty_list(self, mock_openai):
        p = EmbeddingProvider(api_key="sk-test")
        result = p.embed_sync([])
        assert result == []

    def test_embed_query_sync(self, mock_openai):
        mock_openai.embeddings.create.return_value = MagicMock(
            data=[MagicMock(index=0, embedding=[0.5, 0.5])]
        )
        p = EmbeddingProvider(api_key="sk-test")
        result = p.embed_query_sync("query")
        assert result == [0.5, 0.5]
