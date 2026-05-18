"""Unit tests for memory chunking logic."""

import pytest
from nanobot.memory.indexer import MemoryChunk, chunk_memory


class TestChunkMemory:
    def test_empty_content(self):
        assert chunk_memory("") == []
        assert chunk_memory("   \n\n  ") == []

    def test_single_short_section(self):
        chunks = chunk_memory("## Preferences\nUser likes dark mode.", chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0].heading == "Preferences"
        assert "dark mode" in chunks[0].text

    def test_multi_section(self):
        content = "## Foo\nThis is foo.\n\n## Bar\nThis is bar."
        chunks = chunk_memory(content, chunk_size=500)
        assert len(chunks) == 2
        assert chunks[0].heading == "Foo"
        assert chunks[1].heading == "Bar"

    def test_preamble_text_before_headings(self):
        content = "Preamble text here.\n\n## Section A\nContent A."
        chunks = chunk_memory(content, chunk_size=500)
        assert len(chunks) == 2
        assert chunks[0].heading == ""
        assert "Preamble" in chunks[0].text

    def test_chunk_positions_are_sequential(self):
        content = "## A\nA\n\n## B\nB\n\n## C\nC"
        chunks = chunk_memory(content, chunk_size=500)
        positions = [c.position for c in chunks]
        assert positions == list(range(len(chunks)))

    def test_chunk_ids_are_unique(self):
        content = "## A\nA\n\n## B\nB"
        chunks = chunk_memory(content, chunk_size=500)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_large_section_split_by_paragraphs(self):
        # Create many paragraphs exceeding chunk_size
        paras = ["Paragraph number {} with some content to fill space.".format(i)
                 for i in range(50)]
        content = "## Big\n\n" + "\n\n".join(paras)
        chunks = chunk_memory(content, chunk_size=100, chunk_overlap=0)
        # Should be split across multiple chunks
        assert len(chunks) > 1
        for c in chunks:
            assert c.heading == "Big"

    def test_chunk_overlap(self):
        content = "## Overlap\n\nFirst chunk content here. " * 20
        chunks = chunk_memory(content, chunk_size=50, chunk_overlap=20)
        assert len(chunks) > 1

    def test_token_count_populated(self):
        content = "## T\nTest content with enough tokens to count."
        chunks = chunk_memory(content, chunk_size=500)
        assert chunks[0].token_count > 0

    def test_heading_preserved_in_text(self):
        content = "## My Heading\nBody content here."
        chunks = chunk_memory(content, chunk_size=500)
        assert "## My Heading" in chunks[0].text

    def test_no_heading(self):
        content = "Just some plain text without any markdown headings."
        chunks = chunk_memory(content, chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0].heading == ""
