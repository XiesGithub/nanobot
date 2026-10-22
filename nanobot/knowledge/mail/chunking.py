"""Deterministic segmentation and FTS normalization without losing source offsets."""

import hashlib
import re
import unicodedata

from nanobot.knowledge.mail.contracts import TextBlock

CHUNKING_REVISION = "text-v1"


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def terms(text: str) -> str:
    normalized = normalize(text)
    tokens = re.findall(r"[a-z0-9]+(?:[-_@.][a-z0-9]+)*", normalized)
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(tokens)


def exact_match(term: str, text: str) -> bool:
    term, text = normalize(term), normalize(text)
    if re.search(r"[a-z0-9]", term):
        # PO-001 must not match PO-0010 or XPO-001; hyphens remain significant.
        return re.search(r"(?<![a-z0-9_@.-])" + re.escape(term) + r"(?![a-z0-9_@.-])", text) is not None
    return term in text


def chunks(blocks: list[TextBlock], *, max_bytes: int = 384, overlap_chars: int = 40):
    """Bound UTF-8 size before encoding; the encoder additionally checks its tokenizer.

    Retrieval text contains no synthesized headers; metadata is indexed separately.
    Chunks never cross a role boundary. Very long single lines retain every tail byte.
    """
    if max_bytes < 4 or overlap_chars < 0:
        raise ValueError("Invalid chunk budget")
    ordinal = 0
    for block in blocks:
        start = 0
        while start < len(block.text):
            end, size = start, 0
            while end < len(block.text) and size + len(block.text[end].encode("utf-8")) <= max_bytes:
                size += len(block.text[end].encode("utf-8"))
                end += 1
            text = block.text[start:end]
            yield {
                "ordinal": ordinal, "text": text,
                "spans": [{"block_ordinal": block.ordinal, "start": start, "end": end,
                           "locator": block.locator}],
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "content_role": block.content_role, "role_basis": block.role_basis,
            }
            ordinal += 1
            if end == len(block.text):
                break
            start = max(start + 1, end - overlap_chars)
