"""Step 1: a file goes in, text and page records come out.

The original is kept content-addressed by SHA-256, so an extraction can always
be re-run against exactly the bytes it was derived from, and the same file
ingested twice occupies one copy.

**Dedup is on content, not filename.** The same contract mailed round twice
under two names is one document, and treating it as two would quietly double
every corpus statistic computed over it.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import textract
from .audit import record_edit
from .rubric import VISIBILITY
from .store import Store
from .utils import NotFound, OrpheusError, new_id, now, require_choice


def storage_path_for(root: str | Path, file_hash: str, extension: str) -> Path:
    """Where an original lives. Fanned out by hash prefix so no one directory
    accumulates every document ever ingested."""
    directory = Path(root) / "documents" / file_hash[:2]
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f".{extension}" if extension else ""
    return directory / f"{file_hash}{suffix}"


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_pages(page_texts: list[str]) -> list[dict]:
    pages = []
    for index, text in enumerate(page_texts, start=1):
        text = text or ""
        pages.append({
            "page_no": index,
            "text": text,
            "char_count": len(text),
            "text_source": ("native" if len(text.strip()) >= textract.OCR_CHAR_THRESHOLD
                            else "pending_ocr"),
            "image_path": None,
        })
    return pages


def ingest(store: Store, path: str | Path, actor_id: str | None = None,
           storage_root: str | Path = "storage", filename: str | None = None,
           visibility: str = "private") -> dict:
    """Ingest one file.

    Pages that yield too little text to be text are rendered to images and sent
    to the configured OCR provider. With no provider available they are recorded
    as `needs_ocr` rather than passed off as empty pages — a gap review can see
    is worth more than a blank a reader would assume was blank.
    """
    store.assert_writable()
    require_choice(visibility, VISIBILITY, "visibility")
    path = Path(path)
    if not path.exists():
        raise OrpheusError(f"No file at {path}.")

    filename = filename or path.name
    kind = textract.detect_kind(filename)
    if kind == "unsupported_doc":
        raise OrpheusError(
            "Legacy .doc files are not supported. Convert to .docx or PDF first."
        )
    if kind == "unknown":
        raise OrpheusError(f"Cannot ingest {filename}: unrecognised file type.")

    file_hash = hash_file(path)
    existing = store.one(
        "SELECT document_id, filename FROM documents WHERE file_hash = ?", (file_hash,))
    if existing:
        return {
            "document_id": existing["document_id"],
            "duplicate": True,
            "filename": existing["filename"],
            "n_pages": store.scalar(
                "SELECT n_pages FROM documents WHERE document_id = ?",
                (existing["document_id"],)),
            "message": (f"Identical content already ingested as "
                        f"'{existing['filename']}'."),
        }

    extension = Path(filename).suffix.lower().lstrip(".")
    stored = storage_path_for(storage_root, file_hash, extension)
    shutil.copyfile(path, stored)

    pages = _build_pages(textract.page_texts(stored, kind))
    document_id = new_id("doc")

    needs_ocr = [p["page_no"] for p in pages if p["text_source"] == "pending_ocr"]
    if needs_ocr:
        _apply_ocr(pages, needs_ocr, stored, kind, storage_root, document_id)

    sources = {p["text_source"] for p in pages}
    text_source = sources.pop() if len(sources) == 1 else "mixed"

    with store.transaction():
        store.insert("documents", {
            "document_id": document_id,
            "filename": filename,
            "file_hash": file_hash,
            "mime_type": textract.mime_for(kind, filename),
            "byte_size": stored.stat().st_size,
            "storage_path": str(stored),
            "n_pages": len(pages),
            "text_source": text_source,
            "date_added": now(),
            "created_by": actor_id,
            "visibility": visibility,
            "review_status": "unreviewed",
        })
        for page in pages:
            store.insert("document_pages", {
                "document_id": document_id,
                "page_no": page["page_no"],
                "text": page["text"],
                "text_source": page["text_source"],
                "image_path": page["image_path"],
                "char_count": page["char_count"],
            })
        record_edit(store, "documents", document_id, document_id, "ingest",
                    new={"filename": filename, "file_hash": file_hash},
                    actor_id=actor_id)

    return {
        "document_id": document_id,
        "duplicate": False,
        "filename": filename,
        "n_pages": len(pages),
        "text_source": text_source,
        "needs_ocr": [p["page_no"] for p in pages if p["text_source"] == "needs_ocr"],
    }


def _apply_ocr(pages: list[dict], page_numbers: list[int], stored: Path,
               kind: str, storage_root: str | Path, document_id: str) -> None:
    image_dir = Path(storage_root) / "pages" / document_id
    if kind == "pdf":
        images = textract.render_pdf_pages(stored, image_dir, page_numbers)
    elif kind == "image":
        images = {page_numbers[0]: str(stored)} if page_numbers else {}
    else:
        images = {}

    provider = textract.ocr_provider()
    for page_no in page_numbers:
        page = pages[page_no - 1]
        image = images.get(page_no)
        page["image_path"] = image
        if provider is None or not image or not Path(image).exists():
            page["text_source"] = "needs_ocr"
            continue
        try:
            text = provider(image) or ""
        except Exception:
            text = ""
        page["text"] = text
        page["char_count"] = len(text)
        page["text_source"] = "ocr" if text.strip() else "needs_ocr"


# ---------------------------------------------------------------------------
# Reading a document back
# ---------------------------------------------------------------------------

class OriginalUnavailable(OrpheusError):
    """The row is there; the bytes behind it are not what they should be.

    `reason` says which, because the three call for different responses and a
    single "could not serve that" would hide the one that matters:

    - `not_stored` -- the row never recorded a path.
    - `misfiled`   -- the path is not where a document of that hash belongs.
    - `missing`    -- the path is right and nothing is there.
    - `altered`    -- something is there and it is not what was ingested.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def original(store: Store, document_id: str, *, verify: bool = True) -> dict:
    """The file that was ingested, located and checked before it is handed back.

    The docstring at the top of this module promises the original is kept "so
    an extraction can always be re-run against exactly the bytes it was derived
    from". This is where that promise is made good, and the whole of it is in
    the word *exactly*: a path that resolves is not enough, because the thing
    at the end of it may have been replaced, truncated, or pruned and restored
    from a backup taken at the wrong moment.

    So the check that carries the weight is the hash. `file_hash` was computed
    over the bytes at ingest and every excerpt, page number and character
    offset in the store was computed from them. Re-reading it costs about
    150ms for a 50MB file and is worth every one of them: it is the difference
    between serving the document and serving whatever is at its address.

    It also happens to be what makes trusting `storage_path` safe. That column
    is a path in a database, and a database is a thing that gets written to; an
    arbitrary value in it would otherwise be an arbitrary file read. Nothing an
    attacker can point it at will hash to a digest recorded before they got
    there. The layout check below is the cheap version of the same test --
    content-addressed storage puts a document at exactly one place, so a path
    that is not that place is wrong before anything is read.

    `verify=False` skips only the re-read, for a caller that has already done
    it or is about to stream the file and check as it goes. The layout check is
    not optional.
    """
    document = get_document(store, document_id)
    if document is None:
        raise NotFound(f"No document {document_id!r}.")

    recorded = document["storage_path"]
    if not recorded:
        raise OriginalUnavailable(
            f"Document {document_id!r} has no stored original.",
            reason="not_stored")

    file_hash = document["file_hash"]
    path = Path(recorded).resolve()
    # `storage_path_for` puts it at `<root>/documents/<hash[:2]>/<hash>[.ext]`
    # and nothing else ever writes this column, so anything else is a path the
    # store did not choose.
    if not (path.name == file_hash or path.name.startswith(f"{file_hash}.")) \
            or path.parent.name != file_hash[:2] \
            or path.parent.parent.name != "documents":
        raise OriginalUnavailable(
            f"The original of {document_id!r} is recorded at a path no "
            f"document of that hash belongs at: {recorded}.",
            reason="misfiled")

    if not path.is_file():
        raise OriginalUnavailable(
            f"The original of {document_id!r} is recorded at {recorded}, and "
            "there is nothing there. The row is intact; the file is not.",
            reason="missing")

    if verify:
        found = hash_file(path)
        if found != file_hash:
            raise OriginalUnavailable(
                f"The file stored for {document_id!r} is not the file that was "
                f"ingested: recorded {file_hash[:12]}, found {found[:12]}. "
                "Every excerpt and page offset in the store was computed from "
                "the recorded bytes, so serving these would be serving a "
                "different document under the same provenance.",
                reason="altered")

    return {
        "document_id": document_id,
        "path": path,
        "filename": document["filename"],
        # The recorded type, not a guess from the extension. It is what the
        # uploader sent and what every other surface reports; disagreeing with
        # it here would make the download a second opinion nobody asked for.
        "media_type": document["mime_type"] or "application/octet-stream",
        # From disk, because this is what a `Content-Length` has to match. When
        # `verify` ran it is necessarily the recorded size as well.
        "byte_size": path.stat().st_size,
        "file_hash": file_hash,
        "verified": verify,
    }


def audit_storage(store: Store, *, verify: bool = False,
                  document_id: str | None = None,
                  limit: int | None = None) -> dict:
    """Ask of every document whether its original is still there and still itself.

    Two passes in one function, because they differ only in what they can
    afford. Without `verify` this is one `stat` per document and answers "is
    there a file" -- cheap enough to run on every lint. With it, every file is
    re-read and hashed, which answers "is it the right file" and costs the size
    of the corpus in disk reads.

    That is the whole reason `orpheus verify` is its own command rather than a
    `deep` lint check. `deep` means a few seconds of SQL; this means minutes of
    I/O over gigabytes, and a check somebody stops running is worse than one
    they have to ask for.

    The question it exists to answer is a restore. A database and a `storage/`
    from two different moments looks perfectly healthy from the inside: every
    row is there, every excerpt renders, and the offsets point into bytes
    nobody has compared to anything.
    """
    rows = store.query(
        "SELECT document_id, filename FROM documents "
        + ("WHERE document_id = ? " if document_id else "")
        + "ORDER BY date_added" + (" LIMIT ?" if limit else ""),
        tuple(p for p in (document_id, limit) if p is not None))

    documents: list[dict] = []
    checked_bytes = 0
    for row in rows:
        # The filename comes from the row rather than from the located
        # file, so a finding about a document whose original is *gone*
        # can still say which document it is.
        entry = {"document_id": row["document_id"],
                 "filename": row["filename"]}
        try:
            located = original(store, row["document_id"], verify=verify)
        except OriginalUnavailable as exc:
            entry.update(available=False, reason=exc.reason, message=str(exc))
        else:
            entry.update(available=True, reason=None,
                         byte_size=located["byte_size"])
            checked_bytes += located["byte_size"] if verify else 0
        documents.append(entry)

    unavailable = [d for d in documents if not d["available"]]
    reasons: dict[str, int] = {}
    for entry in unavailable:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    return {
        "verified": verify,
        "n_documents": len(documents),
        "n_available": len(documents) - len(unavailable),
        "n_unavailable": len(unavailable),
        "reasons": reasons,
        "bytes_read": checked_bytes,
        # A pass that only stat()ed cannot say a corpus is sound, and saying so
        # is the difference between a report and a reassurance.
        "headline": _audit_headline(len(documents), unavailable, verify),
        "documents": documents,
    }


def _audit_headline(total: int, unavailable: list[dict], verify: bool) -> str:
    """What was found, and -- when nothing was read -- what could not have been.

    A pass that only stat()ed cannot say a corpus is unaltered, and the
    dangerous case is not the clean one: a quick pass that finds two problems
    reads like a complete answer, and is not. So the caveat goes on both.
    """
    if not total:
        return "No documents to check."
    unread = ("Nothing was read, so this says they exist, not that they are "
              "unaltered -- run `orpheus verify` for that.")
    if not unavailable:
        return (f"All {total} originals are present and hash to the digests "
                f"recorded at ingest." if verify else
                f"All {total} originals are where they should be. {unread}")
    kinds = ", ".join(f"{sum(1 for u in unavailable if u['reason'] == r)} "
                      f"{r}" for r in sorted({u["reason"] for u in unavailable}))
    found = (f"{len(unavailable)} of {total} originals cannot be served: "
             f"{kinds}. Every excerpt taken from them is now unverifiable.")
    return found if verify else f"{found} {unread}"


def get_document(store: Store, document_id: str) -> dict | None:
    return store.one("SELECT * FROM documents WHERE document_id = ?", (document_id,))


def document_pages(store: Store, document_id: str) -> list[dict]:
    return store.query(
        "SELECT page_no, text, text_source, image_path, char_count "
        "FROM document_pages WHERE document_id = ? ORDER BY page_no",
        (document_id,),
    )


def document_text(store: Store, document_id: str, with_markers: bool = True) -> str:
    """The document as one string.

    Page markers are included by default because every downstream excerpt is
    traced back to a page number, and the marker is how a model's excerpt can be
    attributed to one. `has_text()` exists because those markers mean an empty
    document is not an empty string.
    """
    pages = document_pages(store, document_id)
    if with_markers:
        return "\n\n".join(f"--- Page {p['page_no']} ---\n{p['text'] or ''}"
                           for p in pages)
    return "\n\n".join(p["text"] or "" for p in pages)


def has_text(store: Store, document_id: str) -> bool:
    """Whether there is any text at all, page markers not counting.

    Without this a document of blank scanned pages looks like it has content,
    because the markers alone are hundreds of characters.
    """
    return any((p["text"] or "").strip() for p in document_pages(store, document_id))
