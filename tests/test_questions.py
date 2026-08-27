"""Questions the shape of a corpus raises.

The property defended throughout: **this module never reports a finding.** A
shared subcontractor is not wrongdoing, and in the setting Orpheus is built for
an accusation drawn from a graph join does real damage. Every question names a
chain, cites the documents behind each hop, and says how much of it anybody has
actually checked.

The second property is the ordering. Checked chains come first, which is the
opposite of a severity sort and is the point: a question assembled from
unreviewed machine guesses is a reason to check the extraction, not to act.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.graph import build
from orpheus.questions import (circular_relation, raised, shared_counterparty,
                               two_parts_in_one_document)
from orpheus.utils import naive_key

#   A -- M -- B     M is the only route between A and B
#   C               unconnected, and must raise nothing
LINKS = [("A", "M"), ("M", "B")]


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
    for letter in "AMBC":
        name = f"Company {letter}"
        store.execute(
            "INSERT INTO entities (entity_id, type_id, canonical_name, naive_key,"
            " source, confidence, status, created_at)"
            " VALUES (?,'Company',?,?,'human',1.0,'confirmed',datetime('now'))",
            (f"ent_{letter}", name, naive_key(name)))

    def mention(letter, document_id, role=None):
        instance_id = f"i_{letter}_{document_id}"
        if store.one("SELECT 1 FROM instance_index WHERE instance_id = ?",
                     (instance_id,)):
            return instance_id
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, role, source, confidence, status, created_at)"
            " VALUES (?,?,?,?,?,'ai_local',0.9,'unconfirmed',datetime('now'))",
            (instance_id, document_id, f"Company {letter}",
             naive_key(f"Company {letter}"), role))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
            " basis, confidence, status, linked_at)"
            " VALUES (?,?,?,'human',1.0,'confirmed',datetime('now'))",
            (f"ent_{letter}", instance_id, document_id))
        return instance_id

    for index, (left, right) in enumerate(LINKS):
        document_id = f"doc_{index + 1}"
        store.execute(
            "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
            " link_type_id, document_id, evidence, source, confidence, status,"
            " created_at) VALUES (?,?,?,'subcontracts_to',?,?,'ai_local',0.8,"
            "'unconfirmed',datetime('now'))",
            (f"edge_{left}{right}", mention(left, document_id),
             mention(right, document_id), document_id,
             f"{left} subcontracts to {right}"))
    mention("C", "doc_1")
    store.conn.commit()
    return store


# -- never a finding ---------------------------------------------------------

def test_every_question_asks_rather_than_concludes(corpus):
    report = raised(corpus)
    assert report["questions"]
    for question in report["questions"]:
        assert question["asks"].strip().endswith(("?", "above.", "hop."))
        # The chain is the point: a claim about closeness with no citations is
        # an accusation drawn from a join.
        assert question["chain"]
        assert question["documents"]
    assert "None of this is a finding" in report["note"]


def test_a_question_says_how_much_of_it_has_been_checked(corpus):
    found = shared_counterparty(corpus)
    assert found and all(q["confirmed_throughout"] is False for q in found)

    corpus.execute("UPDATE edges SET status = 'confirmed'")
    corpus.conn.commit()
    assert all(q["confirmed_throughout"] for q in shared_counterparty(corpus))


def test_checked_chains_come_first(corpus):
    # The opposite of a severity sort, on purpose.
    corpus.execute("UPDATE edges SET status = 'confirmed' WHERE edge_id = 'edge_AM'")
    corpus.conn.commit()
    # Give the graph a second, wholly unreviewed shared party to rank below.
    order = [q["confirmed_throughout"] for q in raised(corpus)["questions"]]
    assert order == sorted(order, key=lambda checked: not checked)


# -- the only route ----------------------------------------------------------

def test_two_pages_joined_only_through_a_third_are_a_question(corpus):
    found = shared_counterparty(corpus)
    assert len(found) == 1
    names = {e["name"] for e in found[0]["entities"]}
    assert names == {"Company A", "Company M", "Company B"}
    shared = next(e for e in found[0]["entities"] if e["part"] == "shared")
    assert shared["name"] == "Company M"


def test_pages_that_also_connect_directly_are_not_a_question(corpus):
    # Then the middle is not the only route, and every well-connected page
    # would otherwise generate a question about every pair around it.
    corpus.execute(
        "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
        " link_type_id, document_id, evidence, source, confidence, status,"
        " created_at) VALUES ('edge_AB','i_A_doc_1','i_B_doc_2',"
        "'subcontracts_to','doc_1','direct','ai_local',0.8,'unconfirmed',"
        " datetime('now'))")
    corpus.conn.commit()
    assert shared_counterparty(corpus) == []


def test_an_unconnected_page_raises_nothing(corpus):
    assert not any("Company C" in q["summary"] for q in raised(corpus)["questions"])


# -- two parts in one document -----------------------------------------------

def test_one_party_in_two_roles_in_one_document_is_a_question(corpus):
    corpus.execute("UPDATE instances_Company SET role = 'buyer' "
                   "WHERE instance_id = 'i_A_doc_1'")
    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, role, source, confidence, status, created_at)"
        " VALUES ('i_A2_doc_1','doc_1','Company A','company a','supplier',"
        "'ai_local',0.9,'unconfirmed',datetime('now'))")
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i_A2_doc_1','Company',"
        "'instances_Company','doc_1',datetime('now'))")
    corpus.execute(
        "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
        " basis, confidence, status, linked_at) VALUES ('ent_A','i_A2_doc_1',"
        "'doc_1','human',1.0,'confirmed',datetime('now'))")
    corpus.conn.commit()

    found = two_parts_in_one_document(corpus)
    assert len(found) == 1
    assert {"buyer", "supplier"} <= set(found[0]["summary"].split())
    # It might be an extraction error rather than a dual capacity, and the
    # question says so rather than picking.
    assert "extraction" in found[0]["asks"]


def test_one_role_in_one_document_is_not_a_question(corpus):
    corpus.execute("UPDATE instances_Company SET role = 'supplier'")
    corpus.conn.commit()
    assert two_parts_in_one_document(corpus) == []


def test_a_rejected_mention_does_not_raise_a_question(corpus):
    corpus.execute("UPDATE instances_Company SET role = 'buyer' "
                   "WHERE instance_id = 'i_A_doc_1'")
    corpus.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, role, source, confidence, status, created_at)"
        " VALUES ('i_A2_doc_1','doc_1','Company A','company a','supplier',"
        "'ai_local',0.9,'rejected',datetime('now'))")
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('i_A2_doc_1','Company',"
        "'instances_Company','doc_1',datetime('now'))")
    corpus.execute(
        "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
        " basis, confidence, status, linked_at) VALUES ('ent_A','i_A2_doc_1',"
        "'doc_1','human',1.0,'confirmed',datetime('now'))")
    corpus.conn.commit()
    assert two_parts_in_one_document(corpus) == []


# -- coming back round -------------------------------------------------------

def test_a_relation_that_returns_is_a_question(corpus):
    corpus.execute(
        "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
        " link_type_id, document_id, evidence, source, confidence, status,"
        " created_at) VALUES ('edge_BA','i_B_doc_2','i_A_doc_1',"
        "'subcontracts_to','doc_2','and back again','ai_local',0.8,"
        "'unconfirmed',datetime('now'))")
    corpus.conn.commit()
    found = circular_relation(corpus)
    assert len(found) == 1
    assert "comes back to where it started" in found[0]["summary"]
    # It may be one relationship recorded twice with its ends swapped.
    assert "swapped" in found[0]["asks"]


def test_a_cycle_is_reported_once_not_once_per_rotation(corpus):
    corpus.execute(
        "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
        " link_type_id, document_id, evidence, source, confidence, status,"
        " created_at) VALUES ('edge_BA','i_B_doc_2','i_A_doc_1',"
        "'subcontracts_to','doc_2','back','ai_local',0.8,'unconfirmed',"
        " datetime('now'))")
    corpus.conn.commit()
    assert len(circular_relation(corpus)) == 1


def test_no_cycle_means_no_question(corpus):
    assert circular_relation(corpus) == []


# -- what the report leads with ----------------------------------------------

def test_coverage_comes_first_because_two_checks_read_the_graph(corpus):
    report = raised(corpus)
    assert list(report)[0] == "coverage"


def test_finding_nothing_is_not_a_clean_bill_of_health(store):
    bundle = bundle_mod.load()
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    note = raised(store)["note"]
    assert "not a clean bill of health" in note
