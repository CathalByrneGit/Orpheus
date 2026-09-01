"""What falls due, and how much of that this corpus can actually tell you.

`KeyDate` has carried a `date_role` since the deterministic pass was written --
`end`, `milestone`, `start`, `signature` -- and `Obligation` has carried a
`due_date` and a `recurrence` since the bundle was authored. **Nothing has ever
queried either.** Every date in the store was extracted, located on a page,
graded by the rubric and left there.

That is the gap between a document intelligence system and a system somebody
opens on a Monday. A public servant holding four hundred contracts does not
begin with "what does the corpus say about Ardmore"; they begin with "what
expires this quarter, and have I looked at it".

Three rules keep this from becoming a dashboard that lies.

**A machine reading is not a commitment.** Every entry carries its review
status and the split is in the headline. An expiry date nobody has confirmed is
worth showing -- it is the whole point of showing it, so somebody checks -- but
showing it *as though a person had agreed to it* would be the single most
damaging thing this module could do. Rejected extractions are excluded
outright.

**Overdue is its own bucket, not a negative number of days.** A contract whose
end date has passed and which nobody reviewed is the most alarming row in the
store, and burying it under today's date would hide it.

**An empty calendar is ambiguous, so it never stands alone.** Nothing due may
mean nothing is due, or it may mean no end date was ever extracted from a
single document. Those are opposite findings, and `coverage` is reported beside
the entries for the same reason `graph.py` reports it beside the topology.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import bundle as bundle_mod
from .rubric import EXCLUDED_STATUSES, REVIEWED_STATUSES, confidence_label
from .store import Store
from .utils import now

#: Roles that mean *something happens on this date*. `start` and `signature`
#: are dates a contract has, not dates that fall due, and putting them in a
#: diary would fill it with things nobody has to act on.
DUE_ROLES = ("end", "milestone", "renewal", "review", "break")

#: Recorded so the report can say what it left out. A calendar that silently
#: drops two thirds of the dates in the store is one nobody can audit.
CONTEXT_ROLES = ("start", "signature", "unknown")

DEFAULT_WINDOW = 90

#: A slash date whose two leading fields are both 12 or less can be read
#: day-first or month-first, and `find_dates` records it at `inferred` with the
#: raw text kept rather than picking a side silently. In a diary that matters
#: more than anywhere else: the entry may be three months from where it is
#: shown, so it is flagged rather than quietly sorted.
_SLASH_PARTS = 3


def other_reading(raw_text: str | None) -> str | None:
    """The date this would be if read month-first instead, or `None`.

    "Ambiguous" on its own is a warning a reader cannot act on. `04/07/2026`
    is shown as 4 July because `find_dates` reads slash dates day-first, which
    is right for Irish, UK and EU documents; the useful thing to say beside it
    is *"or 7 April"*, because that is a different quarter and the reader knows
    which their document meant.
    """
    if not raw_text:
        return None
    for separator in ("/", "."):
        parts = [p.strip() for p in raw_text.strip().split(separator)]
        if len(parts) == _SLASH_PARTS and all(p.isdigit() for p in parts):
            first, second, year = int(parts[0]), int(parts[1]), parts[2]
            if len(year) == 4 and first <= 12 and second <= 12 and first != second:
                return f"{year}-{first:02d}-{second:02d}"
    return None


def _today() -> str:
    return now()[:10]


def _table(store: Store, bundle: dict, type_id: str) -> str | None:
    obj = bundle_mod.object_type(bundle, type_id) if bundle else None
    if not obj:
        return None
    table = bundle_mod.table_name(obj)
    return table if store.table_exists(table) else None


def _entries(store: Store, bundle: dict, document_id: str | None) -> list[dict]:
    """Every dated thing in the store, whatever its date.

    Windowing happens afterwards, in `upcoming`, so that the same read can
    answer "what is due" and "how much of this corpus has a date at all"
    without asking the store twice for nearly the same rows.
    """
    scope = " AND i.document_id = ?" if document_id else ""
    params = (document_id,) if document_id else ()
    filenames = {r["document_id"]: r["filename"] for r in store.query(
        "SELECT document_id, filename FROM documents")}
    found: list[dict] = []

    key_dates = _table(store, bundle, "KeyDate")
    if key_dates:
        for row in store.query(
                f'SELECT i.*, p.excerpt, p.page_no AS provenance_page '
                f'FROM "{key_dates}" i '
                "LEFT JOIN provenance p ON p.instance_id = i.instance_id "
                f"WHERE i.value IS NOT NULL AND i.value != ''{scope}", params):
            found.append({
                "instance_id": row["instance_id"],
                "type_id": "KeyDate",
                "date": row["value"],
                "role": row["date_role"] or "unknown",
                "kind": "date",
                "document_id": row["document_id"],
                "filename": filenames.get(row["document_id"]),
                "page_no": row["page_no"] or row["provenance_page"],
                "raw_text": row["raw_text"],
                "excerpt": row["excerpt"],
                "recurrence": None,
                "subject": None,
                "status": row["status"],
                "confidence": row["confidence"],
                "other_reading": other_reading(row["raw_text"]),
            })

    obligations = _table(store, bundle, "Obligation")
    if obligations:
        for row in store.query(
                f'SELECT i.*, p.excerpt, p.page_no FROM "{obligations}" i '
                "LEFT JOIN provenance p ON p.instance_id = i.instance_id "
                f"WHERE i.due_date IS NOT NULL AND i.due_date != ''{scope}",
                params):
            found.append({
                "instance_id": row["instance_id"],
                "type_id": "Obligation",
                "date": row["due_date"],
                # An obligation *is* a thing that falls due; it needs no role
                # to say so, which is why it is not filtered by one.
                "role": "obligation",
                "kind": "obligation",
                "document_id": row["document_id"],
                "filename": filenames.get(row["document_id"]),
                "page_no": row["page_no"],
                "raw_text": None,
                "excerpt": row["excerpt"],
                # Reported verbatim, never expanded. "quarterly" and "annually
                # on the anniversary" are free text a model wrote, and turning
                # either into a series of dates would be inventing entries no
                # document contains.
                "recurrence": row["recurrence"],
                "subject": row["obligated_party"] or row["summary"],
                "status": row["status"],
                "confidence": row["confidence"],
                "other_reading": None,
            })

    for entry in found:
        entry["confidence_label"] = (confidence_label(entry["confidence"])
                                     if entry["confidence"] is not None else None)
        entry["reviewed"] = entry["status"] in REVIEWED_STATUSES
        entry["ambiguous"] = entry["other_reading"] is not None
    return found


def upcoming(store: Store, *, within_days: int = DEFAULT_WINDOW,
             as_of: str | None = None, document_id: str | None = None,
             limit: int = 200) -> dict:
    """What falls due between `as_of` and `within_days` later, and what is past.

    `as_of` is a plain ISO date and defaults to today. It is a parameter rather
    than a constant so that a report can be reproduced: "seventeen things were
    due in the last quarter" is a claim about a date, and reading it off the
    clock makes the same command answer differently every morning with nothing
    recording why.
    """
    as_of = as_of or _today()
    bundle = bundle_mod.active(store) or bundle_mod.load()
    horizon = (date.fromisoformat(as_of) + timedelta(days=within_days)).isoformat()

    everything = _entries(store, bundle, document_id)
    live = [e for e in everything if e["status"] not in EXCLUDED_STATUSES]
    # Excluded by role rather than dropped quietly: a start date is a fact
    # about a contract and not a thing that falls due, and the count of what
    # was set aside is part of being able to audit this.
    due_shaped = [e for e in live if e["role"] not in CONTEXT_ROLES]
    set_aside = len(live) - len(due_shaped)

    overdue = sorted((e for e in due_shaped if e["date"] < as_of),
                     key=lambda e: e["date"], reverse=True)
    due = sorted((e for e in due_shaped if as_of <= e["date"] <= horizon),
                 key=lambda e: e["date"])
    for entry in overdue:
        entry["days"] = -(date.fromisoformat(as_of)
                          - date.fromisoformat(entry["date"])).days
    for entry in due:
        entry["days"] = (date.fromisoformat(entry["date"])
                         - date.fromisoformat(as_of)).days

    shown = overdue[:limit] + due[:limit]
    reviewed = sum(1 for e in shown if e["reviewed"])
    ambiguous = sum(1 for e in shown if e["ambiguous"])
    cover = coverage(store, bundle=bundle, document_id=document_id,
                     entries=everything)

    return {
        "as_of": as_of,
        "within_days": within_days,
        "window_ends": horizon,
        "overdue": overdue[:limit],
        "due": due[:limit],
        "n_overdue": len(overdue),
        "n_due": len(due),
        "n_reviewed": reviewed,
        "n_unreviewed": len(shown) - reviewed,
        "n_ambiguous": ambiguous,
        "n_context_dates_set_aside": set_aside,
        "coverage": cover,
        "headline": _headline(len(overdue), len(due), within_days, reviewed,
                              len(shown), ambiguous, cover),
    }


def coverage(store: Store, *, bundle: dict | None = None,
             document_id: str | None = None,
             entries: list[dict] | None = None) -> dict:
    """How many documents this calendar could speak for at all.

    Nothing due may mean nothing is due, or it may mean no end date was ever
    extracted from a single document. Those are opposite findings and an
    entry list cannot tell them apart, which is why this travels beside it --
    the same reason `graph.py` reports coverage beside its topology.
    """
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    entries = entries if entries is not None else _entries(store, bundle,
                                                           document_id)
    scope = " WHERE document_id = ?" if document_id else ""
    params = (document_id,) if document_id else ()
    documents = [r["document_id"] for r in store.query(
        f"SELECT document_id FROM documents{scope}", params)]

    with_due = {e["document_id"] for e in entries
                if e["role"] not in CONTEXT_ROLES
                and e["status"] not in EXCLUDED_STATUSES}
    obligations = sum(1 for e in entries if e["type_id"] == "Obligation")

    return {
        "n_documents": len(documents),
        "n_with_a_due_date": len(with_due & set(documents)),
        "share": (round(len(with_due & set(documents)) / len(documents), 3)
                  if documents else None),
        "n_obligations_extracted": obligations,
        "note": _coverage_note(len(documents), len(with_due & set(documents)),
                               obligations),
    }


def _coverage_note(n_documents: int, n_with_due: int, n_obligations: int) -> str:
    if not n_documents:
        return "No documents."
    missing = n_documents - n_with_due
    parts = []
    if missing:
        parts.append(
            f"{missing} of {n_documents} document(s) have no date that falls "
            "due at all. An empty stretch in this calendar may be that rather "
            "than a quiet quarter.")
    else:
        parts.append(f"Every one of {n_documents} document(s) has at least one "
                     "date that falls due.")
    if not n_obligations:
        # Worth saying plainly: nothing in this codebase writes an Obligation.
        # Only a model proposing `type: "Obligation"` ever has, so a corpus run
        # with the deterministic pass alone will never have one, and "no
        # obligations" would otherwise read as "no obligations exist".
        parts.append("No `Obligation` has been extracted from this corpus, so "
                     "everything here is a date rather than a duty somebody "
                     "owes. The deterministic pass does not propose them.")
    return " ".join(parts)


def _headline(n_overdue: int, n_due: int, within_days: int, reviewed: int,
              shown: int, ambiguous: int, cover: dict) -> str:
    if not shown:
        return ("Nothing falls due in this window. " + cover["note"])
    parts = []
    if n_overdue:
        parts.append(f"{n_overdue} past its date")
    if n_due:
        parts.append(f"{n_due} in the next {within_days} days")
    line = " and ".join(parts) + "."
    # The split is in the headline rather than in a column somebody has to
    # notice. A diary of unconfirmed machine readings presented as a diary is
    # the failure this module has to work hardest to avoid.
    line += (f" {reviewed} of {shown} shown have been checked by a person; "
             f"the other {shown - reviewed} are machine readings nobody has "
             "confirmed.") if reviewed < shown else \
            f" All {shown} shown have been checked by a person."
    if ambiguous:
        line += (f" {ambiguous} came from a slash date that can be read two "
                 "ways, so may fall in a different month entirely.")
    return line + " " + cover["note"]
