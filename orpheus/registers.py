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
import re
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

#: A page's link to a register row. `proposed` is the machine's suggestion and
#: means nothing until somebody looks; `rejected` is a person saying this row is
#: not about this page, which is worth keeping so the same wrong pair is not
#: offered again tomorrow.
LINK_STATUSES = ("proposed", "confirmed", "rejected")

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
         limit: int = 100, column: str | None = None,
         value: str | None = None) -> list[dict]:
    """Rows of one register, optionally filtered on an exposed column.

    `column` has to be one that has been exposed. That is not only a safety
    rule about interpolating a name into SQL -- it is the honest answer to
    "filter by county": until somebody exposes it, the store cannot, and saying
    so beats scanning every row and pretending it can.
    """
    clause = "AND status = ?" if status else ""
    params: tuple = (register_id,) + ((status,) if status else ())
    if column:
        available = {c["column"] for c in exposed_columns(store)}
        if column not in available:
            raise OrpheusError(
                f"{column!r} is not an exposed column, so nothing can filter "
                "on it. Exposed: " + (", ".join(sorted(available)) or "none")
                + ". Expose it first -- the values are in values_json either "
                "way, this is what makes them queryable.")
        # Safe to interpolate: `column` has just been checked against the
        # columns the schema actually has, and nothing else reaches this.
        clause += f' AND "{column}" = ?'
        params = params + (value,)
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

# ---------------------------------------------------------------------------
# Exposing a column
# ---------------------------------------------------------------------------
#
# `load_csv` lifts three fields out of the CSV into real columns -- `name`,
# `naive_key`, `identifier` -- because matching needs them indexed. Everything
# else goes into `values_json`, where it is readable and unqueryable: a register
# with a county, a SIC code or a status column cannot be filtered on any of
# them, and "show me the dissolved rows" is a question nobody can ask.
#
# A generated column is the fix. `json_extract` over the blob is an O(n) scan;
# the same predicate against an indexed generated column is a B-tree search --
# `SEARCH ... USING INDEX` rather than `SCAN`.
#
# **How much it is worth tracks selectivity, not table size.** Measured on a
# real 20,000-row register:
#
#   count(*) on an exposed column          8.9ms -> 0.15ms   58x
#   fetching 1/6 of the table              10.2ms -> 2.3ms    4.4x
#   exposed AND un-exposed predicate       10.1ms -> 2.8ms    3.6x
#
# The first is the honest headline for an aggregate or a rare value, and the
# other two are what most filtering actually looks like: once rows have to be
# fetched, retrieval dominates and the index saves the predicate rather than
# the work. The third row is the one to design around -- SQLite uses the index
# for the exposed half and then extracts JSON for every row it returns, so a
# pair of predicates is worth two exposed columns rather than one.
#
# VIRTUAL rather than STORED, for two reasons. It costs no row storage, only the
# index (a 20,000-row table went from 1,204 to 1,272 pages). And STORED cannot
# be added to a table that already exists -- SQLite refuses -- which would make
# this a migration rather than something a reviewer does when they notice they
# need it.

#: Generated columns arrived in SQLite 3.31.0 (2020-01-22). Checked where the
#: feature is used rather than when a store is opened: a deployment that never
#: exposes a column works perfectly well without it, and refusing to open a
#: store over a feature nobody reached for would be the wrong trade.
GENERATED_COLUMNS_FROM = (3, 31, 0)

#: The columns `register_rows` owns. An exposed column may not take one of
#: these names -- `name` and `identifier` in particular are already the lifted
#: fields, and shadowing them would make matching read from the wrong place.
FIXED_COLUMNS = ("register_id", "row_no", "name", "naive_key", "identifier",
                 "values_json", "status", "note")

#: The one DDL shape `expose_column` writes, read back to recover the key a
#: column came from. Safe as a regex only because both ends are written here.
_EXPOSED = re.compile(
    r'"(?P<column>[a-z0-9_]+)" TEXT GENERATED ALWAYS AS '
    r"\(json_extract\(values_json, '\$\.\"(?P<key>[^\"]*)\"'\)\) VIRTUAL")


def _ddl(column: str, key: str) -> str:
    """The ALTER TABLE `expose_column` writes, and `_EXPOSED` reads back."""
    path = "'$.\"" + key + "\"'"
    return (f'ALTER TABLE register_rows ADD COLUMN "{column}" TEXT '
            f"GENERATED ALWAYS AS (json_extract(values_json, {path})) VIRTUAL")


def _populated(store: Store, column: str) -> int:
    """How many rows carry a value for this column, blanks not counting."""
    return store.scalar(
        f'SELECT COUNT(*) FROM register_rows '
        f'WHERE "{column}" IS NOT NULL AND "{column}" != \'\'') or 0


def column_name_for(key: str) -> str:
    """A column name for a key a register spelled however it liked.

    Lowercased and underscored, the same transformation `ontology` uses for a
    header field, and for the same reason: a reviewer can predict it.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    return slug or "field"


def _supports_generated_columns(store: Store) -> bool:
    version = tuple(int(part) for part in
                    store.scalar("SELECT sqlite_version()").split(".")[:3])
    return version >= GENERATED_COLUMNS_FROM


def exposed_columns(store: Store) -> list[dict]:
    """Which register keys are queryable, read from the schema itself.

    There is no table recording this. The column either exists or it does not,
    and a second copy of that fact is a second thing that can be wrong -- the
    same reason `schema_ops` changes the table and the bundle together rather
    than tracking one against the other.
    """
    sql = store.scalar(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'register_rows'") or ""
    found = []
    for match in _EXPOSED.finditer(sql):
        column = match.group("column")
        found.append({
            "column": column,
            "key": match.group("key"),
            # How much of the corpus of rows actually carries it. A key one
            # register in four uses is worth exposing and worth knowing about.
            "n_rows": _populated(store, column),
        })
    return sorted(found, key=lambda c: c["column"])


def expose_column(store: Store, key: str, actor_id: str | None = None,
                  note: str | None = None, as_column: str | None = None) -> dict:
    """Lift one key out of `values_json` into an indexed column.

    Store-wide, not per register: `register_rows` holds every register's rows,
    so exposing `county` exposes it for all of them. A register whose rows have
    no such key reads NULL there, which is the truthful answer and not an error.

    Nothing is copied and nothing is destroyed. The column is computed from the
    JSON on read, so the register's rows are exactly the bytes that were loaded
    -- which matters here more than elsewhere, because a register is only worth
    what its provenance is worth.

    `as_column` names it something else. Not a nicety: `register_rows` already
    has a `status` (the review state of the row), and a companies register with
    its own `status` column is the ordinary case rather than the exotic one --
    found by loading a real one. Without the alias that key could never be
    exposed at all.
    """
    store.assert_writable()
    require_string(key, "key")
    if '"' in key:
        # `$."k"` cannot express a key containing a quote, and the failure is
        # silent: the path parses and extracts NULL from every row.
        raise OrpheusError(
            f"{key!r} contains a double quote, which a JSON path cannot "
            "address. Rename the column in the source data first.")
    if not _supports_generated_columns(store):
        raise OrpheusError(
            "Exposing a register column needs generated columns, which arrived "
            f"in SQLite {'.'.join(str(n) for n in GENERATED_COLUMNS_FROM)}. "
            f"This build is {store.scalar('SELECT sqlite_version()')}.")

    column = column_name_for(as_column or key)
    if column in FIXED_COLUMNS:
        raise OrpheusError(
            f"{(as_column or key)!r} reduces to {column!r}, which is a column "
            "`register_rows` already owns. `name` and `identifier` are the "
            "fields matching reads, and shadowing one would make it read from "
            "the wrong place. A register really can have its own `status` "
            f"column -- give it another name: as_column='company_{column}'.")
    already = {c["column"]: c for c in exposed_columns(store)}
    if column in already:
        raise OrpheusError(
            f"{column!r} is already exposed, from key "
            f"{already[column]['key']!r}.")

    with store.transaction():
        store.execute(_ddl(column, key))
        store.execute(f'CREATE INDEX IF NOT EXISTS idx_register_rows_{column} '
                      f'ON register_rows ("{column}")')
        record_edit(store, "register_rows", column, None, "expose_column",
                    previous=None, new={"column": column, "key": key},
                    actor_id=actor_id, note=note)

    populated = _populated(store, column)
    total = store.scalar("SELECT COUNT(*) FROM register_rows") or 0
    return {
        "column": column, "key": key, "n_rows": populated, "n_total": total,
        # The number that says whether this was worth doing. Exposing a key no
        # register carries is a column of nulls, and saying so beats letting
        # somebody find out by filtering on it.
        "reading": (
            f"{populated} of {total} row(s) carry {key!r}."
            if populated else
            f"No row carries {key!r}. The column is there and every value in "
            "it is null -- check the spelling against the register's own "
            "columns, which are case-sensitive."),
    }


def hide_column(store: Store, column: str, actor_id: str | None = None,
                note: str | None = None) -> dict:
    """Drop an exposed column and its index.

    Safe in a way `schema_ops.drop_property` is not, and worth saying: an
    exposed column holds no data of its own. The values live in `values_json`
    and are untouched, so this destroys nothing and can be undone by exposing
    the key again.
    """
    store.assert_writable()
    require_string(column, "column")
    found = {c["column"]: c for c in exposed_columns(store)}
    if column not in found:
        raise NotFound(
            f"{column!r} is not an exposed column. Exposed: "
            + (", ".join(sorted(found)) or "none"))
    with store.transaction():
        store.execute(f"DROP INDEX IF EXISTS idx_register_rows_{column}")
        store.execute(f'ALTER TABLE register_rows DROP COLUMN "{column}"')
        record_edit(store, "register_rows", column, None, "hide_column",
                    previous={"column": column, "key": found[column]["key"]},
                    new=None, actor_id=actor_id, note=note)
    return {"column": column, "key": found[column]["key"],
            "reading": "The values are still in values_json; nothing was lost."}


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


def links_for(store: Store, entity_id: str,
              status: str = "confirmed") -> list[dict]:
    """Register rows a person has said are about this page."""
    return [{**dict(r), "values": from_json(r["values_json"]) or {}}
            for r in store.query(
                "SELECT l.*, w.name, w.identifier, w.values_json, "
                "       g.name AS register_name "
                "FROM entity_register_links l "
                "JOIN register_rows w ON w.register_id = l.register_id "
                "  AND w.row_no = l.row_no "
                "JOIN registers g ON g.register_id = l.register_id "
                "WHERE l.entity_id = ? AND l.status = ? "
                "AND g.status = 'active' AND w.status = 'accepted' "
                "ORDER BY l.decided_at", (entity_id, status))]


def identifier_candidates(store: Store, type_id: str | None = None,
                          limit: int = 50) -> dict:
    """Pages that a register could give an identifier to, if somebody agreed.

    This is the gap `registers.py` opened with. Only 2 of 74 companies in the
    calibration corpus state a registered number, and a shared registered
    number is the decisive, rare value resolution otherwise never gets. An
    active register holds those numbers. Nothing joined the two.

    What it will not do is join them itself. The match is on a normalised name
    -- the same weak basis the wiki is built on -- and `bearing_on` already
    warns that a wrong match into a register "argues confidently for the wrong
    answer". Applied automatically that warning becomes the design: every page
    would get a number, some of them wrong, and the wrong ones would be
    indistinguishable from the right ones forever.

    **A page whose matches disagree is reported and not proposed.** Two rows
    with two different identifiers is a name that means two organisations, and
    a coin toss between them is worse than leaving the page unidentified: an
    absent identifier is only missing evidence, and a wrong one is evidence
    pointing the wrong way.
    """
    already = {r["entity_id"] for r in store.query(
        "SELECT DISTINCT entity_id FROM entity_register_links "
        "WHERE status IN ('confirmed', 'rejected')")}

    scope = " AND type_id = ?" if type_id else ""
    params = (type_id,) if type_id else ()
    proposals, ambiguous = [], []
    for page in store.query(
            "SELECT entity_id, canonical_name, type_id FROM entities "
            f"WHERE merged_into IS NULL{scope} ORDER BY canonical_name", params):
        if page["entity_id"] in already:
            continue
        matches = [m for m in matches_for(store, page["canonical_name"],
                                          page["type_id"])
                   if m["identifier"]]
        if not matches:
            continue
        identifiers = {m["identifier"] for m in matches}
        entry = {
            "entity_id": page["entity_id"],
            "canonical_name": page["canonical_name"],
            "type_id": page["type_id"],
            "matches": [{"register_id": m["register_id"],
                         "register": m["register_name"],
                         "row_no": m["row_no"], "name": m["name"],
                         "identifier": m["identifier"],
                         "values": m["values"]} for m in matches],
        }
        if len(identifiers) > 1:
            entry["reading"] = (
                "This name matches register rows with "
                f"{len(identifiers)} different identifiers, which says it is "
                "more than one organisation. Nothing is proposed: a wrong "
                "identifier argues confidently for the wrong answer, and no "
                "identifier only leaves the question open.")
            ambiguous.append(entry)
            continue
        entry["identifier"] = matches[0]["identifier"]
        entry["basis"] = "naive_key"
        entry["reading"] = (
            f"One active register row, matched on a normalised name, gives "
            f"{page['canonical_name']!r} the identifier "
            f"{matches[0]['identifier']!r}. Check it is the right row: the "
            "match is on a name, and confirming it is what makes every later "
            "comparison rest on a number instead.")
        proposals.append(entry)
        if len(proposals) >= limit:
            break

    return {
        "proposals": proposals,
        "ambiguous": ambiguous,
        "n_proposed": len(proposals),
        "n_ambiguous": len(ambiguous),
        "headline": _candidates_headline(store, proposals, ambiguous, type_id),
    }


def _candidates_headline(store: Store, proposals: list, ambiguous: list,
                         type_id: str | None) -> str:
    scope = " AND type_id = ?" if type_id else ""
    params = (type_id,) if type_id else ()
    pages = store.scalar(
        f"SELECT COUNT(*) FROM entities WHERE merged_into IS NULL{scope}",
        params)
    linked = store.scalar(
        "SELECT COUNT(DISTINCT entity_id) FROM entity_register_links "
        "WHERE status = 'confirmed'")
    if not pages:
        return "No pages."
    if not proposals and not ambiguous:
        return (f"{linked} of {pages} page(s) are linked to a register row. No "
                "active register has an identifier for any of the rest -- "
                "which is the absence of evidence, not evidence they have "
                "none.")
    line = (f"{linked} of {pages} page(s) are linked to a register row. "
            f"{len(proposals)} more could be, if somebody agrees the row is "
            "the right one.")
    if ambiguous:
        line += (f" {len(ambiguous)} name(s) match rows with different "
                 "identifiers and are not proposed at all: that is a name "
                 "meaning two organisations, and guessing between them is "
                 "worse than leaving it open.")
    return line


def link_row(store: Store, entity_id: str, register_id: str, row_no: int,
             status: str, actor_id: str | None = None,
             note: str | None = None, basis: str = "naive_key") -> dict:
    """Record a person's decision that a register row is, or is not, this page.

    `rejected` is kept rather than forgotten, so the same wrong pair is not
    proposed again tomorrow -- the same reason a settled merge stops being
    offered.
    """
    store.assert_writable()
    require_choice(status, LINK_STATUSES, "status")
    page = store.one("SELECT canonical_name FROM entities WHERE entity_id = ?",
                     (entity_id,))
    if page is None:
        raise NotFound(f"No page {entity_id!r}.")
    row = store.one(
        "SELECT w.identifier, w.name, g.status AS register_status "
        "FROM register_rows w JOIN registers g ON g.register_id = w.register_id "
        "WHERE w.register_id = ? AND w.row_no = ?", (register_id, row_no))
    if row is None:
        raise NotFound(f"No row {row_no} in register {register_id!r}.")
    if status == "confirmed" and row["register_status"] != "active":
        # A staged register is present and not vouched for. Linking a page to
        # one would rest every later comparison on reference data nobody has
        # promoted.
        raise OrpheusError(
            f"Register {register_id!r} is {row['register_status']}, not active. "
            "Promote it before linking pages to its rows.")

    with store.transaction():
        store.execute(
            "INSERT INTO entity_register_links (entity_id, register_id, row_no, "
            "basis, status, note, decided_by, decided_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id, register_id, row_no) DO UPDATE SET "
            "status = excluded.status, note = excluded.note, "
            "decided_by = excluded.decided_by, decided_at = excluded.decided_at",
            (entity_id, register_id, row_no, basis, status, note, actor_id,
             now(), now()))
        record_edit(store, "entity_register_links", entity_id, None,
                    f"register_link_{status}",
                    new={"register_id": register_id, "row_no": row_no,
                         "identifier": row["identifier"]},
                    actor_id=actor_id, note=note)

    return {"entity_id": entity_id, "canonical_name": page["canonical_name"],
            "register_id": register_id, "row_no": row_no, "status": status,
            "identifier": row["identifier"] if status == "confirmed" else None}


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
    # A confirmed link is a person saying this row is about this page. It
    # outranks a name match completely, and is the whole point of having
    # links: without them every reading below rests on two normalised names
    # happening to agree, which is the basis this evidence exists to improve on.
    linked_a = links_for(store, a.get("entity_id") or "")
    linked_b = links_for(store, b.get("entity_id") or "")
    left = linked_a or matches_for(store, a["canonical_name"], a.get("type_id"))
    right = linked_b or matches_for(store, b["canonical_name"], b.get("type_id"))
    checked = bool(linked_a) and bool(linked_b)

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

    if shared_ids and checked:
        reading = ("A register gives both pages the same identifier, and "
                   "somebody has confirmed that both rows are about these "
                   "pages. There is nothing stronger this store can hold short "
                   "of a person saying the two pages are one thing.")
    elif shared_ids:
        reading = ("A register gives both pages the same identifier. That is "
                   "the strongest evidence for one thing this store can hold "
                   "short of a person saying so -- though the match into the "
                   "register is still on a normalised name. Confirming the two "
                   "links would settle that half of it.")
    elif ids_a and ids_b and not shared_ids and checked:
        reading = ("A register gives these pages different identifiers, and "
                   "somebody has confirmed both rows. That is a register "
                   "saying, on evidence a person has checked, that these are "
                   "two organisations.")
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
        # Which basis the reading rests on, said plainly. Two names that
        # normalise the same and two rows a person checked are not the same
        # quality of evidence, and a reader deciding a merge is entitled to
        # know which they are looking at.
        "basis": "confirmed_links" if checked else "naive_key",
        "reading": reading,
    }
