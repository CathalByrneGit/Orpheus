"""A register: authoritative reference data, held apart from the corpus.

Conflict-of-interest work needs ownership and directorships, and those live in
registers rather than in the contracts. This is how one gets in.

**A register is not a document, and its rows never become facts.** Everything
else in the store is something a model read out of a document, carrying a page
and an excerpt that `align.py` located in it. A row in a companies register has
neither, and giving it one would be an invention. Worse, a register import is
trivially correct — so counting its rows as extractions would inflate the number
[extraction quality](provenance-and-amendment.md) exists to report with work no
model did.

So rows live in their own tables and feed exactly one thing: the evidence a
person weighs when deciding whether two pages are one thing. That is also where
a register is worth most. Only 2 of 74 companies in the calibration corpus state
a registered number, and a shared registered number is the decisive, rare value
that resolution otherwise never gets.

**Nothing counts until somebody has looked at it.** A register arrives `staged`,
which means present, readable and not evidence. A person promotes it, and can
reject individual rows on the way — a register is only as good as its source,
and that is not machine-checkable.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from . import bundle as bundle_mod
from .audit import record_edit
from .store import Store
from .utils import (NotFound, OrpheusError, from_json, naive_key, new_id, now,
                    require_choice, require_string, to_json)

#: A register is present and readable from the moment it lands. `staged` says
#: nobody has vouched for it yet, and only an `active` register is evidence.
#: `withdrawn` is how one stops being evidence without being deleted -- the
#: same reason nothing else here is deleted.
REGISTER_STATUSES = ("staged", "active", "withdrawn")

#: A row a person has looked at. `rejected` keeps it readable and stops it
#: counting, because a bad row is evidence about the register.
ROW_STATUSES = ("staged", "accepted", "rejected")

#: Column names a register is likely to use for the two fields matching needs.
#: Guessed, then shown to a person, because guessing wrong quietly is the
#: failure that matters -- a register matched on the wrong column produces
#: confident nonsense.
NAME_HINTS = ("name", "company_name", "companyname", "entity_name", "title",
              "registered_name", "legal_name", "organisation", "organization")
IDENTIFIER_HINTS = ("number", "company_number", "companynumber", "reg_no",
                    "registration_number", "registered_number", "id",
                    "identifier", "crn", "vat", "duns", "lei")


def _guess(columns: Iterable[str], hints: Iterable[str]) -> str | None:
    """Which column probably holds this, by name. Never silently."""
    lowered = {c.lower().replace(" ", "_"): c for c in columns}
    for hint in hints:
        if hint in lowered:
            return lowered[hint]
    for key, original in lowered.items():
        if any(hint in key for hint in hints):
            return original
    return None


def create_register(store: Store, name: str, description: str | None = None,
                    origin: str | None = None,
                    actor_id: str | None = None) -> str:
    """Declare a register. Empty, staged, and not yet evidence."""
    store.assert_writable()
    require_string(name, "name")
    register_id = new_id("reg")
    store.insert("registers", {
        "register_id": register_id,
        "name": name,
        "description": description,
        "origin": origin,
        "status": "staged",
        "created_at": now(),
        "created_by": actor_id,
        "promoted_at": None,
        "promoted_by": None,
    })
    record_edit(store, "registers", register_id, None, "create",
                new={"name": name, "origin": origin}, actor_id=actor_id)
    return register_id


def load_csv(store: Store, register_id: str, text: str,
             name_column: str | None = None,
             identifier_column: str | None = None,
             type_id: str | None = None,
             actor_id: str | None = None) -> dict:
    """Read a delimited file into a staged register.

    The whole row is kept as it arrived, so nothing is lost on the way in and a
    person reviewing it sees what they are judging rather than a projection of
    it. Two fields are lifted out because matching uses them, and which columns
    those are is reported back rather than assumed — a register matched on the
    wrong column produces confident nonsense, and the only defence is saying
    which column was used.

    `type_id` decides how a name normalises, the same way it does everywhere
    else: a register of people and a register of companies do not share a rule.
    """
    store.assert_writable()
    register = get_register(store, register_id)
    if register["status"] != "staged":
        raise OrpheusError(
            f"{register['name']!r} is {register['status']}, and rows are only "
            "loaded into a staged register. Create another one.")

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise OrpheusError("No rows in that file.")
    columns = [c for c in (rows[0].keys()) if c is not None]

    name_column = name_column or _guess(columns, NAME_HINTS)
    identifier_column = identifier_column or _guess(columns, IDENTIFIER_HINTS)
    if not name_column:
        raise OrpheusError(
            "Cannot tell which column holds the name. Pass name_column "
            f"explicitly. Columns: {', '.join(columns)}.")

    bundle = bundle_mod.active(store)
    existing = store.scalar(
        "SELECT COALESCE(MAX(row_no), 0) FROM register_rows WHERE register_id = ?",
        (register_id,)) or 0

    written = 0
    for offset, row in enumerate(rows, start=existing + 1):
        value = (row.get(name_column) or "").strip()
        identifier = (row.get(identifier_column) or "").strip() \
            if identifier_column else None
        store.insert("register_rows", {
            "register_id": register_id,
            "row_no": offset,
            "name": value or None,
            "naive_key": bundle_mod.key_for(bundle, type_id, value)
                         if value else None,
            "identifier": identifier or None,
            "values_json": to_json(dict(row)),
            "status": "staged",
            "note": None,
        })
        written += 1

    record_edit(store, "registers", register_id, None, "load",
                new={"rows": written, "name_column": name_column,
                     "identifier_column": identifier_column},
                actor_id=actor_id)
    return {
        "register_id": register_id, "n_rows": written,
        "columns": columns,
        "name_column": name_column,
        "identifier_column": identifier_column,
        # Said out loud, every time. The guess is usually right and the failure
        # when it is wrong is silent.
        "caveat": (
            f"Names were read from {name_column!r}"
            + (f" and identifiers from {identifier_column!r}"
               if identifier_column else ", and no identifier column was found")
            + ". Check that before promoting: matching on the wrong column "
              "produces confident nonsense."),
    }


def get_register(store: Store, register_id: str) -> dict:
    row = store.one("SELECT * FROM registers WHERE register_id = ?",
                    (register_id,))
    if row is None:
        raise NotFound(f"No register {register_id!r}.")
    return dict(row)


def list_registers(store: Store) -> list[dict]:
    return [dict(r) for r in store.query(
        "SELECT r.*, "
        "  (SELECT COUNT(*) FROM register_rows w WHERE w.register_id = "
        "     r.register_id) AS n_rows, "
        "  (SELECT COUNT(*) FROM register_rows w WHERE w.register_id = "
        "     r.register_id AND w.status = 'rejected') AS n_rejected "
        "FROM registers r ORDER BY r.created_at DESC")]


def rows(store: Store, register_id: str, status: str | None = None,
         limit: int = 100) -> list[dict]:
    clause = "AND status = ?" if status else ""
    params: tuple = (register_id,) + ((status,) if status else ())
    return [{**dict(r), "values": from_json(r["values_json"]) or {}}
            for r in store.query(
                f"SELECT * FROM register_rows WHERE register_id = ? {clause} "
                "ORDER BY row_no LIMIT ?", params + (limit,))]


def review_row(store: Store, register_id: str, row_no: int, status: str,
               note: str | None = None, actor_id: str | None = None) -> dict:
    """Accept or reject one row. A rejected row stays readable and stops
    counting, because a bad row is evidence about the register."""
    store.assert_writable()
    require_choice(status, ROW_STATUSES, "status")
    before = store.one(
        "SELECT * FROM register_rows WHERE register_id = ? AND row_no = ?",
        (register_id, row_no))
    if before is None:
        raise NotFound(f"No row {row_no} in {register_id!r}.")
    store.execute(
        "UPDATE register_rows SET status = ?, note = ? "
        "WHERE register_id = ? AND row_no = ?",
        (status, note, register_id, row_no))
    record_edit(store, "register_rows", f"{register_id}:{row_no}", None,
                "review", previous={"status": before["status"]},
                new={"status": status}, actor_id=actor_id, note=note)
    return {"register_id": register_id, "row_no": row_no, "status": status}


def promote(store: Store, register_id: str, actor_id: str | None = None,
            note: str | None = None) -> dict:
    """Somebody vouches for this register, and it becomes evidence.

    Every row still `staged` is accepted by this, because promoting is the act
    of saying the register is good. A row a person rejected stays rejected.
    """
    store.assert_writable()
    register = get_register(store, register_id)
    if register["status"] == "active":
        raise OrpheusError(f"{register['name']!r} is already active.")

    accepted = store.scalar(
        "SELECT COUNT(*) FROM register_rows WHERE register_id = ? "
        "AND status = 'staged'", (register_id,)) or 0
    with store.transaction():
        store.execute(
            "UPDATE register_rows SET status = 'accepted' "
            "WHERE register_id = ? AND status = 'staged'", (register_id,))
        store.execute(
            "UPDATE registers SET status = 'active', promoted_at = ?, "
            "promoted_by = ? WHERE register_id = ?",
            (now(), actor_id, register_id))
        record_edit(store, "registers", register_id, None, "promote",
                    previous={"status": register["status"]},
                    new={"status": "active", "rows_accepted": accepted},
                    actor_id=actor_id, note=note)
    return {"register_id": register_id, "status": "active",
            "rows_accepted": accepted}


def withdraw(store: Store, register_id: str, actor_id: str | None = None,
             note: str | None = None) -> dict:
    """Stop a register being evidence, without deleting it.

    A register somebody relied on and then withdrew is part of the record of
    how a decision was reached, which is why this is a status and not a DELETE.
    """
    store.assert_writable()
    register = get_register(store, register_id)
    store.execute("UPDATE registers SET status = 'withdrawn' "
                  "WHERE register_id = ?", (register_id,))
    record_edit(store, "registers", register_id, None, "withdraw",
                previous={"status": register["status"]},
                new={"status": "withdrawn"}, actor_id=actor_id, note=note)
    return {"register_id": register_id, "status": "withdrawn"}


# ---------------------------------------------------------------------------
# What a register says about a page
# ---------------------------------------------------------------------------

def matches_for(store: Store, canonical_name: str, type_id: str | None = None,
                limit: int = 5) -> list[dict]:
    """Register rows that might be about this page.

    Matched on the normalised name, which is the same weak basis the wiki is
    built on and is labelled as such wherever it is reported. The register's
    value is not that it matches better; it is that a matched row carries an
    *identifier*, and an identifier settles what a name cannot.

    Only from an active register: a staged one is present and not vouched for,
    and evidence nobody has checked is the thing this whole design refuses.
    """
    bundle = bundle_mod.active(store)
    key = bundle_mod.key_for(bundle, type_id, canonical_name)
    if not key:
        return []
    return [{**dict(r), "values": from_json(r["values_json"]) or {},
             "basis": "naive_key",
             "evidence": f"register name key {key!r}"}
            for r in store.query(
                "SELECT w.*, g.name AS register_name FROM register_rows w "
                "JOIN registers g ON g.register_id = w.register_id "
                "WHERE g.status = 'active' AND w.status = 'accepted' "
                "AND w.naive_key = ? LIMIT ?", (key, limit))]


def bearing_on(store: Store, a: dict, b: dict) -> dict:
    """What the registers say about whether two pages are one thing.

    The first thing in this codebase that can argue *against* a merge with
    something better than a spelling. Two pages landing on one register row is
    strong evidence for; two pages landing on rows with **different**
    identifiers is a register saying they are two organisations, which no
    amount of name similarity should outweigh.

    It says what it found and draws no conclusion, like everything else a
    person is asked to decide on.
    """
    left = matches_for(store, a["canonical_name"], a.get("type_id"))
    right = matches_for(store, b["canonical_name"], b.get("type_id"))

    shared_rows = [
        {"register_id": x["register_id"], "register": x["register_name"],
         "row_no": x["row_no"], "name": x["name"],
         "identifier": x["identifier"]}
        for x in left
        for y in right
        if (x["register_id"], x["row_no"]) == (y["register_id"], y["row_no"])]

    ids_a = {x["identifier"] for x in left if x["identifier"]}
    ids_b = {y["identifier"] for y in right if y["identifier"]}
    shared_ids = sorted(ids_a & ids_b)
    conflicting = sorted(ids_a - ids_b) and sorted(ids_b - ids_a)

    if shared_ids:
        reading = ("A register gives both pages the same identifier. That is "
                   "the strongest evidence for one thing this store can hold "
                   "short of a person saying so.")
    elif ids_a and ids_b and not shared_ids:
        reading = ("A register gives these pages different identifiers, which "
                   "says they are two organisations. Check that both rows are "
                   "about the right pages before acting on it -- the match "
                   "into the register is on a normalised name, and a wrong "
                   "match here argues confidently for the wrong answer.")
    elif shared_rows:
        reading = ("Both pages match one register row, on a normalised name "
                   "and with no identifier to confirm it.")
    elif not left and not right:
        reading = ("No active register has a row for either page. That is not "
                   "evidence that they are different -- it is the absence of "
                   "evidence, and a staged register nobody has promoted counts "
                   "as absent here.")
    else:
        reading = ("A register has a row for one page and not the other, which "
                   "says nothing about whether they are the same: a register "
                   "is rarely complete.")

    return {
        "matches": {a["entity_id"]: left, b["entity_id"]: right},
        "shared_rows": shared_rows,
        "shared_identifiers": shared_ids,
        "identifiers_conflict": bool(conflicting) and not shared_ids,
        "reading": reading,
    }
