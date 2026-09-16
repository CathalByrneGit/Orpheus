"""Citing the corpus from outside it.

Three rules, and every test here is one of them.

**A reference carries the review state.** This is the reason the module exists.
A drafting space that renders `unconfirmed` and `confirmed` in the same voice
launders a machine's guess into a fact one memo at a time.

**A reference is resolved against the reader.** Papers live where `auth.can()`
never runs, so the card is built per viewer, and "you may not see this" and
"this does not exist" answer identically — because telling them apart
enumerates the corpus.

**A reference follows the store.** Merges, amendments and redactions all move
under prose that was written once and never touched again.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus import api, auth, entities as entities_mod, ingest as ingest_mod
from orpheus import redact, refs, tensions as tensions_mod
from orpheus.utils import naive_key


@pytest.fixture
def corpus(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.owner = auth.create_actor(store, "Nuala Ryan", idp="d", external_id="n")
    store.stranger = auth.create_actor(store, "Sean Kelly", idp="d", external_id="s")
    store.admin = auth.create_actor(store, "Root", idp="d", external_id="r",
                                    is_admin=True)
    path = tmp_path / "a.txt"
    path.write_text("Agreement between Halloran Instruments, Inc. and "
                    "Kestrel Medical Group PLC.")
    store.document_id = ingest_mod.ingest(
        store, path, actor_id=store.owner, storage_root=tmp_path / "storage",
    )["document_id"]
    return store


def _mention(store, instance_id, name, status="unconfirmed"):
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, "
        "naive_key, source, confidence, status, created_at) "
        "VALUES (?,?,?,?,'ai_local',0.9,?,datetime('now'))",
        (instance_id, store.document_id, name, naive_key(name), status))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, "
        "document_id, created_at) VALUES (?,'Company','instances_Company',?,"
        "datetime('now'))", (instance_id, store.document_id))
    return instance_id


def owner(store):
    return {"actor_id": store.owner, "is_admin": False}


def stranger(store):
    return {"actor_id": store.stranger, "is_admin": False}


def admin(store):
    return {"actor_id": store.admin, "is_admin": True}


# -- what a ref is ----------------------------------------------------------

def test_a_ref_is_the_id_a_person_can_already_copy():
    # No second identifier scheme: the ref is the id on the page.
    assert refs.kind_of("ent_abc") == "entity"
    assert refs.kind_of("doc_abc") == "document"
    assert refs.kind_of("inst_abc") == "instance"
    assert refs.kind_of("tns_abc") == "tension"


def test_something_that_is_not_ours_costs_nothing_to_reject():
    for value in ("garbage", "ent", "", None, 17, "/-/other/thing"):
        assert refs.kind_of(value) is None


def test_an_unknown_ref_resolves_rather_than_raising(corpus):
    # A drafting space renders whatever somebody typed; an exception there
    # would take the paragraph around it down too.
    card = refs.resolve(corpus, "not-a-ref", owner(corpus))
    assert card["status"] == refs.NOT_FOUND
    assert "label" not in card


# -- the review state, which is the point -----------------------------------

def test_an_unchecked_fact_says_so_in_words_a_reader_can_use(corpus):
    instance_id = _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    card = refs.resolve(corpus, instance_id, owner(corpus))

    assert card["status"] == refs.OK
    assert card["label"] == "Halloran Instruments, Inc."
    assert card["review"]["checked"] is False
    assert "Nobody has checked this" in card["review"]["caveat"]


def test_a_checked_fact_is_marked_apart_from_an_unchecked_one(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.", status="confirmed")
    _mention(corpus, "inst_2", "Kestrel Medical Group PLC")

    checked = refs.resolve(corpus, "inst_1", owner(corpus))
    guessed = refs.resolve(corpus, "inst_2", owner(corpus))
    assert checked["review"]["checked"] is True
    assert guessed["review"]["checked"] is False
    # The whole failure this prevents: two cards that read the same.
    assert checked["review"] != guessed["review"]


def test_an_amended_value_counts_as_checked(corpus):
    # `amended` means a person looked and corrected it, which is a stronger
    # statement than `confirmed`, not a weaker one.
    _mention(corpus, "inst_1", "Halloran Instruments", status="amended")
    assert refs.resolve(corpus, "inst_1", owner(corpus))["review"]["checked"]


def test_a_contested_page_is_never_reported_as_checked(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    _mention(corpus, "inst_2", "Halloran Instruments Inc")
    proposed = entities_mod.propose_entities(corpus, actor_id=corpus.owner)
    entity_id = proposed["entities"][0]["entity_id"]
    entities_mod.confirm_entity(corpus, entity_id, corpus.owner)
    tensions_mod.raise_tension(
        corpus, scope="entity", subject_id=entity_id, kind="conflicting_value",
        summary="Two documents give different registered addresses.",
        sides=["inst_1", "inst_2"], actor_id=corpus.owner)

    card = refs.resolve(corpus, entity_id, owner(corpus))
    assert card["review"]["contested"] == 1
    # Confirmed *and* contested. The dispute wins: an idea built on a contested
    # reading has to show that it was contested.
    assert card["review"]["checked"] is False
    assert "both be right" in card["review"]["caveat"]


# -- resolved against the reader --------------------------------------------

def test_a_document_you_cannot_see_answers_with_no_label(corpus):
    card = refs.resolve(corpus, corpus.document_id, stranger(corpus))
    assert card["status"] == refs.DENIED
    assert "label" not in card and "detail" not in card and "href" not in card


def test_not_yours_and_not_here_answer_identically(corpus):
    """The house rule, kept here for the reason it exists everywhere else:
    distinguishing them enumerates the corpus for anybody trying ids."""
    withheld = refs.resolve(corpus, corpus.document_id, stranger(corpus))
    missing = refs.resolve(corpus, "doc_neverexisted", stranger(corpus))
    # Everything but the ref the caller asked about, which tells them nothing
    # they did not already have.
    assert {k: v for k, v in withheld.items() if k != "ref"} == \
        {k: v for k, v in missing.items() if k != "ref"}


def test_an_administrator_is_told_a_document_is_simply_absent(corpus):
    # Nothing is hidden from an administrator, so collapsing the two answers
    # would cost clarity and buy no secrecy.
    assert refs.resolve(corpus, "doc_neverexisted",
                        admin(corpus))["status"] == refs.NOT_FOUND


def test_a_fact_inherits_the_permission_of_the_document_it_was_read_from(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    assert refs.resolve(corpus, "inst_1", stranger(corpus))["status"] == refs.DENIED
    assert refs.resolve(corpus, "inst_1", owner(corpus))["status"] == refs.OK


def test_the_same_memo_resolves_differently_for_two_readers(corpus):
    """The property that makes this safe to paste into a shared document."""
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    cited = [corpus.document_id, "inst_1"]
    mine = refs.resolve_many(corpus, cited, owner(corpus))
    theirs = refs.resolve_many(corpus, cited, stranger(corpus))
    assert [c["status"] for c in mine] == [refs.OK, refs.OK]
    assert [c["status"] for c in theirs] == [refs.DENIED, refs.DENIED]


# -- following the store -----------------------------------------------------

def test_a_citation_of_a_merged_page_lands_on_the_survivor_and_says_so(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    _mention(corpus, "inst_2", "Kestrel Medical Group PLC")
    proposed = entities_mod.propose_entities(corpus, actor_id=corpus.owner)
    keep, merged = (e["entity_id"] for e in proposed["entities"][:2])
    entities_mod.merge_entities(corpus, keep, merged, corpus.owner)

    card = refs.resolve(corpus, merged, owner(corpus))
    assert card["status"] == refs.OK
    assert card["href"].endswith(keep)
    # Said out loud. A citation that quietly lands elsewhere is how two things
    # become one in a reader's head without anybody deciding they were.
    assert card["redirected_from"] == merged
    assert "merged into" in card["redirect_note"]


def test_a_redacted_document_resolves_to_a_tombstone_not_to_silence(corpus):
    redact.redact_document(corpus, corpus.document_id, actor_id=corpus.admin,
                           note="subject access request")
    card = refs.resolve(corpus, corpus.document_id, admin(corpus))

    assert card["status"] == refs.REDACTED
    assert card["redaction"]["note"] == "subject access request"
    # The one refusal that carries anything, because the row was kept to say
    # exactly this. It still carries no filename.
    assert "label" not in card


def test_a_fact_from_a_redacted_document_is_gone_rather_than_hidden(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    redact.redact_document(corpus, corpus.document_id, actor_id=corpus.admin,
                           note="subject access request")
    # Redaction deletes the rows, so `not_found` is the true answer here.
    assert refs.resolve(corpus, "inst_1", admin(corpus))["status"] == refs.NOT_FOUND


# -- many at once ------------------------------------------------------------

def test_a_page_of_prose_resolves_in_one_pass_with_duplicates_collapsed(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    cited = ["inst_1", corpus.document_id, "inst_1", "inst_1"]
    resolved = refs.resolve_many(corpus, cited, owner(corpus))
    assert [c["ref"] for c in resolved] == ["inst_1", corpus.document_id]


# -- the picker --------------------------------------------------------------

def test_the_picker_offers_only_what_the_asker_may_see(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    entities_mod.propose_entities(corpus, actor_id=corpus.owner)

    mine = refs.search(corpus, "a.txt", owner(corpus))
    theirs = refs.search(corpus, "a.txt", stranger(corpus))
    assert [hit["label"] for hit in mine] == ["a.txt"]
    # The autocomplete must not become the enumeration tool the resolver
    # refuses to be.
    assert theirs == []


def test_an_empty_query_offers_nothing_rather_than_everything(corpus):
    assert refs.search(corpus, "", owner(corpus)) == []
    assert refs.search(corpus, "   ", owner(corpus)) == []


def test_the_picker_never_offers_a_page_that_was_merged_away(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    _mention(corpus, "inst_2", "Kestrel Medical Group PLC")
    proposed = entities_mod.propose_entities(corpus, actor_id=corpus.owner)
    keep, merged = (e["entity_id"] for e in proposed["entities"][:2])
    entities_mod.merge_entities(corpus, keep, merged, corpus.owner)

    offered = {hit["ref"] for hit in refs.search(corpus, "Kestrel", owner(corpus))}
    assert merged not in offered


# -- over the API ------------------------------------------------------------

def test_a_dead_reference_is_a_200_with_a_status_not_an_error(corpus):
    """A page of prose resolves many of these at once; one dead citation must
    not read as a failed request, and `denied` and `not_found` have to come
    back through the same door or the difference is readable from outside."""
    status, payload = api.handle(corpus, "GET", f"/refs/{corpus.document_id}",
                                 actor=stranger(corpus))
    assert status == 200
    assert payload["status"] == refs.DENIED


def test_the_api_resolves_a_page_of_citations_at_once(corpus):
    _mention(corpus, "inst_1", "Halloran Instruments, Inc.")
    status, payload = api.handle(
        corpus, "POST", "/refs", {"refs": ["inst_1", corpus.document_id]},
        actor=owner(corpus))
    assert status == 200
    assert [card["status"] for card in payload["refs"]] == [refs.OK, refs.OK]


def test_a_request_may_not_walk_the_corpus_one_id_at_a_time(corpus):
    status, payload = api.handle(
        corpus, "POST", "/refs", {"refs": ["doc_x"] * (api.REF_BATCH + 1)},
        actor=owner(corpus))
    assert status == 400
    assert "At most" in payload["error"]["message"]


def test_the_refs_body_has_to_be_a_list(corpus):
    status, _ = api.handle(corpus, "POST", "/refs", {"refs": "inst_1"},
                           actor=owner(corpus))
    assert status == 400


def test_resolving_a_reference_needs_an_actor(corpus):
    status, _ = api.handle(corpus, "GET", f"/refs/{corpus.document_id}")
    assert status == 401
