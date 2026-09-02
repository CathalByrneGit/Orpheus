"""The corpus as a network, projected up from mentions to entity pages.

`edges` records a relation between two *instances* — one clause in one
document. That is the right unit to extract and the wrong unit to think with:
"Ardmore supplies the HSE" asserted in four contracts is four unrelated rows,
and nothing in the store says the four are the same claim. This module joins
each endpoint through `entity_mentions` to the page it belongs to, so the
relation becomes one canonical edge between two pages with four sources behind
it.

**The graph is a projection, and its completeness is the wiki's.** An edge
exists only where *both* endpoints resolve to entity pages. A mention still
sitting in the queue contributes nothing, so a sparse graph may mean a sparse
corpus or an unbuilt wiki — and those are opposite findings. Every summary here
reports `coverage` for that reason: how much of the extracted relation material
actually reached the graph. A topology read without it is a confident picture of
whatever happens to have been linked.

Two kinds of structure, kept apart because they are not equally trustworthy:

- **Deterministic.** Components, articulation points, degree, isolates. No
  randomness, no threshold, no parameter. Run twice, get the same answer; these
  are facts about the graph.
- **Heuristic.** Communities — Louvain, or label propagation without networkx —
  and the bridges defined in terms of them. Seeded so a run is reproducible, but
  the partition is one of many defensible ones and a different seed gives a
  different map. Labelled `heuristic` everywhere it is returned, and carrying
  the `method` that produced it, because a cluster boundary presented as a fact
  is a claim the data does not support, and two reports run under different
  methods are not comparable.

The deterministic half needs no third-party package and never will: a store must
be readable, structurally, by a script with nothing installed. `networkx` is an
optional extra (`pip install 'orpheus[graph]'`) and buys two things worth the
import — Louvain in place of label propagation, and betweenness centrality.
Everything degrades with a message saying which ran, so a report never quietly
means something different from the last one.
"""

from __future__ import annotations

import random
from collections import defaultdict, deque
from typing import Any

from .store import Store
from .utils import OrpheusError

# Statuses that put a row out of the graph. A rejected relation is a known
# error; drawing it would let a mistake the review already caught shape the
# structure everything else is read from.
EXCLUDED = ("rejected",)

# Label propagation needs a seed to be reproducible. Fixed rather than exposed
# by default, so two people reading the same store see the same communities.
DEFAULT_SEED = 20260824

#: How many rows of a ranked list a structural report carries.
#:
#: Every capped list here is sorted so the first rows are the ones worth
#: reading -- islands by size, cut vertices by degree -- and every cap is
#: reported beside the total it was taken from. A page that renders "20 of
#: 1,225" is shorter *and* says more than one that renders 1,225 rows, because
#: the total is the finding and the rows are the illustration.
LIST_CAP = 20

#: Sources to sample when estimating betweenness.
#:
#: Brandes' algorithm is O(nm), and on a corpus-sized graph that is not a
#: constant factor: measured here, betweenness on 3,000 pages and 8,000 edges
#: took 51 seconds against 1.3 seconds for every other computation on the page
#: combined. Sampling 100 sources brings it to 1.7s -- the same order as
#: everything else -- and the result is approximate, which `method` says.
#:
#: Exact is still available and still correct; it is one request away rather
#: than the thing everybody waits for by default.
DEFAULT_BETWEENNESS_SAMPLE = 100


# ---------------------------------------------------------------------------
# Projecting mentions up to pages
# ---------------------------------------------------------------------------

def canonical_edges(store: Store, link_type_id: str | None = None,
                    reviewed_only: bool = False) -> list[dict]:
    """One row per `(from_page, link_type, to_page)`, with every source behind it.

    The join that makes the rest of this module possible, and the answer to a
    question the store could not previously ask: *how many documents say this?*

    `support` keeps each contributing edge whole — its document, its evidence,
    its confidence, its review status. Collapsing those into a count would lose
    the citations, and a relation nobody can trace back is not evidence.
    """
    marks = ",".join("?" * len(EXCLUDED))
    clauses = [
        f"e.status NOT IN ({marks})",
        "fm.unlinked_at IS NULL", "tm.unlinked_at IS NULL",
        # Follow a merge rather than dropping the edge: a link made before two
        # pages were joined still points at a real page.
        "fe.entity_id != te.entity_id",
    ]
    params: list[Any] = list(EXCLUDED)
    if link_type_id:
        clauses.append("e.link_type_id = ?")
        params.append(link_type_id)
    if reviewed_only:
        clauses.append("e.status IN ('confirmed', 'amended')")

    rows = store.query(
        "SELECT e.edge_id, e.link_type_id, e.document_id, e.evidence, "
        "       e.confidence, e.status, e.source, "
        "       fe.entity_id AS from_entity_id, fe.canonical_name AS from_name, "
        "       fe.type_id AS from_type, "
        "       te.entity_id AS to_entity_id, te.canonical_name AS to_name, "
        "       te.type_id AS to_type, d.filename "
        "FROM edges e "
        "JOIN entity_mentions fm ON fm.instance_id = e.from_instance_id "
        "JOIN entity_mentions tm ON tm.instance_id = e.to_instance_id "
        "JOIN entities fe0 ON fe0.entity_id = fm.entity_id "
        "JOIN entities te0 ON te0.entity_id = tm.entity_id "
        "JOIN entities fe ON fe.entity_id = COALESCE(fe0.merged_into, fe0.entity_id) "
        "JOIN entities te ON te.entity_id = COALESCE(te0.merged_into, te0.entity_id) "
        "LEFT JOIN documents d ON d.document_id = e.document_id "
        f"WHERE {' AND '.join(clauses)}", tuple(params))

    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (row["from_entity_id"], row["link_type_id"], row["to_entity_id"])
        edge = grouped.setdefault(key, {
            "from_entity_id": row["from_entity_id"],
            "from_name": row["from_name"], "from_type": row["from_type"],
            "to_entity_id": row["to_entity_id"],
            "to_name": row["to_name"], "to_type": row["to_type"],
            "link_type_id": row["link_type_id"],
            "support": [],
        })
        edge["support"].append({
            "edge_id": row["edge_id"], "document_id": row["document_id"],
            "filename": row["filename"], "evidence": row["evidence"],
            "confidence": row["confidence"], "status": row["status"],
            "source": row["source"],
        })

    out = []
    for edge in grouped.values():
        support = edge["support"]
        documents = {s["document_id"] for s in support if s["document_id"]}
        edge["n_sources"] = len(support)
        edge["n_documents"] = len(documents)
        edge["documents"] = sorted(documents)
        edge["n_confirmed"] = sum(1 for s in support
                                  if s["status"] in ("confirmed", "amended"))
        # The highest any single source claimed. Deliberately not combined
        # across sources -- see `corroboration.py` for why adding them up would
        # manufacture certainty out of documents that copy each other.
        edge["max_confidence"] = max(s["confidence"] for s in support)
        out.append(edge)
    out.sort(key=lambda e: (-e["n_documents"], -e["n_sources"], e["from_name"]))
    return out


def coverage(store: Store) -> dict:
    """How much of the extracted relation material reached the graph.

    Without this a topology is a confident picture of whatever happened to have
    been linked, and there is no way to tell a thin corpus from an unbuilt wiki.
    """
    marks = ",".join("?" * len(EXCLUDED))
    total = store.scalar(
        f"SELECT COUNT(*) FROM edges WHERE status NOT IN ({marks})",
        EXCLUDED) or 0
    projected = store.scalar(
        "SELECT COUNT(*) FROM edges e "
        "JOIN entity_mentions fm ON fm.instance_id = e.from_instance_id "
        "  AND fm.unlinked_at IS NULL "
        "JOIN entity_mentions tm ON tm.instance_id = e.to_instance_id "
        "  AND tm.unlinked_at IS NULL "
        f"WHERE e.status NOT IN ({marks})", EXCLUDED) or 0
    unlinked = store.scalar(
        "SELECT COUNT(*) FROM instance_index i "
        "LEFT JOIN entity_mentions m ON m.instance_id = i.instance_id "
        "  AND m.unlinked_at IS NULL WHERE m.instance_id IS NULL") or 0

    # An edge can only ever reach the graph if both its endpoints are the kind
    # of thing that gets a page. A relation from a contract to one of its
    # clauses is not a wiki backlog and never becomes one: no amount of review
    # turns a clause into an entity. Counting it as missing work sends a reader
    # to a queue that cannot move the number.
    nameable = _projectable_edges(store, total)
    awaiting = max(nameable - projected, 0)
    structural = max(total - nameable, 0)

    rate = round(projected / total, 3) if total else None
    if not total:
        note = ("No relations have been extracted, so there is no graph to read. "
                "This is a corpus with no relation material, not a sparse one.")
    elif nameable and projected < nameable:
        note = (f"{rate:.0%} of extracted relations reached the graph. "
                f"{awaiting} more join a named thing to a named thing and wait "
                f"on the wiki queue; {structural} link to a clause, a date or a "
                f"document, which never gets a page. Only the first number is "
                f"work. Read the shape as the linked part of the corpus.")
    elif structural:
        note = (f"{rate:.0%} of extracted relations reached the graph. Every "
                f"relation between two named things is drawn; the other "
                f"{structural} link to a clause, a date or a document, which "
                f"never gets a page. This is the shape of the corpus, not an "
                f"unbuilt wiki.")
    else:
        note = (f"{rate:.0%} of extracted relations reached the graph. "
                f"{unlinked} mention(s) still have no page.")
    return {"n_edges_total": total, "n_edges_projected": projected,
            "n_edges_projectable": nameable,
            "n_edges_awaiting_pages": awaiting,
            "n_edges_structural": structural,
            "projected_rate": rate, "n_unlinked_mentions": unlinked,
            "note": note}


def _projectable_edges(store: Store, total: int) -> int:
    """Edges whose endpoints are both the kind of thing that gets a page.

    Which types those are is the bundle's business, not this module's: a
    bundle describing planning applications resolves applicants, not
    companies. Types implementing `Named` are the ones the wiki is built from.
    """
    from . import bundle as bundle_mod

    active = bundle_mod.active(store)
    if active is None:
        return total
    named = list(bundle_mod.implementing_types(active, "Named"))
    if not named:
        return 0
    marks = ",".join("?" * len(EXCLUDED))
    slots = ",".join("?" * len(named))
    return store.scalar(
        "SELECT COUNT(*) FROM edges e "
        "JOIN instance_index fi ON fi.instance_id = e.from_instance_id "
        "JOIN instance_index ti ON ti.instance_id = e.to_instance_id "
        f"WHERE e.status NOT IN ({marks}) "
        f"AND fi.type_id IN ({slots}) AND ti.type_id IN ({slots})",
        tuple(EXCLUDED) + tuple(named) + tuple(named)) or 0


def build(store: Store, reviewed_only: bool = False) -> dict:
    """Nodes and adjacency, ready for the structural functions below."""
    edges = canonical_edges(store, reviewed_only=reviewed_only)
    nodes: dict[str, dict] = {}
    for row in store.query(
            "SELECT entity_id, canonical_name, type_id, status FROM entities "
            "WHERE merged_into IS NULL"):
        nodes[row["entity_id"]] = {**row, "degree": 0}

    adjacency: dict[str, set] = defaultdict(set)
    for edge in edges:
        source, target = edge["from_entity_id"], edge["to_entity_id"]
        if source not in nodes or target not in nodes:
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)
    for entity_id, neighbours in adjacency.items():
        if entity_id in nodes:
            nodes[entity_id]["degree"] = len(neighbours)

    return {"nodes": nodes, "edges": edges,
            "adjacency": {k: set(v) for k, v in adjacency.items()}}


# ---------------------------------------------------------------------------
# Deterministic structure
# ---------------------------------------------------------------------------

def components(graph: dict) -> list[dict]:
    """Islands: sets of pages reachable from each other and from nothing else.

    The most useful structural finding here and the only one that needs no
    parameter. Two islands mean the corpus knows about two worlds and has not
    connected them — which is either a fact about the domain or a gap in the
    linking, and worth looking at either way.
    """
    adjacency, nodes = graph["adjacency"], graph["nodes"]
    seen: set[str] = set()
    found = []
    for entity_id in sorted(nodes):
        if entity_id in seen or not adjacency.get(entity_id):
            continue
        members, queue = set(), deque([entity_id])
        while queue:
            current = queue.popleft()
            if current in members:
                continue
            members.add(current)
            queue.extend(n for n in adjacency.get(current, ()) if n not in members)
        seen |= members
        found.append(_describe(graph, members))
    found.sort(key=lambda c: -c["n_entities"])
    for index, component in enumerate(found, 1):
        component["component"] = index
    return found


def articulation_points(graph: dict) -> list[dict]:
    """Pages whose removal would split the graph.

    A *structural* bridge, in the graph-theory sense, and deterministic — no
    community partition needed, so no seed and no judgement call. If one entity
    is the only thing joining two halves of a corpus, that is worth knowing
    before it turns out to rest on a single unconfirmed link.

    Tarjan's algorithm, iteratively: recursion depth would follow the longest
    path in the corpus, which is not a number this can bound.
    """
    adjacency, nodes = graph["adjacency"], graph["nodes"]
    visited: set[str] = set()
    discovery: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    found: set[str] = set()
    counter = 0

    for root in sorted(adjacency):
        if root in visited:
            continue
        root_children = 0
        stack: list[tuple[str, Any]] = [(root, iter(sorted(adjacency[root])))]
        visited.add(root)
        discovery[root] = low[root] = counter
        counter += 1
        parent[root] = None

        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for neighbour in neighbours:
                if neighbour not in visited:
                    parent[neighbour] = node
                    if node == root:
                        root_children += 1
                    visited.add(neighbour)
                    discovery[neighbour] = low[neighbour] = counter
                    counter += 1
                    stack.append((neighbour, iter(sorted(adjacency.get(neighbour, ())))))
                    advanced = True
                    break
                if neighbour != parent.get(node):
                    low[node] = min(low[node], discovery[neighbour])
            if advanced:
                continue
            stack.pop()
            if stack:
                above = stack[-1][0]
                low[above] = min(low[above], low[node])
                # The test that makes `above` a cut vertex: nothing below
                # `node` reaches back past it.
                if above != root and low[node] >= discovery[above]:
                    found.add(above)
        # The root is a cut vertex only if it has more than one subtree.
        if root_children > 1:
            found.add(root)

    return sorted(
        ({"entity_id": entity_id,
          "name": nodes.get(entity_id, {}).get("canonical_name", entity_id),
          "type_id": nodes.get(entity_id, {}).get("type_id"),
          "degree": nodes.get(entity_id, {}).get("degree", 0)}
         for entity_id in found),
        key=lambda b: (-b["degree"], b["name"]))


def isolates(graph: dict) -> list[dict]:
    """Pages with no relation to anything.

    Not necessarily a problem — a page can be a well-cited fact about one
    company that no clause relates to anything. It is a problem when there are
    many, because it usually means relations were never extracted rather than
    that the corpus has none.
    """
    nodes, adjacency = graph["nodes"], graph["adjacency"]
    return [{"entity_id": entity_id, "name": node["canonical_name"],
             "type_id": node["type_id"], "status": node["status"]}
            for entity_id, node in sorted(nodes.items())
            if not adjacency.get(entity_id)]


# ---------------------------------------------------------------------------
# Heuristic structure
# ---------------------------------------------------------------------------

def _networkx():
    """networkx if it is installed, else None.

    Checked at the call rather than at import, so a store opened by a script
    with nothing installed still reads structurally -- which is the guarantee
    the whole deterministic half exists to keep.
    """
    try:
        import networkx  # noqa: PLC0415
    except ImportError:
        return None
    return networkx


def to_networkx(graph: dict):
    """The graph as an `nx.Graph`, for anything this module does not do.

    An escape hatch on purpose. Wrapping networkx function by function would be
    a worse library than networkx, so the useful few are wrapped below and
    everything else is one call away.
    """
    nx = _networkx()
    if nx is None:
        raise OrpheusError(
            "networkx is not installed. `pip install 'orpheus[graph]'`.")
    built = nx.Graph()
    for entity_id, node in graph["nodes"].items():
        built.add_node(entity_id, **{k: v for k, v in node.items()
                                     if k != "entity_id"})
    for edge in graph["edges"]:
        built.add_edge(edge["from_entity_id"], edge["to_entity_id"],
                       link_type_id=edge["link_type_id"],
                       n_documents=edge["n_documents"],
                       n_confirmed=edge["n_confirmed"])
    return built


def centrality(graph: dict, k: int | None = None) -> dict:
    """Which pages carry the traffic between others.

    Degree answers "who appears in the most relations"; betweenness answers
    "who sits on the most paths between other pages", and for a contracts
    corpus the second is much closer to structurally important. An intermediary
    with three links can matter far more than a buyer with twenty.

    Needs networkx. Without it the degree ranking comes back on its own and says
    so, rather than some other number standing in for one that would not mean
    the same thing.
    """
    nodes = graph["nodes"]
    ranked = sorted(
        ({"entity_id": e, "name": n["canonical_name"], "type_id": n["type_id"],
          "degree": n["degree"]} for e, n in nodes.items() if n["degree"]),
        key=lambda n: (-n["degree"], n["name"]))

    nx = _networkx()
    if nx is None:
        return {"method": "degree_only", "by_degree": ranked,
                "by_betweenness": None,
                "note": ("Betweenness needs networkx: `pip install "
                         "'orpheus[graph]'`. Degree is shown on its own, and "
                         "answers a different question -- how many relations a "
                         "page is in, not how much sits on paths through it.")}

    built = to_networkx(graph)
    # Brandes' algorithm is O(nm); `k` samples that many sources instead, which
    # is what keeps this usable on a corpus-sized graph. A sampled result is
    # approximate and the method name says so.
    scores = nx.betweenness_centrality(built, k=k, seed=DEFAULT_SEED,
                                       normalized=True)
    by_betweenness = sorted(
        ({"entity_id": e, "name": nodes[e]["canonical_name"],
          "type_id": nodes[e]["type_id"], "degree": nodes[e]["degree"],
          "betweenness": round(score, 4)}
         for e, score in scores.items() if nodes.get(e)),
        key=lambda n: (-n["betweenness"], n["name"]))
    return {
        "method": "betweenness_sampled" if k else "betweenness_exact",
        "by_degree": ranked,
        "by_betweenness": [n for n in by_betweenness if n["betweenness"] > 0],
        "note": ("Betweenness is the share of shortest paths between other "
                 "pages that run through this one."
                 + (f" Approximate: sampled from {k} sources." if k else "")),
    }


def communities(graph: dict, seed: int = DEFAULT_SEED,
                max_rounds: int = 100, method: str = "auto") -> list[dict]:
    """Clusters: Louvain where networkx is installed, label propagation otherwise.

    Both are heuristics and both say so. The difference is how much. Louvain
    optimises modularity and lands in much the same place run to run; label
    propagation breaks ties at random and can collapse a dense graph into one
    cluster. `method` is recorded on every row, so two reports are never
    silently comparing different things.

    Louvain also reports `modularity`, which is the number that says whether the
    partition means anything at all: near zero, the clusters are no better than
    chance and should not be read as structure.
    """
    if method not in ("auto", "louvain", "label_propagation"):
        raise OrpheusError(f"Unknown community method {method!r}.")
    nx = _networkx() if method in ("auto", "louvain") else None
    if method == "louvain" and nx is None:
        raise OrpheusError(
            "Louvain needs networkx. `pip install 'orpheus[graph]'`, or pass "
            "method='label_propagation'.")
    if nx is not None:
        return _louvain(graph, nx, seed)
    return _label_propagation(graph, seed, max_rounds)


def _louvain(graph: dict, nx, seed: int) -> list[dict]:
    built = to_networkx(graph)
    built.remove_nodes_from([n for n in list(built) if built.degree(n) == 0])
    if not built:
        return []
    partition = nx.community.louvain_communities(built, seed=seed)
    score = round(nx.community.modularity(built, partition), 4)

    found = [_describe(graph, set(members)) for members in partition if members]
    found.sort(key=lambda c: -c["n_entities"])
    for index, community in enumerate(found, 1):
        community["community"] = index
        community["basis"] = "heuristic"
        community["method"] = "louvain"
        community["modularity"] = score
        # Below about 0.3 a partition is not saying much, and a reader who has
        # not met modularity before should not have to know that.
        community["modularity_note"] = (
            "Strong structure." if score >= 0.3 else
            "Weak: the clusters are barely better than chance, so read them as "
            "a suggestion rather than as structure.")
    return found


def _label_propagation(graph: dict, seed: int, max_rounds: int) -> list[dict]:
    """The fallback, so the core still clusters with nothing installed.

    Every node takes the label most common among its neighbours until nothing
    changes. Cheap, needs no parameter, and finds the obvious groupings well.

    It is also **unstable**: ties are broken at random, so the partition depends
    on the seed, and on a dense graph it can collapse everything into one
    cluster. Seeded here so two readers of the same store see the same map, and
    marked `heuristic` so nobody mistakes a cluster boundary for a fact about
    the corpus. Where a claim needs to be defensible, use `components()`
    instead — an island is a fact; a community is a reading.
    """
    adjacency, nodes = graph["adjacency"], graph["nodes"]
    labels = {entity_id: entity_id for entity_id in nodes}
    order = sorted(adjacency)
    rng = random.Random(seed)

    for _ in range(max_rounds):
        rng.shuffle(order)
        changed = False
        for entity_id in order:
            neighbours = adjacency.get(entity_id)
            if not neighbours:
                continue
            tally: dict[str, int] = defaultdict(int)
            for neighbour in neighbours:
                tally[labels[neighbour]] += 1
            best = max(tally.values())
            candidates = sorted(label for label, n in tally.items() if n == best)
            chosen = rng.choice(candidates)
            if chosen != labels[entity_id]:
                labels[entity_id] = chosen
                changed = True
        if not changed:
            break

    grouped: dict[str, set] = defaultdict(set)
    for entity_id, label in labels.items():
        if adjacency.get(entity_id):
            grouped[label].add(entity_id)

    found = [_describe(graph, members) for members in grouped.values()]
    found.sort(key=lambda c: -c["n_entities"])
    for index, community in enumerate(found, 1):
        community["community"] = index
        community["basis"] = "heuristic"
        community["method"] = "label_propagation"
    return found


def bridges(graph: dict, found: list[dict] | None = None,
            seed: int = DEFAULT_SEED) -> list[dict]:
    """Pages joining two or more communities.

    A softer notion than `articulation_points()`: removing one of these does not
    necessarily disconnect anything, it just carries most of the traffic between
    clusters. Inherits the community partition's instability, so it is marked
    the same way.
    """
    found = communities(graph, seed=seed) if found is None else found
    membership = {entity_id: c["community"]
                  for c in found for entity_id in c["members"]}
    adjacency, nodes = graph["adjacency"], graph["nodes"]

    out = []
    for entity_id, neighbours in adjacency.items():
        home = membership.get(entity_id)
        if home is None:
            continue
        elsewhere = {membership[n] for n in neighbours
                     if membership.get(n) not in (None, home)}
        if not elsewhere:
            continue
        node = nodes.get(entity_id, {})
        out.append({
            "entity_id": entity_id,
            "name": node.get("canonical_name", entity_id),
            "type_id": node.get("type_id"),
            "community": home,
            "connects": sorted({home} | elsewhere),
            "cross_community_edges": sum(
                1 for n in neighbours
                if membership.get(n) not in (None, home)),
            "basis": "heuristic",
        })
    out.sort(key=lambda b: (-b["cross_community_edges"], b["name"]))
    return out


def community_connections(graph: dict, found: list[dict] | None = None,
                          seed: int = DEFAULT_SEED,
                          limit: int | None = None) -> list[dict]:
    """Which clusters are joined, and how weakly.

    The pairs with **no** shared edge are the point. Two clusters that never
    touch are two things the corpus knows about and has never connected, and
    that is a question worth asking rather than a gap to fill in silently.

    `limit` caps the *disconnected* rows only, and exists because there are
    quadratically many of them: a hundred clusters make 4,950 pairs, and in a
    corpus that has not been linked up almost all of them never touch. The
    pairs that *do* share an edge are as sparse as the graph and are always
    returned in full. Use `n_community_pairs` for the true total.
    """
    found = communities(graph, seed=seed) if found is None else found
    membership = {entity_id: c["community"]
                  for c in found for entity_id in c["members"]}
    names = {c["community"]: c["label"] for c in found}

    shared: dict[tuple, int] = defaultdict(int)
    for edge in graph["edges"]:
        left = membership.get(edge["from_entity_id"])
        right = membership.get(edge["to_entity_id"])
        if left is None or right is None or left == right:
            continue
        shared[tuple(sorted((left, right)))] += 1

    def row(left, right, n):
        return {"communities": [left, right],
                "labels": [names[left], names[right]],
                "shared_edges": n,
                "disconnected": n == 0,
                "basis": "heuristic"}

    connected = [row(left, right, n) for (left, right), n in shared.items() if n]
    connected.sort(key=lambda c: (c["shared_edges"], c["communities"]))

    # Generated rather than filtered out of the full cross product, and
    # stopped at `limit`. Building 4,950 dictionaries to show twenty of them is
    # the cost this avoids.
    apart: list[dict] = []
    identifiers = sorted(names)
    for index, left in enumerate(identifiers):
        if limit is not None and len(apart) >= limit:
            break
        for right in identifiers[index + 1:]:
            if not shared.get((left, right), 0):
                apart.append(row(left, right, 0))
                if limit is not None and len(apart) >= limit:
                    break
    apart.sort(key=lambda c: c["communities"])
    # Disconnected first, which is the order the old full sort produced and the
    # order a reader wants: the pairs that never touch are the finding.
    return apart + connected


def n_community_pairs(found: list[dict]) -> int:
    """How many cluster pairs exist at all, computed rather than counted.

    The denominator for a capped list of pairs that never touch. Materialising
    them to count them is the thing the cap exists to avoid.
    """
    n = len(found)
    return n * (n - 1) // 2


def _describe(graph: dict, members: set) -> dict:
    """Summarise a set of pages: what is in it, and what it is mostly about."""
    nodes = graph["nodes"]
    types: dict[str, int] = defaultdict(int)
    for entity_id in members:
        types[nodes.get(entity_id, {}).get("type_id") or "unknown"] += 1
    ranked = sorted(members,
                    key=lambda e: (-nodes.get(e, {}).get("degree", 0),
                                   nodes.get(e, {}).get("canonical_name", e)))
    top = [{"entity_id": e,
            "name": nodes.get(e, {}).get("canonical_name", e),
            "degree": nodes.get(e, {}).get("degree", 0)} for e in ranked[:5]]
    # Named after its most connected page rather than numbered. A label read
    # from the data is checkable; "Community 3" tells a reader nothing and
    # invites them to invent a meaning for it.
    label = top[0]["name"] if top else "empty"
    return {"label": label, "members": sorted(members),
            "n_entities": len(members),
            "entity_types": dict(sorted(types.items())),
            "top_entities": top}


# ---------------------------------------------------------------------------
# How two pages are connected
# ---------------------------------------------------------------------------

def paths_between(store: Store, from_entity_id: str, to_entity_id: str,
                  max_paths: int = 5, max_length: int = 6,
                  graph: dict | None = None) -> dict:
    """Every short chain joining two pages, with the evidence for each hop.

    The question a corpus is actually asked — *how is this supplier connected to
    that one?* — and the one a list of entities cannot answer.

    A path is a chain of claims, so it is only as good as its weakest hop. One
    running through an unconfirmed machine guess is not the same finding as one
    where a person has checked every link, and a chain reported without that
    distinction invites exactly the conclusion the store exists to prevent. So
    every path carries `weakest`: the least-reviewed hop on it, and whether the
    whole chain has been vouched for.
    """
    graph = graph or build(store)
    nodes, adjacency = graph["nodes"], graph["adjacency"]
    for entity_id in (from_entity_id, to_entity_id):
        if entity_id not in nodes:
            from .utils import NotFound
            raise NotFound(f"No entity {entity_id!r} in the graph.")
    if from_entity_id == to_entity_id:
        from .utils import OrpheusError
        raise OrpheusError("A page is not connected to itself.")

    # Edges both ways round, so a chain can traverse a relation against its
    # direction: "A subcontracts to B" connects the two whichever end you start.
    between: dict[tuple, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        pair = (edge["from_entity_id"], edge["to_entity_id"])
        between[pair].append(edge)
        between[(pair[1], pair[0])].append(edge)

    found: list[list[str]] = []
    # Breadth-first, so the shortest chains come out first: a four-hop
    # connection is a much weaker claim than a one-hop one and should not be
    # the first thing a reader sees.
    queue = deque([[from_entity_id]])
    while queue and len(found) < max_paths:
        path = queue.popleft()
        if len(path) > max_length:
            continue
        for neighbour in sorted(adjacency.get(path[-1], ())):
            if neighbour in path:
                continue
            extended = path + [neighbour]
            if neighbour == to_entity_id:
                found.append(extended)
                if len(found) >= max_paths:
                    break
            else:
                queue.append(extended)

    described = [_describe_path(path, nodes, between) for path in found]
    if not described:
        note = (f"No chain of {max_length} steps or fewer joins these two. They "
                f"may be in different islands, or the relation that joins them "
                f"may not have been extracted.")
    else:
        vouched = [p for p in described if p["confirmed_throughout"]]
        note = (f"{len(described)} chain(s), shortest {described[0]['n_hops']} "
                f"hop(s). {len(vouched)} vouched for at every hop; the rest "
                f"pass through at least one link nobody has checked.")
    return {"from": nodes[from_entity_id], "to": nodes[to_entity_id],
            "paths": described, "n_paths": len(described), "note": note}


def _describe_path(path: list[str], nodes: dict,
                   between: dict[tuple, list[dict]]) -> dict:
    """One chain, hop by hop, each hop carrying what it rests on."""
    hops = []
    for left, right in zip(path, path[1:]):
        candidates = between.get((left, right), [])
        # The best-supported relation joining this pair, when several do.
        edge = max(candidates,
                   key=lambda e: (e["n_confirmed"], e["n_documents"]),
                   default=None)
        hops.append({
            "from_entity_id": left, "from_name": nodes[left]["canonical_name"],
            "to_entity_id": right, "to_name": nodes[right]["canonical_name"],
            "link_type_id": edge["link_type_id"] if edge else None,
            "n_documents": edge["n_documents"] if edge else 0,
            "n_confirmed": edge["n_confirmed"] if edge else 0,
            "documents": edge["documents"] if edge else [],
        })
    confirmed = all(h["n_confirmed"] for h in hops) if hops else False
    weakest = min(hops, key=lambda h: (h["n_confirmed"], h["n_documents"]),
                  default=None)
    return {
        "entities": [{"entity_id": e, "name": nodes[e]["canonical_name"],
                      "type_id": nodes[e]["type_id"]} for e in path],
        "hops": hops, "n_hops": len(hops),
        "confirmed_throughout": confirmed,
        # Named rather than left to be worked out: a chain is as good as its
        # worst link and a reader should not have to scan for it.
        "weakest": weakest,
    }


# ---------------------------------------------------------------------------
# Reading one neighbourhood
# ---------------------------------------------------------------------------

def neighbourhood(store: Store, entity_id: str, depth: int = 1,
                  graph: dict | None = None) -> dict:
    """One page and what surrounds it, out to `depth` hops.

    What an agent or a reviewer actually wants: the whole graph is too much to
    hold, and one page on its own has no context.
    """
    graph = graph or build(store)
    nodes, adjacency = graph["nodes"], graph["adjacency"]
    if entity_id not in nodes:
        from .utils import NotFound
        raise NotFound(f"No entity {entity_id!r} in the graph.")

    reached = {entity_id: 0}
    frontier = {entity_id}
    for hop in range(1, max(0, depth) + 1):
        following = set()
        for current in frontier:
            for neighbour in adjacency.get(current, ()):
                if neighbour not in reached:
                    reached[neighbour] = hop
                    following.add(neighbour)
        frontier = following
        if not frontier:
            break

    edges = [e for e in graph["edges"]
             if e["from_entity_id"] in reached and e["to_entity_id"] in reached]
    return {
        "entity": nodes[entity_id],
        "depth": depth,
        "nodes": [{**nodes[e], "hops": hop} for e, hop in
                  sorted(reached.items(), key=lambda kv: (kv[1], kv[0]))],
        "edges": edges,
        "n_nodes": len(reached), "n_edges": len(edges),
    }


# ---------------------------------------------------------------------------
# Everything, for an agent or a report
# ---------------------------------------------------------------------------

def topology(store: Store, seed: int = DEFAULT_SEED,
             reviewed_only: bool = False, list_cap: int | None = LIST_CAP,
             exact_betweenness: bool = False) -> dict:
    """The whole structural picture, with its own reliability attached.

    Ordered deliberately: `coverage` first, because every number after it is
    conditional on how much of the corpus reached the graph, and a reader who
    skips it will draw confident conclusions from a fraction of the evidence.

    **Every list here is capped and every cap reports its total.** Four of them
    have no natural ceiling -- islands, cut vertices, isolates, and pairs of
    clusters that never touch, the last of which is quadratic in the number of
    clusters. Rendering all of them was never more informative than rendering
    the first twenty and the count: the total is the finding and the rows are
    the illustration. Pass `list_cap=None` for everything, which is what a
    script wants and a page does not.

    Betweenness is sampled unless `exact_betweenness`, for the reason
    `DEFAULT_BETWEENNESS_SAMPLE` gives: it is the only computation here that
    does not finish in about a second on a corpus-sized graph.
    """
    graph = build(store, reviewed_only=reviewed_only)
    found = communities(graph, seed=seed)
    # `k` must not exceed the node count, and on a small graph sampling buys
    # nothing anyway -- so the exact answer is what a small corpus gets, and
    # `method` says which ran rather than leaving it to be inferred.
    sample = (None if exact_betweenness
              or len(graph["nodes"]) <= DEFAULT_BETWEENNESS_SAMPLE
              else DEFAULT_BETWEENNESS_SAMPLE)
    important = centrality(graph, k=sample)
    islands = components(graph)
    stranded = isolates(graph)
    cut_points = articulation_points(graph)
    connections = community_connections(graph, found, limit=list_cap)
    apart = [c for c in connections if c["disconnected"]]
    n_pairs = n_community_pairs(found)
    n_touching = sum(1 for c in connections if not c["disconnected"])

    def capped(rows):
        return rows if list_cap is None else rows[:list_cap]

    linked = [n for n in graph["nodes"].values() if n["degree"]]
    return {
        "coverage": coverage(store),
        "counts": {
            "entities": len(graph["nodes"]),
            "connected_entities": len(linked),
            "isolated_entities": len(stranded),
            "canonical_edges": len(graph["edges"]),
            "components": len(islands),
            "communities": len(found),
            "articulation_points": len(cut_points),
            # Computed, not counted: the pairs that never touch are generated
            # only up to the cap, so this is the arithmetic total minus the
            # sparse set that do share an edge.
            "disconnected_pairs": n_pairs - n_touching,
            "community_pairs": n_pairs,
        },
        # What each list was cut to, so a reader can tell a short list from a
        # truncated one without counting rows.
        "list_cap": list_cap,
        "components": capped(islands),
        "articulation_points": capped(cut_points),
        "communities": found,
        "bridges": bridges(graph, found),
        "community_connections": connections,
        "disconnected_pairs": apart,
        "isolates": capped(stranded),
        "most_connected": important["by_degree"][:20],
        # A different question from degree: who sits on the paths between other
        # pages. An intermediary with three links can matter more than a buyer
        # with twenty, and only this ranking shows it.
        "most_between": (important["by_betweenness"] or [])[:20],
        "centrality_method": important["method"],
        "centrality_note": important["note"],
        "note": ("Components, articulation points, degree and isolates are "
                 "deterministic. Communities and the bridges defined from them "
                 "are a seeded heuristic: reproducible, but one defensible "
                 "partition among several. Where a claim has to hold up, use a "
                 "component -- an island is a fact, a community is a reading."),
    }
