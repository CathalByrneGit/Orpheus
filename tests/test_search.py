"""Full-text search, and the screen entity resolution actually needs.

An exact-key match can only join instances that were already extracted. The
question that matters for building a wiki is the opposite one: which documents
say this name and have *nothing* extracted from them? Those are the misses, and
they are invisible to key matching by construction.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.search import (available, enable_search, search_excerpts,
                            search_pages, unextracted_mentions)
from orpheus.utils import OrpheusError, naive_key

pytest.importorskip("sqlite_utils")

PAGES = [
    ("doc_1", "This Agreement is between Halloran Instruments, Inc. and "
              "Kestrel Medical Group PLC, governed by the laws of New York."),
    ("doc_2", "Amendment No. 2 with Halloran Instruments Inc, extending the term."),
    ("doc_3", "Kestrel Medical Group entered a licence with Ardmore Digital Ltd."),
]


@pytest.fixture
def corpus(store):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    for document_id, text in PAGES:
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, visibility, review_status)"
            " VALUES (?,?,?,?,1,datetime('now'),'private','unreviewed')",
            (document_id, f"{document_id}.txt", document_id, len(text)))
        store.execute("INSERT INTO document_pages (document_id, page_no, text,"
                      " text_source) VALUES (?,1,?,'native')", (document_id, text))
    # Only doc_1 has the company extracted from it.
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, naive_key,"
        " source, confidence, status, created_at)"
        " VALUES ('i1','doc_1','Halloran Instruments, Inc.',?,'ai_local',1.0,"
        " 'confirmed',datetime('now'))", (naive_key("Halloran Instruments, Inc."),))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, document_id,"
        " created_at) VALUES ('i1','Company','instances_Company','doc_1',"
        " datetime('now'))")
    enable_search(store)
    return store


def test_building_the_index_twice_is_a_no_op(store):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    first = enable_search(store)
    assert set(first.values()) == {"indexed"}
    assert set(enable_search(store).values()) == {"already indexed"}


def test_searching_before_building_says_so(store):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    with pytest.raises(OrpheusError, match="not indexed"):
        search_pages(store, "anything")


def test_a_phrase_is_found_across_documents(corpus):
    hits = search_pages(corpus, "Halloran")
    assert {h["document_id"] for h in hits} == {"doc_1", "doc_2"}


def test_a_quoted_phrase_with_punctuation_does_not_break_the_query(corpus):
    # A stray quote or comma in a company name is a syntax error in raw FTS5.
    for query in ('"Kestrel Medical Group"', '"Halloran Instruments, Inc."'):
        assert search_pages(corpus, query)


def test_the_index_keeps_up_with_a_new_page(corpus):
    corpus.execute("INSERT INTO document_pages (document_id, page_no, text,"
                   " text_source) VALUES ('doc_3', 2, 'Halloran Instruments is "
                   "hereby joined as an additional supplier.', 'native')")
    corpus.conn.commit()
    assert "doc_3" in {h["document_id"] for h in search_pages(corpus, "Halloran")}


def test_excerpts_are_searchable_separately_from_source_text(corpus):
    # Two indexes, two questions: what the document says, and what the machine
    # quoted. A name in the source with no excerpt is exactly the gap.
    corpus.execute(
        "INSERT INTO provenance (provenance_id, instance_id, document_id,"
        " source_label, page_no, excerpt, confidence, created_at, source,"
        " alignment) VALUES ('p1','i1','doc_1','doc_1.txt',1,"
        " 'Halloran Instruments, Inc.',1.0,datetime('now'),'ai_local','match_exact')")
    corpus.conn.commit()
    quoted = search_excerpts(corpus, "Halloran")
    assert [h["instance_id"] for h in quoted] == ["i1"]
    assert quoted[0]["alignment"] == "match_exact"
    # The source mentions it twice; only one of those was ever extracted.
    assert len(search_pages(corpus, "Halloran")) == 2


def test_unextracted_mentions_finds_the_document_nothing_was_extracted_from(corpus):
    result = unextracted_mentions(corpus, "Halloran Instruments")
    assert result["linked_documents"] == ["doc_1"]
    assert [h["document_id"] for h in result["unlinked"]] == ["doc_2"]


def test_unextracted_mentions_stays_labelled_unresolved(corpus):
    # It is a list of candidates for a person, never a merge the machine did.
    result = unextracted_mentions(corpus, "Halloran Instruments")
    assert result["resolution_quality"] == "naive_unresolved"
    assert "not matches" in result["caveat"]


def test_a_name_with_nothing_to_find_returns_nothing_rather_than_failing(corpus):
    result = unextracted_mentions(corpus, "Nonexistent Holdings")
    assert result["unlinked"] == []
    assert result["linked_documents"] == []
