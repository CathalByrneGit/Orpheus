"""Getting text out of a file, and OCR when there is none to get.

Every backend here is optional and every one is behind a check, because the
useful answer to "can this server read a scanned PDF?" is one an operator can
get *before* someone uploads one — see `capabilities()`.

OCR is a provider rather than a hardcoded tool. Which OCR engine is right is
genuinely open (see docs), and a deployment that has a better one should be able
to use it through `set_ocr_provider()` rather than by editing the pipeline. With
no provider available a page is recorded as `needs_ocr` — visible as a gap,
rather than passed off as an empty page.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from .utils import OrpheusError

# A page yielding less than this is treated as image-only and sent to OCR.
OCR_CHAR_THRESHOLD = 40

_ocr_provider: Callable[[str], str] | None = None


# ---------------------------------------------------------------------------
# What this server can actually read
# ---------------------------------------------------------------------------

def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _have_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def capabilities() -> dict:
    """Backend availability, exposed over the API.

    An operator can see what the running server can read before a user uploads
    something it cannot.
    """
    return {
        "pdf_text": (_have_module("docling") or _have_module("pdfminer")
                     or _have("pdftotext")),
        "pdf_backend": ("docling" if _have_module("docling")
                        else "pdfminer" if _have_module("pdfminer")
                        else "pdftotext" if _have("pdftotext") else None),
        "layout_aware": _have_module("docling"),
        "pdf_render": _have("pdftoppm"),
        "docx": True,
        "plain_text": True,
        "ocr": ocr_provider() is not None,
        "ocr_backend": (
            "custom" if _ocr_provider is not None
            else "pytesseract" if _have_module("pytesseract")
            else "tesseract-cli" if _have("tesseract")
            else None
        ),
    }


def set_ocr_provider(fn: Callable[[str], str] | None) -> Callable[[str], str] | None:
    """Register the OCR provider. Returns the previous one."""
    global _ocr_provider
    if fn is not None and not callable(fn):
        raise OrpheusError("An OCR provider must be callable, or None.")
    previous, _ocr_provider = _ocr_provider, fn
    return previous


def ocr_provider() -> Callable[[str], str] | None:
    """The active provider, falling back to whatever is installed."""
    if _ocr_provider is not None:
        return _ocr_provider

    if _have_module("pytesseract") and _have_module("PIL"):
        def _pytesseract(image_path: str) -> str:
            import pytesseract
            from PIL import Image
            return pytesseract.image_to_string(Image.open(image_path))
        return _pytesseract

    if _have("tesseract"):
        def _tesseract_cli(image_path: str) -> str:
            with tempfile.TemporaryDirectory() as tmp:
                stem = str(Path(tmp) / "out")
                result = subprocess.run(["tesseract", image_path, stem],
                                        capture_output=True)
                produced = Path(stem + ".txt")
                if result.returncode != 0 or not produced.exists():
                    return ""
                return produced.read_text(errors="replace")
        return _tesseract_cli

    return None


# ---------------------------------------------------------------------------
# Per-format extraction
# ---------------------------------------------------------------------------

_KINDS = {
    "pdf": "pdf",
    "docx": "docx",
    "doc": "unsupported_doc",
    "txt": "text", "md": "text", "markdown": "text",
    "png": "image", "jpg": "image", "jpeg": "image",
    "tif": "image", "tiff": "image", "bmp": "image",
}

_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text": "text/plain",
}


def detect_kind(filename: str) -> str:
    return _KINDS.get(Path(filename).suffix.lower().lstrip("."), "unknown")


def mime_for(kind: str, filename: str) -> str:
    if kind == "image":
        return "image/" + Path(filename).suffix.lower().lstrip(".")
    return _MIME.get(kind, "application/octet-stream")


def _docling_pages(path: str | Path) -> list[str] | None:
    """Page texts via Docling, or None if it is not installed or cannot read it.

    Docling understands page layout, reading order, tables and formulas, and
    OCRs scanned pages itself -- all of which pdftotext does not, and all of
    which change how much of a document survives into the store. It also carries
    bounding boxes, which is what a reading companion needs to highlight a
    clause rather than merely name its page. Only the text is taken here; the
    boxes are the next thing to plumb through, alongside LangExtract's character
    intervals.

    It is heavy -- machine-learning models, downloaded on first run -- so it is
    optional and tried first only when present, never installed by default.

    NOT EXERCISED IN CI. Docling would not build in the environment this was
    written in (antlr4-python3-runtime fails to compile), so this path has been
    written against the documented API and never run. Treat it as untested until
    someone has.
    """
    if not _have_module("docling"):
        return None
    try:
        from docling.document_converter import DocumentConverter
        document = DocumentConverter().convert(str(path)).document
        pages: dict[int, list[str]] = {}
        for item, _level in document.iterate_items():
            text = getattr(item, "text", None)
            if not text:
                continue
            for provenance in (getattr(item, "prov", None) or []):
                page_no = getattr(provenance, "page_no", None)
                if page_no is not None:
                    pages.setdefault(page_no, []).append(text)
                    break
            else:
                pages.setdefault(1, []).append(text)
        if not pages:
            return None
        return ["\n".join(pages[n]) for n in sorted(pages)]
    except Exception:
        # A parser that cannot read this file is not a reason to fail the
        # ingest; the simpler backend below may manage it.
        return None


def pdf_pages(path: str | Path) -> list[str]:
    """Page texts from a PDF, in order.

    `pdftotext` separates pages with a form feed, which is the page break the
    splitting below relies on.
    """
    path = str(path)
    docling = _docling_pages(path)
    if docling is not None:
        return docling
    if _have_module("pdfminer"):
        try:
            from pdfminer.high_level import extract_text
            return split_pages(extract_text(path) or "")
        except Exception:      # fall through to the binary
            pass
    if _have("pdftotext"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            result = subprocess.run(["pdftotext", "-layout", path, str(out)],
                                    capture_output=True)
            if result.returncode != 0 or not out.exists():
                raise OrpheusError(f"pdftotext failed on {Path(path).name}.")
            return split_pages(out.read_text(errors="replace"))
    raise OrpheusError(
        "No PDF text backend is available. Install pdfminer.six "
        "(pip install 'orpheus[pdf]'), or put pdftotext on PATH."
    )


def render_pdf_pages(path: str | Path, out_dir: str | Path,
                     pages: list[int]) -> dict[int, str]:
    """Render the named 1-based pages to PNG, for OCR. Missing renderer is not
    an error: the pages are simply recorded as needing OCR."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not _have("pdftoppm"):
        return {}
    rendered: dict[int, str] = {}
    for page_no in pages:
        stem = out_dir / f"page-{page_no:03d}"
        result = subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-f", str(page_no), "-l",
             str(page_no), str(path), str(stem)],
            capture_output=True)
        if result.returncode != 0:
            continue
        hits = sorted(out_dir.glob(f"page-{page_no:03d}*"))
        if hits:
            rendered[page_no] = str(hits[0])
    return rendered


def split_pages(text: str) -> list[str]:
    """Split on form feeds, dropping the artefact one at the end.

    pdftotext terminates the last page with a form feed rather than separating
    pages with one, so a naive split reports one page too many, and the extra is
    empty -- which then looks like a page that needs OCR. R's strsplit dropped a
    trailing empty field for free; Python's split does not, and the difference
    is a whole phantom page in every PDF.
    """
    pages = (text or "").split("\f")
    if len(pages) > 1 and pages[-1] == "":
        pages.pop()
    return pages or [""]


_TAG = re.compile(r"<[^>]+>")


def docx_text(path: str | Path) -> str:
    """Read a .docx without a dependency.

    A .docx is a zip whose `word/document.xml` holds the body. Explicit page
    breaks are the only page signal the format gives, so they become form feeds
    and everything else is one page.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise OrpheusError(f"{Path(path).name} is not a readable .docx.") from exc

    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    xml = re.sub(r"<w:lastRenderedPageBreak[^>]*/>", "\f", xml)
    xml = xml.replace("</w:p>", "\n")
    text = html.unescape(_TAG.sub("", xml))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def page_texts(path: str | Path, kind: str) -> list[str]:
    """Page texts for any supported kind. An image has none until OCR runs."""
    if kind == "pdf":
        return pdf_pages(path)
    if kind == "docx":
        return split_pages(docx_text(path))
    if kind == "text":
        return split_pages(Path(path).read_text(errors="replace"))
    if kind == "image":
        return [""]
    raise OrpheusError(f"Unhandled document kind: {kind}")
