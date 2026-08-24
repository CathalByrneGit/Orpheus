"""When sources agree, and whether the agreement means anything.

`tensions.py` gave a verified disagreement somewhere to live. That left the
mirror case with nothing: when four contracts independently name the same
director, the store holds four rows and knows nothing about the fact that they
agree. Agreement is evidence, and it was being thrown away.

**Corroboration does not move confidence.** The temptation is to combine — three
sources at 0.7 into 0.97, the way a product-complement would — and it is wrong
here for two reasons.

The first is the rubric. Confidence in this store is one of five levels, each
meaning something a reviewer can state: *explicit* is stated verbatim, *implied*
is mentioned with structure implied. A combined score is none of those, and once
one row carries 0.973 the levels have stopped meaning anything.

The second is worse. Combining assumes the sources are independent, and
documents in a real corpus are frequently not: an amendment quoting its parent,
a supplier declaration pasted into six tenders, a framework's boilerplate
inherited by every call-off under it. Six copies of one sentence is one source
wearing six hats, and treating it as six is manufacturing certainty out of
duplication — in a corpus assembled precisely because somebody suspects
something, in exactly the direction that suspicion would prefer.

So this counts and cites, and leaves the rubric alone. What it adds is the
thing a reader can check: *how many documents, in how many distinct wordings.*
Identical excerpts are grouped, so copied boilerplate shows as one wording
across six documents rather than as six agreeing sources. Two independent
wordings is corroboration. Six copies is a citation chain, and the difference is
visible rather than buried in a number.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from typing import Any

from . import graph as graph_mod
from .store import Store

# Below this, two excerpts are different wordings of the same claim. At or
# above it, one is a copy of the other and they are one source.
#
# Not calibrated against a corpus -- like the entity similarity threshold, it is
# a module setting and an argument because it will need revisiting. It is set
# high deliberately: the failure that matters is counting copies as independent
# sources, so the bias is toward calling near-identical text a copy.
COPY_THRESHOLD = 0.92

# Fewer than this and there is nothing to corroborate.
MIN_DOCUMENTS = 2

# Columns that are not claims about the thing. `name` is excluded because two
# spellings of one company are aliases, not agreement about a fact.
_NOT_A_CLAIM = {"instance_id", "document_id", "source", "confidence", "status",
                "amended_by", "amended_at", "created_at", "naive_key", "name"}


def _normalise(text: str | None) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def wordings(excerpts: list[dict]) -> list[dict]:
    """Group excerpts that say the same thing the same way.

    The whole honesty of this module sits here. Six documents carrying one
    pasted paragraph are one wording, and reporting them as six agreeing
    sources would be the exact error the module docstring refuses.
    """
    groups: list[dict] = []
    for excerpt in excerpts:
        text = _normalise(excerpt.get("excerpt"))
        placed = False
        for group in groups:
            if not text and not group["text"]:
                placed = True
            elif text and group["text"]:
                ratio = difflib.SequenceMatcher(None, text, group["text"]).ratio()
                placed = ratio >= COPY_THRESHOLD
            if placed:
                group["sources"].append(excerpt)
                break
        if not placed:
            groups.append({"text": text, "sources": [excerpt]})

    out = []
    for index, group in enumerate(groups, 1):
        documents = {s.get("document_id") for s in group["sources"]
                     if s.get("document_id")}
        out.append({
            "wording": index,
            "excerpt": next((s.get("excerpt") for s in group["sources"]
                             if s.get("excerpt")), None),
            "n_sources": len(group["sources"]),
            "n_documents": len(documents),
            "documents": sorted(documents),
            # More documents than wordings on this group means the same
            # sentence appears in several files.
            "copied": len(documents) > 1,
        })
    return out


def _independence(sources: list[dict]) -> dict:
    """How much of this agreement is actually independent."""
    grouped = wordings(sources)
    documents = {s.get("document_id") for s in sources if s.get("document_id")}
    n_wordings = len(grouped)

    if len(documents) < MIN_DOCUMENTS:
        note = "One document. Nothing corroborates it."
    elif n_wordings == 1:
        note = (f"{len(documents)} documents, one wording. The same sentence "
                f"appears in each, so this is one source quoted several times "
                f"rather than several sources agreeing.")
    elif n_wordings < len(documents):
        note = (f"{len(documents)} documents in {n_wordings} distinct wordings. "
                f"Some of the agreement is copied text; {n_wordings} of these "
                f"were written independently.")
    else:
        note = (f"{len(documents)} documents, each wording it differently. "
                f"This is independent agreement.")
    return {"n_documents": len(documents), "n_wordings": n_wordings,
            "wordings": grouped, "independent": n_wordings >= MIN_DOCUMENTS,
            "note": note}


# ---------------------------------------------------------------------------
# Properties several documents agree on
# ---------------------------------------------------------------------------

def corroborated_properties(store: Store, entity_id: str | None = None,
                            type_id: str | None = None,
                            min_documents: int = MIN_DOCUMENTS,
                            reviewed_only: bool = False) -> list[dict]:
    """Values that more than one document gives for the same property.

    The exact inverse of `tensions.detect_conflicts`, over the same join, so
    the two cannot disagree about what the mentions of an entity say.

    `reviewed_only` is off by default here, unlike conflict detection. Two
    unconfirmed extractions agreeing is weak but real evidence that the
    extraction is right; two unconfirmed extractions *disagreeing* is much more
    likely to be one bad extraction than a real conflict, which is why that
    direction demands review first.
    """
    if entity_id:
        entities = [e for e in [store.one(
            "SELECT * FROM entities WHERE entity_id = ?", (entity_id,))] if e]
    else:
        clauses = ["merged_into IS NULL"]
        params: list[Any] = []
        if type_id:
            clauses.append("type_id = ?")
            params.append(type_id)
        entities = store.query(
            f"SELECT * FROM entities WHERE {' AND '.join(clauses)}", tuple(params))

    out = []
    for entity in entities:
        links = store.query(
            "SELECT instance_id, document_id, status FROM entity_mentions "
            "WHERE entity_id = ? AND unlinked_at IS NULL", (entity["entity_id"],))
        if reviewed_only:
            links = [l for l in links if l["status"] in ("confirmed", "amended")]
        if len({l["document_id"] for l in links}) < min_documents:
            continue

        excerpts = {row["instance_id"]: row for row in store.query(
            "SELECT instance_id, excerpt, page_no FROM provenance "
            "WHERE instance_id IN (%s)" %
            ",".join("?" * len(links)), tuple(l["instance_id"] for l in links))} \
            if links else {}

        values: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for link in links:
            row = store.one(
                "SELECT i.table_name FROM instance_index i WHERE i.instance_id = ?",
                (link["instance_id"],))
            if row is None:
                continue
            detail = store.one(
                f'SELECT * FROM "{row["table_name"]}" WHERE instance_id = ?',
                (link["instance_id"],))
            if detail is None or detail.get("status") == "rejected":
                continue
            evidence = excerpts.get(link["instance_id"], {})
            for key, value in detail.items():
                if key in _NOT_A_CLAIM or value is None or value == "":
                    continue
                values[key][str(value)].append({
                    "instance_id": link["instance_id"],
                    "document_id": link["document_id"],
                    "excerpt": evidence.get("excerpt"),
                    "page_no": evidence.get("page_no"),
                    "status": detail.get("status"),
                })

        for key, by_value in values.items():
            for value, sources in by_value.items():
                if len({s["document_id"] for s in sources}) < min_documents:
                    continue
                independence = _independence(sources)
                out.append({
                    "scope": "entity", "subject_id": entity["entity_id"],
                    "subject_name": entity["canonical_name"],
                    "type_id": entity["type_id"],
                    "property_id": key, "value": value,
                    "sources": sources,
                    # Whether other values exist for the same property. Agreement
                    # among three sources means something different when a
                    # fourth disagrees, and a reader has to be told.
                    "n_other_values": len(by_value) - 1,
                    **independence,
                })
    out.sort(key=lambda c: (-c["n_wordings"], -c["n_documents"],
                            c["subject_name"], c["property_id"]))
    return out


# ---------------------------------------------------------------------------
# Relations several documents assert
# ---------------------------------------------------------------------------

def corroborated_relations(store: Store, min_documents: int = MIN_DOCUMENTS,
                           reviewed_only: bool = False) -> list[dict]:
    """Canonical edges more than one document asserts.

    Reads `graph.canonical_edges`, which has already done the projection from
    instance-level rows to pages, and applies the same wording test — a
    subcontracting clause copied verbatim into every call-off under a framework
    is one assertion, however many contracts carry it.
    """
    out = []
    for edge in graph_mod.canonical_edges(store, reviewed_only=reviewed_only):
        if edge["n_documents"] < min_documents:
            continue
        sources = [{"document_id": s["document_id"], "excerpt": s["evidence"],
                    "edge_id": s["edge_id"], "status": s["status"],
                    "filename": s["filename"]}
                   for s in edge["support"]]
        out.append({
            "scope": "relation",
            "from_entity_id": edge["from_entity_id"], "from_name": edge["from_name"],
            "to_entity_id": edge["to_entity_id"], "to_name": edge["to_name"],
            "link_type_id": edge["link_type_id"],
            "sources": sources,
            "n_confirmed": edge["n_confirmed"],
            "max_confidence": edge["max_confidence"],
            **_independence(sources),
        })
    out.sort(key=lambda c: (-c["n_wordings"], -c["n_documents"], c["from_name"]))
    return out


# ---------------------------------------------------------------------------
# For a page, and for the corpus
# ---------------------------------------------------------------------------

def for_entity(store: Store, entity_id: str) -> dict:
    """Everything corroborated about one page, for the page to render."""
    properties = corroborated_properties(store, entity_id=entity_id)
    relations = [r for r in corroborated_relations(store)
                 if entity_id in (r["from_entity_id"], r["to_entity_id"])]
    return {
        "properties": properties,
        "relations": relations,
        # What the page marks. Only genuinely independent agreement earns it:
        # a property held up by six copies of one sentence is not corroborated,
        # and marking it so would be the error this module exists to avoid.
        "corroborated_properties": sorted(
            {c["property_id"] for c in properties if c["independent"]}),
        "copied_properties": sorted(
            {c["property_id"] for c in properties if not c["independent"]}),
    }


def summary(store: Store, min_documents: int = MIN_DOCUMENTS) -> dict:
    """Where the corpus agrees with itself, and where it is quoting itself."""
    properties = corroborated_properties(store, min_documents=min_documents)
    relations = corroborated_relations(store, min_documents=min_documents)
    everything = properties + relations
    independent = [c for c in everything if c["independent"]]
    copied = [c for c in everything if not c["independent"]]

    if not everything:
        headline = ("Nothing is asserted by more than one document. Either the "
                    "corpus does not overlap, or the wiki has not been built "
                    "far enough for the overlap to be visible.")
    elif not independent:
        headline = (f"{len(everything)} claim(s) appear in several documents, "
                    f"and every one is the same wording repeated. That is a "
                    f"citation chain, not corroboration.")
    else:
        headline = (f"{len(independent)} claim(s) are asserted independently by "
                    f"two or more documents. {len(copied)} more appear in "
                    f"several documents in one wording, which is copied text "
                    f"rather than agreement.")
    return {
        "headline": headline,
        "n_corroborated": len(independent),
        "n_copied": len(copied),
        "properties": properties,
        "relations": relations,
        "note": ("Corroboration is counted in distinct wordings across distinct "
                 "documents, and does not change any confidence value. "
                 "Confidence says how sure one extraction is; this says how "
                 "many independent sources there are. Combining them would put "
                 "a number on the rubric that no reviewer could state."),
    }
