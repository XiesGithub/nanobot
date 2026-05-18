"""Memory chunking and indexing logic.

Splits MEMORY.md into semantic chunks (by markdown headings, then paragraphs,
then sentences) and orchestrates embedding + vector-store insertion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tiktoken

from nanobot.memory.embedding import EmbeddingProvider
from nanobot.memory.vector_store import VectorStore

_ENC = tiktoken.get_encoding("cl100k_base")


@dataclass
class MemoryChunk:
    chunk_id: str
    text: str
    heading: str = ""
    position: int = 0
    token_count: int = 0


def _token_count(text: str) -> int:
    return len(_ENC.encode(text))


def chunk_memory(
    content: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[MemoryChunk]:
    """Split *content* into semantic chunks.

    Strategy:
    1. Split by ``## `` heading boundaries first.
    2. Sections exceeding *chunk_size* tokens are split by paragraph boundaries.
    3. Paragraphs still exceeding *chunk_size* are split by sentence boundaries.
    4. Each chunk carries *chunk_overlap* tokens from the previous chunk.
    """
    if not content.strip():
        return []

    sections = _split_by_headings(content)
    chunks: list[MemoryChunk] = []
    position = 0

    for heading, body in sections:
        body = body.strip()
        if not body:
            continue
        if _token_count(body) <= chunk_size:
            chunks.append(MemoryChunk(
                chunk_id=f"mem_{position:04d}",
                text=_full_text(heading, body),
                heading=heading,
                position=position,
                token_count=_token_count(body),
            ))
            position += 1
        else:
            sub_chunks = _split_paragraphs(
                heading, body, position, chunk_size, chunk_overlap
            )
            chunks.extend(sub_chunks)
            position += len(sub_chunks)

    return chunks


def _full_text(heading: str, body: str) -> str:
    if heading:
        return f"## {heading}\n\n{body}"
    return body


def _split_by_headings(content: str) -> list[tuple[str, str]]:
    """Split content by ``## `` heading boundaries.

    Returns list of ``(heading, body)`` tuples. The first tuple may have an
    empty heading (text before any ``## `` heading).
    """
    parts = re.split(r"^## (.+)$", content, flags=re.MULTILINE)
    sections: list[tuple[str, str]] = []
    i = 0
    if parts[0].strip():
        sections.append(("", parts[0]))
        i = 1
    else:
        i = 1  # skip empty leading text
    while i + 1 < len(parts):
        heading = parts[i].strip()
        body = parts[i + 1]
        sections.append((heading, body))
        i += 2
    return sections


def _split_paragraphs(
    heading: str,
    body: str,
    start_pos: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[MemoryChunk]:
    """Split a section body by paragraph boundaries."""
    paragraphs = re.split(r"\n\n+", body.strip())
    chunks: list[MemoryChunk] = []
    current_chunks: list[str] = []
    current_tokens = 0
    prev_tail = ""

    def _flush() -> None:
        nonlocal current_chunks, current_tokens, prev_tail
        if not current_chunks:
            return
        text = "\n\n".join(current_chunks)
        full = _full_text(heading, prev_tail + "\n\n" + text if prev_tail else text)
        chunks.append(MemoryChunk(
            chunk_id=f"mem_{start_pos + len(chunks):04d}",
            text=full,
            heading=heading,
            position=start_pos + len(chunks),
            token_count=_token_count(full),
        ))
        if chunk_overlap > 0 and len(text) > chunk_overlap:
            prev_tail = text[-chunk_overlap:]
        else:
            prev_tail = text
        current_chunks = []
        current_tokens = 0

    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        pt = _token_count(p)
        if pt > chunk_size:
            _flush()
            sub = _split_sentences(heading, p, start_pos + len(chunks),
                                   chunk_size, chunk_overlap)
            chunks.extend(sub)
            if sub:
                prev_tail = sub[-1].text[-chunk_overlap:] if chunk_overlap else ""
            continue
        if current_tokens + pt > chunk_size and current_chunks:
            _flush()
        current_chunks.append(p)
        current_tokens += pt

    _flush()
    return chunks


def _split_sentences(
    heading: str,
    text: str,
    start_pos: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[MemoryChunk]:
    """Split a long paragraph by sentence boundaries."""
    sentences = re.split(r"(?<=[.。!?！？])\s+", text.strip())
    if len(sentences) <= 1:
        # Can't split further, just make oversized chunks by character count
        return _split_by_chars(heading, text, start_pos, chunk_size, chunk_overlap)

    chunks: list[MemoryChunk] = []
    current: list[str] = []
    current_tokens = 0

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        st = _token_count(s)
        if current_tokens + st > chunk_size and current:
            body = " ".join(current)
            chunks.append(MemoryChunk(
                chunk_id=f"mem_{start_pos + len(chunks):04d}",
                text=_full_text(heading, body),
                heading=heading,
                position=start_pos + len(chunks),
                token_count=_token_count(body),
            ))
            if chunk_overlap > 0:
                overlap_text = " ".join(current)[-chunk_overlap:]
                current = [overlap_text] if overlap_text.strip() else []
                current_tokens = _token_count(overlap_text) if current else 0
            else:
                current = []
                current_tokens = 0
        current.append(s)
        current_tokens += st

    if current:
        body = " ".join(current)
        chunks.append(MemoryChunk(
            chunk_id=f"mem_{start_pos + len(chunks):04d}",
            text=_full_text(heading, body),
            heading=heading,
            position=start_pos + len(chunks),
            token_count=_token_count(body),
        ))

    return chunks


def _split_by_chars(
    heading: str,
    text: str,
    start_pos: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[MemoryChunk]:
    """Last-resort split by character count (approx 4 chars/token)."""
    chars_per_chunk = chunk_size * 4
    chunks: list[MemoryChunk] = []
    step = max(chars_per_chunk - chunk_overlap * 4, 100)
    for i in range(0, len(text), step):
        segment = text[i:i + chars_per_chunk]
        chunks.append(MemoryChunk(
            chunk_id=f"mem_{start_pos + len(chunks):04d}",
            text=_full_text(heading, segment),
            heading=heading,
            position=start_pos + len(chunks),
            token_count=_token_count(segment),
        ))
    return chunks


async def index_memory(
    memory_content: str,
    embedder: EmbeddingProvider,
    vector_store: VectorStore,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> int:
    """Chunk, embed, and store *memory_content* into *vector_store*.

    Returns the number of chunks indexed.
    """
    import time

    chunks = chunk_memory(memory_content, chunk_size, chunk_overlap)
    if not chunks:
        vector_store.clear()
        return 0

    texts = [c.text for c in chunks]
    embeddings = await embedder.embed(texts)

    vector_store.clear()
    vector_store.add(
        ids=[c.chunk_id for c in chunks],
        documents=texts,
        embeddings=embeddings,
        metadatas=[
            {"heading": c.heading, "position": c.position} for c in chunks
        ],
    )
    return len(chunks)
