"""Entities: the thing itself, as against a mention of it in one document.

The property every test here defends is that an entity page is a *projection*
of evidence rather than a place to write claims. A line on a page comes from a
mention, a mention comes from a document, and the page says which. Anything
that lets an assertion onto a page without a source behind it has broken the
one thing that makes this reusable.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.entities import (candidates_for_mention, confirm_entity,
                              confirm_link, create_entity, describe_entity,
                              entities_in_document, entity_page, get_entity,
                              link_mention, list_entities, merge_entities,
                              propose_entities, reject_entity, rename_entity,
                              unlink_mention, unlinked_mentions)
from orpheus.utils import NotFound, OrpheusError, naive_key

# One company written two ways with a shared registration number; a genuinely
# different company; and one with no identifier at all.
MENTIONS = [
    ("i1", "doc_1", "Halloran Instruments, Inc.", "482991", "supplier"),
    ("i2", "doc_2", "Halloran Instruments Inc", "482991", "supplier"),
    ("i3", "doc_3", "Halloran Group", "771020", "buyer"),
    ("i4", "doc_1", "Kestrel Medical Group PLC", None, "buyer"),
]


@pytest.fixture
def corpus(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_a")
    bundle_mod.apply_schema(store, bundle_mod.load())
    for document_id in ("doc_1", "doc_2", "doc_3"):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
    for instance_id, document_id, name, registration, role in MENTIONS:
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, registration_number, role, source, confidence, status,"
            " created_at) VALUES (?,?,?,?,?,?,'ai_cloud',0.9,'unconfirmed',"
            " datetime('now'))",
            (instance_id, document_id, name, naive_key(name), registration, role))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id,"
            " source_label, page_no, excerpt, confidence, created_at, source,"
            " alignment, char_start, char_end)"
            " VALUES (?,?,?,?,1,?,0.9,datetime('now'),'ai_cloud','match_exact',10,40)",
            (f"p_{instance_id}", instance_id, document_id,
             f"{document_id}.pdf", name))
    store.conn.commit()
    return store


# -- the work queue ----------------------------------------------------------

def test_every_mention_starts_unlinked(corpus):
    assert {m["instance_id"] for m in unlinked_mentions(corpus)} == \
        {"i1", "i2", "i3", "i4"}


def test_linking_takes_a_mention_out_of_the_queue(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a")
    assert "i1" not in {m["instance_id"] for m in unlinked_mentions(corpus)}


# -- proposing ---------------------------------------------------------------

def test_a_shared_identifier_groups_two_spellings(corpus):
    """An exact stated registration number is better evidence than a spelling."""
    result = propose_entities(corpus, actor_id="act_a")
    by_name = {e["canonical_name"]: e for e in result["entities"]}
    halloran = by_name["Halloran Instruments, Inc."]
    assert halloran["n_mentions"] == 2
    assert halloran["basis"] == "identifier"


def test_a_different_company_is_not_swept_in(corpus):
    # Different registration number, similar name. Merging these is the failure
    # that matters, because a holding company is not its subsidiary.
    propose_entities(corpus, actor_id="act_a")
    names = {e["canonical_name"] for e in list_entities(corpus)}
    assert "Halloran Group" in names
    assert len(names) == 3


def test_the_page_title_is_the_least_abbreviated_spelling(corpus):
    propose_entities(corpus, actor_id="act_a")
    names = {e["canonical_name"] for e in list_entities(corpus)}
    assert "Halloran Instruments, Inc." in names
    assert "Halloran Instruments Inc" not in names


def test_everything_proposed_is_unconfirmed_and_says_why(corpus):
    """The machine proposes; a person decides. This exists to make a reviewable
    queue, not to resolve anything."""
    result = propose_entities(corpus, actor_id="act_a")
    assert all(e["status"] == "unconfirmed" for e in list_entities(corpus))
    assert "candidates, not resolution" in result["caveat"]


def test_proposing_twice_does_not_duplicate(corpus):
    propose_entities(corpus, actor_id="act_a")
    again = propose_entities(corpus, actor_id="act_a")
    assert again["proposed"] == 0
    assert len(list_entities(corpus)) == 3


# -- one mention, one home ---------------------------------------------------

def test_a_mention_cannot_sit_on_two_pages(corpus):
    """Two pages citing the same excerpt is two pages claiming the same
    evidence, and nothing would notice."""
    first = create_entity(corpus, "Company", "First", actor_id="act_a")
    second = create_entity(corpus, "Company", "Second", actor_id="act_a")
    link_mention(corpus, first, "i1", actor_id="act_a")
    with pytest.raises(OrpheusError, match="already linked"):
        link_mention(corpus, second, "i1", actor_id="act_a")


def test_the_database_enforces_it_not_just_the_code(corpus):
    first = create_entity(corpus, "Company", "First", actor_id="act_a")
    second = create_entity(corpus, "Company", "Second", actor_id="act_a")
    link_mention(corpus, first, "i1", actor_id="act_a")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        corpus.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, basis,"
            " confidence, status, linked_at) VALUES (?,?,'human',1.0,"
            "'confirmed',datetime('now'))", (second, "i1"))


def test_relinking_after_an_unlink_is_allowed(corpus):
    first = create_entity(corpus, "Company", "First", actor_id="act_a")
    second = create_entity(corpus, "Company", "Second", actor_id="act_a")
    link_mention(corpus, first, "i1", actor_id="act_a")
    unlink_mention(corpus, first, "i1", actor_id="act_a")
    link_mention(corpus, second, "i1", actor_id="act_a")
    assert entity_page(corpus, second)["counts"]["mentions"] == 1


def test_a_mention_of_another_type_is_refused(corpus):
    # A name that is a company here and a person elsewhere is a finding to look
    # at, not a link to make.
    person = create_entity(corpus, "Person", "Halloran", actor_id="act_a")
    with pytest.raises(OrpheusError, match="is a Company and"):
        link_mention(corpus, person, "i1", actor_id="act_a")


# -- nothing destructive -----------------------------------------------------

def test_unlinking_keeps_the_row(corpus):
    """A link a person removed is evidence about how well matching works."""
    entity_id = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a")
    unlink_mention(corpus, entity_id, "i1", actor_id="act_a", note="Wrong company.")

    row = corpus.one("SELECT unlinked_at, unlinked_by, note FROM entity_mentions "
                     "WHERE entity_id = ? AND instance_id = ?", (entity_id, "i1"))
    assert row["unlinked_at"] and row["unlinked_by"] == "act_a"
    assert row["note"] == "Wrong company."
    # And it returns to the queue rather than vanishing.
    assert "i1" in {m["instance_id"] for m in unlinked_mentions(corpus)}


def test_rejecting_an_entity_releases_its_mentions(corpus):
    propose_entities(corpus, actor_id="act_a")
    entity_id = list_entities(corpus)[0]["entity_id"]
    freed = entity_page(corpus, entity_id)["counts"]["mentions"]
    reject_entity(corpus, entity_id, "act_a", note="Two things confused.")

    assert get_entity(corpus, entity_id)["status"] == "rejected"
    assert len(unlinked_mentions(corpus)) >= freed


# -- merging -----------------------------------------------------------------

def test_merging_moves_the_mentions_and_keeps_the_old_row(corpus):
    propose_entities(corpus, actor_id="act_a")
    by_name = {e["canonical_name"]: e["entity_id"] for e in list_entities(corpus)}
    keep, gone = by_name["Halloran Instruments, Inc."], by_name["Halloran Group"]

    result = merge_entities(corpus, keep, gone, "act_a", note="Same group.")
    assert result["mentions_moved"] == 1
    assert entity_page(corpus, keep)["counts"]["mentions"] == 3
    assert gone not in {e["entity_id"] for e in list_entities(corpus)}


def test_a_link_made_before_a_merge_still_resolves(corpus):
    """Which is why the merged row is kept rather than deleted."""
    propose_entities(corpus, actor_id="act_a")
    by_name = {e["canonical_name"]: e["entity_id"] for e in list_entities(corpus)}
    keep, gone = by_name["Halloran Instruments, Inc."], by_name["Halloran Group"]
    merge_entities(corpus, keep, gone, "act_a")

    assert get_entity(corpus, gone)["entity_id"] == keep
    assert get_entity(corpus, gone, follow_merge=False)["merged_into"] == keep


def test_merging_across_types_is_refused(corpus):
    company = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    person = create_entity(corpus, "Person", "Halloran", actor_id="act_a")
    with pytest.raises(OrpheusError, match="Merging across types"):
        merge_entities(corpus, company, person, "act_a")


def test_an_entity_cannot_be_merged_into_itself(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    with pytest.raises(OrpheusError, match="into itself"):
        merge_entities(corpus, entity_id, entity_id, "act_a")


# -- the page ----------------------------------------------------------------

def test_every_line_on_a_page_carries_its_source(corpus):
    """The property the whole thing exists for. A claim with no mention behind
    it cannot be written, because the page is a projection of mentions."""
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    page = entity_page(corpus, entity_id)

    assert page["mentions"]
    for record in page["mentions"]:
        assert record["document"]["filename"]
        assert record["evidence"]["excerpt"]
        assert record["evidence"]["page_no"]
        assert record["evidence"]["alignment"]


def test_the_page_never_picks_a_winner_between_disagreeing_documents(corpus):
    # Both may be true of the moment each document was written, and the
    # disagreement is usually the interesting part.
    corpus.execute("UPDATE instances_Company SET role = 'subcontractor' "
                   "WHERE instance_id = 'i2'")
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]

    roles = entity_page(corpus, entity_id)["properties"]["role"]
    assert {v["value"] for v in roles} == {"supplier", "subcontractor"}
    assert all(v["mentions"] for v in roles)


def test_other_spellings_become_aliases(corpus):
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    assert entity_page(corpus, entity_id)["aliases"] == ["Halloran Instruments Inc"]


def test_the_confirmed_only_view_hides_proposals(corpus):
    """What the wiki asserts, as against what it is offering."""
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    confirm_link(corpus, entity_id, "i1", "act_a")

    everything = entity_page(corpus, entity_id)
    asserted = entity_page(corpus, entity_id, include_unconfirmed=False)
    assert everything["counts"]["mentions"] == 2
    assert asserted["counts"]["mentions"] == 1


def test_prose_is_kept_apart_from_what_documents_say(corpus):
    # So a reader can see at a glance which half of a page is sourced.
    entity_id = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    describe_entity(corpus, entity_id, "Delaware corporation.", "act_a")
    page = entity_page(corpus, entity_id)
    assert page["description"] == "Delaware corporation."
    assert page["mentions"] == []


def test_a_page_built_only_on_names_says_it_is_unresolved(corpus):
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Kestrel")][0]
    page = entity_page(corpus, entity_id)
    assert page["resolution_quality"] == "naive_unresolved"
    assert "candidates, not resolution" in page["caveat"]


def test_a_page_a_person_built_by_hand_is_resolved(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a", basis="human")
    confirm_entity(corpus, entity_id, "act_a")
    assert entity_page(corpus, entity_id)["resolution_quality"] == "resolved"


def test_confirming_every_proposed_link_settles_the_page(corpus):
    """Otherwise confirmation means nothing here, and a page would carry a
    caveat about naive matching after every link on it was checked by hand."""
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    assert entity_page(corpus, entity_id)["resolution_quality"] == "naive_unresolved"

    for record in entity_page(corpus, entity_id)["mentions"]:
        confirm_link(corpus, entity_id, record["instance_id"], "act_a")
    confirm_entity(corpus, entity_id, "act_a")

    page = entity_page(corpus, entity_id)
    assert page["resolution_quality"] == "resolved"
    assert page["caveat"] is None


def test_one_unconfirmed_link_is_enough_to_keep_the_caveat(corpus):
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    confirm_entity(corpus, entity_id, "act_a")
    confirm_link(corpus, entity_id, "i1", "act_a")      # but not i2
    assert entity_page(corpus, entity_id)["caveat"] is not None


def test_confirming_the_links_but_not_the_page_is_not_enough(corpus):
    # The page itself is a claim that this is one distinct thing.
    propose_entities(corpus, actor_id="act_a")
    entity_id = [e["entity_id"] for e in list_entities(corpus)
                 if e["canonical_name"].startswith("Halloran Instruments")][0]
    for record in entity_page(corpus, entity_id)["mentions"]:
        confirm_link(corpus, entity_id, record["instance_id"], "act_a")
    assert entity_page(corpus, entity_id)["resolution_quality"] == "naive_unresolved"


# -- candidates and the reverse view -----------------------------------------

def test_an_identifier_candidate_outranks_a_name_candidate(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a")
    candidates = candidates_for_mention(corpus, "i2")
    assert candidates[0]["entity_id"] == entity_id
    assert candidates[0]["basis"] == "identifier"


def test_a_document_says_which_entities_it_is_evidence_about(corpus):
    propose_entities(corpus, actor_id="act_a")
    names = {e["canonical_name"] for e in entities_in_document(corpus, "doc_1")}
    assert names == {"Halloran Instruments, Inc.", "Kestrel Medical Group PLC"}


# -- review ------------------------------------------------------------------

def test_renaming_keeps_the_old_name_in_history(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran Instrments",
                              actor_id="act_a")
    rename_entity(corpus, entity_id, "Halloran Instruments, Inc.", "act_a",
                  note="typo")
    assert get_entity(corpus, entity_id)["canonical_name"] == \
        "Halloran Instruments, Inc."
    row = corpus.one("SELECT previous_value FROM edit_history "
                     "WHERE table_name = 'entities' AND action = 'amend' "
                     "ORDER BY seq DESC LIMIT 1")
    assert "Instrments" in row["previous_value"]


def test_renaming_to_the_same_name_is_refused(corpus):
    entity_id = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    with pytest.raises(OrpheusError, match="already the name"):
        rename_entity(corpus, entity_id, "Halloran", "act_a")


def test_an_unknown_entity_or_mention_is_a_not_found(corpus):
    with pytest.raises(NotFound):
        get_entity(corpus, "ent_nope")
    entity_id = create_entity(corpus, "Company", "Halloran", actor_id="act_a")
    with pytest.raises(NotFound):
        link_mention(corpus, entity_id, "inst_nope", actor_id="act_a")


def test_an_entity_type_the_bundle_lacks_is_refused(corpus):
    with pytest.raises(OrpheusError, match="no object type"):
        create_entity(corpus, "Spaceship", "Enterprise", actor_id="act_a")
