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
    assert ("wait on the wiki queue" in report["note"]
            or "still have no page" in report["note"])
    assert "linked part of the corpus" in report["note"]


def test_a_structural_relation_is_not_counted_as_unbuilt_wiki(corpus):
    # A relation from a contract to one of its clauses can never reach the
    # graph: a clause is not a Named type and so never gets a page. Counting
    # it as a missing page sends the reader to a queue that cannot move the
    # number. A real six-document corpus of SEC contracts read 11% covered,
    # with every projectable edge already drawn and 153 structural ones.
    corpus.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, "
        "document_id, created_at) VALUES "
        "('inst_clause_x', 'Clause', 'instances_Clause', 'doc_1', "
        " '2026-01-01T00:00:00Z')")
    corpus.execute(
        "INSERT INTO edges (edge_id, from_instance_id, to_instance_id, "
        "link_type_id, document_id, source, confidence, status, created_at) "
        "SELECT 'edge_structural', from_instance_id, 'inst_clause_x', "
        "       link_type_id, document_id, source, confidence, status, "
        "       created_at FROM edges LIMIT 1")
    corpus.conn.commit()

    report = coverage(corpus)
    assert report["n_edges_structural"] == 1
    assert report["n_edges_awaiting_pages"] == 0
    assert "never gets a page" in report["note"]
    assert "not an unbuilt wiki" in report["note"]


def test_a_page_that_is_genuinely_missing_is_still_called_work(corpus):
    # The other half of the distinction: an endpoint that *is* a Named type
    # but has no page yet is real queue work, and still says so.
    corpus.execute("UPDATE entity_mentions SET unlinked_at = datetime('now') "
                   "WHERE entity_id IN ('ent_C', 'ent_D')")
    corpus.conn.commit()
    report = coverage(corpus)
    assert report["n_edges_awaiting_pages"] > 0
    assert "wait on the wiki queue" in report["note"]


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


# -- how two pages are connected ---------------------------------------------
#
# The question a corpus is actually asked, and the one a list of entities
# cannot answer. Stdlib only: it is a breadth-first walk over the adjacency
# that is already built, so it works in a bare install like the rest of the
# deterministic half.

def test_a_chain_is_found_and_the_shortest_comes_first(corpus):
    from orpheus.graph import paths_between
    result = paths_between(corpus, "ent_A", "ent_D")
    assert result["n_paths"] == 1
    path = result["paths"][0]
    assert [e["entity_id"] for e in path["entities"]] == \
        ["ent_A", "ent_B", "ent_C", "ent_D"]
    assert path["n_hops"] == 3


def test_every_hop_carries_what_it_rests_on(corpus):
    # A chain reported without its evidence invites exactly the conclusion the
    # store exists to prevent.
    from orpheus.graph import paths_between
    path = paths_between(corpus, "ent_A", "ent_D")["paths"][0]
    assert all(hop["link_type_id"] and hop["documents"] for hop in path["hops"])
    assert path["hops"][0]["n_documents"] == 3


def test_a_chain_names_its_weakest_hop(corpus):
    # A chain is as good as its worst link, and a reader should not have to
    # scan for it.
    from orpheus.graph import paths_between
    path = paths_between(corpus, "ent_A", "ent_D")["paths"][0]
    assert path["confirmed_throughout"] is False
    assert path["weakest"]["n_confirmed"] == 0

    corpus.execute("UPDATE edges SET status = 'confirmed'")
    corpus.conn.commit()
    vouched = paths_between(corpus, "ent_A", "ent_D")["paths"][0]
    assert vouched["confirmed_throughout"] is True


def test_two_islands_have_no_chain_between_them_and_it_says_why(corpus):
    from orpheus.graph import paths_between
    result = paths_between(corpus, "ent_A", "ent_E")
    assert result["paths"] == []
    assert "different islands" in result["note"]


def test_a_chain_longer_than_the_limit_is_not_reported(corpus):
    from orpheus.graph import paths_between
    assert paths_between(corpus, "ent_A", "ent_D", max_length=2)["paths"] == []


def test_a_page_is_not_connected_to_itself(corpus):
    from orpheus.graph import paths_between
    from orpheus.utils import OrpheusError
    with pytest.raises(OrpheusError):
        paths_between(corpus, "ent_A", "ent_A")


# -- the optional networkx layer ---------------------------------------------

def test_the_hand_rolled_cut_vertices_agree_with_networkx(corpus):
    # The deterministic half is stdlib so a bare install still reads
    # structurally, which means this Tarjan implementation is load-bearing and
    # unvalidated by anything but itself. networkx settles it.
    nx = pytest.importorskip("networkx")
    from orpheus.graph import articulation_points, to_networkx
    graph = build(corpus)
    mine = {b["entity_id"] for b in articulation_points(graph)}
    theirs = set(nx.articulation_points(to_networkx(graph)))
    assert mine == theirs


def test_the_hand_rolled_components_agree_with_networkx(corpus):
    nx = pytest.importorskip("networkx")
    from orpheus.graph import to_networkx
    graph = build(corpus)
    built = to_networkx(graph)
    built.remove_nodes_from([n for n in list(built) if built.degree(n) == 0])
    mine = {frozenset(c["members"]) for c in components(graph)}
    theirs = {frozenset(c) for c in nx.connected_components(built)}
    assert mine == theirs


def test_louvain_runs_where_networkx_is_installed_and_says_so(corpus):
    pytest.importorskip("networkx")
    found = communities(build(corpus))
    assert found and all(c["method"] == "louvain" for c in found)
    # Still a heuristic. What changes is how much of one, not whether.
    assert all(c["basis"] == "heuristic" for c in found)


def test_louvain_reports_whether_its_partition_means_anything(corpus):
    # Modularity near zero means the clusters are no better than chance, and a
    # reader who has not met the measure should not have to know that.
    pytest.importorskip("networkx")
    found = communities(build(corpus))
    assert "modularity" in found[0]
    assert found[0]["modularity_note"]


def test_the_fallback_can_still_be_asked_for_by_name(corpus):
    # The core must cluster with nothing installed, so the path has to stay
    # exercised even in an environment that has networkx.
    found = communities(build(corpus), method="label_propagation")
    assert found and all(c["method"] == "label_propagation" for c in found)


def test_an_unknown_method_is_refused_rather_than_guessed(corpus):
    from orpheus.utils import OrpheusError
    with pytest.raises(OrpheusError):
        communities(build(corpus), method="spectral")


def test_betweenness_finds_what_degree_cannot(corpus):
    # A -- B -- C -- D: B and C carry every path across, and each has the same
    # degree as A and D have between them. Degree alone does not distinguish.
    pytest.importorskip("networkx")
    from orpheus.graph import centrality
    result = centrality(build(corpus))
    assert result["method"] == "betweenness_exact"
    top = {n["entity_id"] for n in result["by_betweenness"][:2]}
    assert top == {"ent_B", "ent_C"}


def test_the_topology_carries_both_rankings_and_names_the_method(corpus):
    report = topology(corpus)
    assert report["centrality_method"] in (
        "betweenness_exact", "betweenness_sampled", "degree_only")
    assert report["most_connected"]


# -- and without it ----------------------------------------------------------
#
# The bare install is the guarantee, so it has to be exercised in an
# environment that happens to have networkx. Otherwise the fallback is only
# ever tested by not being reached.

@pytest.fixture
def without_networkx(monkeypatch):
    import orpheus.graph as graph_mod
    monkeypatch.setattr(graph_mod, "_networkx", lambda: None)
    return graph_mod


def test_clustering_falls_back_rather_than_failing(corpus, without_networkx):
    found = without_networkx.communities(build(corpus))
    assert found and all(c["method"] == "label_propagation" for c in found)


def test_asking_for_louvain_without_it_says_what_to_install(corpus, without_networkx):
    from orpheus.utils import OrpheusError
    with pytest.raises(OrpheusError) as caught:
        without_networkx.communities(build(corpus), method="louvain")
    assert "orpheus[graph]" in str(caught.value)


def test_degree_is_returned_alone_rather_than_a_stand_in_for_betweenness(
        corpus, without_networkx):
    # Some other number computed some other way would not mean the same thing,
    # and a report that quietly changed meaning is worse than one that is short.
    result = without_networkx.centrality(build(corpus))
    assert result["method"] == "degree_only"
    assert result["by_betweenness"] is None
    assert result["by_degree"]
    assert "orpheus[graph]" in result["note"]


def test_the_deterministic_half_is_untouched_by_its_absence(corpus, without_networkx):
    graph = build(corpus)
    assert len(components(graph)) == 2
    assert {b["entity_id"] for b in
            without_networkx.articulation_points(graph)} == {"ent_B", "ent_C"}
    assert [i["entity_id"] for i in isolates(graph)] == ["ent_G"]
    assert without_networkx.paths_between(corpus, "ent_A", "ent_D")["n_paths"] == 1


def test_the_whole_topology_still_runs(corpus, without_networkx):
    report = without_networkx.topology(corpus)
    assert report["counts"]["components"] == 2
    assert report["centrality_method"] == "degree_only"
    assert report["most_between"] == []
