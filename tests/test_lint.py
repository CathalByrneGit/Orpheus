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


def test_a_join_the_graph_depends_on_with_no_confirmed_evidence_is_flagged(corpus):
    # The topology gives no hint of this: a link's review status does not
    # change the shape of the graph.
    from orpheus.entities import create_entity, link_mention

    middle = create_entity(corpus, "Company", "Bridging Ltd", actor_id="act_a")
    ends = {}
    for n, name in ((8, "Left Ltd"), (9, "Right Ltd")):
        document_id = f"doc_{n}"
        corpus.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
        for suffix, who in ((f"m{n}", "Bridging Ltd"), (f"e{n}", name)):
            corpus.execute(
                "INSERT INTO instances_Company (instance_id, document_id, name,"
                " naive_key, source, confidence, status, created_at)"
                " VALUES (?,?,?,?,'ai_local',0.9,'unconfirmed',datetime('now'))",
                (suffix, document_id, who, who.lower()))
            corpus.execute(
                "INSERT INTO instance_index (instance_id, type_id, table_name,"
                " document_id, created_at) VALUES (?,'Company',"
                "'instances_Company',?,datetime('now'))", (suffix, document_id))
        ends[n] = create_entity(corpus, "Company", name, actor_id="act_a")
        link_mention(corpus, middle, f"m{n}", actor_id="act_a", basis="naive_key")
        link_mention(corpus, ends[n], f"e{n}", actor_id="act_a", basis="naive_key")
        corpus.execute(
            "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
            " link_type_id, document_id, evidence, source, confidence, status,"
            " created_at) VALUES (?,?,?,'subcontracts_to',?,'x','ai_local',0.8,"
            "'unconfirmed',datetime('now'))",
            (f"edge{n}", f"m{n}", f"e{n}", document_id))
    corpus.conn.commit()

    found = findings_of(lint(corpus), "fragile_join")
    assert [f["where"]["entity_id"] for f in found] == [middle]
    assert "falls into two" in found[0]["suggestion"]


def test_a_rejected_extraction_stops_being_reported_as_a_citation(corpus):
    # The check told a reviewer to reject the finding, and went on saying it
    # after they had. A finding whose advice has been followed and which still
    # will not clear is how a check teaches people to skip the whole list.
    from orpheus.lint import ungrounded_quotations

    store = corpus
    doc = store.scalar("SELECT document_id FROM documents LIMIT 1")
    store.insert("instance_index", {
        "instance_id": "inst_ug", "type_id": "Flag",
        "table_name": "instances_Flag", "document_id": doc,
        "created_at": "2026-01-01T00:00:00Z"})
    store.insert("instances_Flag", {
        "instance_id": "inst_ug", "document_id": doc,
        "flag_type": "missing_signature", "severity": "low",
        "source": "ai_cloud", "confidence": 1.0, "status": "unconfirmed",
        "created_at": "2026-01-01T00:00:00Z"})
    store.insert("provenance", {
        "provenance_id": "prov_ug", "instance_id": "inst_ug",
        "document_id": doc, "source_label": "t", "page_no": 1,
        "excerpt": "a sentence this document does not contain",
        "confidence": 1.0, "alignment": None,
        "created_at": "2026-01-01T00:00:00Z"})
    store.conn.commit()

    assert any(f["where"]["instance_id"] == "inst_ug"
               for f in ungrounded_quotations(store)), \
        "an ungrounded quotation on a live row is exactly what this check is for"

    store.execute("UPDATE instances_Flag SET status = 'rejected' "
                  "WHERE instance_id = 'inst_ug'")
    store.conn.commit()

    assert not any(f["where"]["instance_id"] == "inst_ug"
                   for f in ungrounded_quotations(store))
