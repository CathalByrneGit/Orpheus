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
- **Heuristic.** Communities, by label propagation, and the bridges defined in
  terms of them. Seeded so a run is reproducible, but the partition is one of
  many defensible ones and a different seed gives a different map. Labelled
  `heuristic` everywhere it is returned, because a cluster boundary presented
  as a fact is a claim the data does not support.

No third-party dependency, which rules out networkx and Louvain. That is a
constraint rather than a preference, but label propagation and Tarjan are a few
dozen lines each and the graphs here are corpus-sized, not web-sized.
"""

from __future__ import annotations

import random
from collections import defaultdict, deque
from typing import Any

from .store import Store

# Statuses that put a row out of the graph. A rejected relation is a known
# error; drawing it would let a mistake the review already caught shape the
# structure everything else is read from.
EXCLUDED = ("rejected",)

# Label propagation needs a seed to be reproducible. Fixed rather than exposed
# by default, so two people reading the same store see the same communities.
DEFAULT_SEED = 20260824


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

    rate = round(projected / total, 3) if total else None
    if not total:
        note = ("No relations have been extracted, so there is no graph to read. "
                "This is a corpus with no relation material, not a sparse one.")
    elif rate is not None and rate < 0.5:
        note = (f"Only {rate:.0%} of extracted relations reached the graph: the "
                f"rest have at least one endpoint with no entity page. The "
                f"structure below describes the linked part of the corpus, not "
                f"the corpus. Work the wiki queue before reading anything into "
                f"the shape.")
    else:
        note = (f"{rate:.0%} of extracted relations reached the graph. "
                f"{unlinked} mention(s) still have no page.")
    return {"n_edges_total": total, "n_edges_projected": projected,
            "projected_rate": rate, "n_unlinked_mentions": unlinked,
            "note": note}


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

def communities(graph: dict, seed: int = DEFAULT_SEED,
                max_rounds: int = 100) -> list[dict]:
    """Clusters, by label propagation.

    Every node takes the label most common among its neighbours until nothing
    changes. Cheap, needs no parameter, and finds the obvious groupings well.

    It is also **unstable**: ties are broken at random, so the partition depends
    on the seed, and on a dense graph it can collapse everything into one
    cluster. Seeded here so two readers of the same store see the same map, and
    returned marked `heuristic` so nobody mistakes a cluster boundary for a fact
    about the corpus. Where a claim needs to be defensible, use `components()`
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
                          seed: int = DEFAULT_SEED) -> list[dict]:
    """Which clusters are joined, and how weakly.

    The pairs with **no** shared edge are the point. Two clusters that never
    touch are two things the corpus knows about and has never connected, and
    that is a question worth asking rather than a gap to fill in silently.
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

    out = []
    identifiers = sorted(names)
    for index, left in enumerate(identifiers):
        for right in identifiers[index + 1:]:
            n = shared.get((left, right), 0)
            out.append({"communities": [left, right],
                        "labels": [names[left], names[right]],
                        "shared_edges": n,
                        "disconnected": n == 0,
                        "basis": "heuristic"})
    out.sort(key=lambda c: (c["shared_edges"], c["communities"]))
    return out


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
             reviewed_only: bool = False) -> dict:
    """The whole structural picture, with its own reliability attached.

    Ordered deliberately: `coverage` first, because every number after it is
    conditional on how much of the corpus reached the graph, and a reader who
    skips it will draw confident conclusions from a fraction of the evidence.
    """
    graph = build(store, reviewed_only=reviewed_only)
    found = communities(graph, seed=seed)
    islands = components(graph)
    stranded = isolates(graph)
    connections = community_connections(graph, found)

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
        },
        "components": islands,
        "articulation_points": articulation_points(graph),
        "communities": found,
        "bridges": bridges(graph, found),
        "community_connections": connections,
        "disconnected_pairs": [c for c in connections if c["disconnected"]],
        "isolates": stranded,
        "most_connected": sorted(
            ({"entity_id": e, "name": n["canonical_name"],
              "type_id": n["type_id"], "degree": n["degree"]}
             for e, n in graph["nodes"].items() if n["degree"]),
            key=lambda n: (-n["degree"], n["name"]))[:20],
        "note": ("Components, articulation points, degree and isolates are "
                 "deterministic. Communities and the bridges defined from them "
                 "are a seeded heuristic: reproducible, but one defensible "
                 "partition among several. Where a claim has to hold up, use a "
                 "component -- an island is a fact, a community is a reading."),
    }
