"""Portable fixture checks for the V019 PDF contract."""

import shutil
import uuid
from pathlib import Path

import pytest

from evals.batch_basic import _build_v019_pdf, cases, execute


def _workspace():
    path = Path.cwd() / f".v019-fixture-test-{uuid.uuid4().hex}"
    path.mkdir()
    for relative, content in cases()["V019"]["fixtures"].items():
        fixture = path / relative
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(content, encoding="utf-8")
    return path


def test_v019_fixture_is_deterministic_and_embeds_cjk_font():
    workspace = _workspace()
    try:
        first = workspace / "first.pdf"
        second = workspace / "second.pdf"
        provenance = _build_v019_pdf(first)
        _build_v019_pdf(second)
        assert first.read_bytes() == second.read_bytes()
        assert provenance["source"] == "pymupdf.Font('cjk').buffer"
        assert provenance["name"]
        assert len(provenance["sha256"]) == 64
        assert provenance["pymupdf_version"]
    finally:
        shutil.rmtree(workspace)


@pytest.mark.asyncio
async def test_v019_extracts_the_original_two_pages_without_images():
    workspace = _workspace()
    try:
        observed = await execute("V019", workspace)
        assert observed["extracted_text"] == (
            "\n\n[File: contract.pdf]\n--- Page 1 ---\n合同编号 HT-017；预付款 100 元"
            "\n\n--- Page 2 ---\n尾款 230 元；到期日 2026-10-15"
        )
        assert observed["images"] == []
        assert observed["page2_extracted"] is True
        assert observed["balance"] == 230
        assert observed["due_date"] == "2026-10-15"
        assert observed["fixture_unchanged"] is True
        assert observed["fixture_font"]["source"] == "pymupdf.Font('cjk').buffer"
    finally:
        shutil.rmtree(workspace)


def test_v019_font_failure_is_clear_and_leaves_no_pdf():
    workspace = _workspace()
    path = workspace / "contract.pdf"
    try:
        def unavailable(_name):
            raise OSError("font unavailable")

        with pytest.raises(RuntimeError, match="embedded PyMuPDF CJK font") as error:
            _build_v019_pdf(path, font_factory=unavailable)
        assert isinstance(error.value.__cause__, OSError)
        assert not path.exists()
    finally:
        shutil.rmtree(workspace)


def test_v019_missing_glyph_fails_without_fallback_or_pdf():
    class IncompleteFont:
        buffer = b"fixture-font"
        name = "incomplete test font"

        @staticmethod
        def has_glyph(_codepoint):
            return False

    workspace = _workspace()
    path = workspace / "contract.pdf"
    try:
        with pytest.raises(RuntimeError, match="lacks required glyphs"):
            _build_v019_pdf(path, font_factory=lambda _name: IncompleteFont())
        assert not path.exists()
    finally:
        shutil.rmtree(workspace)


def test_v019_does_not_delete_an_existing_fixture_on_font_failure():
    workspace = _workspace()
    path = workspace / "contract.pdf"
    original = b"existing evidence"
    path.write_bytes(original)
    try:
        with pytest.raises(FileExistsError):
            _build_v019_pdf(path, font_factory=lambda _: pytest.fail("must not load a font"))
        assert path.read_bytes() == original
    finally:
        shutil.rmtree(workspace)


def test_v019_empty_font_buffer_is_an_explicit_error():
    class EmptyFont:
        buffer = b""

    workspace = _workspace()
    try:
        with pytest.raises(RuntimeError, match="empty font buffer"):
            _build_v019_pdf(workspace / "contract.pdf", font_factory=lambda _: EmptyFont())
        assert not (workspace / "contract.pdf").exists()
    finally:
        shutil.rmtree(workspace)
