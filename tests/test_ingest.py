"""Ingest, and the text extraction underneath it."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from orpheus import textract
from orpheus.audit import row_history
from orpheus.ingest import (document_pages, document_text, get_document,
                            has_text, ingest, storage_path_for)
from orpheus.utils import OrpheusError

FIXTURES = Path(__file__).parent / "fixtures"
PDF = FIXTURES / "services-agreement.pdf"


@pytest.fixture
def seeded(store):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    return store


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


# -- text extraction --------------------------------------------------------

def test_file_kinds_and_mime_types():
    assert textract.detect_kind("Agreement.PDF") == "pdf"
    assert textract.detect_kind("a.docx") == "docx"
    assert textract.detect_kind("a.doc") == "unsupported_doc"
    assert textract.detect_kind("a.tiff") == "image"
    assert textract.detect_kind("a.xyz") == "unknown"
    assert textract.mime_for("image", "scan.PNG") == "image/png"
    assert textract.mime_for("pdf", "a.pdf") == "application/pdf"


def test_a_trailing_form_feed_is_not_an_extra_page():
    # pdftotext terminates the last page with a form feed rather than separating
    # pages with one. Splitting naively reports one page too many, and the extra
    # is empty -- so it then looks like a page that needs OCR.
    assert textract.split_pages("a\fb\f") == ["a", "b"]
    assert textract.split_pages("a") == ["a"]
    assert textract.split_pages("") == [""]
    # A genuinely blank middle page is still a page.
    assert textract.split_pages("a\f\fb") == ["a", "", "b"]


def test_a_real_pdf_splits_where_the_page_breaks_are():
    pages = textract.page_texts(PDF, "pdf")
    assert len(pages) == 2
    assert "SERVICES AGREEMENT" in pages[0]
    assert "Limitation of liability" in pages[1]
    assert "Limitation of liability" not in pages[0]


def test_docx_is_read_without_a_dependency(tmp_path):
    document_xml = (
        '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
        "<w:p><w:r><w:t>SERVICES AGREEMENT</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Value &amp; scope</w:t></w:r></w:p>"
        '<w:p><w:r><w:lastRenderedPageBreak/><w:t>Second page</w:t></w:r></w:p>'
        "</w:body></w:document>"
    )
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    pages = textract.page_texts(path, "docx")
    assert len(pages) == 2
    assert "SERVICES AGREEMENT" in pages[0]
    assert "Value & scope" in pages[0]      # entities decoded
    assert "Second page" in pages[1]


def test_a_file_that_is_not_a_docx_says_so(tmp_path):
    path = tmp_path / "not-really.docx"
    path.write_text("plain text")
    with pytest.raises(OrpheusError, match="not a readable .docx"):
        textract.docx_text(path)


def test_capabilities_reports_what_this_server_can_read():
    caps = textract.capabilities()
    assert set(caps) == {"pdf_text", "pdf_backend", "layout_aware", "pdf_render",
                         "docx", "plain_text", "ocr", "ocr_backend"}
    assert caps["docx"] is True and caps["plain_text"] is True
    # Which PDF backend is in use is worth naming: docling reads layout, tables
    # and scanned pages, and pdftotext reads none of those, so the same file
    # yields materially different text depending on what is installed.
    assert caps["pdf_backend"] in ("docling", "pdfminer", "pdftotext", None)
    assert caps["layout_aware"] is (caps["pdf_backend"] == "docling")


def test_the_pdf_backend_falls_back_rather_than_failing(monkeypatch):
    # Docling is optional and heavy. When it is absent, or present and unable to
    # read a particular file, ingest continues on the simpler backend instead of
    # refusing the document.
    monkeypatch.setattr(textract, "_docling_pages", lambda path: None)
    assert len(textract.page_texts(PDF, "pdf")) == 2


# -- ingest -----------------------------------------------------------------

def test_ingesting_a_real_pdf(seeded, tmp_path):
    result = ingest(seeded, PDF, actor_id="act_test", storage_root=tmp_path / "storage")
    assert result["n_pages"] == 2
    assert result["text_source"] == "native"
    assert result["needs_ocr"] == []

    document = get_document(seeded, result["document_id"])
    assert document["mime_type"] == "application/pdf"
    assert document["review_status"] == "unreviewed"
    assert document["byte_size"] == PDF.stat().st_size

    pages = document_pages(seeded, result["document_id"])
    assert [p["char_count"] for p in pages] == [684, 544]


def test_the_original_is_kept_content_addressed(seeded, tmp_path):
    root = tmp_path / "storage"
    result = ingest(seeded, PDF, actor_id="act_test", storage_root=root)
    document = get_document(seeded, result["document_id"])
    stored = Path(document["storage_path"])

    assert stored.exists()
    assert stored.read_bytes() == PDF.read_bytes()
    # Self-verifying: the path is the hash, so an extraction can always be
    # re-run against exactly the bytes it came from.
    assert document["file_hash"] in stored.name
    assert stored == storage_path_for(root, document["file_hash"], "pdf")


def test_dedup_is_on_content_not_filename(seeded, tmp_path):
    root = tmp_path / "storage"
    first = ingest(seeded, PDF, actor_id="act_test", storage_root=root)
    renamed = tmp_path / "same-contract-different-name.pdf"
    renamed.write_bytes(PDF.read_bytes())

    second = ingest(seeded, renamed, actor_id="act_test", storage_root=root)
    assert second["duplicate"] is True
    assert second["document_id"] == first["document_id"]
    assert second["n_pages"] == 2
    assert seeded.scalar("SELECT COUNT(*) FROM documents") == 1


def test_ingest_is_recorded_in_the_history(seeded, tmp_path):
    result = ingest(seeded, PDF, actor_id="act_test", storage_root=tmp_path)
    history = row_history(seeded, "documents", result["document_id"])
    assert [h["action"] for h in history] == ["ingest"]
    assert history[0]["edited_by"] == "act_test"


def test_plain_text_and_page_markers(seeded, tmp_path):
    path = write(tmp_path, "note.txt", "First page\fSecond page")
    result = ingest(seeded, path, actor_id="act_test", storage_root=tmp_path / "s")
    assert result["n_pages"] == 2

    marked = document_text(seeded, result["document_id"])
    assert "--- Page 1 ---" in marked and "--- Page 2 ---" in marked
    plain = document_text(seeded, result["document_id"], with_markers=False)
    assert "--- Page" not in plain


def test_an_empty_document_is_not_mistaken_for_a_full_one(seeded, tmp_path):
    # document_text() adds page markers, so an empty document is hundreds of
    # characters of marker. has_text() is what the pipeline asks instead.
    path = write(tmp_path, "blank.txt", "   \f  ")
    result = ingest(seeded, path, actor_id="act_test", storage_root=tmp_path / "s")
    assert has_text(seeded, result["document_id"]) is False
    assert len(document_text(seeded, result["document_id"])) > 20


def test_a_page_with_no_text_and_no_ocr_backend_is_recorded_as_a_gap(seeded, tmp_path):
    previous = textract.set_ocr_provider(None)
    try:
        path = write(tmp_path, "scan.txt", "tiny")   # under the OCR threshold
        result = ingest(seeded, path, actor_id="act_test", storage_root=tmp_path / "s")
        assert result["needs_ocr"] == [1]
        assert document_pages(seeded, result["document_id"])[0]["text_source"] == "needs_ocr"
    finally:
        textract.set_ocr_provider(previous)


def test_a_registered_ocr_provider_is_used(seeded, tmp_path):
    # OCR is a provider, not a hardcoded tool: which engine is right is still
    # open, and a deployment with a better one should not have to edit the
    # pipeline to use it.
    calls = []

    def fake_ocr(image_path):
        calls.append(image_path)
        return "TEXT RECOVERED FROM THE SCAN, AT LENGTH"

    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    previous = textract.set_ocr_provider(fake_ocr)
    try:
        result = ingest(seeded, image, actor_id="act_test", storage_root=tmp_path / "s")
        page = document_pages(seeded, result["document_id"])[0]
        assert page["text_source"] == "ocr"
        assert "RECOVERED" in page["text"]
        assert calls
    finally:
        textract.set_ocr_provider(previous)


def test_unsupported_and_unknown_files_are_refused(seeded, tmp_path):
    legacy = write(tmp_path, "old.doc", "x")
    with pytest.raises(OrpheusError, match="Legacy .doc files"):
        ingest(seeded, legacy, storage_root=tmp_path / "s")

    unknown = write(tmp_path, "thing.xyz", "x")
    with pytest.raises(OrpheusError, match="unrecognised file type"):
        ingest(seeded, unknown, storage_root=tmp_path / "s")

    with pytest.raises(OrpheusError, match="No file at"):
        ingest(seeded, tmp_path / "missing.pdf", storage_root=tmp_path / "s")


def test_a_read_connection_cannot_ingest(db_path, tmp_path):
    from orpheus.store import connect
    connect(db_path).close()
    reader = connect(db_path, mode="read")
    try:
        with pytest.raises(OrpheusError, match="read-only"):
            ingest(reader, PDF, storage_root=tmp_path)
    finally:
        reader.close()
