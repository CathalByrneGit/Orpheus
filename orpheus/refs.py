"""A pointer from outside the store to something inside it, resolved live.

This exists for the drafting space: somebody writing a memo, a brief or a
half-formed idea wants to point at a page, a document or a single extracted
fact without retyping it. `datasette-paper` gives them the room to write in;
this gives them something to point *at*.

Three things make a reference different from a hyperlink, and each is the
reason this is a module rather than an `<a href>`.

**A reference carries the review state, always.** This is the whole point. An
extracted fact that nobody has checked is a machine's guess; quoted into a memo
with nothing attached, it reads exactly like a fact somebody verified. That is
laundering, and it is the specific failure this project exists to prevent, so
`review.checked` is on every card and `review.caveat` is a sentence written for
the reader rather than a flag for a template. A drafting space that let a
reviewer cite `unconfirmed` and `confirmed` in the same voice would undo the
review model one memo at a time.

**A reference is resolved now, not when it was written.** The store moves: a
page is merged, a value is amended, a document is redacted. A link copied into
prose would keep saying what was true the afternoon it was pasted. So nothing
here is cached, a merged page follows to its survivor and says it did, and a
redacted document resolves to a tombstone rather than to the text it used to
have.

**A reference must not become a disclosure.** The papers live outside Orpheus
-- in Datasette's internal database, where `auth.can()` never runs -- so every
card is resolved against the *reader*, not the author. Two people open the same
memo and the ones pointing at documents they may not see resolve to nothing, in
the same voice for "you may not see this" and "this does not exist", because
telling those apart tells you which documents exist. That is the rule
`api.handle()` already keeps for a 403; it is kept here for the same reason.
"""

from __future__ import annotations

from . import auth
from . import entities as entities_mod
from .store import Store

# What each id prefix points at. Taken from `new_id()`'s prefixes rather than
# invented, so a ref is exactly the id a person can already see on a page and
# copy out of a URL -- there is no second identifier scheme to keep in step.
PREFIXES = {
    "doc": "document",
    "ent": "entity",
    "inst": "instance",
    "tns": "tension",
}

KINDS = tuple(PREFIXES.values())

# `ok` carries a label. The other three never do.
#
# `denied` and `not_found` are deliberately *not* interchangeable in meaning but
# are interchangeable in what a reader may infer: a document nobody shared with
# you answers `denied`, and so does one that was never here. `redacted` is the
# exception and is safe to distinguish, because the row was deliberately kept
# to say that a document was removed -- that is the point of a tombstone.
OK = "ok"
DENIED = "denied"
NOT_FOUND = "not_found"
REDACTED = "redacted"
STATUSES = (OK, DENIED, NOT_FOUND, REDACTED)

# A fact a person has ruled on. Everything else is the machine still talking,
# whatever its confidence says.
CHECKED = ("confirmed", "amended")


def kind_of(ref: str | None) -> str | None:
    """Which kind of thing this ref names, or None if it names nothing.

    Prefix matching rather than a lookup per table: a ref that is not one of
    ours should cost nothing to reject, because the drafting space hands every
    unclaimed reference to every provider in turn.
    """
    if not ref or not isinstance(ref, str):
        return None
    prefix, _, rest = ref.partition("_")
    return PREFIXES.get(prefix) if rest else None


def _card(ref: str, status: str, **extra) -> dict:
    """One shape for all four kinds, so a caller renders one thing.

    A non-`ok` card is built here and nowhere else, which is what makes the
    leak rule checkable: there is exactly one construction site for a refusal,
    and it has no way to put a label on it.
    """
    card = {"ref": ref, "status": status, "kind": kind_of(ref)}
    if status != OK:
        return card
    card.update(extra)
    return card


def _review(state: str, caveat: str | None = None,
            contested: int = 0) -> dict:
    """What a reader needs to know before quoting this.

    `checked` is one bit and it is the one that matters: did a person rule on
    this, or is it still the machine talking. `state` is the row's own word,
    kept beside it because `amended` and `confirmed` are both checked and mean
    different things to somebody deciding whether to cite it.
    """
    checked = state in CHECKED
    if contested:
        caveat = (f"{contested} open "
                  f"{'disagreement' if contested == 1 else 'disagreements'} "
                  f"recorded against this. Two sources may both be right.")
    elif caveat is None and not checked:
        caveat = ("Nobody has checked this. It is what the machine read, not "
                  "something a person has agreed with.")
    return {"state": state, "checked": checked and not contested,
            "contested": contested, "caveat": caveat}


# ---------------------------------------------------------------------------
# The four kinds
# ---------------------------------------------------------------------------

def _open_tensions(store: Store, scope: str, subject_id: str) -> int:
    return store.scalar(
        "SELECT COUNT(*) FROM tensions WHERE scope = ? AND subject_id = ? "
        "AND status = 'open'", (scope, subject_id)) or 0


def _entity(store: Store, ref: str, actor: dict | None) -> dict:
    """A wiki page.

    Not per-document permissioned, because a page is a projection over mentions
    from many documents and the surface already treats it as visible to any
    signed-in actor. Following a merge is the reason `merged_into` keeps the
    row: a memo written in March that cites a page merged in June should land on
    the surviving page rather than break, and should say that it moved.
    """
    row = store.one("SELECT * FROM entities WHERE entity_id = ?", (ref,))
    if row is None:
        return _card(ref, NOT_FOUND)

    landed = entities_mod.get_entity(store, ref, follow_merge=True)
    if landed is None:                               # pragma: no cover - dangling
        return _card(ref, NOT_FOUND)
    entity_id = landed["entity_id"]

    mentions = store.scalar(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ? "
        "AND unlinked_at IS NULL", (entity_id,)) or 0
    documents = store.scalar(
        "SELECT COUNT(DISTINCT i.document_id) FROM entity_mentions m "
        "JOIN instance_index i ON i.instance_id = m.instance_id "
        "WHERE m.entity_id = ? AND m.unlinked_at IS NULL", (entity_id,)) or 0

    card = _card(
        ref, OK,
        label=landed["canonical_name"],
        detail=(f"{landed['type_id']} · {mentions} mention"
                f"{'' if mentions == 1 else 's'} in {documents} document"
                f"{'' if documents == 1 else 's'}"),
        href=f"/-/orpheus/wiki/{entity_id}",
        review=_review(landed["status"],
                       contested=_open_tensions(store, "entity", entity_id)))
    if entity_id != ref:
        # Said out loud rather than followed silently. A citation that quietly
        # lands somewhere else is how two things become one in a reader's head
        # without anybody deciding that they were.
        card["redirected_from"] = ref
        card["redirect_note"] = (
            f"This page was merged into {landed['canonical_name']!r}. "
            "You are reading the surviving page.")
    return card


def _document(store: Store, ref: str, actor: dict | None) -> dict:
    """An ingested file.

    The only kind with per-document permission, so the only one that can answer
    `denied`. A missing row answers `denied` too for anyone who is not an
    administrator, because `can()` is False for it -- which is the house rule,
    not an accident: distinguishing "not yours" from "not here" enumerates the
    corpus for anybody patient enough to try ids.
    """
    row = store.one(
        "SELECT document_id, filename, n_pages, review_status, redacted_at, "
        "redaction_note FROM documents WHERE document_id = ?", (ref,))
    if row is None:
        return _card(ref, NOT_FOUND if (actor or {}).get("is_admin") else DENIED)
    if not auth.can(store, actor, ref, "view"):
        return _card(ref, DENIED)
    if row["redacted_at"]:
        # The one refusal that carries anything, and deliberately: the row was
        # kept precisely so a reader learns the document was removed rather
        # than concluding it said nothing.
        card = _card(ref, REDACTED)
        card["redaction"] = {"at": row["redacted_at"],
                             "note": row["redaction_note"]}
        return card

    findings = store.scalar(
        "SELECT COUNT(*) FROM instance_index WHERE document_id = ?", (ref,)) or 0
    return _card(
        ref, OK,
        label=row["filename"],
        detail=(f"{row['n_pages']} page{'' if row['n_pages'] == 1 else 's'} · "
                f"{findings} finding{'' if findings == 1 else 's'}"),
        href=f"/-/orpheus/document/{ref}",
        review=_review(row["review_status"],
                       contested=_open_tensions(store, "document", ref)))


def _instance(store: Store, ref: str, actor: dict | None) -> dict:
    """One extracted fact -- the reference this module exists for.

    Permissioned through the document it was read from, because that is where
    the words are. A redacted document leaves no instance rows at all, so this
    answers `not_found`, which is true: the fact is gone, not hidden.
    """
    located = store.one(
        "SELECT instance_id, type_id, table_name, document_id FROM "
        "instance_index WHERE instance_id = ?", (ref,))
    if located is None:
        return _card(ref, NOT_FOUND)
    if not auth.can(store, actor, located["document_id"], "view"):
        return _card(ref, DENIED)

    row = store.one(
        f'SELECT * FROM "{located["table_name"]}" WHERE instance_id = ?', (ref,))
    if row is None:                                  # pragma: no cover - dangling
        return _card(ref, NOT_FOUND)
    filename = store.scalar("SELECT filename FROM documents WHERE document_id = ?",
                            (located["document_id"],))
    return _card(
        ref, OK,
        label=_instance_label(row, located["type_id"]),
        detail=f"{located['type_id']} · read from {filename or located['document_id']}",
        href=f"/-/orpheus/document/{located['document_id']}#{ref}",
        review=_review(row["status"] if "status" in row.keys() else "unconfirmed",
                       contested=_open_tensions(store, "instance", ref)))


# Which column stands in for "what this row says", in order of preference. A
# bundle names its own properties, so there is no single right column; these are
# the conventional ones, and the type id is the answer when none is present --
# better a pill that says `KeyDate` than one that says `inst_9f2c`.
LABEL_COLUMNS = ("name", "title", "value", "summary", "date_value", "text")


def _instance_label(row, type_id: str) -> str:
    keys = row.keys()
    for column in LABEL_COLUMNS:
        if column in keys and row[column]:
            return str(row[column])
    return type_id


def _tension(store: Store, ref: str, actor: dict | None) -> dict:
    """A recorded disagreement.

    Worth citing in a memo more than most things here, because it is the one
    kind whose whole content is "these two sources do not agree" -- and an idea
    built on a contested reading should show that it was contested.
    """
    row = store.one("SELECT * FROM tensions WHERE tension_id = ?", (ref,))
    if row is None:
        return _card(ref, NOT_FOUND)
    if row["scope"] == "document" and row["subject_id"] and \
            not auth.can(store, actor, row["subject_id"], "view"):
        return _card(ref, DENIED)
    return _card(
        ref, OK,
        label=row["summary"],
        detail=f"{row['kind']} · {row['scope']}",
        href=f"/-/orpheus/review#{ref}",
        # An open tension is not "unchecked" -- somebody raised it on purpose.
        # It is checked when it has been settled, and its own caveat says which.
        review=_review("confirmed" if row["status"] != "open" else "unconfirmed",
                       caveat=(None if row["status"] != "open" else
                               "Open: raised, and nobody has settled it yet.")))


_RESOLVERS = {"entity": _entity, "document": _document,
              "instance": _instance, "tension": _tension}


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------

def resolve(store: Store, ref: str, actor: dict | None) -> dict:
    """What this ref points at, as this actor, right now.

    Never raises for a bad ref: a drafting space renders whatever somebody
    typed, and an exception there would take a paragraph of prose down with it.
    A ref that is not ours answers `not_found` and the caller moves on.
    """
    kind = kind_of(ref)
    if kind is None:
        return {"ref": ref, "status": NOT_FOUND, "kind": None}
    return _RESOLVERS[kind](store, ref, actor)


def resolve_many(store: Store, refs, actor: dict | None) -> list[dict]:
    """A page of prose at once, in the order asked, duplicates collapsed.

    One request rather than one per pill: a memo can easily cite the same page
    six times, and six round trips to say the same thing is how a document
    full of references becomes slow enough that people stop making them.
    """
    seen: dict[str, dict] = {}
    ordered = []
    for ref in refs:
        if ref not in seen:
            seen[ref] = resolve(store, ref, actor)
            ordered.append(seen[ref])
    return ordered


def search(store: Store, query: str, actor: dict | None,
           limit: int = 10) -> list[dict]:
    """Things this actor could cite, for the picker that offers them.

    Filtered by the reader, so the autocomplete cannot become the enumeration
    tool the resolver refuses to be. Documents come from
    `auth.visible_documents`, which is the same rule `can()` applies one at a
    time.
    """
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{query}%"
    hits: list[dict] = []

    for row in store.query(
            "SELECT entity_id FROM entities WHERE merged_into IS NULL "
            "AND canonical_name LIKE ? ORDER BY canonical_name LIMIT ?",
            (like, limit)):
        hits.append(_entity(store, row["entity_id"], actor))

    # Every visible document, filtered in Python on the name. The alternative
    # is a LIKE the permission rule cannot see, and a query that returns rows
    # the reader may not have is one bug away from showing them.
    for row in auth.visible_documents(store, actor, limit=200):
        if len(hits) >= limit * 2:
            break
        if query.lower() in (row["filename"] or "").lower():
            hits.append(_document(store, row["document_id"], actor))

    return [hit for hit in hits if hit["status"] == OK][:limit * 2]
