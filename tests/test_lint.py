"""The adversarial pass.

What is defended here is that the report is *useful*, which for a lint means
two things: every finding names a row a person can open, and a clean result
never reads as a clean bill of health when nothing has been reviewed.

A lint that says "some docs may be inconsistent" is one nobody acts on. A lint
that says "looks coherent" over four unreviewed documents is worse -- it is
wrong in the direction of comfort.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.entities import (confirm_link, create_entity, link_mention,
                              unlink_mention)
from orpheus.lint import ENOUGH_TO_JUDGE, SEVERITIES, lint
from orpheus.tensions import propose_tensions
from orpheus.utils import naive_key

MENTIONS = [
    ("i1", "doc_1", "Ardmore Digital Ltd", "12 Ushers Quay, Dublin 8"),
    ("i2", "doc_2", "Ardmore Digital Limited", "4 Sandwith Street, Dublin 2"),
]


@pytest.fixture
def corpus(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    for document_id in ("doc_1", "doc_2"):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
    for instance_id, document_id, name, address in MENTIONS:
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, address, source, confidence, status, created_at)"
            " VALUES (?,?,?,?,?,'ai_local',0.9,'confirmed',datetime('now'))",
            (instance_id, document_id, name, naive_key(name), address))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id,"
            " source_label, page_no, excerpt, confidence, created_at, source,"
            " alignment, char_start, char_end)"
            " VALUES (?,?,?,?,1,?,0.9,datetime('now'),'ai_local','match_exact',10,40)",
            (f"p_{instance_id}", instance_id, document_id,
             f"{document_id}.pdf", f"{name} of {address}"))
    store.conn.commit()
    return store


@pytest.fixture
def one_page(corpus):
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    for instance_id in ("i1", "i2"):
        link_mention(corpus, entity_id, instance_id, actor_id="act_a",
                     basis="naive_key")
        confirm_link(corpus, entity_id, instance_id, actor_id="act_a")
    corpus.conn.commit()
    return corpus, entity_id


def findings_of(report, check):
    return [f for f in report["findings"] if f["check"] == check]


# -- every finding is somewhere a person can go ------------------------------

def test_every_finding_names_a_row(one_page):
    corpus, _ = one_page
    report = lint(corpus)
    assert report["findings"], "the fixture is contrived to have problems"
    for found in report["findings"]:
        assert found["where"], found
        # "Some pages may be inconsistent" is not actionable. A finding has to
        # name the thing, not the category.
        assert any(k.endswith("_id") for k in found["where"]), found
        assert found["suggestion"]
        assert found["severity"] in SEVERITIES


def test_findings_are_ordered_worst_first(one_page):
    corpus, _ = one_page
    severities = [f["severity"] for f in lint(corpus)["findings"]]
    rank = [SEVERITIES.index(s) for s in severities]
    assert rank == sorted(rank)


# -- consensus disguising conflict -------------------------------------------

def test_two_confirmed_values_with_no_conflict_recorded_is_a_finding(one_page):
    corpus, entity_id = one_page
    found = findings_of(lint(corpus), "smoothed_conflict")
    assert len(found) == 1
    assert found[0]["where"]["property_id"] == "address"
    assert found[0]["severity"] == "high"


def test_recording_the_conflict_clears_the_finding(one_page):
    corpus, entity_id = one_page
    propose_tensions(corpus, actor_id="act_a")
    report = lint(corpus)
    assert findings_of(report, "smoothed_conflict") == []
    # It moves rather than vanishing: raised is not ruled on.
    assert len(findings_of(report, "unchecked_conflict")) == 1


# -- assertions with nothing behind them -------------------------------------

def test_a_page_with_no_source_is_the_worst_finding(corpus):
    entity_id = create_entity(corpus, "Company", "Invented Holdings",
                              actor_id="act_a",
                              description="a person wrote this from memory")
    corpus.conn.commit()
    found = findings_of(lint(corpus), "uncited_page")
    assert [f["where"]["entity_id"] for f in found] == [entity_id]
    assert found[0]["severity"] == "high"


def test_a_quotation_the_document_does_not_contain_is_a_finding(corpus):
    corpus.execute("UPDATE provenance SET alignment = NULL WHERE instance_id = 'i1'")
    corpus.conn.commit()
    found = findings_of(lint(corpus), "ungrounded_quotation")
    assert len(found) == 1
    assert found[0]["where"]["instance_id"] == "i1"
    assert found[0]["where"]["filename"] == "doc_1.pdf"


# -- misleading by omission --------------------------------------------------

def test_a_confirmed_mention_on_no_page_is_a_finding(corpus):
    # The wiki looks complete and is missing a document, and neither surface
    # says so.
    found = findings_of(lint(corpus), "orphan_mention")
    assert {f["where"]["instance_id"] for f in found} == {"i1", "i2"}


def test_unlinking_puts_a_mention_back_in_the_orphan_list(one_page):
    corpus, entity_id = one_page
    assert findings_of(lint(corpus), "orphan_mention") == []
    unlink_mention(corpus, entity_id, "i2", actor_id="act_a", note="not this")
    corpus.conn.commit()
    assert {f["where"]["instance_id"]
            for f in findings_of(lint(corpus), "orphan_mention")} == {"i2"}


def test_a_document_nothing_was_extracted_from_is_a_finding(corpus):
    corpus.execute(
        "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
        " n_pages, date_added, created_by, visibility, review_status)"
        " VALUES ('doc_9','never-read.pdf','h9',100,1,datetime('now'),'act_a',"
        "'private','unreviewed')")
    corpus.conn.commit()
    found = findings_of(lint(corpus), "unextracted_document")
    assert [f["where"]["document_id"] for f in found] == ["doc_9"]


def test_a_grouping_nobody_checked_is_flagged_in_proportion_to_its_claim(corpus):
    # A page over one document is a proposal; over three it asserts that three
    # documents are about one thing.
    for n in (3, 4, 5):
        document_id = f"doc_{n}"
        corpus.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
        corpus.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, source, confidence, status, created_at)"
            " VALUES (?,?,'Kestrel Ltd','kestrel','ai_local',0.9,'unconfirmed',"
            " datetime('now'))", (f"k{n}", document_id))
        corpus.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (f"k{n}", document_id))
    entity_id = create_entity(corpus, "Company", "Kestrel Ltd", actor_id="act_a")
    for n in (3, 4, 5):
        link_mention(corpus, entity_id, f"k{n}", actor_id="act_a",
                     basis="naive_key")
    corpus.conn.commit()
    found = findings_of(lint(corpus), "unreviewed_grouping")
    assert [f["where"]["entity_id"] for f in found] == [entity_id]


# -- the headline ------------------------------------------------------------

def test_finding_nothing_on_an_unreviewed_store_says_so(store):
    # The most dangerous output this could produce is a clean bill of health.
    bundle = bundle_mod.load()
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    report = lint(store)
    assert report["n_findings"] == 0
    assert "how little has been checked" in report["headline"]
    assert "0 link(s)" in report["headline"]


def test_a_reviewed_store_with_nothing_found_still_states_the_limit(one_page):
    corpus, entity_id = one_page
    propose_tensions(corpus, actor_id="act_a")
    # Enough reviewed links to earn the other headline.
    for n in range(ENOUGH_TO_JUDGE):
        corpus.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
            " basis, confidence, status, linked_at) VALUES (?,?,?,'human',1.0,"
            "'confirmed',datetime('now'))", (entity_id, f"pad{n}", "doc_1"))
    corpus.conn.commit()
    report = lint(corpus, checks=["uncited_page"])
    assert report["n_findings"] == 0
    assert "evidence, not proof" in report["headline"]


def test_the_headline_leads_with_the_worst(one_page):
    corpus, _ = one_page
    report = lint(corpus)
    assert "high" in report["headline"]
    assert str(report["n_findings"]) in report["headline"]


def test_a_shallow_pass_skips_the_expensive_comparisons(one_page):
    corpus, _ = one_page
    shallow = lint(corpus, deep=False)
    assert "smoothed_conflict" not in shallow["checks_run"]
    assert "uncited_page" in shallow["checks_run"]
