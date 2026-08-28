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
                              duplicate_pages, entities_in_document,
                              entity_page, get_entity, link_mention,
                              list_entities, merge_entities, propose_entities,
                              reject_entity, rename_entity, unlink_mention,
                              unlinked_mentions)
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


# -- fuzzy candidates --------------------------------------------------------

pytest.importorskip("rapidfuzz")


def test_a_close_spelling_is_offered_that_an_exact_key_cannot_see(corpus):
    """`naive_key` compares keys for equality, so a name that normalises
    differently is invisible to it however obviously it is the same thing.

    "Ernst & Young" against "Ernst and Young" is the documented case: the
    ampersand becomes a space and the word does not, and no suffix rule fixes
    it.
    """
    from orpheus.entities import similar_names

    create_entity(corpus, "Company", "Ernst & Young", actor_id="act_a")
    hits = similar_names(corpus, "Ernst and Young", "Company")
    assert [h["canonical_name"] for h in hits] == ["Ernst & Young"]
    assert hits[0]["basis"] == "similar"
    assert hits[0]["score"] >= 85


def test_a_holding_company_is_not_offered_as_its_subsidiary(corpus):
    # The false merge this whole area keeps trying to make. Jaro-Winkler scores
    # this pair *higher* than a true match, which is why token_sort_ratio is
    # the scorer.
    from orpheus.entities import similar_names

    create_entity(corpus, "Company", "Kestrel Medical Group", actor_id="act_a")
    assert similar_names(corpus, "Kestrel Medical Ltd", "Company") == []
    create_entity(corpus, "Company", "Ardmore Holdings plc", actor_id="act_a")
    assert similar_names(corpus, "Ardmore Ltd", "Company") == []


def test_an_exact_key_match_is_not_reported_twice(corpus):
    # It is already a `naive_key` candidate, which is the stronger basis.
    from orpheus.entities import similar_names

    create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                  actor_id="act_a")
    hits = similar_names(corpus, "Halloran Instruments Inc", "Company")
    assert hits == []


def test_candidates_rank_evidence_before_similarity(corpus):
    """An exact stated identifier beats a close spelling, always."""
    exact = create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                          actor_id="act_a")
    link_mention(corpus, exact, "i1", actor_id="act_a")
    create_entity(corpus, "Company", "Halloran Instrument Inc.", actor_id="act_a")

    candidates = candidates_for_mention(corpus, "i2")
    assert candidates[0]["entity_id"] == exact
    assert candidates[0]["basis"] == "identifier"
    assert "similar" in {c["basis"] for c in candidates}


def test_similarity_degrades_to_nothing_without_rapidfuzz(corpus, monkeypatch):
    # Exact matching still works, and it is what the rest of the system rests
    # on, so a missing optional install must not break candidate generation.
    import builtins
    from orpheus.entities import similar_names

    real_import = builtins.__import__

    def no_rapidfuzz(name, *args, **kwargs):
        if name.startswith("rapidfuzz"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    create_entity(corpus, "Company", "Ernst & Young", actor_id="act_a")
    monkeypatch.setattr(builtins, "__import__", no_rapidfuzz)
    assert similar_names(corpus, "Ernst and Young", "Company") == []
    assert candidates_for_mention(corpus, "i1") == []


def test_a_false_split_shows_up_as_two_pages_to_merge(corpus):
    """The gap the queue cannot show.

    `propose_entities()` groups on exact keys, so a name that normalises two
    ways becomes two pages -- and every mention then has a home, so the queue
    is empty and the split is invisible exactly when the machine has finished.
    """
    from orpheus.entities import duplicate_pages

    create_entity(corpus, "Company", "Ernst & Young", actor_id="act_a")
    create_entity(corpus, "Company", "Ernst and Young", actor_id="act_a")
    create_entity(corpus, "Company", "Kestrel Medical Group", actor_id="act_a")
    create_entity(corpus, "Company", "Kestrel Medical Ltd", actor_id="act_a")

    pairs = duplicate_pages(corpus, type_id="Company")
    names = {frozenset((p["keep"]["canonical_name"],
                        p["merge"]["canonical_name"])) for p in pairs}
    assert frozenset({"Ernst & Young", "Ernst and Young"}) in names
    # And the pair that must never be offered.
    assert frozenset({"Kestrel Medical Group", "Kestrel Medical Ltd"}) not in names


def test_the_page_with_more_evidence_is_offered_as_the_survivor(corpus):
    from orpheus.entities import duplicate_pages

    keep = create_entity(corpus, "Company", "Halloran Instruments, Inc.",
                         actor_id="act_a")
    thin = create_entity(corpus, "Company", "Halloran Instruments Inc.",
                         actor_id="act_a")
    link_mention(corpus, keep, "i1", actor_id="act_a")
    link_mention(corpus, keep, "i2", actor_id="act_a")

    pair = duplicate_pages(corpus, type_id="Company")[0]
    assert pair["keep"]["entity_id"] == keep
    assert pair["merge"]["entity_id"] == thin
    assert pair["keep"]["n_mentions"] == 2


def test_a_merged_page_stops_being_offered(corpus):
    from orpheus.entities import duplicate_pages

    keep = create_entity(corpus, "Company", "Ernst & Young", actor_id="act_a")
    gone = create_entity(corpus, "Company", "Ernst and Young", actor_id="act_a")
    assert duplicate_pages(corpus, type_id="Company")
    merge_entities(corpus, keep, gone, "act_a")
    assert duplicate_pages(corpus, type_id="Company") == []


# -- proposing again, on a corpus that grew ----------------------------------
#
# The normal thing to do after ingesting more documents. Before this, every
# round minted a fresh page beside the one already there -- two pages with an
# identical key, which is the strongest evidence of sameness the store has.
# Found by running a real corpus twice, not by reading the code.

def test_proposing_twice_does_not_split_a_page(corpus):
    first = propose_entities(corpus, actor_id="act_a")
    assert first["proposed"] == 3 and first["attached"] == 0

    # A second document mentioning a company that already has a page.
    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, source, confidence, status, created_at)"
        " VALUES ('i5','doc_2','Kestrel Medical Group PLC',?,'ai_local',0.9,"
        "'unconfirmed',datetime('now'))", (naive_key("Kestrel Medical Group PLC"),))
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i5','Company','instances_Company',"
        "'doc_2',datetime('now'))")
    corpus.conn.commit()

    again = propose_entities(corpus, actor_id="act_a")
    assert again["proposed"] == 0
    assert again["attached"] == 1
    assert again["entities"][0]["existing"] is True

    # One page, two mentions -- not two pages with the same key.
    keys = corpus.query(
        "SELECT naive_key, COUNT(*) n FROM entities WHERE merged_into IS NULL "
        "GROUP BY type_id, naive_key HAVING n > 1")
    assert keys == []


def test_attaching_does_not_rename_the_page_it_attaches_to(corpus):
    # The existing title was somebody's decision, or an earlier proposal's. A
    # later batch carrying a longer spelling is not grounds to overwrite it.
    propose_entities(corpus, actor_id="act_a")
    page = next(e for e in list_entities(corpus)
                if e["canonical_name"].startswith("Kestrel"))
    before = page["canonical_name"]

    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, source, confidence, status, created_at)"
        " VALUES ('i6','doc_2','Kestrel Medical Group Public Limited Company',?,"
        "'ai_local',0.9,'unconfirmed',datetime('now'))",
        (naive_key("Kestrel Medical Group PLC"),))
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i6','Company','instances_Company',"
        "'doc_2',datetime('now'))")
    corpus.conn.commit()

    propose_entities(corpus, actor_id="act_a")
    assert get_entity(corpus, page["entity_id"])["canonical_name"] == before


def test_a_stated_identifier_attaches_to_the_page_already_citing_it(corpus):
    # An exact registration number is better evidence than a spelling, so it is
    # tried first -- a company that renamed still lands on its own page.
    propose_entities(corpus, actor_id="act_a")
    halloran = next(e for e in list_entities(corpus)
                    if e["canonical_name"].startswith("Halloran Instruments"))

    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, registration_number, source, confidence, status, created_at)"
        " VALUES ('i7','doc_3','Halloran Scientific Ltd','halloran scientific',"
        "'482991','ai_local',0.9,'unconfirmed',datetime('now'))")
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i7','Company','instances_Company',"
        "'doc_3',datetime('now'))")
    corpus.conn.commit()

    again = propose_entities(corpus, actor_id="act_a")
    assert again["attached"] == 1
    assert again["entities"][0]["entity_id"] == halloran["entity_id"]


def test_a_merged_away_page_is_not_attached_to(corpus):
    # It points at its successor; hanging new mentions on it would resurrect a
    # page a person deliberately retired.
    propose_entities(corpus, actor_id="act_a")
    pages = [e for e in list_entities(corpus) if e["canonical_name"].startswith("Kestrel")]
    keep = create_entity(corpus, "Company", "Kestrel Holdings", actor_id="act_a")
    merge_entities(corpus, keep, pages[0]["entity_id"], actor_id="act_a")
    corpus.conn.commit()

    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, source, confidence, status, created_at)"
        " VALUES ('i8','doc_2','Kestrel Medical Group',?,'ai_local',0.9,"
        "'unconfirmed',datetime('now'))", (naive_key("Kestrel Medical Group"),))
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i8','Company','instances_Company',"
        "'doc_2',datetime('now'))")
    corpus.conn.commit()

    again = propose_entities(corpus, actor_id="act_a")
    assert again["entities"][0]["entity_id"] != pages[0]["entity_id"]


# ---------------------------------------------------------------------------
# Document-scoped pages
# ---------------------------------------------------------------------------
#
# A Company's name identifies it across documents; a Contract's does not. Its
# name is a title, and three pairs of unrelated agreements in the calibration
# corpus are each called "STRATEGIC ALLIANCE AGREEMENT". Grouping those the way
# companies are grouped merges two agreements into one page, and a false merge
# is strictly worse than a false split: a split leaves two rows a person can
# join, a merge leaves nothing to notice.

def _contract(store, instance_id, document_id, name):
    store.execute(
        "INSERT INTO instances_Contract (instance_id, document_id, name,"
        " naive_key, source, confidence, status, created_at)"
        " VALUES (?,?,?,?,'ai_cloud',1.0,'unconfirmed',datetime('now'))",
        (instance_id, document_id, name, naive_key(name)))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES (?,'Contract','instances_Contract',?,"
        " datetime('now'))", (instance_id, document_id))
    return instance_id


@pytest.fixture
def contracts(corpus):
    """The same title in two documents, and a third with its own."""
    _contract(corpus, "c_1", "doc_1", "STRATEGIC ALLIANCE AGREEMENT")
    _contract(corpus, "c_2", "doc_2", "STRATEGIC ALLIANCE AGREEMENT")
    _contract(corpus, "c_3", "doc_3", "SPONSORSHIP AGREEMENT")
    corpus.conn.commit()
    return corpus


def test_the_same_title_in_two_documents_is_two_pages(contracts):
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    pages = contracts.query(
        "SELECT entity_id, canonical_name FROM entities WHERE type_id = 'Contract'"
        " AND merged_into IS NULL")
    assert len(pages) == 3, "one page per document, not one per title"
    titles = sorted(p["canonical_name"] for p in pages)
    assert titles == ["SPONSORSHIP AGREEMENT",
                      "STRATEGIC ALLIANCE AGREEMENT",
                      "STRATEGIC ALLIANCE AGREEMENT"]


def test_one_document_describing_two_agreements_gets_two_pages(contracts):
    # Found by running 40 real filings: doc_8d39 holds both "AMENDMENT NO. 1"
    # and "Wireless Content License Agreement Number 12965" -- an amendment and
    # the agreement it amends. Keying on the document alone merged them, which
    # is the same false merge from the other direction.
    _contract(contracts, "c_4", "doc_1", "AMENDMENT NO. 1")
    contracts.conn.commit()
    propose_entities(contracts, type_id="Contract", actor_id="act_a")

    titles = sorted(r["canonical_name"] for r in contracts.query(
        "SELECT e.canonical_name FROM entities e"
        " JOIN entity_mentions m ON m.entity_id = e.entity_id"
        " WHERE e.type_id = 'Contract' AND m.document_id = 'doc_1'"))
    assert titles == ["AMENDMENT NO. 1", "STRATEGIC ALLIANCE AGREEMENT"]


def test_the_same_agreement_read_twice_from_one_document_is_one_page(contracts):
    # The other side of the rule: two instances, one document, one title. Two
    # readings of the same thing, not two things.
    _contract(contracts, "c_1b", "doc_1", "Strategic Alliance Agreement")
    contracts.conn.commit()
    propose_entities(contracts, type_id="Contract", actor_id="act_a")

    assert contracts.scalar(
        "SELECT COUNT(DISTINCT e.entity_id) FROM entities e"
        " JOIN entity_mentions m ON m.entity_id = e.entity_id"
        " WHERE e.type_id = 'Contract' AND m.document_id = 'doc_1'") == 1


def test_a_contract_page_is_linked_on_the_document_not_the_name(contracts):
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    bases = {r["basis"] for r in contracts.query(
        "SELECT m.basis FROM entity_mentions m JOIN entities e"
        " ON e.entity_id = m.entity_id WHERE e.type_id = 'Contract'")}
    assert bases == {"document"}


def test_a_contract_page_says_which_document_it_came_from(contracts):
    # Two pages with the same title are honest -- both really are called that
    # -- but unreadable side by side without saying which is which. The
    # description carries the document rather than inventing a name nobody used.
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    notes = [r["description"] for r in contracts.query(
        "SELECT description FROM entities WHERE type_id = 'Contract'"
        " ORDER BY description")]
    assert notes == ["Read from doc_1.pdf.", "Read from doc_2.pdf.",
                     "Read from doc_3.pdf."]


def test_re_extracting_a_document_attaches_rather_than_minting_a_page(contracts):
    # Proposing again after ingesting more is the normal thing to do, and
    # without this the wiki fragments a little every round.
    _contract(contracts, "c_4", "doc_1", "AMENDMENT NO. 1")
    contracts.conn.commit()
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    before = contracts.scalar("SELECT COUNT(*) FROM entities"
                              " WHERE type_id = 'Contract' AND merged_into IS NULL")

    # doc_1 read again: both of its agreements come back as new instances.
    _contract(contracts, "c_1b", "doc_1", "STRATEGIC ALLIANCE AGREEMENT")
    _contract(contracts, "c_4b", "doc_1", "AMENDMENT NO. 1")
    contracts.conn.commit()
    result = propose_entities(contracts, type_id="Contract", actor_id="act_a")

    assert result["proposed"] == 0 and result["attached"] == 2
    assert contracts.scalar(
        "SELECT COUNT(*) FROM entities WHERE type_id = 'Contract'"
        " AND merged_into IS NULL") == before
    # And each went back to its own page rather than both to the first.
    assert contracts.scalar(
        "SELECT COUNT(*) FROM entity_mentions m JOIN entities e"
        " ON e.entity_id = m.entity_id WHERE e.canonical_name = 'AMENDMENT NO. 1'"
        " AND m.unlinked_at IS NULL") == 2


def test_a_second_document_never_attaches_to_the_first_ones_page(contracts):
    # The failure this exists to stop, from the other direction: propose one
    # document, then the other, and the second must not find the first's page
    # by its title.
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    pages = {r["entity_id"] for r in contracts.query(
        "SELECT DISTINCT e.entity_id FROM entities e"
        " JOIN entity_mentions m ON m.entity_id = e.entity_id"
        " WHERE e.type_id = 'Contract' AND m.document_id IN ('doc_1','doc_2')")}
    assert len(pages) == 2


def test_two_contracts_sharing_a_title_are_not_offered_as_a_merge(contracts):
    pytest.importorskip("rapidfuzz")
    propose_entities(contracts, type_id="Contract", actor_id="act_a")
    # An identical title scores 100%, which for a Company would be the
    # strongest candidate there is and for a Contract is near-worthless.
    assert duplicate_pages(contracts, type_id="Contract") == []
    assert not [p for p in duplicate_pages(contracts)
                if p["keep"]["type_id"] == "Contract"]


def test_a_company_is_still_grouped_across_documents_by_name(contracts):
    # The rule is per type, not a general retreat from name matching.
    result = propose_entities(contracts, type_id="Company", actor_id="act_a")
    assert {e["basis"] for e in result["entities"]} <= {"identifier", "naive_key"}
