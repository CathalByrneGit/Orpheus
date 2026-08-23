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

import json

from . import bundle as bundle_mod
from . import llm
from .audit import record_edit
from .ingest import document_text, get_document, has_text
from .rubric import snap_confidence
from .store import Store
from .utils import OrpheusError, from_json, now

MAX_CHARS = 12000


def classify_prompt(document_types: list[str]) -> str:
    return (
        "Classify this document. Return JSON only, with these fields:\n"
        f"  doc_type      one of: {', '.join(document_types)}\n"
        "  sector        the public-sector domain if evident, or null\n"
        "  jurisdiction  the governing jurisdiction if stated or clearly "
        "inferable, or null\n"
        "  confidence    one of 1.0, 0.9, 0.7, 0.5, 0.2\n"
        "  rationale     one short sentence\n\n"
        "Use the confidence rubric strictly:\n"
        "  1.0 stated explicitly in the document\n"
        "  0.9 clearly identifiable from headings and structure\n"
        "  0.7 implied by the content\n"
        "  0.5 inferred from context\n"
        "  0.2 speculative\n"
        "Return null rather than guessing a sector or jurisdiction that is not "
        "supported by the text."
    )


def classify(store: Store, document_id: str, actor_id: str | None = None,
             max_chars: int = MAX_CHARS, tier: str = "local",
             opt_in: bool = False) -> dict:
    """Ask a model what this document is, and record the answer as a proposal."""
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

    reply = _ask(store, tier, classify_prompt(document_types), text,
                 document_id=document_id, actor_id=actor_id,
                 excerpt_only=truncated, opt_in=opt_in)

    doc_type = reply.get("doc_type") or "other"
    if doc_type not in document_types:
        # A model naming a type outside the vocabulary is answering a different
        # question. "other" is the honest place for it, and the rationale keeps
        # what it actually said.
        doc_type = "other"
    confidence = snap_confidence(reply.get("confidence", 0.5))

    previous = {"doc_type": document["doc_type"], "sector": document["sector"],
                "jurisdiction": document["jurisdiction"]}
    updated = {"doc_type": doc_type, "sector": reply.get("sector"),
               "jurisdiction": reply.get("jurisdiction")}

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
         actor_id: str | None, excerpt_only: bool, opt_in: bool) -> dict:
    """One JSON-returning model call, through the gate and into the audit log.

    Uses the `llm` library when it is installed and falls back to the plain
    HTTP path otherwise — the same two routes the extraction engines use, for
    the same reason.
    """
    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)
    config = llm.model_config(store, tier)

    error, content = None, ""
    try:
        content = _call_model(store, tier, config, system, text)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="classify", prompt_chars=len(text),
            provider=config["provider"], model=config["model_id"],
            document_id=document_id, actor_id=actor_id,
            excerpt_only=excerpt_only, payload=text, error=error)
    if error:
        raise OrpheusError(f"Classification failed: {error}")

    parsed = _parse_json(content)
    if parsed is None:
        raise OrpheusError(
            "The model's reply was not JSON, so there is nothing to record. "
            f"It began: {content[:120]!r}"
        )
    return parsed


def _call_model(store: Store, tier: str, config: dict, system: str, text: str) -> str:
    from .engines import _default_base_url, _post_chat, _llm_model_id

    try:
        import llm as llm_lib
    except ImportError:
        llm_lib = None

    if llm_lib is not None:
        model = llm_lib.get_model(_llm_model_id(store, tier, config))
        kwargs = {"system": system, "stream": False}
        if config.get("api_key") and isinstance(model, llm_lib.KeyModel):
            kwargs["key"] = config["api_key"]
        return model.prompt(text, **kwargs).text()

    return _post_chat(
        _default_base_url(store, tier), config.get("api_key"),
        {"model": config["model_id"], "temperature": 0,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": text}]})


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
