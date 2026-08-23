"""Step 4: text in, typed instances out — via LangExtract.

The R implementation adapted `ontologyDiscoverR`'s prompt-and-validate loop.
The port does not reimplement that. `docs/prior-art.md` had already concluded
LangExtract does the same job better, and the only thing standing in the way was
that Orpheus was written in R; it is not any more.

What LangExtract brings that would otherwise have to be written: chunking,
parallel workers, multi-pass extraction for recall, and — the one that changes
the data model — **source grounding**. Every extraction carries the character
interval it was found at, and an `alignment_status` saying how exactly it
matched. Orpheus stored an excerpt string and a page number, which cannot be
highlighted reliably: you can only search for the excerpt and hope it occurs
once.

Grounding also solves a problem the rubric had. LangExtract reports no
confidence score, which reads like a gap and is closer to a virtue: a model's
opinion of its own certainty is exactly what the rubric was invented to avoid
storing. `alignment_status` is a fact about the text instead — did this span
actually appear in the document, verbatim, or did the model assert something
that cannot be found? That maps onto the rubric directly, and it is a better
signal than a number the model chose.

The gate is not delegated. LangExtract would happily resolve its own API key and
call its own provider, which routes around the org policy, the per-request
opt-in and the `llm_calls` audit together. So it is called through
`orpheus.llm`: Orpheus decides whether a call may happen and which model serves
it, LangExtract does the calling, and the attempt is recorded either way.
"""

from __future__ import annotations

from typing import Any, Callable

from . import bundle as bundle_mod
from . import llm
from .ingest import document_pages, document_text, get_document
from .rubric import CONFIDENCE, RESERVED_PROPS, snap_confidence
from .store import Store
from .utils import OrpheusError, new_id

# The seam. A test double, a different library, or a remote service goes here;
# everything downstream works on the normalised shape returned below and never
# sees the engine.
_populator: Callable[..., dict] | None = None


def set_populator(fn: Callable[..., dict] | None) -> Callable[..., dict] | None:
    global _populator
    if fn is not None and not callable(fn):
        raise OrpheusError("A populator must be callable, or None.")
    previous, _populator = _populator, fn
    return previous


def populator() -> Callable[..., dict] | None:
    return _populator


# ---------------------------------------------------------------------------
# Grounding -> the confidence rubric
# ---------------------------------------------------------------------------

# An extraction LangExtract located verbatim is, precisely, "stated verbatim in
# the document" -- the rubric's own words for 1.0. One it could only match
# fuzzily is weaker evidence, and one it could not locate at all is the model
# asserting something the text does not say, which is the definition of
# inferred. The rubric levels were written before LangExtract was considered and
# they line up without being bent, which is some evidence both are describing
# the same thing.
ALIGNMENT_CONFIDENCE = {
    "match_exact": CONFIDENCE["explicit"],
    "match_greater": CONFIDENCE["named"],
    "match_lesser": CONFIDENCE["named"],
    "match_fuzzy": CONFIDENCE["implied"],
    None: CONFIDENCE["inferred"],
}


def confidence_for_alignment(alignment: Any) -> float:
    """Rubric level for one extraction's grounding status."""
    if alignment is None:
        return ALIGNMENT_CONFIDENCE[None]
    value = getattr(alignment, "value", alignment)
    return ALIGNMENT_CONFIDENCE.get(str(value), CONFIDENCE["inferred"])


# ---------------------------------------------------------------------------
# Char offsets -> page numbers
# ---------------------------------------------------------------------------

def page_offsets(store: Store, document_id: str) -> list[tuple[int, int, int]]:
    """`(page_no, start, end)` for the text `document_text()` produces.

    Built by reconstructing that string rather than by guessing, so an offset
    maps to a page exactly. The R implementation searched the excerpt for a
    "--- Page n ---" marker, which fails whenever an excerpt does not happen to
    span one.
    """
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for index, page in enumerate(document_pages(store, document_id)):
        if index:
            cursor += 2                                   # the "\n\n" join
        marker = f"--- Page {page['page_no']} ---\n"
        start = cursor
        cursor += len(marker) + len(page["text"] or "")
        spans.append((page["page_no"], start, cursor))
    return spans


def page_for_offset(spans: list[tuple[int, int, int]], offset: int | None) -> int | None:
    if offset is None:
        return None
    for page_no, start, end in spans:
        if start <= offset < end:
            return page_no
    return spans[-1][0] if spans else None


# ---------------------------------------------------------------------------
# Prompting from the bundle
# ---------------------------------------------------------------------------

def prompt_for(bundle: dict) -> str:
    """The extraction brief, written from the bundle rather than by hand.

    Adding a type to the bundle is the whole of adding a type: the prompt, the
    table and the validation all follow from the same declaration.
    """
    lines = [
        "Extract the entities this ontology describes from the document.",
        "Use the exact words of the document for every extracted span; do not "
        "paraphrase, summarise or infer values that are not written down.",
        "Do not overlap extractions.",
        "",
        "Entity types and their properties:",
    ]
    # The platform owns the review columns, and the container property holds an
    # internal instance id that links a child to its parent -- a value the model
    # cannot know. Asking for either invites an invention, and the container
    # link is established afterwards from the document structure anyway.
    withheld = set(RESERVED_PROPS)
    container = bundle_mod.domain(bundle).get("containerProperty")
    if container:
        withheld.add(container)

    for obj in bundle_mod.managed_object_types(bundle):
        managed_props = [p for p in obj.get("properties", [])
                         if p["id"] not in withheld]
        if not managed_props:
            continue
        label = bundle_mod.label(obj)
        description = (obj.get("display") or {}).get("description", "")
        lines.append(f"- {obj['id']} ({label}): {description}".rstrip(": "))
        for prop in managed_props:
            prop_desc = (prop.get("display") or {}).get("description", "")
            values = (prop.get("extensions") or {}).get("values")
            allowed = f" one of: {', '.join(values)}" if values else ""
            lines.append(f"    {prop['id']} ({prop['type']})"
                         f"{': ' + prop_desc if prop_desc else ''}{allowed}")
    return "\n".join(lines)


def examples_for(bundle: dict) -> list:
    """Few-shot examples, in LangExtract's own classes.

    Deliberately generic rather than contract-flavoured: the engine is
    domain-neutral and the examples teach the *shape* of an answer, not the
    domain. A deployment wanting domain examples puts them in the bundle.
    """
    import langextract as lx

    configured = bundle_mod.domain(bundle).get("extractionExamples")
    if configured:
        return [
            lx.data.ExampleData(
                text=example["text"],
                extractions=[
                    lx.data.Extraction(
                        extraction_class=item["type"],
                        extraction_text=item["text"],
                        attributes=item.get("properties", {}),
                    )
                    for item in example["extractions"]
                ],
            )
            for example in configured
        ]

    primary = bundle_mod.domain(bundle).get("primaryObjectType") or "Thing"
    return [
        lx.data.ExampleData(
            text="Reference AB-1. Agreed with Northwind Trading Limited.",
            extractions=[
                lx.data.Extraction(
                    extraction_class=primary,
                    extraction_text="Reference AB-1",
                    attributes={"reference": "AB-1"},
                ),
            ],
        )
    ]


# ---------------------------------------------------------------------------
# Running a pass
# ---------------------------------------------------------------------------

def populate(store: Store, document_id: str, bundle: dict | None = None,
             tier: str = "local", opt_in: bool = False,
             actor_id: str | None = None) -> dict:
    """Run one population pass and return the normalised shape."""
    bundle = bundle or bundle_mod.load()
    document = get_document(store, document_id)
    if document is None:
        raise OrpheusError(f"No document {document_id!r}.")

    text = document_text(store, document_id)
    spans = page_offsets(store, document_id)

    engine = _populator or _langextract_populate
    raw = engine(store=store, document=document, bundle=bundle, text=text,
                 tier=tier, opt_in=opt_in, actor_id=actor_id)
    return normalise_population(raw, spans=spans,
                                source_label=document.get("filename"))


def _langextract_populate(*, store: Store, document: dict, bundle: dict,
                          text: str, tier: str, opt_in: bool,
                          actor_id: str | None) -> dict:
    try:
        import langextract as lx
    except ImportError as exc:      # pragma: no cover - depends on the install
        raise OrpheusError(
            "LangExtract is not installed, and it is the extraction engine. "
            "Install it with `pip install 'orpheus[llm]'`, or register another "
            "engine with orpheus.population.set_populator()."
        ) from exc

    # The gate, before any text leaves. Local needs no permission; cloud needs
    # both the org policy and this request's opt-in.
    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)

    config = llm.model_config(store, tier)
    kwargs: dict[str, Any] = {
        "text_or_documents": text,
        "prompt_description": prompt_for(bundle),
        "examples": examples_for(bundle),
        "model_id": config["model_id"],
    }
    if config.get("model_url"):
        kwargs["model_url"] = config["model_url"]
    if config.get("api_key"):
        kwargs["api_key"] = config["api_key"]

    error = None
    annotated = None
    try:
        annotated = lx.extract(**kwargs)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # Recorded either way: the question the log answers is what left this
        # deployment, and a call that failed sent the payload just the same.
        llm.record_llm_call(
            store, tier=tier, purpose="populate", prompt_chars=len(text),
            provider=config["provider"], model=config["model_id"],
            document_id=document["document_id"], actor_id=actor_id,
            excerpt_only=False, payload=text, error=error,
        )
    if error:
        raise OrpheusError(f"Extraction failed: {error}")

    return {"extractions": list(getattr(annotated, "extractions", None) or [])}


# ---------------------------------------------------------------------------
# Normalising whatever the engine returned
# ---------------------------------------------------------------------------

def normalise_population(raw: Any, spans: list[tuple[int, int, int]] | None = None,
                         source_label: str | None = None) -> dict:
    """Translate an engine's output into the shape the store expects.

    Accepts either LangExtract's `Extraction` objects or the plain dicts a test
    double or another engine would return, so the seam does not force callers to
    depend on LangExtract's classes.
    """
    spans = spans or []
    if isinstance(raw, dict):
        extractions = raw.get("extractions") or raw.get("entities") or []
        relationships = raw.get("relationships") or []
        amendments = raw.get("amendments") or []
    else:
        extractions, relationships, amendments = list(raw or []), [], []

    entities = []
    for item in extractions:
        entities.append(_normalise_extraction(item, spans, source_label))

    known = {e["instance_id"] for e in entities}
    kept, dropped = [], 0
    for link in relationships:
        link = dict(link)
        if link.get("from_instance_id") in known and link.get("to_instance_id") in known:
            link["confidence"] = snap_confidence(link.get("confidence"))
            kept.append(link)
        else:
            # An engine may report an edge against an instance it did not
            # return. Dropping it here keeps the edge table referentially sound
            # rather than deferring the problem to whoever queries it.
            dropped += 1

    return {"entities": entities, "relationships": kept,
            "amendments": [dict(a) for a in amendments], "dropped_edges": dropped}


def _normalise_extraction(item: Any, spans, source_label) -> dict:
    if isinstance(item, dict):
        interval = item.get("char_interval") or {}
        start = interval.get("start_pos") if isinstance(interval, dict) else None
        end = interval.get("end_pos") if isinstance(interval, dict) else None
        alignment = item.get("alignment_status")
        type_id = item.get("type_id") or item.get("extraction_class") or "Unknown"
        excerpt = item.get("excerpt") or item.get("extraction_text") or ""
        properties = dict(item.get("properties") or item.get("attributes") or {})
        confidence = item.get("confidence")
        instance_id = item.get("instance_id")
    else:
        interval = getattr(item, "char_interval", None)
        start = getattr(interval, "start_pos", None) if interval else None
        end = getattr(interval, "end_pos", None) if interval else None
        alignment = getattr(item, "alignment_status", None)
        type_id = getattr(item, "extraction_class", None) or "Unknown"
        excerpt = getattr(item, "extraction_text", "") or ""
        properties = dict(getattr(item, "attributes", None) or {})
        confidence = None
        instance_id = None

    if confidence is None:
        confidence = confidence_for_alignment(alignment)

    return {
        "instance_id": instance_id or new_id("inst"),
        "type_id": type_id,
        "properties": properties,
        "confidence": snap_confidence(confidence),
        "excerpt": excerpt,
        "source_label": source_label or "",
        "page_no": page_for_offset(spans, start),
        # Kept because it is what a reading UI needs and what an excerpt string
        # cannot give: the exact span, not a phrase to go looking for.
        "char_start": start,
        "char_end": end,
        "alignment": (getattr(alignment, "value", alignment)
                      if alignment is not None else None),
    }
