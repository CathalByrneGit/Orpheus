"""Reading a document with the machine, a passage at a time.

The plugin reviews a document *after* extraction has run over the whole of it.
That is the right shape for grading a batch and the wrong shape for the thing
this project was started to do: a person and a machine going through a file
together, the machine offering things worth recording and the person deciding.

Three problems stood between the two. Identity is settled — the plugin
provisions an actor from whatever Datasette authenticated. The other two are
what this module is.

**Latency.** A companion reacting to a person scrolling cannot take seconds. So
the unit is the page, which is already how text is stored, and the default
engine is the deterministic pass — pattern matching over one page, microseconds,
no model, no network, no gate. A model engine can be asked for per passage and
goes through the same cloud gate as everything else.

**The unit of provenance.** This is the part that matters, and it is not a
performance question.

A batch extraction is a deliberate act: somebody asked for it, over a whole
document, and the queue it produces is the thing they wanted. A companion firing
as a person reads produces proposals nobody asked for, most of which will be
ignored — and if those land as `unconfirmed` instances they flood the review
queue, let `propose_entities()` build wiki pages out of guesses nobody looked at,
and pollute `extraction_quality` with proposals that were never reviewed. That
last one is the number Phase 1 turns on.

So **a suggestion is not an instance.** It lives in its own table until a person
accepts it, and accepting writes the row through the same `insert_instance` and
`write_provenance` path a batch pass uses — one way an instance comes into
being, still. Dismissals are kept, like rejected instances, because they measure
how good the suggestions are.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import bundle as bundle_mod
from .audit import record_edit
from .deterministic import (AMOUNT_ROLE_CUES, DATE_ROLE_CUES, find_amounts,
                            find_dates, infer_role)
from .align import MATCH_EXACT, align
from .extract import excerpt_around, insert_instance, write_provenance
from .population import confidence_for_alignment, page_offsets
from .rubric import SUGGESTION_STATUSES
from .store import Store
from .utils import (NotFound, OrpheusError, from_json, new_id, now,
                    require_choice, require_string, to_json)

# The always-available engine: no model, no network, no opt-in, microseconds
# over one page. Anything slower than this is not a companion.
DEFAULT_ENGINE = "deterministic"


def _fingerprint(type_id: str, properties: dict, page_no: int | None) -> str:
    """What makes two offers the same offer.

    Keyed on the page and the values rather than on the excerpt, so re-running a
    passage with a different engine does not re-offer what a person already
    dismissed. Without this, scrolling back is unusable and the companion
    becomes something to close.
    """
    payload = json.dumps({"type": type_id, "page": page_no,
                          "properties": {k: str(v) for k, v in
                                         sorted((properties or {}).items())}},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Reading one passage
# ---------------------------------------------------------------------------

def read_passage(store: Store, document_id: str, page_no: int,
                 actor_id: str | None = None, engine: str = DEFAULT_ENGINE,
                 tier: str = "local", opt_in: bool = False,
                 bundle: dict | None = None) -> dict:
    """Offer what this page seems to contain, and record that it was read.

    Idempotent by construction: a page read twice re-offers only what is still
    outstanding. What was accepted is an instance now, and what was dismissed
    stays dismissed.
    """
    store.assert_writable()
    page = store.one(
        "SELECT page_no, text FROM document_pages "
        "WHERE document_id = ? AND page_no = ?", (document_id, page_no))
    if page is None:
        raise NotFound(f"No page {page_no} in {document_id}.")
    bundle = bundle or bundle_mod.active(store)
    if bundle is None:
        raise OrpheusError("No bundle is registered, so nothing can be typed.")

    text = page["text"] or ""
    found = (_deterministic_offers(text, page_no) if engine == DEFAULT_ENGINE
             else _engine_offers(store, document_id, page_no, text, bundle,
                                 engine, tier, opt_in, actor_id))

    # Page-local offsets are lifted to document offsets here, for the same
    # reason the batch pass does it: both write to the same columns, and a
    # reading surface that highlights the right span on the wrong page is worse
    # than one that highlights nothing.
    offset = dict((p, start + len(f"--- Page {p} ---\n"))
                  for p, start, _ in page_offsets(store, document_id)
                  ).get(page_no, 0)

    offered = []
    with store.transaction():
        for item in found:
            fingerprint = _fingerprint(item["type_id"], item["properties"], page_no)
            settled = store.one(
                "SELECT suggestion_id, status FROM suggestions "
                "WHERE document_id = ? AND fingerprint = ? "
                "AND status IN ('accepted', 'dismissed') LIMIT 1",
                (document_id, fingerprint))
            if settled:
                continue
            live = store.one(
                "SELECT suggestion_id FROM suggestions WHERE document_id = ? "
                "AND fingerprint = ? AND status = 'offered'",
                (document_id, fingerprint))
            if live:
                offered.append(get_suggestion(store, live["suggestion_id"]))
                continue

            suggestion_id = new_id("sug")
            start = item.get("char_start")
            store.insert("suggestions", {
                "suggestion_id": suggestion_id,
                "document_id": document_id,
                "page_no": page_no,
                "type_id": item["type_id"],
                "properties_json": to_json(item["properties"]),
                "excerpt": item.get("excerpt"),
                "char_start": None if start is None else offset + start,
                "char_end": None if item.get("char_end") is None
                            else offset + item["char_end"],
                "alignment": item.get("alignment"),
                "engine": engine,
                "source": item.get("source", "ai_local"),
                "confidence": item["confidence"],
                "fingerprint": fingerprint,
                "status": "offered",
                "instance_id": None,
                "suggested_at": now(),
                "decided_by": None,
                "decided_at": None,
                "note": None,
            })
            offered.append(get_suggestion(store, suggestion_id))

        # Recorded whether or not anything was found. A page read and found to
        # hold nothing is not the same as a page nobody has opened, and only
        # this row can tell them apart.
        store.execute(
            "INSERT INTO reading_passages (document_id, page_no, actor_id, "
            "engine, n_suggested, first_read, last_read, n_reads) "
            "VALUES (?,?,?,?,?,?,?,1) "
            "ON CONFLICT(document_id, page_no, actor_id, engine) DO UPDATE SET "
            "last_read = excluded.last_read, n_reads = n_reads + 1, "
            "n_suggested = n_suggested + excluded.n_suggested",
            (document_id, page_no, actor_id, engine, len(offered), now(), now()))

    return {"document_id": document_id, "page_no": page_no, "engine": engine,
            "suggestions": offered, "n_offered": len(offered),
            "text": text}


def _deterministic_offers(text: str, page_no: int) -> list[dict]:
    """Dates and money by pattern. Grounded by construction — it cannot offer
    something the page does not contain, which is what separates it from a
    model and why it needs no opt-in."""
    offers = []
    for found in find_dates(text):
        offers.append({
            "type_id": "KeyDate",
            "properties": {"value": found["value"], "raw_text": found["raw_text"],
                           "date_role": infer_role(text, found["position"],
                                                   DATE_ROLE_CUES),
                           "page_no": page_no},
            "excerpt": excerpt_around(text, found["position"], found["raw_text"]),
            "char_start": found["position"],
            "char_end": found["position"] + len(found["raw_text"]),
            "alignment": MATCH_EXACT, "source": "ai_local",
            "confidence": found["confidence"],
        })
    for found in find_amounts(text):
        offers.append({
            "type_id": "MonetaryAmount",
            "properties": {"amount": found["amount"], "currency": found["currency"],
                           "raw_text": found["raw_text"],
                           "role": infer_role(text, found["position"],
                                              AMOUNT_ROLE_CUES),
                           "page_no": page_no},
            "excerpt": excerpt_around(text, found["position"], found["raw_text"]),
            "char_start": found["position"],
            "char_end": found["position"] + len(found["raw_text"]),
            "alignment": MATCH_EXACT, "source": "ai_local",
            "confidence": found["confidence"],
        })
    return offers


def _engine_offers(store: Store, document_id: str, page_no: int, text: str,
                   bundle: dict, engine: str, tier: str, opt_in: bool,
                   actor_id: str | None) -> list[dict]:
    """A model, over one page rather than the whole document.

    Goes through the registry and the cloud gate exactly as a batch run does —
    a companion is not a reason to send text somewhere a batch could not.
    """
    from .engines import get_engine
    from .population import normalise_population

    document = store.one("SELECT * FROM documents WHERE document_id = ?",
                         (document_id,))
    raw = get_engine(engine)(store=store, document=document, bundle=bundle,
                             text=text, tier=tier, opt_in=opt_in,
                             actor_id=actor_id)
    population = normalise_population(raw, source_label=f"page {page_no}",
                                      text=text)
    offers = []
    for entity in population.get("entities", []):
        properties = dict(entity.get("properties") or {})
        properties.setdefault("page_no", page_no)
        offers.append({
            "type_id": entity["type_id"], "properties": properties,
            "excerpt": entity.get("excerpt"),
            "char_start": entity.get("char_start"),
            "char_end": entity.get("char_end"),
            "alignment": entity.get("alignment"),
            "source": "ai_cloud" if tier == "cloud" else "ai_local",
            "confidence": entity.get("confidence"),
        })
    return offers


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def propose(store: Store, document_id: str, page_no: int, type_id: str,
            properties: dict, quote: str, engine: str,
            actor_id: str | None = None, bundle: dict | None = None) -> dict:
    """Offer something that did not come from a pass over the page.

    A page read is not the only way the machine proposes. Somebody reading with
    a chat beside them gets offers too, and those were going straight to
    `record` -- an instance written, and no trace at all when the offer was
    declined. The companion keeps a dismissal precisely because it is the only
    evidence there is about whether these offers are worth reading, and an
    offer that skips this table cannot be measured by anything.

    So a proposal from anywhere lands here first, and is accepted or dismissed
    through the same two functions as any other. `engine` says where it came
    from, which is what makes the rate answerable per source rather than in
    aggregate.

    Note this is for a *machine's* offer. A person recording what they read
    themselves has no offer to measure and goes through `record.record_fact`.
    """
    store.assert_writable()
    quote = (quote or "").strip()
    if not quote:
        raise OrpheusError(
            "An offer has to quote the page it came from. Without it there is "
            "nothing for the next person to check, and nothing to locate.")

    page = store.one(
        "SELECT page_no, text FROM document_pages "
        "WHERE document_id = ? AND page_no = ?", (document_id, page_no))
    if page is None:
        raise NotFound(f"No page {page_no} in {document_id}.")

    bundle = bundle or bundle_mod.active(store)
    if bundle is None:
        raise OrpheusError("No bundle is registered, so nothing can be typed.")
    if bundle_mod.object_type(bundle, type_id) is None:
        raise OrpheusError(
            f"{type_id} is not a type this bundle declares, so there is "
            "nowhere for this to go. A schema amendment comes first.")

    # Located in the page, by the code that locates a model's quotations
    # anywhere else. An offer citing text the page does not contain is the one
    # kind this surface must not carry: a person skimming offers is reading the
    # excerpt, not the document.
    start, end, alignment = align(page["text"] or "", quote)
    if alignment is None or start is None:
        raise OrpheusError(
            f"Page {page_no} of {document_id} does not contain that quote, so "
            "it cannot be offered against it. Quote the page as written.")

    offset = dict((n, begin + len(f"--- Page {n} ---\n"))
                  for n, begin, _ in page_offsets(store, document_id)
                  ).get(page_no, 0)

    fingerprint = _fingerprint(type_id, properties, page_no)
    with store.transaction():
        settled = store.one(
            "SELECT suggestion_id, status FROM suggestions "
            "WHERE document_id = ? AND fingerprint = ? "
            "AND status IN ('accepted', 'dismissed') LIMIT 1",
            (document_id, fingerprint))
        if settled:
            raise OrpheusError(
                f"That was already {settled['status']} on this page. Re-offering "
                "what somebody has settled is how a queue becomes something to "
                "be closed.")
        live = store.one(
            "SELECT suggestion_id FROM suggestions WHERE document_id = ? "
            "AND fingerprint = ? AND status = 'offered'",
            (document_id, fingerprint))
        if live:
            return get_suggestion(store, live["suggestion_id"])

        suggestion_id = new_id("sug")
        store.insert("suggestions", {
            "suggestion_id": suggestion_id,
            "document_id": document_id,
            "page_no": page_no,
            "type_id": type_id,
            "properties_json": to_json(properties),
            "excerpt": (page["text"] or "")[start:end],
            "char_start": offset + start,
            "char_end": offset + end,
            "alignment": alignment,
            "engine": engine,
            "source": "ai_cloud",
            "confidence": confidence_for_alignment(alignment),
            "fingerprint": fingerprint,
            "status": "offered",
            "instance_id": None,
            "suggested_at": now(),
            "decided_by": None,
            "decided_at": None,
            "note": None,
        })
    return get_suggestion(store, suggestion_id)


def accept_suggestion(store: Store, suggestion_id: str, actor_id: str,
                      properties: dict | None = None,
                      bundle: dict | None = None, note: str | None = None) -> dict:
    """Record it, through the path every other instance comes in by.

    `properties` corrects it on the way in — the companion's equivalent of
    amending, and the common case: the machine spotted the right thing and got
    one field wrong. What it offered is kept on the suggestion row either way,
    so "what did the machine say before a person fixed it" stays answerable.
    """
    store.assert_writable()
    suggestion = get_suggestion(store, suggestion_id)
    if suggestion["status"] != "offered":
        raise OrpheusError(
            f"That suggestion was {suggestion['status']} on "
            f"{suggestion['decided_at']}. Read the passage again to be offered "
            "anything still outstanding.")

    bundle = bundle or bundle_mod.active(store)
    values = dict(suggestion["properties"] or {})
    corrected = {k: v for k, v in (properties or {}).items()
                 if str(values.get(k, "")) != str(v)}
    values.update(properties or {})

    instance_id = new_id("inst")
    with store.transaction():
        written = insert_instance(
            store, bundle, suggestion["type_id"], instance_id,
            suggestion["document_id"], values,
            # A person accepted it, so the row is theirs. `source` says who
            # vouches for the value; the suggestion row keeps what the machine
            # actually offered, and the engine that offered it.
            "human", suggestion["confidence"], status="confirmed",
            actor_id=actor_id)
        if written is None:
            raise OrpheusError(
                f"{suggestion['type_id']} is not a type this bundle declares, "
                "so the suggestion cannot be recorded. A schema amendment was.")
        write_provenance(
            store, instance_id, suggestion["document_id"],
            f"companion:{suggestion['engine']}", suggestion["source"],
            suggestion["page_no"], suggestion["excerpt"],
            suggestion["confidence"], alignment=suggestion["alignment"],
            char_start=suggestion["char_start"], char_end=suggestion["char_end"])
        store.execute(
            "UPDATE suggestions SET status = 'accepted', instance_id = ?, "
            "decided_by = ?, decided_at = ?, note = ? WHERE suggestion_id = ?",
            (instance_id, actor_id, now(), note, suggestion_id))
        record_edit(store, "suggestions", suggestion_id,
                    suggestion["document_id"], "accept_suggestion",
                    previous=suggestion["properties"], new=values,
                    actor_id=actor_id, note=note)
    return {**get_suggestion(store, suggestion_id), "corrected": corrected}


def dismiss_suggestion(store: Store, suggestion_id: str, actor_id: str,
                       note: str | None = None) -> dict:
    """Not worth recording. Kept, not deleted.

    A dismissed suggestion is the only evidence there is about how good the
    suggestions are — the same reason a rejected instance is kept, and the
    reason the companion can be measured at all rather than merely felt.
    """
    store.assert_writable()
    suggestion = get_suggestion(store, suggestion_id)
    if suggestion["status"] != "offered":
        raise OrpheusError(f"That suggestion was already {suggestion['status']}.")
    with store.transaction():
        store.execute(
            "UPDATE suggestions SET status = 'dismissed', decided_by = ?, "
            "decided_at = ?, note = ? WHERE suggestion_id = ?",
            (actor_id, now(), note, suggestion_id))
        record_edit(store, "suggestions", suggestion_id,
                    suggestion["document_id"], "dismiss_suggestion",
                    previous=suggestion["properties"], actor_id=actor_id,
                    note=note)
    return get_suggestion(store, suggestion_id)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get_suggestion(store: Store, suggestion_id: str) -> dict:
    row = store.one("SELECT * FROM suggestions WHERE suggestion_id = ?",
                    (suggestion_id,))
    if row is None:
        raise NotFound(f"No suggestion {suggestion_id!r}.")
    return {**row, "properties": from_json(row["properties_json"]) or {}}


def passage(store: Store, document_id: str, page_no: int,
            status: str = "offered") -> dict:
    """One page, its text, and what stands on it."""
    require_choice(status, SUGGESTION_STATUSES + ("all",), "status")
    page = store.one(
        "SELECT page_no, text, char_count FROM document_pages "
        "WHERE document_id = ? AND page_no = ?", (document_id, page_no))
    if page is None:
        raise NotFound(f"No page {page_no} in {document_id}.")
    clause = "" if status == "all" else " AND status = ?"
    params: tuple = ((document_id, page_no) if status == "all"
                     else (document_id, page_no, status))
    rows = store.query(
        f"SELECT * FROM suggestions WHERE document_id = ? AND page_no = ?"
        f"{clause} ORDER BY char_start", params)
    reads = store.query(
        "SELECT actor_id, engine, n_reads, first_read, last_read "
        "FROM reading_passages WHERE document_id = ? AND page_no = ?",
        (document_id, page_no))
    return {
        "document_id": document_id, "page_no": page_no,
        "text": page["text"], "char_count": page["char_count"],
        "suggestions": [{**r, "properties": from_json(r["properties_json"]) or {}}
                        for r in rows],
        "read_by": reads,
        "has_been_read": bool(reads),
    }


def reading_progress(store: Store, document_id: str,
                     actor_id: str | None = None) -> dict:
    """How far through this document a person and the machine have got.

    The honest measure of a companion, and the reason `reading_passages` exists:
    pages read is a fact about the person, pages with findings is a fact about
    the document, and reporting the second as the first would overstate how much
    has actually been looked at.
    """
    total = store.scalar(
        "SELECT COUNT(*) FROM document_pages WHERE document_id = ?",
        (document_id,)) or 0
    clause, params = ((" AND actor_id = ?", (document_id, actor_id))
                      if actor_id else ("", (document_id,)))
    read = store.scalar(
        "SELECT COUNT(DISTINCT page_no) FROM reading_passages "
        f"WHERE document_id = ?{clause}", params) or 0
    counts = {row["status"]: row["n"] for row in store.query(
        "SELECT status, COUNT(*) AS n FROM suggestions WHERE document_id = ? "
        "GROUP BY status", (document_id,))}
    outstanding = counts.get("offered", 0)
    accepted = counts.get("accepted", 0)
    dismissed = counts.get("dismissed", 0)
    decided = accepted + dismissed

    if not total:
        note = "This document has no page text, so there is nothing to read."
    elif not read:
        note = f"None of the {total} page(s) has been read yet."
    else:
        note = (f"{read} of {total} page(s) read. {accepted} suggestion(s) "
                f"recorded, {dismissed} dismissed, {outstanding} still offered.")
        if decided:
            # The number that says whether the companion is worth having. A
            # low rate is not a failure to report -- it is the measurement.
            note += (f" {accepted / decided:.0%} of what was decided on was "
                     f"worth recording.")
    return {
        "n_pages": total, "n_read": read,
        "unread": [r["page_no"] for r in store.query(
            "SELECT page_no FROM document_pages WHERE document_id = ? "
            "AND page_no NOT IN (SELECT page_no FROM reading_passages "
            f"WHERE document_id = ?{clause}) ORDER BY page_no",
            (document_id, *params))],
        "n_accepted": accepted, "n_dismissed": dismissed,
        "n_offered": outstanding,
        "acceptance_rate": round(accepted / decided, 3) if decided else None,
        "note": note,
    }


def suggestion_quality(store: Store, document_id: str | None = None) -> dict:
    """How often the companion was right, by engine.

    Deliberately separate from `quality.extraction_quality()`. That measures
    extraction against review; this measures *offers* against a person's
    attention, and mixing them would answer neither question.
    """
    clause, params = ((" WHERE document_id = ?", (document_id,))
                      if document_id else ("", ()))
    rows = store.query(
        "SELECT engine, status, COUNT(*) AS n FROM suggestions"
        f"{clause} GROUP BY engine, status", params)
    by_engine: dict[str, dict] = {}
    for row in rows:
        entry = by_engine.setdefault(row["engine"], {
            "engine": row["engine"], "offered": 0, "accepted": 0, "dismissed": 0})
        entry[row["status"]] = row["n"]
    out = []
    for entry in by_engine.values():
        decided = entry["accepted"] + entry["dismissed"]
        entry["n_decided"] = decided
        entry["acceptance_rate"] = (round(entry["accepted"] / decided, 3)
                                    if decided else None)
        out.append(entry)
    out.sort(key=lambda e: e["engine"])

    judged = [e for e in out if e["n_decided"]]
    if not judged:
        note = ("Nothing offered has been decided on yet, so there is no "
                "acceptance rate. Offers nobody has looked at say nothing about "
                "whether the companion is useful.")
    else:
        best = max(judged, key=lambda e: e["acceptance_rate"])
        note = (f"{best['engine']} had {best['acceptance_rate']:.0%} of its "
                f"decided suggestions accepted, over {best['n_decided']}.")
    return {"by_engine": out, "note": note}
