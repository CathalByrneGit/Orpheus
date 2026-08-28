"""Entities: the thing itself, as against a mention of it in one document.

Everything before this was mentions. `instances_Company` rows are document-
scoped by construction — two documents naming one company are two rows, and the
only thing joining them was a key computed from the spelling. That is fine for
"does this name appear elsewhere" and useless for "what do we know about this
company", which is the question a reusable body of knowledge has to answer.

The split is small and it changes what the store is. An entity page is a
**projection**: the entity row, plus every mention, each carrying its document,
page, excerpt, confidence, grounding and review status. So every line on that
page points at a source, and a claim with no mention behind it cannot be
written. A knowledge base of uncited assertions is worth nothing to the next
project that wants to use it; this one is cited by construction.

Three rules carried over from the rest of the store, because they are what make
it trustworthy rather than merely convenient:

- **Nothing is destructive.** Unlinking a mention records the unlink; it does
  not delete the row. A merged entity keeps its row and points at its
  successor. Both are evidence about how well matching works.
- **The machine proposes, a person decides.** Linking on a normalised name is
  `unconfirmed` and says so. Only an exact stated identifier, or a person, is
  worth more, and `basis` records which — those are different kinds of evidence
  and collapsing them would lose the distinction permanently.
- **One mention has one home.** Enforced by a partial unique index, not by
  convention: the same excerpt cited on two entity pages is two pages claiming
  the same evidence, and nothing would notice.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import bundle as bundle_mod
from .audit import record_edit
from .rubric import (CONFIDENCE, EXCLUDED_STATUSES,
                     RESOLUTION_STATUSES, STATUSES)
from .store import Store
from .utils import (HONORIFICS, LEGAL_FORMS, NotFound, OrpheusError,
                    naive_key, new_id, now,
                    require_choice, require_string)

# What a link rests on. Not a confidence score — a kind of evidence. An exact
# registration number and a normalised name are not the same claim, and the
# difference has to survive into anything built on top.
BASES = ("human", "document", "identifier", "naive_key", "initials",
         "similar", "search")

BASIS_CONFIDENCE = {
    "human": CONFIDENCE["explicit"],
    # Not a match at all: for a document-scoped type the document *is* the
    # identity, so this link is the rule rather than an inference from it.
    # Stronger than `identifier` for that reason -- an identifier was extracted
    # and may be wrong, whereas which document an instance was read from is
    # recorded by ingest and is not the model's to get wrong.
    "document": CONFIDENCE["explicit"],
    # A stated company number matching exactly is about as good as machine
    # evidence gets, but the number itself was extracted and may be wrong.
    "identifier": CONFIDENCE["named"],
    # A normalised name. The caveat that follows this everywhere is the point.
    "naive_key": CONFIDENCE["implied"],
    # Two personal names agreeing on first and last, differing by a middle
    # initial. Ranked above `similar` because it is a structural reason rather
    # than a character distance, and a reviewer can check it by reading it. Not
    # ranked higher than that: two people do share a first and last name.
    "initials": CONFIDENCE["inferred"],
    # Names that are close but not equal after normalising. Weaker than an
    # exact key by definition -- it is offered as a candidate, never linked
    # automatically.
    "similar": CONFIDENCE["inferred"],
    "search": CONFIDENCE["inferred"],
}

# Above this, two names are worth *offering* as the same thing. Never worth
# linking on: everything at this basis arrives unconfirmed.
#
# Measured on the cases that matter here, with `token_sort_ratio` over
# lowercased names:
#
#   Halloran Instruments, Inc. / Halloran Instruments Inc   96.0   same
#   O'Sullivan Engineering     / OSullivan Engineering      97.7   same
#   MERIDIAN SYSTEMS LTD       / Meridian Systems Limited   90.9   same
#   Ardmore Digital Limited    / Ardmore Digital Ltd        90.5   same
#   Ernst & Young              / Ernst and Young            85.7   same
#   ---------------------------------------------------------- 80 --
#   Kestrel Medical Group      / Kestrel Medical Ltd        75.0   different
#   Ardmore Holdings plc       / Ardmore Ltd                64.5   different
#   Kestrel Medical Group      / Kestrel Dental Group       63.4   different
#   CRH Group                  / CRH plc                    62.5   different
#   Halloran Instruments       / Halloran Group             47.1   different
#
# Two things that had to be measured rather than assumed:
#
# **Case must be normalised first.** On raw names the same table overlaps
# catastrophically -- "MERIDIAN SYSTEMS LTD" against "Meridian Systems Limited"
# scores 22.7, well below pairs that are genuinely different companies.
#
# **Jaro-Winkler is the wrong scorer**, despite being the obvious choice and
# what `datasette-jellyfish` leads with. It scores "Kestrel Medical Group"
# against "Kestrel Medical Ltd" at 0.921 -- higher than it scores several true
# matches -- so it would recreate the false merge that stripping `group` as a
# suffix caused.
#
# Ten cases chosen by hand is not a calibration. The gap here is 75 to 85.7 and
# the threshold sits in the middle of it; that wants revisiting against a real
# corpus, which is why it is a module setting and an argument.
SIMILARITY_THRESHOLD = 80.0

NAIVE_CAVEAT = (
    "Links made on a normalised name are candidates, not resolution. Two "
    "organisations sharing a name are merged by it and one organisation "
    "written two ways is not."
)


# ---------------------------------------------------------------------------
# Reading mentions
# ---------------------------------------------------------------------------

def _key(store: Store, type_id: str | None, name,
         bundle: dict | None = None) -> str:
    """The naive key for a name, in the style this type's bundle asks for.

    Every comparison in this module has to use the same one. A page written
    with an organisation key and looked up with a personal one silently never
    matches, which reads as "no page exists" rather than as a bug.
    """
    return bundle_mod.key_for(
        bundle if bundle is not None else bundle_mod.active(store), type_id, name)


def _document_scoped(store: Store, bundle: dict | None = None) -> set[str]:
    """Types whose identity is the document they were read from.

    A contract's name is a title, not an identifier: three pairs of documents
    in the calibration corpus are both called "STRATEGIC ALLIANCE AGREEMENT",
    and they are different agreements, years and jurisdictions apart. Grouping
    those on a normalised name would merge them into one page, and a false
    merge is strictly worse than a false split -- a split leaves two rows a
    person can join, a merge leaves nothing to notice.

    So a document-scoped type gets one page per document. That is a false
    *split* by construction where a contract really does appear in two filings,
    which is the safe direction and exactly what `duplicate_pages()` exists to
    put in front of a person.
    """
    bundle = bundle or bundle_mod.active(store)
    if bundle is None:
        return set()
    return set(bundle_mod.implementing_types(bundle, "DocumentScoped"))


def _named_tables(store: Store, bundle: dict | None = None) -> list[tuple[str, str]]:
    """`(type_id, table)` for every type that carries a name.

    Read from the bundle's `Named` interface where there is one, so a bundle
    describing planning applications resolves applicants rather than companies.
    """
    bundle = bundle or bundle_mod.active(store)
    if bundle is None:
        return []
    type_ids = bundle_mod.implementing_types(bundle, "Named")
    out = []
    for type_id in type_ids:
        obj = bundle_mod.object_type(bundle, type_id)
        table = bundle_mod.table_name(obj) if obj else None
        if table and store.table_exists(table):
            out.append((type_id, table))
    return out


def mention(store: Store, instance_id: str) -> dict:
    """One mention, with the document it was read from."""
    row = store.one(
        "SELECT instance_id, type_id, table_name, document_id FROM instance_index "
        "WHERE instance_id = ?", (instance_id,))
    if row is None:
        raise NotFound(f"No instance {instance_id!r}.")
    detail = store.one(f'SELECT * FROM "{row["table_name"]}" WHERE instance_id = ?',
                       (instance_id,))
    return {**row, "properties": dict(detail) if detail else {}}


def _home(store: Store, instance_id: str) -> dict | None:
    return store.one(
        "SELECT entity_id, basis, status FROM entity_mentions "
        "WHERE instance_id = ? AND unlinked_at IS NULL", (instance_id,))


# ---------------------------------------------------------------------------
# Creating and reviewing an entity
# ---------------------------------------------------------------------------

def create_entity(store: Store, type_id: str, canonical_name: str,
                  actor_id: str | None = None, description: str | None = None,
                  source: str = "human", status: str | None = None) -> str:
    """Register an entity. Says nothing yet about which mentions are it."""
    store.assert_writable()
    require_string(type_id, "type_id")
    require_string(canonical_name, "canonical_name")
    bundle = bundle_mod.active(store)
    if bundle is not None and bundle_mod.object_type(bundle, type_id) is None:
        raise OrpheusError(f"The active bundle has no object type {type_id!r}.")

    entity_id = new_id("ent")
    # A person naming an entity is asserting it exists; a machine proposing one
    # is not, and the status has to say which.
    status = status or ("confirmed" if source == "human" else "unconfirmed")
    require_choice(status, STATUSES, "status")
    store.insert("entities", {
        "entity_id": entity_id,
        "type_id": type_id,
        "canonical_name": canonical_name,
        "naive_key": _key(store, type_id, canonical_name, bundle),
        "description": description,
        "source": source,
        "confidence": CONFIDENCE["explicit"] if source == "human"
                      else CONFIDENCE["implied"],
        "status": status,
        "created_at": now(),
        "created_by": actor_id,
    })
    record_edit(store, "entities", entity_id, None, "create",
                new={"type_id": type_id, "canonical_name": canonical_name,
                     "source": source},
                actor_id=actor_id)
    return entity_id


def get_entity(store: Store, entity_id: str, follow_merge: bool = True) -> dict:
    row = store.one("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))
    if row is None:
        raise NotFound(f"No entity {entity_id!r}.")
    if follow_merge and row["merged_into"]:
        # A link made before a merge still resolves, which is the reason the
        # merged row is kept rather than deleted.
        return get_entity(store, row["merged_into"], follow_merge=True)
    return dict(row)


def confirm_entity(store: Store, entity_id: str, actor_id: str,
                   note: str | None = None) -> str:
    """A person agrees this entity is a real, distinct thing."""
    return _set_entity_status(store, entity_id, "confirmed", actor_id, note)


def reject_entity(store: Store, entity_id: str, actor_id: str,
                  note: str | None = None) -> str:
    """Not a real entity — a parsing artefact, or two things confused.

    Excluded rather than deleted, and its mentions are released so they can be
    linked somewhere correct.
    """
    entity_id = _set_entity_status(store, entity_id, "rejected", actor_id, note)
    with store.transaction():
        for row in store.query(
                "SELECT instance_id FROM entity_mentions "
                "WHERE entity_id = ? AND unlinked_at IS NULL", (entity_id,)):
            unlink_mention(store, entity_id, row["instance_id"], actor_id,
                           note="Entity rejected.")
    return entity_id


def rename_entity(store: Store, entity_id: str, canonical_name: str,
                  actor_id: str, note: str | None = None) -> str:
    """Correct the name the page is filed under, keeping the old one in history."""
    store.assert_writable()
    require_string(canonical_name, "canonical_name")
    before = get_entity(store, entity_id, follow_merge=False)
    if before["canonical_name"] == canonical_name:
        raise OrpheusError(
            "That is already the name. Nothing was changed.")
    with store.transaction():
        store.execute(
            "UPDATE entities SET canonical_name = ?, naive_key = ?, "
            "status = 'amended', source = 'human', confidence = ?, "
            "amended_by = ?, amended_at = ? WHERE entity_id = ?",
            (canonical_name, _key(store, before["type_id"], canonical_name),
             CONFIDENCE["explicit"],
             actor_id, now(), entity_id))
        record_edit(store, "entities", entity_id, None, "amend",
                    previous={"canonical_name": before["canonical_name"]},
                    new={"canonical_name": canonical_name},
                    actor_id=actor_id, note=note)
    return entity_id


def describe_entity(store: Store, entity_id: str, description: str,
                    actor_id: str, note: str | None = None) -> str:
    """The page's own prose — the one thing on it that is not from a document.

    Kept apart from everything else for that reason: a reader can see at a
    glance which part of a page is sourced and which part is a person writing.
    """
    store.assert_writable()
    before = get_entity(store, entity_id, follow_merge=False)
    with store.transaction():
        store.execute(
            "UPDATE entities SET description = ?, amended_by = ?, amended_at = ? "
            "WHERE entity_id = ?", (description, actor_id, now(), entity_id))
        record_edit(store, "entities", entity_id, None, "amend",
                    previous={"description": before["description"]},
                    new={"description": description}, actor_id=actor_id, note=note)
    return entity_id


def _set_entity_status(store: Store, entity_id: str, status: str,
                       actor_id: str, note: str | None) -> str:
    store.assert_writable()
    require_string(actor_id, "actor_id")
    require_choice(status, STATUSES, "status")
    before = get_entity(store, entity_id, follow_merge=False)
    with store.transaction():
        store.execute(
            "UPDATE entities SET status = ?, amended_by = ?, amended_at = ? "
            "WHERE entity_id = ?", (status, actor_id, now(), entity_id))
        # Named explicitly rather than derived from the status: rstrip() strips
        # characters, not a suffix, and happens to work for these three only.
        action = {"confirmed": "confirm", "rejected": "reject",
                  "amended": "amend"}.get(status, status)
        record_edit(store, "entities", entity_id, None, action,
                    previous={"status": before["status"]}, new={"status": status},
                    actor_id=actor_id, note=note)
    return entity_id


# ---------------------------------------------------------------------------
# Linking mentions
# ---------------------------------------------------------------------------

def link_mention(store: Store, entity_id: str, instance_id: str,
                 actor_id: str | None = None, basis: str = "human",
                 note: str | None = None, status: str | None = None) -> dict:
    """Attach one mention to one entity.

    Refuses if the mention already has a home. Moving it is `unlink` then
    `link`, deliberately two steps: a silent re-home would rewrite which page
    cites a piece of evidence with nothing recording that it moved.
    """
    store.assert_writable()
    require_choice(basis, BASES, "basis")
    entity = get_entity(store, entity_id)
    found = mention(store, instance_id)

    if entity["type_id"] != found["type_id"]:
        raise OrpheusError(
            f"{instance_id} is a {found['type_id']} and {entity_id} is a "
            f"{entity['type_id']}. A name that is a company in one document and "
            "a person in another is a finding to look at, not a link to make."
        )

    existing = _home(store, instance_id)
    if existing and existing["entity_id"] == entity["entity_id"]:
        return {"entity_id": entity["entity_id"], "instance_id": instance_id,
                "already_linked": True}
    if existing:
        raise OrpheusError(
            f"{instance_id} is already linked to {existing['entity_id']}. "
            "Unlink it first — moving evidence between pages silently would "
            "leave nothing recording that it moved."
        )

    status = status or ("confirmed" if basis == "human" else "unconfirmed")
    require_choice(status, STATUSES, "status")
    with store.transaction():
        # A previous, unlinked row for this pair may exist; the primary key is
        # (entity_id, instance_id), so relinking updates it rather than
        # inserting a duplicate.
        store.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, document_id, "
            "basis, confidence, status, linked_by, linked_at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id, instance_id) DO UPDATE SET "
            "basis = excluded.basis, confidence = excluded.confidence, "
            "status = excluded.status, linked_by = excluded.linked_by, "
            "linked_at = excluded.linked_at, note = excluded.note, "
            "unlinked_at = NULL, unlinked_by = NULL",
            (entity["entity_id"], instance_id, found["document_id"], basis,
             BASIS_CONFIDENCE[basis], status, actor_id, now(), note))
        record_edit(store, "entity_mentions", instance_id, found["document_id"],
                    "link", new={"entity_id": entity["entity_id"], "basis": basis},
                    actor_id=actor_id, note=note)
    return {"entity_id": entity["entity_id"], "instance_id": instance_id,
            "basis": basis, "status": status}


def unlink_mention(store: Store, entity_id: str, instance_id: str,
                   actor_id: str | None = None, note: str | None = None) -> dict:
    """Detach a mention. The row stays, marked unlinked.

    A link a person removed is evidence about how well matching works — the
    same reason a rejected instance is kept rather than deleted.
    """
    store.assert_writable()
    row = store.one(
        "SELECT * FROM entity_mentions WHERE entity_id = ? AND instance_id = ? "
        "AND unlinked_at IS NULL", (entity_id, instance_id))
    if row is None:
        raise NotFound(
            f"{instance_id} is not currently linked to {entity_id}.")
    with store.transaction():
        store.execute(
            "UPDATE entity_mentions SET unlinked_at = ?, unlinked_by = ?, "
            "note = COALESCE(?, note) WHERE entity_id = ? AND instance_id = ?",
            (now(), actor_id, note, entity_id, instance_id))
        record_edit(store, "entity_mentions", instance_id, row["document_id"],
                    "unlink", previous={"entity_id": entity_id, "basis": row["basis"]},
                    new={"entity_id": None}, actor_id=actor_id, note=note)
    return {"entity_id": entity_id, "instance_id": instance_id, "unlinked": True}


def confirm_link(store: Store, entity_id: str, instance_id: str, actor_id: str,
                 note: str | None = None) -> dict:
    """A person agrees a proposed link is right."""
    store.assert_writable()
    row = store.one(
        "SELECT status FROM entity_mentions WHERE entity_id = ? "
        "AND instance_id = ? AND unlinked_at IS NULL", (entity_id, instance_id))
    if row is None:
        raise NotFound(f"{instance_id} is not currently linked to {entity_id}.")
    with store.transaction():
        store.execute(
            "UPDATE entity_mentions SET status = 'confirmed', confidence = ? "
            "WHERE entity_id = ? AND instance_id = ?",
            (CONFIDENCE["explicit"], entity_id, instance_id))
        record_edit(store, "entity_mentions", instance_id, None, "confirm",
                    previous={"status": row["status"]}, new={"status": "confirmed"},
                    actor_id=actor_id, note=note)
    return {"entity_id": entity_id, "instance_id": instance_id,
            "status": "confirmed"}


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def merge_entities(store: Store, keep_id: str, merge_id: str, actor_id: str,
                   note: str | None = None) -> dict:
    """Two pages turn out to be one thing.

    The merged entity keeps its row and points at the survivor, so a link made
    before the merge still resolves and the merge itself is readable afterwards.
    Splitting is the inverse and is deliberately not one operation: unlink the
    mentions that belong elsewhere and create the entity they belong to, so each
    half is a decision with its own record.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    if keep_id == merge_id:
        raise OrpheusError("An entity cannot be merged into itself.")
    keep = get_entity(store, keep_id)
    merge = get_entity(store, merge_id, follow_merge=False)
    if merge["merged_into"]:
        raise OrpheusError(
            f"{merge_id} was already merged into {merge['merged_into']}.")
    if keep["type_id"] != merge["type_id"]:
        raise OrpheusError(
            f"{keep_id} is a {keep['type_id']} and {merge_id} is a "
            f"{merge['type_id']}. Merging across types would make the type "
            "meaningless on the surviving page.")

    moved = 0
    with store.transaction():
        for row in store.query(
                "SELECT instance_id, basis, status, note FROM entity_mentions "
                "WHERE entity_id = ? AND unlinked_at IS NULL", (merge_id,)):
            unlink_mention(store, merge_id, row["instance_id"], actor_id,
                           note=f"Merged into {keep['entity_id']}.")
            link_mention(store, keep["entity_id"], row["instance_id"], actor_id,
                         basis=row["basis"], status=row["status"],
                         note=row["note"])
            moved += 1
        store.execute(
            "UPDATE entities SET merged_into = ?, status = 'amended', "
            "amended_by = ?, amended_at = ? WHERE entity_id = ?",
            (keep["entity_id"], actor_id, now(), merge_id))
        record_edit(store, "entities", merge_id, None, "merge",
                    previous={"entity_id": merge_id,
                              "canonical_name": merge["canonical_name"]},
                    new={"merged_into": keep["entity_id"], "mentions_moved": moved},
                    actor_id=actor_id, note=note)
    return {"kept": keep["entity_id"], "merged": merge_id,
            "mentions_moved": moved}


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------

# Words that say how a thing is incorporated or addressed, not which thing it
# is. Two names differing only in these are two renderings of one name; two
# names differing in anything else are making different claims.
#
# `group` and `holdings` are deliberately absent, for the reason they are absent
# from the suffix list: they denote a different legal entity in a corporate
# structure, so "Kestrel Medical Group" and "Kestrel Medical Ltd" differ in
# something that matters and stay a question for a person.
_GENERIC_TOKENS = frozenset(LEGAL_FORMS) | frozenset(HONORIFICS) | {
    "the", "and", "of", "for", "a", "an",
}

_TOKEN = re.compile(r"\w+", re.UNICODE)

#: Properties that state which thing this is, rather than describe it. Sharing
#: one is the strongest evidence short of a person saying so.
_IDENTIFIER_PROPS = frozenset({"registration_number", "reference"})


def _distinctive(token: str) -> bool:
    """Does this word say *which* thing is meant?

    A single letter does not -- it is an initial, and "Mitchell S. Felder"
    differs from "Mitchell Felder" by one.
    """
    return len(token) > 1 and token.lower() not in _GENERIC_TOKENS


#: Above this, two words are one word written twice rather than two words.
#:
#: Measured on the cases that matter, with `difflib`'s ratio:
#:
#:     instruments / instrument   0.952   the same word
#:     medical     / medicals     0.933   the same word
#:     services    / service      0.933   the same word
#:     digital     / digitel      0.857   a typo
#:     kestrel     / kestral      0.857   a typo
#:     ardmore     / ardmoor      0.857   a typo
#:     ---------------------------------  the line
#:     operating   / operations   0.842   different words
#:     franchisee  / franchisor   0.800   different words
#:     eftc        / tec          0.571   different words
#:
#: `difflib` rather than rapidfuzz because this rule decides which candidates
#: are offered at all, and it must not change depending on an optional install.
SAME_WORD = 0.85


def _has_counterpart(token: str, others: set[str]) -> bool:
    """Is this word a spelling of one of those?"""
    from difflib import SequenceMatcher

    return any(SequenceMatcher(None, token, other).ratio() >= SAME_WORD
               for other in others)


def distinguishing_tokens(a: str, b: str) -> tuple[set[str], set[str]]:
    """The distinctive words each name has and the other does not.

    Pure, and the whole of the rule below: if *both* sides come back non-empty,
    the two names are naming different things.

    A word with a near-spelling on the other side does not count -- "Halloran
    Instruments, Inc." and "Halloran Instrument Inc." differ by a plural, which
    is exactly the case fuzzy matching exists for and must not be filtered out
    on its way to a person.
    """
    left = {t.lower() for t in _TOKEN.findall(a or "")}
    right = {t.lower() for t in _TOKEN.findall(b or "")}
    return ({t for t in left - right
             if _distinctive(t) and not _has_counterpart(t, right - left)},
            {t for t in right - left
             if _distinctive(t) and not _has_counterpart(t, left - right)})


_INITIAL = re.compile(r"^\w$")


def same_but_for_an_initial(a: str, b: str) -> bool:
    """Do these two personal names agree except for a middle initial?

    "Mitchell Felder" and "Mitchell S. Felder" are one man written twice, and
    the 40-document run made them two pages holding two of his three relations.
    A spelling score says 90.9 and does not say *why*; this says why, which is
    what a reviewer needs to decide.

    Never a merge, always an offer. "John A. Smith" and "John B. Smith" satisfy
    the first-and-last-name test and are two people, so the initials themselves
    are checked for a contradiction -- but even agreeing initials are only ever
    a candidate, because two people do share a name.
    """
    left = [t.lower() for t in _TOKEN.findall(a or "")]
    right = [t.lower() for t in _TOKEN.findall(b or "")]
    if len(left) < 2 or len(right) < 2:
        return False
    if (left[0], left[-1]) != (right[0], right[-1]):
        return False
    # The middles, which is where an initial lives.
    inner_left = [t for t in left[1:-1]]
    inner_right = [t for t in right[1:-1]]
    if inner_left == inner_right:
        return False        # identical names, not this basis
    # Every middle part that both carry has to agree, and at least one side
    # must be an initial rather than a different given name: "John A. Smith"
    # and "John B. Smith" contradict, and "John Paul Smith" and "John Peter
    # Smith" are two names rather than one abbreviated.
    if not any(_INITIAL.match(t) for t in inner_left + inner_right):
        return False
    for x in inner_left:
        for y in inner_right:
            if _INITIAL.match(x) or _INITIAL.match(y):
                if x[0] != y[0]:
                    return False
            elif x != y:
                return False
    return True


def could_be_one_thing(a: str, b: str) -> bool:
    """Could these two names be two renderings of one thing?

    Spelling distance alone says yes far too often, because names in one corpus
    share their boilerplate. Measured on the 40-document run, `token_sort_ratio`
    scored "EFTC OPERATING CORP." against "K*TEC OPERATING CORP." at 87.8 and
    "SUNTRON CORPORATION" against "UTEK Corporation" at 80.0 -- two pairs of
    unrelated companies -- because most of each string is words every name has.
    Corpus frequency does not separate them either: in 74 company names
    "operating" and "healthplan" both appear twice, and only one of those pairs
    is real.

    What separates them is which side carries the difference. A name that
    *extends* another is a candidate -- "Sykes HealthPlan Services, Inc." over
    "HealthPlan Services, Inc.", "Mitchell S. Felder" over "Mitchell Felder",
    "Ernst and Young" over "Ernst & Young". A pair where each name has a
    distinctive word the other lacks is two different things, whatever the
    characters say.

    This only ever withholds a candidate. Nothing here merges anything, and two
    pages this rejects can still be merged by a person who knows better.
    """
    left, right = distinguishing_tokens(a, b)
    return not (left and right)


def similar_names(store: Store, name: str, type_id: str,
                  threshold: float | None = None,
                  limit: int = 5) -> list[dict]:
    """Entity names close to this one but not equal after normalising.

    Catches what `naive_key` cannot by construction: it compares keys for
    equality, so a spelling that normalises differently is invisible to it
    however obviously it is the same thing. `"Ernst & Young"` and `"Ernst and
    Young"` are the documented example -- the ampersand becomes a space and the
    word does not, and no amount of suffix rules fixes that.

    Optional. Without rapidfuzz installed this returns nothing rather than
    failing, because exact matching still works and is what the rest of the
    system is built on.
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return []

    rows = store.query(
        "SELECT entity_id, canonical_name, status FROM entities "
        "WHERE type_id = ? AND merged_into IS NULL AND status != 'rejected'",
        (type_id,))
    if not rows:
        return []

    cutoff = threshold if threshold is not None else SIMILARITY_THRESHOLD
    # Lowercased before scoring, and nothing more. Stripping suffixes here would
    # be `naive_key` again, and stripping them is what caused the false merges
    # this is meant to complement rather than repeat.
    by_folded: dict[str, dict] = {}
    for row in rows:
        by_folded.setdefault(row["canonical_name"].lower(), row)
    matches = process.extract(name.lower(), list(by_folded),
                              scorer=fuzz.token_sort_ratio,
                              limit=limit, score_cutoff=cutoff)
    out = []
    for folded, score, _ in matches:
        row = by_folded[folded]
        matched = row["canonical_name"]
        # An exact key match is reported as `naive_key` by the caller; this is
        # only for the ones that differ.
        if _key(store, type_id, matched) == _key(store, type_id, name):
            continue
        # Each carrying a distinctive word the other lacks means two things,
        # whatever the characters say.
        if not could_be_one_thing(name, matched):
            continue
        out.append({**dict(row), "basis": "similar", "score": round(score, 1),
                    "evidence": f"name {score:.0f}% similar to {matched!r}"})
    return out


def duplicate_pages(store: Store, type_id: str | None = None,
                    threshold: float | None = None,
                    limit: int = 50) -> list[dict]:
    """Pages that look like the same thing, offered for merging.

    `propose_entities()` makes one page per group, and it groups on exact keys.
    So a name that normalises two ways becomes two pages -- `"Ernst & Young"`
    and `"Ernst and Young"` are the standing example -- and nothing surfaces
    them, because every mention has a home and the queue is empty. The split is
    invisible precisely when the machine has finished its work.

    This is the other half of `similar_names()`: that one catches a mention with
    no page, this one catches two pages that should be one. Neither ever merges
    anything; both produce candidates for a person.

    Document-scoped types are left out. For those the name is a title, so an
    identical name is near-worthless evidence -- three pairs of unrelated
    agreements in the calibration corpus are both called "STRATEGIC ALLIANCE
    AGREEMENT" -- and offering them at a 100% score would misrepresent how good
    that evidence is and train a reviewer to merge on it. Two such pages can
    still be merged by hand; what is withheld is the machine offering it on
    evidence it cannot stand behind.
    """
    scoped = _document_scoped(store)
    if type_id and type_id in scoped:
        return []
    clause = "AND type_id = ?" if type_id else ""
    rows = [r for r in store.query(
        "SELECT entity_id, type_id, canonical_name, naive_key, status FROM entities "
        f"WHERE merged_into IS NULL AND status != 'rejected' {clause} "
        "ORDER BY canonical_name", (type_id,) if type_id else ())
        if r["type_id"] not in scoped]

    cutoff = threshold if threshold is not None else SIMILARITY_THRESHOLD
    counts = {row["entity_id"]: store.scalar(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ? "
        "AND unlinked_at IS NULL", (row["entity_id"],)) or 0 for row in rows}

    # A pair somebody has already settled, and whose evidence has not moved
    # since. Offering it again is how a candidate list teaches people to ignore
    # it -- and it would have the next reviewer establish from scratch what the
    # last one wrote down.
    settled = {row["pair"] for row in store.query(
        "SELECT pair FROM resolution_reviews WHERE superseded_at IS NULL "
        "AND status IN ('same', 'different')")}

    def offer(a, b, basis, score, evidence):
        # The page with more evidence behind it is the better survivor, so it
        # is named first -- but this is a suggestion, and merge() takes
        # whichever order a person chooses.
        if counts[a["entity_id"]] < counts[b["entity_id"]]:
            a, b = b, a
        return {"keep": {**dict(a), "n_mentions": counts[a["entity_id"]]},
                "merge": {**dict(b), "n_mentions": counts[b["entity_id"]]},
                "basis": basis, "score": round(score, 1), "evidence": evidence}

    pairs = []
    seen: set[frozenset] = set()

    # Two pages under one key first, and without rapidfuzz. This is the
    # strongest evidence of a split there is -- stronger than any spelling
    # score, because it is the same test `propose_entities` groups on -- and
    # reporting it as "88% similar" describes it as something weaker than it
    # is. It also finds pairs the fuzzy pass cannot: "Foo Co Ltd" and "Foo"
    # share a key and score too low to be offered.
    by_key: dict[tuple, list] = {}
    for row in rows:
        if row["naive_key"]:
            by_key.setdefault((row["type_id"], row["naive_key"]), []).append(row)
    for group in by_key.values():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                seen.add(frozenset((left["entity_id"], right["entity_id"])))
                if _is_settled(store, left, right, settled):
                    continue
                pairs.append(offer(
                    left, right, "naive_key", 100.0,
                    f"both filed under the name key {left['naive_key']!r}"))

    try:
        from rapidfuzz import fuzz
    except ImportError:
        # Exact matching still works and is what the rest of the system rests
        # on, so the key pass above is returned rather than nothing.
        return pairs[:limit]

    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if left["type_id"] != right["type_id"]:
                continue
            if frozenset((left["entity_id"], right["entity_id"])) in seen:
                continue
            score = fuzz.token_sort_ratio(left["canonical_name"].lower(),
                                          right["canonical_name"].lower())
            if score < cutoff:
                continue
            if not could_be_one_thing(left["canonical_name"],
                                      right["canonical_name"]):
                continue
            if _is_settled(store, left, right, settled):
                continue
            initials = same_but_for_an_initial(left["canonical_name"],
                                               right["canonical_name"])
            pairs.append(offer(
                left, right,
                "initials" if initials else "similar", score,
                "same first and last name, differing by an initial" if initials
                else f"names {score:.0f}% similar"))

    # Strongest evidence first, and only then the score. A 90% spelling match
    # ranked above a shared key would put the weaker reason at the top of a
    # reviewer's list.
    pairs.sort(key=lambda p: (BASES.index(p["basis"]), -p["score"]))
    return pairs[:limit]


# ---------------------------------------------------------------------------
# The evidence for merging two pages
# ---------------------------------------------------------------------------

#: Properties that describe a row rather than identify the thing it is about.
#: Sharing one of these is not evidence of anything.
_BOOKKEEPING = frozenset({
    "instance_id", "document_id", "naive_key", "source", "confidence",
    "status", "amended_by", "amended_at", "created_at", "name", "page_no",
})


def _values_by_page(store: Store, type_id: str, table: str,
                    prop: str) -> dict[str, set[str]]:
    """Every page of this type, and the values it carries for one property."""
    out: dict[str, set[str]] = {}
    for row in store.query(
            f'SELECT m.entity_id, i."{prop}" AS value FROM "{table}" i '
            "JOIN entity_mentions m ON m.instance_id = i.instance_id "
            "  AND m.unlinked_at IS NULL "
            "JOIN entities e ON e.entity_id = m.entity_id "
            f'WHERE e.type_id = ? AND e.merged_into IS NULL AND i."{prop}" '
            "IS NOT NULL AND i.\"" + prop + "\" != ''", (type_id,)):
        out.setdefault(row["entity_id"], set()).add(str(row["value"]))
    return out


def shared_attributes(store: Store, a_id: str, b_id: str) -> list[dict]:
    """Property values two pages both carry, and how rare each one is.

    Rarity is the whole of it, and the corpus makes the point sharply. Both
    Felder pages carry `acting_for = "Marv Enterprises, LLC"`, which two pages
    in the corpus share -- that is evidence. "EFTC OPERATING CORP." and "K*TEC
    OPERATING CORP." both carry `entity_kind = "private_company"`, which 64
    pages share, and they are different companies -- that is not.

    So the count comes back with the value rather than a verdict. A reviewer
    reading "an address only these two pages carry" and "a kind 64 pages carry"
    does not need to be told which one matters.
    """
    a = get_entity(store, a_id, follow_merge=False)
    b = get_entity(store, b_id, follow_merge=False)
    if a["type_id"] != b["type_id"]:
        return []

    table = store.scalar(
        "SELECT table_name FROM instance_index i "
        "JOIN entity_mentions m ON m.instance_id = i.instance_id "
        "WHERE m.entity_id = ? LIMIT 1", (a_id,))
    if not table or not store.table_exists(table):
        return []

    columns = [c for c in store.columns(table) if c not in _BOOKKEEPING]
    out = []
    for prop in columns:
        by_page = _values_by_page(store, a["type_id"], table, prop)
        both = by_page.get(a_id, set()) & by_page.get(b_id, set())
        of_type = store.scalar(
            "SELECT COUNT(*) FROM entities WHERE type_id = ? AND merged_into IS NULL",
            (a["type_id"],)) or 0
        for value in sorted(both):
            carrying = sum(1 for page, values in by_page.items()
                           if value in values)
            # The count needs its denominator. Three pages sharing a value is
            # distinctive among 76 and meaningless among 4, and a bare number
            # reads as a score either way.
            share = carrying / of_type if of_type else 0
            out.append({
                "property": prop, "value": value,
                "n_pages_sharing": carrying,
                "n_pages_of_type": of_type,
                "note": (f"{carrying} of {of_type} {a['type_id']} pages carry "
                         f"it" + (", so it says little about these two"
                                  if share > 0.25 else "")),
            })
    # Rarest first: the thing worth reading is at the top.
    out.sort(key=lambda r: (r["n_pages_sharing"], r["property"]))
    return out


def _passages_for(store: Store, entity_id: str, limit: int = 6) -> list[dict]:
    """Where the documents say this name, so a person can read it themselves."""
    return [dict(r) for r in store.query(
        "SELECT p.document_id, d.filename, p.page_no, p.excerpt "
        "FROM entity_mentions m "
        "JOIN provenance p ON p.instance_id = m.instance_id "
        "LEFT JOIN documents d ON d.document_id = p.document_id "
        "WHERE m.entity_id = ? AND m.unlinked_at IS NULL "
        "AND p.excerpt IS NOT NULL AND p.excerpt != '' LIMIT ?",
        (entity_id, limit))]


def resolution_evidence(store: Store, a_id: str, b_id: str) -> dict:
    """Everything the store holds bearing on whether two pages are one thing.

    Assembled, never judged. This returns what there is and how much each part
    is worth; deciding is a person's, and `merge_entities()` is still the only
    way it happens.

    The point of gathering it in one place is that the alternative is a person
    running six queries per pair, or a model inventing the answer. What a model
    is good for here is reading the passages at the end and saying what they
    show -- and the passages are quoted from the documents, so what it says can
    be checked against them.
    """
    a = get_entity(store, a_id, follow_merge=False)
    b = get_entity(store, b_id, follow_merge=False)

    attributes = shared_attributes(store, a_id, b_id)
    identifiers = [r for r in attributes if r["property"] in _IDENTIFIER_PROPS]
    documents_a = {r["document_id"] for r in store.query(
        "SELECT DISTINCT document_id FROM entity_mentions "
        "WHERE entity_id = ? AND unlinked_at IS NULL", (a_id,))}
    documents_b = {r["document_id"] for r in store.query(
        "SELECT DISTINCT document_id FROM entity_mentions "
        "WHERE entity_id = ? AND unlinked_at IS NULL", (b_id,))}

    left, right = distinguishing_tokens(a["canonical_name"], b["canonical_name"])
    return {
        "pages": [
            {"entity_id": a_id, "canonical_name": a["canonical_name"],
             "type_id": a["type_id"], "status": a["status"],
             "n_documents": len(documents_a)},
            {"entity_id": b_id, "canonical_name": b["canonical_name"],
             "type_id": b["type_id"], "status": b["status"],
             "n_documents": len(documents_b)},
        ],
        "same_type": a["type_id"] == b["type_id"],
        "identifiers": identifiers,
        "names": {
            "same_key": a["naive_key"] == b["naive_key"] and bool(a["naive_key"]),
            "differ_by_an_initial": same_but_for_an_initial(
                a["canonical_name"], b["canonical_name"]),
            "words_only_the_first_has": sorted(left),
            "words_only_the_second_has": sorted(right),
            "could_be_one_thing": could_be_one_thing(a["canonical_name"],
                                                    b["canonical_name"]),
        },
        "shared_attributes": [r for r in attributes
                              if r["property"] not in _IDENTIFIER_PROPS],
        # Reported because a reviewer will ask, and labelled because the
        # obvious reading of it is wrong.
        "weak_signals": {
            "shared_documents": sorted(documents_a & documents_b),
            "note": ("Appearing in the same document is not evidence of being "
                     "the same thing. Measured on this corpus: EFTC OPERATING "
                     "CORP. and K*TEC OPERATING CORP. share a document and a "
                     "neighbouring page and are different companies, because "
                     "naming two different parties is what a contract does."),
        },
        "passages": {
            a_id: _passages_for(store, a_id),
            b_id: _passages_for(store, b_id),
        },
        "caveat": ("Assembled, not judged. Nothing here merges anything, and a "
                   "shared value is worth what its rarity says it is worth -- "
                   "read `n_pages_sharing` before reading the match."),
    }


def _is_settled(store: Store, left: dict, right: dict,
                settled: set[str]) -> bool:
    """Has somebody decided this pair on evidence that still holds?

    The digest is only recomputed for a pair somebody actually reviewed, which
    is a handful -- doing it for every pair would make a corpus-wide pass
    quadratic in queries rather than in comparisons.
    """
    key = ":".join(sorted((left["entity_id"], right["entity_id"])))
    if key not in settled:
        return False
    verdict = resolution_verdict(store, left["entity_id"], right["entity_id"])
    return bool(verdict) and not verdict["stale"]


def _pair(a_id: str, b_id: str) -> tuple[str, str]:
    """The pair, ordered. Which way round somebody looked at two pages is not
    a different question."""
    return tuple(sorted((a_id, b_id)))  # type: ignore[return-value]


def evidence_digest(evidence: dict) -> str:
    """A digest of what was known, so a judgement does not outlive it.

    Covers the parts that would change the answer: the identifiers, the shared
    values, the name analysis, and which documents each page rests on. A new
    document carrying a matching address makes this a different question, and
    the pair comes back rather than staying settled on evidence that has moved.

    Deliberately not the passages: more of the same excerpts is more of what
    was already read, and re-asking on every re-extraction would make the
    judgement worthless.
    """
    payload = {
        "identifiers": sorted(
            (r["property"], r["value"]) for r in evidence["identifiers"]),
        "attributes": sorted(
            (r["property"], r["value"]) for r in evidence["shared_attributes"]),
        "names": {k: v for k, v in sorted(evidence["names"].items())},
        "documents": sorted(
            (p["entity_id"], p["n_documents"]) for p in evidence["pages"]),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


def review_resolution(store: Store, a_id: str, b_id: str, status: str,
                      rationale: str, actor_id: str | None = None) -> dict:
    """Record what somebody decided about two pages, and why.

    `rationale` is required for every state, including `unsure`, because the
    reason is the part worth anything later. "Different registered addresses,
    and the 2019 filing names both in the same schedule" is a fact somebody
    established; without it the next reviewer establishes it again.

    This decides nothing by itself. `same` does not merge -- `merge_entities()`
    is still the only thing that does, and a person still calls it.
    """
    store.assert_writable()
    require_choice(status, RESOLUTION_STATUSES, "status")
    require_string(rationale, "rationale")
    if a_id == b_id:
        raise OrpheusError("A page is not a pair with itself.")

    left, right = _pair(a_id, b_id)
    digest = evidence_digest(resolution_evidence(store, left, right))
    review_id = new_id("rrv")
    with store.transaction():
        previous = store.one(
            "SELECT * FROM resolution_reviews WHERE pair = ? "
            "AND superseded_at IS NULL", (f"{left}:{right}",))
        if previous:
            store.execute(
                "UPDATE resolution_reviews SET superseded_at = ? "
                "WHERE review_id = ?", (now(), previous["review_id"]))
        store.insert("resolution_reviews", {
            "review_id": review_id,
            "pair": f"{left}:{right}",
            "entity_a": left, "entity_b": right,
            "evidence_digest": digest,
            "status": status,
            "rationale": rationale,
            "reviewed_by": actor_id,
            "reviewed_at": now(),
            "superseded_at": None,
        })
        record_edit(store, "resolution_reviews", review_id, None,
                    "review_resolution",
                    previous={"status": previous["status"]} if previous else None,
                    new={"status": status, "pair": f"{left}:{right}"},
                    actor_id=actor_id, note=rationale)
    return store.one("SELECT * FROM resolution_reviews WHERE review_id = ?",
                     (review_id,))


def resolution_verdict(store: Store, a_id: str, b_id: str) -> dict | None:
    """The live judgement about two pages, if it still rests on what is known.

    Returns None where nobody has looked, and also where somebody looked and
    the evidence has since moved -- in which case the old judgement is still on
    file, and reported as `stale` rather than quietly applied.
    """
    left, right = _pair(a_id, b_id)
    row = store.one("SELECT * FROM resolution_reviews WHERE pair = ? "
                    "AND superseded_at IS NULL", (f"{left}:{right}",))
    if row is None:
        return None
    fresh = evidence_digest(resolution_evidence(store, left, right))
    settled = dict(row)
    settled["stale"] = fresh != row["evidence_digest"]
    return settled


def candidates_for_mention(store: Store, instance_id: str,
                           limit: int = 10) -> list[dict]:
    """Which existing entities could this mention be?

    Ordered by the strength of the evidence, not by similarity: an exact stated
    identifier beats a normalised name however close the spelling, and a close
    spelling is the weakest of the three.
    """
    found = mention(store, instance_id)
    properties = found["properties"]
    name = properties.get("name") or ""
    key = properties.get("naive_key") or _key(store, found["type_id"], name)

    out: dict[str, dict] = {}

    identifier = properties.get("registration_number")
    if identifier:
        for row in store.query(
                "SELECT DISTINCT e.entity_id, e.canonical_name, e.status "
                "FROM entities e JOIN entity_mentions m USING (entity_id) "
                f'JOIN "{found["table_name"]}" i ON i.instance_id = m.instance_id '
                "WHERE m.unlinked_at IS NULL AND e.merged_into IS NULL "
                "AND i.registration_number = ? AND e.type_id = ?",
                (identifier, found["type_id"])):
            out[row["entity_id"]] = {**dict(row), "basis": "identifier",
                                     "evidence": f"registration number {identifier}"}

    if key:
        for row in store.query(
                "SELECT entity_id, canonical_name, status FROM entities "
                "WHERE naive_key = ? AND type_id = ? AND merged_into IS NULL "
                "AND status != 'rejected' LIMIT ?",
                (key, found["type_id"], limit)):
            out.setdefault(row["entity_id"], {**dict(row), "basis": "naive_key",
                                              "evidence": f"name key {key!r}"})

    if name and bundle_mod.name_style(
            bundle_mod.object_type(bundle_mod.active(store),
                                   found["type_id"])) == "personal":
        for row in store.query(
                "SELECT entity_id, canonical_name, status FROM entities "
                "WHERE type_id = ? AND merged_into IS NULL AND status != 'rejected'",
                (found["type_id"],)):
            if same_but_for_an_initial(name, row["canonical_name"]):
                out.setdefault(row["entity_id"], {
                    **dict(row), "basis": "initials",
                    "evidence": f"same first and last name as "
                                f"{row['canonical_name']!r}, differing by an initial"})

    if name:
        for candidate in similar_names(store, name, found["type_id"],
                                       limit=limit):
            out.setdefault(candidate["entity_id"], candidate)

    ranked = sorted(out.values(), key=lambda c: (BASES.index(c["basis"]),
                                                 -c.get("score", 0)))
    return ranked[:limit]


def unlinked_mentions(store: Store, type_id: str | None = None,
                      document_id: str | None = None,
                      limit: int = 200) -> list[dict]:
    """Mentions with no entity yet — the work queue for building the wiki."""
    rows = []
    for found_type, table in _named_tables(store):
        if type_id and found_type != type_id:
            continue
        clause = "AND i.document_id = ?" if document_id else ""
        # A one-element tuple's repr carries a trailing comma, which is not SQL.
        excluded = ", ".join("?" for _ in EXCLUDED_STATUSES)
        params: tuple = tuple(EXCLUDED_STATUSES) + (
            (document_id,) if document_id else ())
        rows += [dict(r) for r in store.query(
            f'SELECT i.instance_id, i.document_id, i.name, i.naive_key, '
            f"       '{found_type}' AS type_id "
            f'FROM "{table}" i '
            "LEFT JOIN entity_mentions m ON m.instance_id = i.instance_id "
            "  AND m.unlinked_at IS NULL "
            f"WHERE m.entity_id IS NULL AND i.status NOT IN ({excluded}) "
            f"AND i.name IS NOT NULL AND i.name != '' {clause} "
            f"LIMIT {int(limit)}", params)]
    return rows


def propose_entities(store: Store, type_id: str | None = None,
                     actor_id: str | None = None,
                     min_mentions: int = 1) -> dict:
    """Bootstrap the wiki: group unlinked mentions and propose an entity each.

    Everything proposed here is `unconfirmed` and linked on `naive_key`, which
    is the weakest basis there is. That is deliberate — this exists to turn a
    pile of mentions into a reviewable queue, not to decide anything. A person
    confirming a page is what makes it real.

    Mentions carrying the same stated identifier are grouped together even when
    their names differ, because an exact registration number is better evidence
    than a spelling.

    A group whose page **already exists** is attached to it rather than given a
    second one. Without that, the wiki fragments a little more every time a
    corpus grows: proposing is the normal thing to do after ingesting more
    documents, and each round would mint a fresh "Kestrel Medical Group" beside
    the one already there — two pages with an identical key, which is the
    strongest evidence of sameness the store has.
    """
    store.assert_writable()
    pending = unlinked_mentions(store, type_id=type_id)
    if not pending:
        return {"proposed": 0, "linked": 0, "entities": [], "caveat": NAIVE_CAVEAT}

    # Group by identifier where there is one, else by name key -- except for a
    # document-scoped type, where the document *is* the identity and a shared
    # title is not evidence of anything.
    scoped = _document_scoped(store)
    groups: dict[tuple, list[dict]] = {}
    for found in pending:
        detail = mention(store, found["instance_id"])["properties"]
        identifier = detail.get("registration_number")
        if found["type_id"] in scoped:
            # The document bounds how far the name reaches; the name separates
            # things inside it. One document really can describe two contracts
            # -- "AMENDMENT NO. 1" and the agreement it amends sit in the same
            # filing -- so keying on the document alone merges an amendment
            # into the thing it changes. `naive_key` is derived here rather
            # than read, because a bundle that has just gained the column has
            # it empty on every row written before.
            key = (found["type_id"],
                   f"doc:{found['document_id']}:"
                   f"{found['naive_key'] or _key(store, found['type_id'], found['name'], bundle)}")
            identifier = None
        elif identifier:
            key = (found["type_id"], f"id:{identifier}")
        else:
            key = (found["type_id"], f"key:{found['naive_key']}")
        groups.setdefault(key, []).append({**found, "identifier": identifier})

    proposed, attached, linked, entities = 0, 0, 0, []
    with store.transaction():
        for (found_type, key), members in sorted(groups.items()):
            if len(members) < min_mentions:
                continue
            basis = ("document" if key.startswith("doc:")
                     else "identifier" if key.startswith("id:") else "naive_key")
            # The longest spelling is the least abbreviated, which is the better
            # page title: "Halloran Instruments, Inc." over "Halloran".
            canonical = max((m["name"] for m in members), key=len)
            identifier = members[0].get("identifier") if basis == "identifier" else None
            scoped_to = members[0]["document_id"] if basis == "document" else None
            entity_id = _page_for(store, found_type, canonical, identifier,
                                  document_id=scoped_to)

            existing = entity_id is not None
            if existing:
                # Attached, not renamed. The existing title was somebody's
                # decision or an earlier proposal's, and a later batch bringing
                # a longer spelling is not grounds to overwrite it.
                attached += 1
                canonical = get_entity(store, entity_id)["canonical_name"]
            else:
                # Two documents can legitimately produce two pages with the
                # same title -- that is the point of a document-scoped type --
                # so the page says which document it came from rather than
                # inventing a disambiguating name nobody used.
                entity_id = create_entity(
                    store, found_type, canonical, actor_id=actor_id,
                    source="ai_local", status="unconfirmed",
                    description=_scope_note(store, scoped_to) if scoped_to else None)
                proposed += 1
            for member in members:
                link_mention(store, entity_id, member["instance_id"],
                             actor_id=actor_id, basis=basis,
                             status="unconfirmed")
                linked += 1
            entities.append({"entity_id": entity_id, "type_id": found_type,
                             "canonical_name": canonical,
                             "n_mentions": len(members), "basis": basis,
                             "existing": existing})
    return {"proposed": proposed, "attached": attached, "linked": linked,
            "entities": entities, "caveat": NAIVE_CAVEAT}


def _scope_note(store: Store, document_id: str) -> str:
    """Which document a document-scoped page came from, for its description."""
    filename = store.scalar("SELECT filename FROM documents WHERE document_id = ?",
                            (document_id,))
    return f"Read from {filename or document_id}."


def _page_for(store: Store, type_id: str, canonical_name: str,
              identifier: str | None,
              document_id: str | None = None) -> str | None:
    """The page this group already belongs to, if there is one.

    Matched on the same bases the grouping itself uses, in the same order: for
    a document-scoped type the document, and otherwise a stated identifier
    already cited on a page, then a normalised name. Anything weaker than an
    exact match is left to `duplicate_pages()` and a person -- attaching on a
    *similar* name here would be resolution by machine, which is what the whole
    design refuses.
    """
    if document_id:
        # Re-extracting a document makes new instances for things that already
        # have a page. Both halves of the identity are matched: the document,
        # so a different filing with the same title is never pulled in, and the
        # title, so a document holding an agreement and an amendment to it does
        # not attach both to whichever page was made first.
        row = store.one(
            "SELECT e.entity_id FROM entities e "
            "JOIN entity_mentions m ON m.entity_id = e.entity_id "
            "  AND m.unlinked_at IS NULL "
            "WHERE e.merged_into IS NULL AND e.type_id = ? "
            "AND m.document_id = ? AND e.naive_key = ? LIMIT 1",
            (type_id, document_id, _key(store, type_id, canonical_name)))
        return row["entity_id"] if row else None

    if identifier:
        table = store.scalar(
            "SELECT table_name FROM instance_index WHERE type_id = ? LIMIT 1",
            (type_id,))
        if table:
            row = store.one(
                "SELECT e.entity_id FROM entities e "
                "JOIN entity_mentions m ON m.entity_id = e.entity_id "
                "  AND m.unlinked_at IS NULL "
                f'JOIN "{table}" x ON x.instance_id = m.instance_id '
                "WHERE e.merged_into IS NULL AND e.type_id = ? "
                "AND x.registration_number = ? LIMIT 1", (type_id, identifier))
            if row:
                return row["entity_id"]

    row = store.one(
        "SELECT entity_id FROM entities WHERE merged_into IS NULL "
        "AND type_id = ? AND naive_key = ? LIMIT 1",
        (type_id, _key(store, type_id, canonical_name)))
    return row["entity_id"] if row else None


# ---------------------------------------------------------------------------
# The projection: an entity as a page
# ---------------------------------------------------------------------------

def entity_page(store: Store, entity_id: str,
                include_unconfirmed: bool = True) -> dict:
    """Everything known about an entity, with a source on every line.

    This is the shape the wiki renders. Note what it does *not* do: it never
    picks a winner when two documents disagree. A property comes back as the
    set of values seen, each with the mentions asserting it and how many of
    those a person has confirmed. Choosing between them is a judgement, and
    making it here would bury it — the disagreement is usually the interesting
    part, and often both are true of the moment each document was written.

    `include_unconfirmed` is the collapsible half of the page: confirmed facts
    are what the wiki asserts, proposals sit behind a disclosure.
    """
    # Both read mentions, so they are imported here rather than at module top.
    from . import corroboration as corroboration_mod
    from . import tensions as tensions_mod

    entity = get_entity(store, entity_id)
    links = store.query(
        "SELECT instance_id, document_id, basis, confidence, status, "
        "       linked_by, linked_at, note "
        "FROM entity_mentions WHERE entity_id = ? AND unlinked_at IS NULL "
        "ORDER BY linked_at", (entity["entity_id"],))

    mentions, properties = [], {}
    documents: dict[str, dict] = {}
    reserved = {"instance_id", "document_id", "source", "confidence", "status",
                "amended_by", "amended_at", "created_at", "naive_key"}

    for link in links:
        if not include_unconfirmed and link["status"] not in ("confirmed", "amended"):
            continue
        found = mention(store, link["instance_id"])
        provenance = store.one(
            "SELECT excerpt, page_no, confidence, alignment, char_start, char_end, "
            "       source FROM provenance WHERE instance_id = ?",
            (link["instance_id"],))
        document = store.one(
            "SELECT document_id, filename, doc_type, date_added FROM documents "
            "WHERE document_id = ?", (link["document_id"],))

        record = {
            "instance_id": link["instance_id"],
            "document_id": link["document_id"],
            "document": dict(document) if document else None,
            "link": {"basis": link["basis"], "status": link["status"],
                     "confidence": link["confidence"], "linked_by": link["linked_by"]},
            "instance_status": found["properties"].get("status"),
            "instance_source": found["properties"].get("source"),
            "properties": {k: v for k, v in found["properties"].items()
                           if k not in reserved and v is not None},
            # The citation. A page line without this cannot be written.
            "evidence": dict(provenance) if provenance else None,
        }
        mentions.append(record)
        if document:
            documents[document["document_id"]] = dict(document)

        for key, value in record["properties"].items():
            if key == "name":
                continue
            bucket = properties.setdefault(key, {})
            seen = bucket.setdefault(str(value), {
                "value": value, "mentions": [], "n_confirmed": 0})
            seen["mentions"].append(link["instance_id"])
            if link["status"] in ("confirmed", "amended"):
                seen["n_confirmed"] += 1

    # Names seen for this entity, which is how a page shows its own aliases.
    aliases = sorted({m["properties"].get("name") for m in mentions
                      if m["properties"].get("name")}
                     - {entity["canonical_name"]})

    # Conflicts somebody verified, and which mention is a side of which. Read
    # here rather than left to the caller because the page is where the damage
    # happens: two confirmed mentions that contradict each other, rendered in
    # the same voice one under the other, read as though they agree. A property
    # carrying a standing tension must not be renderable without it.
    standing = tensions_mod.tensions_for_entity(store, entity["entity_id"],
                                                standing_only=True)
    by_instance = tensions_mod.tensions_for_instances(
        store, [m["instance_id"] for m in mentions])
    for record in mentions:
        record["tensions"] = by_instance.get(record["instance_id"], [])
    contested = {t["property_id"] for t in standing if t["property_id"]}

    # The mirror. Where the documents agree, and -- the part that matters --
    # whether the agreement is several sources or one sentence copied about.
    agreement = corroboration_mod.for_entity(store, entity["entity_id"])

    return {
        "entity": entity,
        # The one part of the page that is not from a document, kept separate
        # so a reader can see at a glance which is which.
        "description": entity["description"],
        "aliases": aliases,
        "properties": {
            key: sorted(values.values(),
                        key=lambda v: (-v["n_confirmed"], -len(v["mentions"])))
            for key, values in sorted(properties.items())
        },
        # Which properties a person has said are genuinely in conflict. The
        # page shows every value it has seen either way; this is the difference
        # between "we found two" and "we found two and they really do disagree".
        "contested_properties": sorted(contested),
        # Only genuinely independent agreement is marked. A value held up by
        # six copies of one sentence is listed separately, because presenting
        # it as six agreeing sources is the error corroboration exists to prevent.
        "corroborated_properties": agreement["corroborated_properties"],
        "copied_properties": agreement["copied_properties"],
        "corroboration": agreement,
        "tensions": standing,
        "mentions": mentions,
        "documents": sorted(documents.values(), key=lambda d: d["date_added"] or ""),
        "counts": {
            "mentions": len(mentions),
            "documents": len(documents),
            "confirmed_links": sum(1 for m in mentions
                                   if m["link"]["status"] in ("confirmed", "amended")),
            "unconfirmed_links": sum(1 for m in mentions
                                     if m["link"]["status"] == "unconfirmed"),
        },
        # A link is settled when a person made it or a person checked it.
        # Basing this on `basis` alone would mean confirming a proposal changed
        # nothing, which makes confirmation meaningless here -- and the page
        # would carry a caveat about naive matching after every link on it had
        # been verified by hand.
        "resolution_quality": ("resolved" if _settled(entity, mentions)
                               else "naive_unresolved"),
        "n_tensions": len(standing),
        "caveat": None if _settled(entity, mentions) else NAIVE_CAVEAT,
    }


def _settled(entity: dict, mentions: list[dict]) -> bool:
    """Has a person actually vouched for this page, link by link?"""
    if entity["status"] not in ("confirmed", "amended"):
        return False
    if not mentions:
        return False
    return all(m["link"]["basis"] == "human"
               or m["link"]["status"] in ("confirmed", "amended")
               for m in mentions)


def list_entities(store: Store, type_id: str | None = None,
                  status: str | None = None, query: str | None = None,
                  limit: int = 100) -> list[dict]:
    """The wiki's index, with how much sits behind each page."""
    clauses = ["e.merged_into IS NULL"]
    params: list[Any] = []
    if type_id:
        clauses.append("e.type_id = ?")
        params.append(type_id)
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if query:
        clauses.append("(e.canonical_name LIKE ? OR e.naive_key LIKE ?)")
        params += [f"%{query}%", f"%{_key(store, type_id, query)}%"]

    return [dict(r) for r in store.query(
        "SELECT e.entity_id, e.type_id, e.canonical_name, e.status, e.description, "
        "       COUNT(m.instance_id) AS n_mentions, "
        "       COUNT(DISTINCT m.document_id) AS n_documents, "
        "       SUM(CASE WHEN m.status IN ('confirmed','amended') THEN 1 ELSE 0 END) "
        "         AS n_confirmed, "
        # Carried on the index so a contested page is visible before it is
        # opened. A conflict findable only by clicking through is a conflict
        # most people will not find.
        "       (SELECT COUNT(*) FROM tensions t WHERE t.scope = 'entity' "
        "        AND t.subject_id = e.entity_id "
        "        AND t.status IN ('open','accepted')) AS n_tensions "
        "FROM entities e "
        "LEFT JOIN entity_mentions m ON m.entity_id = e.entity_id "
        "  AND m.unlinked_at IS NULL "
        f"WHERE {' AND '.join(clauses)} "
        "GROUP BY e.entity_id ORDER BY n_documents DESC, e.canonical_name "
        f"LIMIT {int(limit)}", tuple(params))]


def entities_in_document(store: Store, document_id: str) -> list[dict]:
    """Which entities this document is evidence about — the reverse view."""
    return [dict(r) for r in store.query(
        "SELECT DISTINCT e.entity_id, e.type_id, e.canonical_name, e.status, "
        "       m.basis, m.status AS link_status, m.instance_id "
        "FROM entity_mentions m JOIN entities e USING (entity_id) "
        "WHERE m.document_id = ? AND m.unlinked_at IS NULL "
        "  AND e.merged_into IS NULL "
        "ORDER BY e.canonical_name", (document_id,))]
