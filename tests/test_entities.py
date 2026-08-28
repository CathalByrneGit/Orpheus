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
from orpheus.entities import (BASES, candidates_for_mention, confirm_entity,
                              confirm_link, could_be_one_thing, create_entity,
                              describe_entity, distinguishing_tokens,
                              same_but_for_an_initial,
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


# ---------------------------------------------------------------------------
# Which names could be one thing
# ---------------------------------------------------------------------------
#
# Spelling distance alone says yes far too often, because every name in one
# corpus shares its boilerplate. All the pairs below came out of the
# 40-document run, with the score `token_sort_ratio` gave them.

def test_two_names_each_with_a_word_the_other_lacks_are_two_things():
    # 87.8 and 80.0 respectively, and neither pair is one company.
    assert not could_be_one_thing("EFTC OPERATING CORP.", "K*TEC OPERATING CORP.")
    assert not could_be_one_thing("SUNTRON CORPORATION", "UTEK Corporation")


def test_a_name_that_extends_another_stays_a_candidate():
    # 89.3. The whole of one name is inside the other, so only one side carries
    # the difference -- a brand, a parent, or the same thing written fuller.
    assert could_be_one_thing("HealthPlan Services, Inc.",
                              "Sykes HealthPlan Services, Inc.")
    assert distinguishing_tokens("HealthPlan Services, Inc.",
                                 "Sykes HealthPlan Services, Inc.") == (set(), {"sykes"})


def test_the_documented_false_split_is_still_offered():
    # `naive_key`'s known failure. If this rule withheld it, the one case the
    # fuzzy matcher exists to catch would never reach a person.
    assert could_be_one_thing("Ernst & Young", "Ernst and Young")


def test_a_word_and_its_near_spelling_are_one_word():
    # A plural or a typo is exactly what fuzzy matching is for, and filtering
    # it out on the way to a person would defeat the point.
    assert could_be_one_thing("Halloran Instruments, Inc.", "Halloran Instrument Inc.")
    assert could_be_one_thing("Ardmore Digital Ltd", "Ardmoor Digital Limited")
    # But two words that merely look alike are still two words.
    assert not could_be_one_thing("Acme Operating Ltd", "Acme Operations Ltd")


def test_a_legal_form_or_a_title_is_never_the_distinguishing_word():
    assert could_be_one_thing("Ardmore Digital Limited", "Ardmore Digital Ltd")
    assert could_be_one_thing("Dr. Mitchell Felder", "Mitchell Felder")
    # A middle initial is one character, so it distinguishes nothing on its own.
    assert could_be_one_thing("Mitchell Felder", "Mitchell S. Felder")


def test_group_and_holdings_still_distinguish():
    # They are absent from the generic list for the same reason they are absent
    # from the suffix list: a holding company is not its subsidiary. Both are
    # still *offered*, because offering is not merging -- what must not happen
    # is them sharing a key, which is tested in test_store.
    assert distinguishing_tokens("Kestrel Medical Group",
                                 "Kestrel Medical Ltd") == ({"group"}, set())


def test_the_rule_only_ever_withholds_a_candidate(corpus):
    # It never merges and never links. Two pages it declines to offer can still
    # be merged by a person who knows better.
    pytest.importorskip("rapidfuzz")
    a = create_entity(corpus, "Company", "EFTC OPERATING CORP.", actor_id="act_a")
    b = create_entity(corpus, "Company", "K*TEC OPERATING CORP.", actor_id="act_a")
    assert not [p for p in duplicate_pages(corpus, type_id="Company")
                if {p["keep"]["entity_id"], p["merge"]["entity_id"]} == {a, b}]
    assert merge_entities(corpus, a, b, actor_id="act_a")["kept"] == a


def test_a_middle_initial_is_offered_as_its_own_reason():
    # 90.9% similar says nothing a reviewer can check. "Same first and last
    # name, differing by an initial" says what to look at.
    assert same_but_for_an_initial("Mitchell Felder", "Mitchell S. Felder")
    assert same_but_for_an_initial("Mary Jane Watson", "Mary J. Watson")
    # An initial that expands to the name it stands for.
    assert same_but_for_an_initial("John A. Smith", "John Alan Smith")


def test_two_people_who_differ_by_their_initial_are_two_people():
    # The failure this basis would cause if it merged rather than offered.
    assert not same_but_for_an_initial("John A. Smith", "John B. Smith")
    assert not same_but_for_an_initial("John Paul Smith", "John Peter Smith")
    # Identical names are an exact key match, which is a stronger basis.
    assert not same_but_for_an_initial("John A. Smith", "John A. Smith")
    # And a surname alone has no first and last to agree on.
    assert not same_but_for_an_initial("Felder", "Felder")


def test_a_middle_initial_candidate_outranks_a_spelling_score(corpus):
    corpus.execute(
        "INSERT INTO instances_Person (instance_id, document_id, name, naive_key,"
        " source, confidence, status, created_at)"
        " VALUES ('p_1','doc_1','Mitchell S. Felder','mitchell s felder',"
        "'ai_cloud',0.9,'unconfirmed',datetime('now'))")
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, document_id,"
        " created_at) VALUES ('p_1','Person','instances_Person','doc_1',datetime('now'))")
    corpus.conn.commit()
    create_entity(corpus, "Person", "Mitchell Felder", actor_id="act_a")

    candidates = candidates_for_mention(corpus, "p_1")
    assert candidates and candidates[0]["basis"] == "initials"
    assert "differing by an initial" in candidates[0]["evidence"]


def test_a_company_is_never_offered_a_middle_initial(corpus):
    # A middle initial is a personal-name idea. The bundle says which types
    # have personal names, and this basis only applies to those.
    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, naive_key,"
        " source, confidence, status, created_at)"
        " VALUES ('c_1','doc_1','Acme S. Trading','acme s trading',"
        "'ai_cloud',0.9,'unconfirmed',datetime('now'))")
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, document_id,"
        " created_at) VALUES ('c_1','Company','instances_Company','doc_1',datetime('now'))")
    corpus.conn.commit()
    create_entity(corpus, "Company", "Acme Trading", actor_id="act_a")

    assert "initials" not in {c["basis"] for c in candidates_for_mention(corpus, "c_1")}


def test_two_pages_under_one_key_are_the_strongest_kind_of_split(corpus):
    # The same test `propose_entities` groups on. Reporting it as a spelling
    # percentage describes it as something weaker than it is.
    a = create_entity(corpus, "Company", "Ardmore Digital Limited",
                      actor_id="act_a", source="ai_local")
    create_entity(corpus, "Company", "Ardmore Digital Ltd",
                  actor_id="act_a", source="ai_local")

    pairs = duplicate_pages(corpus, type_id="Company")
    assert pairs and pairs[0]["basis"] == "naive_key"
    assert "name key" in pairs[0]["evidence"]
    assert a in {pairs[0]["keep"]["entity_id"], pairs[0]["merge"]["entity_id"]}


def test_a_shared_key_is_found_even_when_the_spellings_are_far_apart(corpus):
    # The fuzzy pass cannot see this one: the strings are too different to
    # clear the threshold, and the key is identical.
    create_entity(corpus, "Company", "Foo Co Ltd", actor_id="act_a",
                  source="ai_local")
    create_entity(corpus, "Company", "Foo", actor_id="act_a", source="ai_local")

    bases = {p["basis"] for p in duplicate_pages(corpus, type_id="Company")}
    assert "naive_key" in bases


def test_the_key_pass_works_without_rapidfuzz(corpus, monkeypatch):
    # Exact matching is what the rest of the system rests on, so a missing
    # optional install must not take the strongest check with it.
    import builtins
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "rapidfuzz":
            raise ImportError("not installed")
        return real(name, *args, **kwargs)

    create_entity(corpus, "Company", "Ardmore Digital Limited",
                  actor_id="act_a", source="ai_local")
    create_entity(corpus, "Company", "Ardmore Digital Ltd",
                  actor_id="act_a", source="ai_local")
    monkeypatch.setattr(builtins, "__import__", refuse)

    pairs = duplicate_pages(corpus, type_id="Company")
    assert [p["basis"] for p in pairs] == ["naive_key"]


def test_the_strongest_reason_is_offered_first(corpus):
    # A 90% spelling match above a shared key would put the weaker reason at
    # the top of a reviewer's list.
    pytest.importorskip("rapidfuzz")
    for name in ("Ardmore Digital Limited", "Ardmore Digital Ltd",
                 "Ardmoor Digital Holdings"):
        create_entity(corpus, "Company", name, actor_id="act_a", source="ai_local")

    bases = [p["basis"] for p in duplicate_pages(corpus, type_id="Company")]
    assert bases[0] == "naive_key"
    assert bases == sorted(bases, key=lambda b: BASES.index(b))


# ---------------------------------------------------------------------------
# The evidence for merging two pages
# ---------------------------------------------------------------------------
#
# Assembled, never judged. The point is that a reviewer looking at two pages
# should not have to run six queries, and a model asked to help should not have
# to invent the answer.

@pytest.fixture
def two_people(corpus):
    """Two Person pages, one acting for a company both are tied to."""
    for instance_id, name, acting_for, job in (
            ("p_1", "Mitchell Felder", "Marv Enterprises, LLC", None),
            ("p_2", "Mitchell S. Felder", "Marv Enterprises, LLC", "sole member"),
            ("p_3", "Someone Else", "Marv Enterprises, LLC", None),
            ("p_4", "Ada Nolan", "Ardmore Digital Ltd", None)):
        corpus.execute(
            "INSERT INTO instances_Person (instance_id, document_id, name,"
            " naive_key, acting_for, job_title, source, confidence, status,"
            " created_at) VALUES (?,?,?,?,?,?,'ai_cloud',0.9,'unconfirmed',"
            " datetime('now'))",
            (instance_id, "doc_1", name,
             bundle_mod.key_for(bundle_mod.load(), "Person", name),
             acting_for, job))
        corpus.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Person','instances_Person',"
            " 'doc_1', datetime('now'))", (instance_id,))
    corpus.conn.commit()
    pages = {}
    for instance_id, name in (("p_1", "Mitchell Felder"),
                              ("p_2", "Mitchell S. Felder"),
                              ("p_3", "Someone Else"), ("p_4", "Ada Nolan")):
        pages[name] = create_entity(corpus, "Person", name, actor_id="act_a",
                                    source="ai_local")
        link_mention(corpus, pages[name], instance_id, actor_id="act_a")
    return corpus, pages


def test_a_shared_value_comes_back_with_how_rare_it_is(two_people):
    from orpheus.entities import shared_attributes

    store, pages = two_people
    shared = shared_attributes(store, pages["Mitchell Felder"],
                               pages["Mitchell S. Felder"])
    acting = next(r for r in shared if r["property"] == "acting_for")
    assert acting["value"] == "Marv Enterprises, LLC"
    # Three of the four Person pages carry it, and the number says so rather
    # than the function deciding what it is worth.
    assert acting["n_pages_sharing"] == 3
    assert acting["n_pages_of_type"] == 4


def test_a_value_almost_every_page_carries_says_so(two_people):
    # "EFTC OPERATING CORP." and "K*TEC OPERATING CORP." are different
    # companies that both carry entity_kind = private_company, which 64 of 74
    # pages carry. The note is what stops that reading as a match.
    from orpheus.entities import shared_attributes

    store, pages = two_people
    shared = shared_attributes(store, pages["Mitchell Felder"],
                               pages["Someone Else"])
    acting = next(r for r in shared if r["property"] == "acting_for")
    assert "says little" in acting["note"]


def test_rarest_evidence_is_offered_first(two_people):
    from orpheus.entities import shared_attributes

    store, pages = two_people
    store.execute("UPDATE instances_Person SET job_title = 'sole member' "
                  "WHERE instance_id = 'p_1'")
    store.conn.commit()
    shared = shared_attributes(store, pages["Mitchell Felder"],
                               pages["Mitchell S. Felder"])
    counts = [r["n_pages_sharing"] for r in shared]
    assert counts == sorted(counts)
    assert shared[0]["property"] == "job_title"   # 2 pages, against 3


def test_bookkeeping_columns_are_never_evidence(two_people):
    # Two rows sharing a status, a source or a document id says nothing about
    # whether they are the same thing.
    from orpheus.entities import shared_attributes

    store, pages = two_people
    props = {r["property"] for r in shared_attributes(
        store, pages["Mitchell Felder"], pages["Mitchell S. Felder"])}
    assert not props & {"status", "source", "confidence", "document_id",
                        "naive_key", "created_at", "name"}


def test_two_pages_of_different_types_share_nothing(two_people):
    from orpheus.entities import shared_attributes

    store, pages = two_people
    company = create_entity(store, "Company", "Marv Enterprises, LLC",
                            actor_id="act_a", source="ai_local")
    assert shared_attributes(store, pages["Mitchell Felder"], company) == []


def test_the_dossier_carries_the_name_analysis_and_the_passages(two_people):
    from orpheus.entities import resolution_evidence

    store, pages = two_people
    ev = resolution_evidence(store, pages["Mitchell Felder"],
                             pages["Mitchell S. Felder"])
    assert ev["names"]["differ_by_an_initial"] is True
    assert ev["names"]["could_be_one_thing"] is True
    assert ev["same_type"] is True
    assert set(ev["passages"]) == {pages["Mitchell Felder"],
                                   pages["Mitchell S. Felder"]}


def test_appearing_in_one_document_is_reported_and_labelled(two_people):
    # A reviewer will ask, and the obvious reading of it is wrong -- so it is
    # shown with the measurement that says why.
    from orpheus.entities import resolution_evidence

    store, pages = two_people
    ev = resolution_evidence(store, pages["Mitchell Felder"],
                             pages["Someone Else"])
    assert ev["weak_signals"]["shared_documents"] == ["doc_1"]
    assert "different companies" in ev["weak_signals"]["note"]


def test_the_dossier_never_decides(two_people):
    # It has no verdict field, and gathering it changes nothing.
    from orpheus.entities import resolution_evidence

    store, pages = two_people
    before = store.scalar("SELECT COUNT(*) FROM entities WHERE merged_into IS NOT NULL")
    ev = resolution_evidence(store, pages["Mitchell Felder"],
                             pages["Mitchell S. Felder"])
    assert "verdict" not in ev and "merge" not in ev
    assert "not judged" in ev["caveat"]
    assert store.scalar(
        "SELECT COUNT(*) FROM entities WHERE merged_into IS NOT NULL") == before


# ---------------------------------------------------------------------------
# A pair somebody has settled
# ---------------------------------------------------------------------------

def test_a_pair_ruled_different_stops_being_offered(two_people):
    from orpheus.entities import review_resolution

    pytest.importorskip("rapidfuzz")
    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    assert any({p["keep"]["entity_id"], p["merge"]["entity_id"]} == {a, b}
               for p in duplicate_pages(store, type_id="Person"))

    review_resolution(store, a, b, "different",
                      rationale="Two brothers, both named in the 2019 filing.",
                      actor_id="act_a")
    assert not any({p["keep"]["entity_id"], p["merge"]["entity_id"]} == {a, b}
                   for p in duplicate_pages(store, type_id="Person"))


def test_which_way_round_somebody_looked_is_not_a_new_question(two_people):
    from orpheus.entities import resolution_verdict, review_resolution

    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    review_resolution(store, a, b, "different", rationale="Brothers.",
                      actor_id="act_a")
    assert resolution_verdict(store, b, a)["status"] == "different"


def test_a_judgement_does_not_outlive_the_evidence_it_rested_on(two_people):
    # The rule `question_reviews` already keeps. A new document carrying a
    # matching address makes this a different question.
    from orpheus.entities import resolution_verdict, review_resolution

    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    review_resolution(store, a, b, "different", rationale="Brothers.",
                      actor_id="act_a")
    assert resolution_verdict(store, a, b)["stale"] is False

    store.execute("UPDATE instances_Person SET job_title = 'sole member' "
                  "WHERE instance_id = 'p_1'")
    store.conn.commit()

    verdict = resolution_verdict(store, a, b)
    assert verdict["stale"] is True, "new shared evidence, so ask again"
    assert verdict["status"] == "different", "and what was decided stays on file"


def test_a_stale_judgement_puts_the_pair_back_in_front_of_somebody(two_people):
    from orpheus.entities import review_resolution

    pytest.importorskip("rapidfuzz")
    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    review_resolution(store, a, b, "different", rationale="Brothers.",
                      actor_id="act_a")
    store.execute("UPDATE instances_Person SET job_title = 'sole member' "
                  "WHERE instance_id = 'p_1'")
    store.conn.commit()

    assert any({p["keep"]["entity_id"], p["merge"]["entity_id"]} == {a, b}
               for p in duplicate_pages(store, type_id="Person"))


def test_a_previous_judgement_is_superseded_rather_than_overwritten(two_people):
    from orpheus.entities import resolution_verdict, review_resolution

    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    review_resolution(store, a, b, "unsure", rationale="Cannot tell from this.",
                      actor_id="act_a")
    review_resolution(store, a, b, "different", rationale="Found the filing.",
                      actor_id="act_a")

    assert resolution_verdict(store, a, b)["status"] == "different"
    assert store.scalar(
        "SELECT COUNT(*) FROM resolution_reviews WHERE superseded_at IS NOT NULL") == 1


def test_deciding_two_pages_are_one_thing_does_not_merge_them(two_people):
    # Recording a judgement is not acting on it. merge_entities is still the
    # only thing that merges, and a person still calls it.
    from orpheus.entities import review_resolution

    store, pages = two_people
    a, b = pages["Mitchell Felder"], pages["Mitchell S. Felder"]
    review_resolution(store, a, b, "same", rationale="Same man, same company.",
                      actor_id="act_a")
    assert get_entity(store, b, follow_merge=False)["merged_into"] is None


def test_a_reason_is_required_even_for_unsure(two_people):
    from orpheus.entities import review_resolution

    store, pages = two_people
    with pytest.raises(OrpheusError):
        review_resolution(store, pages["Mitchell Felder"],
                          pages["Mitchell S. Felder"], "unsure", rationale="",
                          actor_id="act_a")


def test_a_page_is_not_a_pair_with_itself(two_people):
    from orpheus.entities import review_resolution

    store, pages = two_people
    with pytest.raises(OrpheusError):
        review_resolution(store, pages["Ada Nolan"], pages["Ada Nolan"],
                          "same", rationale="obviously", actor_id="act_a")
