"""Two different pasts, kept apart on purpose.

An amendment dated 3 March and recorded in the store on 14 November is true
from March and *known* from November. Ask what a contract said on 1 June and
there are two right answers:

- **What was true then.** The amendment was in force. Anyone reading the
  contract on 1 June, correctly, would have read EUR 310,000.
- **What we believed then.** Nobody here had read the amendment yet. Every
  report this store produced on 1 June said EUR 250,000, and said it honestly.

Both matter, and they answer different questions. *Was the department paying
the right rate in June* is valid time. *Why did the June report say what it
said* is transaction time, and it is the one that exonerates or indicts the
people who acted on it. A system that answers one without saying which is
lying by omission -- and the second question is the one almost no system can
answer at all, because almost none keeps the record.

This one does. `edit_history` has an unbroken `seq` chain, and every review
action names the status it produced (`confirm` → `confirmed`, `reject` →
`rejected`) rather than carrying it in a payload. That distinction turns out to
matter twice over: it means transaction time survives redaction, which nulls
`previous_value` and `new_value` but leaves the actions in place. What a
redacted document *said* is gone; that somebody confirmed something about it on
a given date is still answerable, which is exactly the split redaction was
built to make.

So: two functions, deliberately not one, and neither will produce the other's
answer.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from .audit import row_history
from .rubric import (EXCLUDED_STATUSES, REVIEWED_STATUSES,
                     confidence_label)
from .store import Store
from .utils import NotFound, OrpheusError, from_json, now

#: `edit_history.action` → the status it left behind. The action names the
#: outcome, which is what makes replay possible without the payloads.
STATUS_AFTER = {
    "extract": "unconfirmed",
    "create": "unconfirmed",
    "confirm": "confirmed",
    "amend": "amended",
    "reject": "rejected",
}


def _bound(when: str) -> str:
    """A plain date means the whole of that day.

    `2026-06-01` compared against `2026-06-01T14:22:00Z` as strings would
    exclude everything recorded that day, which is not what anybody means by
    "as of the first of June".
    """
    when = (when or "").strip()
    if not when:
        raise OrpheusError("Give a date to ask about, as YYYY-MM-DD.")
    if len(when) == 10:
        return when + "T23:59:59Z"
    return when


def believed_at(store: Store, when: str, *, document_id: str | None = None) -> dict:
    """Transaction time: the review state of the store as it stood on that date.

    Reconstructed rather than stored. An instance existed if its `created_at`
    is not after the moment asked about; its status is whatever the last review
    action at or before that moment left it at, and `unconfirmed` if there was
    none.

    The report this returns is the report that *would have been produced* then,
    which is a different thing from today's report filtered to old rows. It is
    the answer to "why did the June figure say what it said", and it is worth
    having because that question is normally unanswerable and normally asked
    only once something has gone wrong.
    """
    at = _bound(when)
    bundle = bundle_mod.active(store) or bundle_mod.load()
    scope = " AND i.document_id = ?" if document_id else ""
    params: tuple = (at,) + ((document_id,) if document_id else ())

    # The last review action per instance at or before the moment. Ordered by
    # `seq` rather than by `edited_at`: three reviews inside one second are
    # unorderable by timestamp, and the sequence is the record of what
    # happened when.
    settled: dict[str, str] = {}
    for row in store.query(
            "SELECT row_id, action FROM edit_history "
            "WHERE edited_at <= ? AND action IN "
            "('confirm', 'amend', 'reject') ORDER BY seq", (at,)):
        settled[row["row_id"]] = STATUS_AFTER[row["action"]]

    rows: list[dict] = []
    for obj in bundle_mod.managed_object_types(bundle):
        table = bundle_mod.table_name(obj)
        if not table or not store.table_exists(table):
            continue
        for row in store.query(
                f'SELECT i.instance_id, i.document_id, i.created_at, '
                f"       p.confidence, p.source "
                f'FROM "{table}" i '
                "LEFT JOIN provenance p ON p.instance_id = i.instance_id "
                f"WHERE i.created_at <= ? AND IFNULL(p.source, '') != 'human'"
                f"{scope}", params):
            if row["confidence"] is None:
                continue
            rows.append({"instance_id": row["instance_id"],
                         "document_id": row["document_id"],
                         "type_id": obj["id"],
                         "confidence": row["confidence"],
                         "source": row["source"] or "unknown",
                         "status": settled.get(row["instance_id"], "unconfirmed")})

    n_reviewed = sum(1 for r in rows if r["status"] in REVIEWED_STATUSES)
    documents = store.scalar(
        "SELECT COUNT(*) FROM documents WHERE date_added <= ?"
        + (" AND document_id = ?" if document_id else ""), params)

    return {
        "axis": "transaction",
        "as_of": when,
        "n_documents": documents,
        "n_instances": len(rows),
        "n_reviewed": n_reviewed,
        "by_confidence": _by_confidence(rows),
        "verdict": _verdict(rows),
        "headline": (
            f"As of {when} this store held {documents} document(s) and "
            f"{len(rows)} extraction(s), {n_reviewed} of them reviewed. This is "
            "what it believed then, not what it holds now."),
    }


def _by_confidence(rows: list[dict]) -> list[dict]:
    groups: dict[float, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["confidence"], []).append(row)
    out = []
    for level in sorted(groups, reverse=True):
        part = groups[level]
        reviewed = [r for r in part if r["status"] in REVIEWED_STATUSES]
        confirmed = sum(1 for r in reviewed if r["status"] == "confirmed")
        out.append({
            "confidence": level,
            "confidence_label": confidence_label(level),
            "n_total": len(part), "n_reviewed": len(reviewed),
            "accuracy": (round(confirmed / len(reviewed), 3)
                         if reviewed else None),
        })
    return out


def _verdict(rows: list[dict], min_reviewed: int = 5) -> str:
    """The same threshold `confidence_calibration` holds today.

    Applied to the past so that "the report was silent then and speaks now" is
    itself a visible change, rather than the reason two runs look
    incomparable.
    """
    levels = [g for g in _by_confidence(rows) if g["n_reviewed"] >= min_reviewed]
    if len(levels) < 2:
        return "insufficient_evidence"
    accuracies = [g["accuracy"] for g in levels]
    if any(a < b for a, b in zip(accuracies, accuracies[1:])):
        return "inverted"
    return "flat" if len(set(accuracies)) == 1 else "monotonic"


def value_history(store: Store, instance_id: str,
                  property_id: str | None = None) -> dict:
    """What one extracted value has said, and from when.

    The third question the two axes make askable, and the one somebody asks out
    loud: *the contract is for EUR 310,000 -- what was it before, and who
    changed it?* `row_history` has held the raw answer since the audit trail
    was written; this reads it as a timeline per property rather than as a list
    of edits, which is the shape the question has.

    Transaction time throughout: these are the moments the *store* changed its
    mind. When the underlying document changed is a different question, and one
    only an amending document can answer.

    A redacted document has no value history to ask for: redaction deletes the
    instances themselves, so there is no `instance_id` left to name and this
    answers `NotFound`. What survives is the document's own history -- that
    somebody amended something on a date -- with the values gone from it, which
    is exactly the split redaction was built to make.
    """
    location = store.one(
        "SELECT table_name, document_id FROM instance_index "
        "WHERE instance_id = ?", (instance_id,))
    if location is None:
        raise NotFound(f"No instance {instance_id!r}.")
    timeline: dict[str, list[dict]] = {}
    for edit in row_history(store, location["table_name"], instance_id):
        new_value = from_json(edit["new_value"]) or {}
        previous = from_json(edit["previous_value"]) or {}
        if edit["action"] == "extract":
            new_value = {**(new_value.get("properties") or {}),
                         **{k: v for k, v in new_value.items()
                            if k not in ("properties",)}}
        for key, value in new_value.items():
            if property_id and key != property_id:
                continue
            if key in ("type_id", "source", "confidence", "char_start",
                       "char_end", "span", "status"):
                continue
            timeline.setdefault(key, []).append({
                "seq": edit["seq"], "at": edit["edited_at"],
                "action": edit["action"], "by": edit["edited_by"],
                "note": edit["note"],
                "was": previous.get(key), "became": value,
            })

    return {
        "instance_id": instance_id,
        "document_id": location["document_id"],
        "properties": timeline,
        "note": ("Transaction time: when this store changed its mind, not when "
                 "the document did. Only an amending document can say that."),
    }


def in_force_on(store: Store, when: str, *, document_id: str | None = None) -> dict:
    """Valid time: which contracts the corpus says were running on that date.

    Read off the documents' own dates -- a `start` role at or before, and
    either no `end` role or one at or after. Nothing to do with when any of it
    was extracted or reviewed.

    Two honesty rules, both about what cannot be placed on a timeline at all.
    A document with no `start` date has no beginning to compare against, so it
    is neither in force nor out of force; it is *unplaceable*, and counting it
    either way would be inventing a fact. And an unconfirmed date is still a
    machine reading, so the split is reported the way the calendar reports it.
    """
    day = (when or "").strip()[:10]
    if len(day) != 10:
        raise OrpheusError("Give a date to ask about, as YYYY-MM-DD.")
    bundle = bundle_mod.active(store) or bundle_mod.load()
    obj = bundle_mod.object_type(bundle, "KeyDate")
    table = bundle_mod.table_name(obj) if obj else None
    scope = " AND document_id = ?" if document_id else ""
    params = (document_id,) if document_id else ()

    starts: dict[str, list[dict]] = {}
    ends: dict[str, list[dict]] = {}
    if table and store.table_exists(table):
        for row in store.query(
                f'SELECT document_id, value, date_role, status FROM "{table}" '
                f"WHERE value IS NOT NULL AND value != ''{scope}", params):
            if row["status"] in EXCLUDED_STATUSES:
                continue
            bucket = (starts if row["date_role"] == "start"
                      else ends if row["date_role"] in ("end", "renewal")
                      else None)
            if bucket is not None:
                bucket.setdefault(row["document_id"], []).append(dict(row))

    documents = store.query(
        "SELECT document_id, filename, redacted_at FROM documents"
        + (" WHERE document_id = ?" if document_id else ""), params)

    in_force, ended, not_yet, unplaceable = [], [], [], []
    for document in documents:
        if document["redacted_at"]:
            continue
        began = min((r["value"] for r in starts.get(document["document_id"], [])),
                    default=None)
        finished = max((r["value"] for r in ends.get(document["document_id"], [])),
                       default=None)
        reviewed = all(r["status"] in REVIEWED_STATUSES
                       for r in (starts.get(document["document_id"], [])
                                 + ends.get(document["document_id"], [])))
        entry = {"document_id": document["document_id"],
                 "filename": document["filename"],
                 "started": began, "ended": finished, "reviewed": reviewed}
        if began is None:
            unplaceable.append(entry)
        elif began > day:
            not_yet.append(entry)
        elif finished is not None and finished < day:
            ended.append(entry)
        else:
            in_force.append(entry)

    return {
        "axis": "valid",
        "on": day,
        "in_force": sorted(in_force, key=lambda e: e["started"]),
        "not_yet_begun": sorted(not_yet, key=lambda e: e["started"]),
        "ended": sorted(ended, key=lambda e: e["ended"] or ""),
        "unplaceable": unplaceable,
        "n_in_force": len(in_force),
        "n_unplaceable": len(unplaceable),
        "headline": _in_force_headline(day, in_force, ended, not_yet, unplaceable),
    }


def _in_force_headline(day: str, in_force: list, ended: list, not_yet: list,
                       unplaceable: list) -> str:
    total = len(in_force) + len(ended) + len(not_yet) + len(unplaceable)
    if not total:
        return "No documents."
    unchecked = sum(1 for e in in_force if not e["reviewed"])
    line = (f"On {day} the corpus says {len(in_force)} of {total} document(s) "
            f"were in force: {len(not_yet)} had not begun, {len(ended)} had "
            "ended")
    if unplaceable:
        # Not "0 in force" and not silently excluded. A document with no start
        # date has no beginning to compare against, and calling that either
        # answer would be inventing a fact about it.
        line += (f", and {len(unplaceable)} cannot be placed on a timeline at "
                 "all -- no start date was extracted from them")
    line += "."
    if unchecked:
        line += (f" {unchecked} of those in force rest on a date nobody has "
                 "confirmed.")
    return line


def compare(store: Store, when: str, *, document_id: str | None = None) -> dict:
    """Both axes for one date, and the sentence that keeps them apart.

    Returned together only because a person asking "what about June" usually
    wants both and rarely knows there are two. They are never merged into one
    number.
    """
    return {
        "date": when,
        "believed": believed_at(store, when, document_id=document_id),
        "in_force": in_force_on(store, when, document_id=document_id),
        "note": ("These are two different pasts. `believed` is what this store "
                 "held on that date and is the answer to why a report then said "
                 "what it said. `in_force` is what the documents themselves say "
                 "was running on that date, whenever anybody got round to "
                 "reading them. A document signed in March and ingested in "
                 "November appears in the second and not the first."),
        "now": now()[:10],
    }
