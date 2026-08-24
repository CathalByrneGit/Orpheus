"""The corpus as a network.

Two things are defended here. First, that the projection is *right*: an edge
between two clauses in four contracts is one relation between two pages with
four sources, and a merged page still resolves. Second, and more important, that
the module says how much of the corpus it is actually describing — a topology
read off a half-linked wiki is a confident picture of a fraction of the evidence,
and nothing in the numbers themselves would reveal it.

The test graph is drawn by hand so the structural answers are known in advance
rather than asserted from whatever the code produced:

    A -- B -- C -- D        one component; B and C are cut vertices
    E -- F                  a second component, joined to nothing
    G                       a page with no relation at all
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.graph import (articulation_points, build, canonical_edges,
                           communities, community_connections, components,
                           coverage, isolates, neighbourhood, topology)
from orpheus.utils import NotFound, naive_key

COMPANIES = "ABCDEFG"

# from, to, and the documents that each assert it. A -> B is asserted three
# times, which is the case the store previously had no way to notice.
LINKS = [
    ("A", "B", ["doc_1", "doc_2", "doc_3"]),
    ("B", "C", ["doc_1"]),
    ("C", "D", ["doc_2"]),
    ("E", "F", ["doc_3"]),
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

    # One page per company, and one mention of it per document that needs one.
    for letter in COMPANIES:
        name = f"Company {letter}"
        store.execute(
            "INSERT INTO entities (entity_id, type_id, canonical_name, naive_key,"
            " source, confidence, status, created_at, created_by)"
            " VALUES (?,'Company',?,?,'human',1.0,'confirmed',datetime('now'),'act_a')",
            (f"ent_{letter}", name, naive_key(name)))

    def mention(letter, document_id):
        """One mention of a company in a document, already on its page."""
        instance_id = f"i_{letter}_{document_id}"
        if store.one("SELECT 1 FROM instance_index WHERE instance_id = ?",
                     (instance_id,)):
            return instance_id
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, source, confidence, status, created_at)"
            " VALUES (?,?,?,?,'ai_local',0.9,'confirmed',datetime('now'))",
            (instance_id, document_id, f"Company {letter}",
             naive_key(f"Company {letter}")))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO entity_mentions (entity_id, instance_id, document_id,"
            " basis, confidence, status, linked_at, linked_by)"
            " VALUES (?,?,?,'human',1.0,'confirmed',datetime('now'),'act_a')",
            (f"ent_{letter}", instance_id, document_id))
        return instance_id

    for source, target, documents in LINKS:
        for document_id in documents:
            store.execute(
                "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
                " link_type_id, document_id, evidence, source, confidence,"
                " status, created_at) VALUES (?,?,?,'subcontracts_to',?,?,"
                "'ai_local',0.9,'unconfirmed',datetime('now'))",
                (f"edge_{source}{target}_{document_id}",
                 mention(source, document_id), mention(target, document_id),
                 document_id, f"{source} subcontracts to {target}"))
    mention("G", "doc_1")
    store.conn.commit()
    return store


# -- the projection ----------------------------------------------------------

def test_one_relation_asserted_three_times_is_one_edge_with_three_sources(corpus):
    # The question the store could not previously ask.
    edges = {(e["from_name"], e["to_name"]): e for e in canonical_edges(corpus)}
    edge = edges[("Company A", "Company B")]
    assert edge["n_documents"] == 3
    assert edge["n_sources"] == 3
    assert edge["documents"] == ["doc_1", "doc_2", "doc_3"]
    # Collapsing the sources into a count would lose the citations, and a
    # relation nobody can trace back is not evidence.
    assert all(s["evidence"] and s["document_id"] for s in edge["support"])


def test_confidence_is_not_added_up_across_sources(corpus):
    # Three sources at 0.9 stay at 0.9. Combining them would manufacture
    # certainty out of documents that may well copy each other.
    edge = next(e for e in canonical_edges(corpus)
                if e["from_name"] == "Company A")
    assert edge["max_confidence"] == 0.9


def test_a_rejected_relation_is_not_in_the_graph(corpus):
    corpus.execute("UPDATE edges SET status = 'rejected' "
                   "WHERE link_type_id = 'subcontracts_to' "
                   "AND edge_id LIKE 'edge_BC%'")
    corpus.conn.commit()
    pairs = {(e["from_name"], e["to_name"]) for e in canonical_edges(corpus)}
    assert ("Company B", "Company C") not in pairs


def test_a_merged_page_still_carries_its_edges(corpus):
    # A link made before two pages were joined still points at a real page.
    corpus.execute("UPDATE entities SET merged_into = 'ent_A' WHERE entity_id = 'ent_E'")
    corpus.conn.commit()
    edges = canonical_edges(corpus)
    assert ("Company A", "Company F") in {(e["from_name"], e["to_name"]) for e in edges}
    assert "Company E" not in {e["from_name"] for e in edges}


def test_a_relation_between_two_mentions_of_one_page_is_not_an_edge(corpus):
    # It would draw a self-loop, which is not structure.
    corpus.execute(
        "INSERT INTO edges (edge_id, from_instance_id, to_instance_id,"
        " link_type_id, document_id, source, confidence, status, created_at)"
        " VALUES ('edge_self','i_A_doc_1','i_A_doc_2','subcontracts_to','doc_1',"
        "'ai_local',0.9,'unconfirmed',datetime('now'))")
    corpus.conn.commit()
    assert all(e["from_entity_id"] != e["to_entity_id"]
               for e in canonical_edges(corpus))


# -- deterministic structure -------------------------------------------------

def test_the_islands_are_found(corpus):
    found = components(build(corpus))
    assert [c["n_entities"] for c in found] == [4, 2]
    assert set(found[0]["members"]) == {"ent_A", "ent_B", "ent_C", "ent_D"}
    assert set(found[1]["members"]) == {"ent_E", "ent_F"}


def test_a_component_is_named_after_its_most_connected_page(corpus):
    # "Community 3" tells a reader nothing and invites them to invent a meaning.
    found = components(build(corpus))
    assert found[0]["label"] in ("Company B", "Company C")
    assert found[0]["entity_types"] == {"Company": 4}


def test_the_pages_holding_the_graph_together_are_named(corpus):
    # A -- B -- C -- D: removing B or C splits it; removing A or D does not.
    found = {b["entity_id"] for b in articulation_points(build(corpus))}
    assert found == {"ent_B", "ent_C"}


def test_a_page_with_no_relation_is_listed_as_isolated(corpus):
    assert [i["entity_id"] for i in isolates(build(corpus))] == ["ent_G"]


def test_structure_is_the_same_on_every_run(corpus):
    graph = build(corpus)
    first = (components(graph), articulation_points(graph), isolates(graph))
    second = (components(graph), articulation_points(graph), isolates(graph))
    assert first == second


# -- heuristic structure, marked as such -------------------------------------

def test_a_community_says_it_is_a_reading_rather_than_a_fact(corpus):
    for community in communities(build(corpus)):
        assert community["basis"] == "heuristic"


def test_the_same_seed_gives_the_same_partition(corpus):
    graph = build(corpus)
    assert ([c["members"] for c in communities(graph, seed=7)]
            == [c["members"] for c in communities(graph, seed=7)])


def test_clusters_that_never_touch_are_reported_as_such(corpus):
    # The point of the pairing: two clusters the corpus has never connected.
    graph = build(corpus)
    pairs = community_connections(graph)
    assert pairs, "two components should give at least one pair"
    assert any(p["disconnected"] for p in pairs)


# -- how much of the corpus this describes -----------------------------------

def test_coverage_is_reported_alongside_the_structure(corpus):
    report = coverage(corpus)
    assert report["projected_rate"] == 1.0
    assert report["n_edges_projected"] == report["n_edges_total"] == 6


def test_a_half_linked_wiki_says_so_rather_than_looking_sparse(corpus):
    # Nothing in the structural numbers alone would reveal this.
    corpus.execute("UPDATE entity_mentions SET unlinked_at = datetime('now') "
                   "WHERE entity_id IN ('ent_C', 'ent_D')")
    corpus.conn.commit()
    report = coverage(corpus)
    assert report["projected_rate"] < 1.0
    assert "not the corpus" in report["note"] or "still have no page" in report["note"]


def test_a_corpus_with_no_relations_says_that_rather_than_reporting_sparsity(store):
    bundle = bundle_mod.load()
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    assert "no relation material" in coverage(store)["note"]


# -- one neighbourhood -------------------------------------------------------

def test_a_neighbourhood_grows_one_hop_at_a_time(corpus):
    graph = build(corpus)
    one = neighbourhood(corpus, "ent_A", depth=1, graph=graph)
    assert {n["entity_id"] for n in one["nodes"]} == {"ent_A", "ent_B"}
    two = neighbourhood(corpus, "ent_A", depth=2, graph=graph)
    assert {n["entity_id"] for n in two["nodes"]} == {"ent_A", "ent_B", "ent_C"}
    assert two["nodes"][0]["hops"] == 0


def test_an_unknown_page_is_not_an_empty_neighbourhood(corpus):
    with pytest.raises(NotFound):
        neighbourhood(corpus, "ent_nope")


# -- the whole picture -------------------------------------------------------

def test_the_topology_leads_with_how_much_it_describes(corpus):
    report = topology(corpus)
    # Every number after this one is conditional on it.
    assert list(report)[0] == "coverage"
    assert report["counts"]["components"] == 2
    assert report["counts"]["isolated_entities"] == 1
    assert report["counts"]["canonical_edges"] == 4
    assert "island is a fact" in report["note"]
    assert report["most_connected"][0]["degree"] == 2
