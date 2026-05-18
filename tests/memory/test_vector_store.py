"""Unit tests for VectorStore (ChromaDB wrapper)."""

import tempfile
from pathlib import Path

import pytest
from nanobot.memory.vector_store import VectorStore


@pytest.fixture
def persist_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def vector_store(persist_dir):
    return VectorStore(persist_dir)


class TestVectorStore:
    def test_empty_store_count(self, vector_store):
        assert vector_store.count == 0

    def test_add_and_count(self, vector_store):
        vector_store.add(
            ids=["id1"],
            documents=["test document"],
            embeddings=[[0.1, 0.2, 0.3]],
        )
        assert vector_store.count == 1

    def test_query_returns_results(self, vector_store):
        vector_store.add(
            ids=["id1", "id2"],
            documents=["first doc", "second doc"],
            embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        )
        results = vector_store.query([1.0, 0.0, 0.0], top_k=1)
        assert len(results) == 1
        assert results[0]["document"] == "first doc"

    def test_query_respects_top_k(self, vector_store):
        vector_store.add(
            ids=["a", "b", "c"],
            documents=["doc a", "doc b", "doc c"],
            embeddings=[[1.0, 0.0], [0.9, 0.1], [0.5, 0.5]],
        )
        results = vector_store.query([1.0, 0.0], top_k=2)
        assert len(results) == 2

    def test_query_empty_store(self, vector_store):
        results = vector_store.query([0.1, 0.2])
        assert results == []

    def test_clear(self, vector_store):
        vector_store.add(
            ids=["x"],
            documents=["text"],
            embeddings=[[1.0, 2.0]],
        )
        assert vector_store.count == 1
        vector_store.clear()
        assert vector_store.count == 0

    def test_clear_persists(self, vector_store):
        vector_store.add(
            ids=["x"],
            documents=["text"],
            embeddings=[[1.0, 2.0]],
        )
        vector_store.clear()
        vector_store.add(
            ids=["y"],
            documents=["new text"],
            embeddings=[[3.0, 4.0]],
        )
        assert vector_store.count == 1

    def test_index_mtime_roundtrip(self, vector_store):
        assert vector_store.get_index_mtime() is None
        vector_store.set_index_mtime(1234567890.5)
        assert vector_store.get_index_mtime() == 1234567890.5

    def test_index_mtime_survives_operations(self, vector_store):
        vector_store.set_index_mtime(999.0)
        vector_store.add(
            ids=["a"],
            documents=["doc"],
            embeddings=[[1.0, 2.0]],
        )
        assert vector_store.get_index_mtime() == 999.0

    def test_metadata_on_results(self, vector_store):
        vector_store.add(
            ids=["m1"],
            documents=["data"],
            embeddings=[[0.5, 0.5]],
            metadatas=[{"heading": "Test", "position": 0}],
        )
        results = vector_store.query([0.5, 0.5], top_k=1)
        assert results[0]["metadata"]["heading"] == "Test"
        assert results[0]["metadata"]["position"] == 0

    def test_persistence_across_instances(self, persist_dir):
        vs1 = VectorStore(persist_dir)
        vs1.add(
            ids=["p1"],
            documents=["persistent"],
            embeddings=[[7.0, 8.0]],
        )
        del vs1
        vs2 = VectorStore(persist_dir)
        assert vs2.count == 1
        results = vs2.query([7.0, 8.0], top_k=1)
        assert results[0]["document"] == "persistent"
