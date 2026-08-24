"""When sources agree, and whether the agreement means anything.

One property is defended above all others: **copied text is not corroboration.**
A framework's boilerplate inherited by six call-offs is one source wearing six
hats, and a corpus assembled because somebody suspects something is exactly
where counting it as six would do damage — it manufactures certainty in the
direction the suspicion already leans.

The second property is that none of this touches `confidence`. The rubric's five
levels each mean something a reviewer can state out loud; a combined score is
none of them.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.corroboration import (COPY_THRESHOLD, corroborated_properties,
                                   corroborated_relations, for_entity, summary,
                                   wordings)
from orpheus.utils import naive_key

# Three documents agreeing on an address. doc_1 and doc_2 word it differently;
# doc_3 carries doc_1's sentence verbatim, which is the case that matters.
MENTIONS = [
    ("i1", "doc_1", "12 Ushers Quay, Dublin 8",
     "The Supplier's registered office is at 12 Ushers Quay, Dublin 8."),
    ("i2", "doc_2", "12 Ushers Quay, Dublin 8",
     "Ardmore Digital Ltd, having its registered address 12 Ushers Quay."),
    ("i3", "doc_3", "12 Ushers Quay, Dublin 8",
     "The Supplier's registered office is at 12 Ushers Quay, Dublin 8."),
]


@pytest.fixture
def corpus(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    for document_id in ("doc_1", "doc_2", "doc_3"):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
    store.execute(
        "INSERT INTO entities (entity_id, type_id, canonical_name, naive_key,"
        " source, confidence, status, created_at, created_by)"
        " VALUES ('ent_1','Company','Ardmore Digital Ltd',?,'human',1.0,"
        "'confirmed',datetime('now'),'act_a')", (naive_key("Ardmore Digital Ltd"),))
    for instance_id, document_id, address, excerpt in MENTIONS:
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, address, source, confidence, status, created_at)"
            " VALUES (?,?,'Ardmore Digital Ltd',?,?,'ai_local',0.7,'confirmed',"
            " datetime('now'))",
            (instance_id, document_id, naive_key("Ardmore Digital Ltd"), address))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id,"
            " source_label, page_no, excerpt, confidence, created_at, source,"
            " alignment, char_start, char_end)"
            " VALUES (?,?,?,?,1,?,0.7,datetime('now'),'ai_local','match_exact',1,9)",
            (f"p_{instance_id}", instance_id, document_id,
             f"{document_id}.pdf", excerpt))
        store.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
            " basis, confidence, status, linked_at, linked_by)"
            " VALUES ('ent_1',?,?,'human',1.0,'confirmed',datetime('now'),'act_a')",
            (instance_id, document_id))
    store.conn.commit()
    return store


def address_of(rows):
    return next(c for c in rows if c["property_id"] == "address")


# -- copied text is not corroboration ----------------------------------------

def test_a_sentence_appearing_twice_is_one_wording(corpus):
    found = wordings([
        {"document_id": "doc_1", "excerpt": "The fee is EUR 250,000 per annum."},
        {"document_id": "doc_2", "excerpt": "The fee is EUR 250,000 per annum."},
        {"document_id": "doc_3", "excerpt": "An annual charge of 250,000 euro applies."},
    ])
    assert len(found) == 2
    verbatim = next(w for w in found if w["n_documents"] == 2)
    assert verbatim["copied"] is True


def test_three_documents_in_two_wordings_counts_two(corpus):
    # doc_3 carries doc_1's sentence verbatim. Three rows, two sources.
    found = address_of(corroborated_properties(corpus))
    assert found["n_documents"] == 3
    assert found["n_wordings"] == 2
    assert "copied text" in found["note"]


def test_one_wording_across_many_documents_is_not_corroboration(corpus):
    # A framework's boilerplate in every call-off under it.
    corpus.execute(
        "UPDATE provenance SET excerpt = "
        "'The Supplier''s registered office is at 12 Ushers Quay, Dublin 8.'")
    corpus.conn.commit()
    found = address_of(corroborated_properties(corpus))
    assert found["n_documents"] == 3
    assert found["n_wordings"] == 1
    assert found["independent"] is False
    assert "quoted several times" in found["note"]


def test_independent_wordings_are_reported_as_agreement(corpus):
    corpus.execute("UPDATE provenance SET excerpt = "
                   "'Registered at Ushers Quay number twelve, Dublin.' "
                   "WHERE instance_id = 'i3'")
    corpus.conn.commit()
    found = address_of(corroborated_properties(corpus))
    assert found["n_wordings"] == 3
    assert found["independent"] is True
    assert "independent agreement" in found["note"]


def test_the_copy_threshold_is_a_setting_not_a_constant(corpus):
    # Not calibrated against a real corpus, so it has to stay adjustable and
    # visible rather than buried.
    assert 0.5 < COPY_THRESHOLD < 1.0


# -- confidence is left alone ------------------------------------------------

def test_agreement_does_not_move_any_confidence(corpus):
    before = corpus.query("SELECT instance_id, confidence FROM instances_Company "
                          "ORDER BY instance_id")
    corroborated_properties(corpus)
    summary(corpus)
    after = corpus.query("SELECT instance_id, confidence FROM instances_Company "
                         "ORDER BY instance_id")
    assert before == after
    # Three sources at 0.7 stay three sources at 0.7, not one at 0.97.
    assert {r["confidence"] for r in after} == {0.7}


def test_the_summary_says_why_it_does_not_combine(corpus):
    assert "would put a number on the rubric" in summary(corpus)["note"]


# -- a disagreeing fourth source changes what agreement means ----------------

def test_agreement_reports_whether_anything_disagrees(corpus):
    assert address_of(corroborated_properties(corpus))["n_other_values"] == 0
    corpus.execute("UPDATE instances_Company SET address = '4 Sandwith Street' "
                   "WHERE instance_id = 'i2'")
    corpus.conn.commit()
    # Two sources still agree, but a reader has to be told a third dissents.
    found = address_of(corroborated_properties(corpus))
    assert found["n_other_values"] == 1


def test_a_rejected_mention_does_not_corroborate(corpus):
    corpus.execute("UPDATE instances_Company SET status = 'rejected' "
                   "WHERE instance_id = 'i3'")
    corpus.conn.commit()
    found = address_of(corroborated_properties(corpus))
    assert found["n_documents"] == 2


def test_two_spellings_of_a_name_are_aliases_not_agreement(corpus):
    assert not [c for c in corroborated_properties(corpus)
                if c["property_id"] == "name"]


# -- what a page marks -------------------------------------------------------

def test_a_page_marks_only_independent_agreement(corpus):
    corpus.execute("UPDATE provenance SET excerpt = "
                   "'The Supplier''s registered office is at 12 Ushers Quay, Dublin 8.'")
    corpus.conn.commit()
    page = for_entity(corpus, "ent_1")
    # Six copies of one sentence is not a corroborated fact.
    assert page["corroborated_properties"] == []
    assert "address" in page["copied_properties"]


def test_a_page_marks_agreement_when_the_wordings_differ(corpus):
    page = for_entity(corpus, "ent_1")
    assert "address" in page["corroborated_properties"]


# -- the corpus view ---------------------------------------------------------

def test_a_corpus_that_only_quotes_itself_is_told_so(corpus):
    corpus.execute("UPDATE provenance SET excerpt = "
                   "'The Supplier''s registered office is at 12 Ushers Quay, Dublin 8.'")
    corpus.conn.commit()
    assert "citation chain, not corroboration" in summary(corpus)["headline"]


def test_a_corpus_with_no_overlap_says_which_reason(store):
    bundle = bundle_mod.load()
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    headline = summary(store)["headline"]
    assert "does not overlap" in headline and "wiki has not been built" in headline
