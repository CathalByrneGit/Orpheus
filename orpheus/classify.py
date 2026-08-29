"""Step 3: what kind of document is this?

Deliberately a *proposal*, not a verdict. The classification lands
`unconfirmed` with a confidence and a rationale, exactly like any other
AI-sourced value, and a person confirms or amends it. That is what makes it part
of the review loop rather than a label the pipeline assigns to itself.

The document-type vocabulary comes from the bundle, not from this file. A closed
list is the point: an open one produces a new `doc_type` per document and the
column stops being worth grouping by.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from .audit import record_edit
from .ingest import document_text, get_document, has_text
from .rubric import snap_confidence
from .store import Store
from .utils import OrpheusError, from_json

MAX_CHARS = 12000


def classify_prompt(document_types: list[str],
                    sectors: list[str] | None = None,
                    jurisdictions: list[str] | None = None) -> str:
    """What the model is asked, given the vocabularies the bundle declares.

    A field with no vocabulary is **not asked about**. `sector` and
    `jurisdiction` used to be open questions, and on forty-eight documents of
    one corpus `sector` came back as thirteen spellings of a single answer --
    "software/open-source governance", "open-source software governance",
    "open source software governance", and ten more. That is exactly the harm
    `documentTypes` is a closed list to prevent, one field over: a column with
    a value per document is not worth grouping by, and a column of nulls is at
    least honest about knowing nothing.
    """
    fields = [f"  doc_type      one of: {', '.join(document_types)}"]
    if sectors:
        fields.append(f"  sector        one of: {', '.join(sectors)}, or null")
    if jurisdictions:
        fields.append("  jurisdiction  one of: "
                      f"{', '.join(jurisdictions)}, or null")
    fields += ["  confidence    one of 1.0, 0.9, 0.7, 0.5, 0.2",
               "  rationale     one short sentence"]
    closing = (
        "Return null rather than guessing a value that is not supported by "
        "the text, and never a value outside the list it is offered from."
    )
    return (
        "Classify this document. Return JSON only, with these fields:\n"
        + "\n".join(fields) + "\n\n"
        "Use the confidence rubric strictly:\n"
        "  1.0 stated explicitly in the document\n"
        "  0.9 clearly identifiable from headings and structure\n"
        "  0.7 implied by the content\n"
        "  0.5 inferred from context\n"
        "  0.2 speculative\n"
        + closing
    )


def classify(store: Store, document_id: str, actor_id: str | None = None,
             max_chars: int = MAX_CHARS, tier: str = "local",
             opt_in: bool = False, engine: str | None = None) -> dict:
    """Ask a model what this document is, and record the answer as a proposal.

    `engine` names which one to ask, and defaults to whatever the deployment
    configured for extraction when that engine can answer a question at all.
    It used to reach for the `llm` library whenever it imported, resolve the
    tier's model id -- which for the cloud tier names a Gemini model -- and
    fail on every document of a deployment that had `llm` and not `llm-gemini`.
    That is exactly what happened to both new corpora: 88 documents, 88 failed
    classifications, and `doc_type` null throughout.
    """
    store.assert_writable()
    document = get_document(store, document_id)
    if document is None:
        raise OrpheusError(f"No document {document_id!r}.")
    if not has_text(store, document_id):
        raise OrpheusError(
            f"Document {document_id} has no extracted text to classify. Its "
            "pages may need OCR — check text_source on document_pages."
        )

    text = document_text(store, document_id)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    bundle = bundle_mod.active(store) or bundle_mod.load()
    document_types = bundle_mod.document_types(bundle) or ["other"]
    sectors = bundle_mod.sectors(bundle)
    jurisdictions = bundle_mod.jurisdictions(bundle)

    reply = _ask(store, tier,
                 classify_prompt(document_types, sectors, jurisdictions), text,
                 document_id=document_id, actor_id=actor_id,
                 excerpt_only=truncated, opt_in=opt_in, engine=engine)

    doc_type = reply.get("doc_type") or "other"
    if doc_type not in document_types:
        # A model naming a type outside the vocabulary is answering a different
        # question. "other" is the honest place for it, and the rationale keeps
        # what it actually said.
        doc_type = "other"
    confidence = snap_confidence(reply.get("confidence", 0.5))

    previous = {"doc_type": document["doc_type"], "sector": document["sector"],
                "jurisdiction": document["jurisdiction"]}
    updated = {"doc_type": doc_type,
               # Null unless the bundle declared a vocabulary and the answer is
               # in it. A model asked nothing may answer anyway, and a value
               # outside the list is the same "answering a different question"
               # that sends an unknown doc_type to `other`.
               "sector": _in_vocabulary(reply.get("sector"), sectors),
               "jurisdiction": _in_vocabulary(reply.get("jurisdiction"),
                                              jurisdictions)}

    with store.transaction():
        store.execute(
            "UPDATE documents SET doc_type = ?, sector = ?, jurisdiction = ?, "
            "classification_source = ?, classification_confidence = ?, "
            "classification_status = 'unconfirmed' WHERE document_id = ?",
            (doc_type, updated["sector"], updated["jurisdiction"],
             "ai_local" if tier == "local" else "ai_cloud", confidence, document_id))
        record_edit(store, "documents", document_id, document_id, "classify",
                    previous=previous, new={**updated, "confidence": confidence},
                    actor_id=actor_id, note=reply.get("rationale"))

    return {"document_id": document_id, **updated, "confidence": confidence,
            "rationale": reply.get("rationale"), "status": "unconfirmed",
            "excerpt_only": truncated}


def _in_vocabulary(value, allowed: list[str]) -> str | None:
    """The value, if the bundle declared a list and this is on it."""
    if not allowed or value is None:
        return None
    text = str(value).strip()
    for candidate in allowed:
        if text.casefold() == candidate.casefold():
            return candidate
    return None


def confirm_classification(store: Store, document_id: str, actor_id: str,
                           changes: dict | None = None) -> dict:
    """Confirm the proposal, or correct it.

    Correcting sets `classification_source = 'human'`, for the same reason an
    amended instance does: afterwards it is ground truth, not a machine guess.
    """
    store.assert_writable()
    document = get_document(store, document_id)
    if document is None:
        raise OrpheusError(f"No document {document_id!r}.")

    allowed = ("doc_type", "sector", "jurisdiction")
    changes = {k: v for k, v in (changes or {}).items() if k in allowed}
    previous = {k: document[k] for k in allowed}

    with store.transaction():
        if changes:
            assignments = ", ".join(f'"{k}" = ?' for k in changes)
            store.execute(
                f"UPDATE documents SET {assignments}, classification_source = 'human', "
                "classification_confidence = 1.0, classification_status = 'amended' "
                "WHERE document_id = ?",
                tuple(changes.values()) + (document_id,))
            action = "classification_amended"
        else:
            store.execute(
                "UPDATE documents SET classification_status = 'confirmed' "
                "WHERE document_id = ?", (document_id,))
            action = "classification_confirmed"
        record_edit(store, "documents", document_id, document_id, action,
                    previous=previous, new=changes or {"status": "confirmed"},
                    actor_id=actor_id)

    return {"document_id": document_id,
            "status": "amended" if changes else "confirmed", **changes}


def _ask(store: Store, tier: str, system: str, text: str, document_id: str,
         actor_id: str | None, excerpt_only: bool, opt_in: bool,
         engine: str | None = None) -> dict:
    """One JSON-returning model call, through the gate and into the audit log.

    The transport is `engines.ask()` -- the same one the ontology survey uses,
    and the same gate, audit and characters-not-tokens accounting every engine
    uses. This file used to carry its own copy, which chose a provider by which
    library happened to import rather than by what the deployment configured.
    Two transports meant two behaviours, and the one nobody looked at was the
    one that broke.
    """
    from .engines import ask, general_engine_for

    content = ask(store=store, system=system, text=text, purpose="classify",
                  engine=general_engine_for(store, engine), tier=tier,
                  opt_in=opt_in, actor_id=actor_id, document_id=document_id,
                  excerpt_only=excerpt_only)

    parsed = _parse_json(content)
    if parsed is None:
        raise OrpheusError(
            "The model's reply was not JSON, so there is nothing to record. "
            f"It began: {content[:120]!r}"
        )
    return parsed


def _parse_json(content: str) -> dict | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    parsed = from_json(text)
    if isinstance(parsed, dict):
        return parsed
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        parsed = from_json(text[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    return None
