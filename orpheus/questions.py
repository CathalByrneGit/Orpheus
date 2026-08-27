"""Questions the shape of a corpus raises.

The network says which pages are joined and how. This asks the next thing: are
any of those joins worth someone looking at? A supplier reachable from a
competitor only through one shared subcontractor, a party appearing on two sides
of the same agreement, a chain of subcontracts that comes back to where it
started — none of that is wrongdoing, and this module is careful never to say it
is.

**Everything here is a question, not a finding.** A shared intermediary usually
has a dull explanation: a small market, a specialist supplier everybody uses, a
parent company. What the corpus can honestly say is *these two are closer than
they look, here is the chain, and here is how much of it anybody has checked*.
Saying more than that from graph shape alone would be an accusation drawn from a
join, in exactly the setting where an accusation does damage.

So every question carries three things, and the third is what makes it usable:

- **the entities involved**, each a page with its own sources
- **the chain**, hop by hop, with the documents behind each one
- **how much of it has been reviewed.** A question resting entirely on
  unconfirmed machine guesses is not a lead. `confirmed_throughout` says so, and
  the ordering puts checked chains first — because the thing you least want is
  somebody acting on a chain that turned out to be two extraction errors.

The checks are about graph shape and property values, not about procurement. A
bundle from another domain gets the same three questions about its own types.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import bundle as bundle_mod
from . import graph as graph_mod
from .store import Store

# Where a bundle names the property that says what part something played in a
# document. Falls back to `role`, which is what the shipped contract bundle
# calls it; a bundle for another domain names its own.
ROLE_PROPERTY_HINT = "roleProperty"
DEFAULT_ROLE_PROPERTY = "role"

# A chain longer than this is not a question anybody can act on. Everything is
# connected to everything at six hops.
MAX_CHAIN = 3


def _role_property(bundle: dict | None) -> str:
    extensions = ((bundle or {}).get("extensions", {}) or {}).get("orpheus", {}) or {}
    return extensions.get(ROLE_PROPERTY_HINT) or DEFAULT_ROLE_PROPERTY


def _question(kind: str, summary: str, entities: list[dict], chain: list[dict],
              asks: str, confirmed: bool, documents: list[str]) -> dict:
    return {
        "kind": kind, "summary": summary, "entities": entities,
        "chain": chain, "documents": sorted(set(documents)),
        # Not a severity. A question whose every hop a person has checked is
        # worth someone's time; one built from unreviewed guesses is worth
        # checking the extraction first, and conflating the two is how a
        # reviewer ends up acting on two mistakes.
        "confirmed_throughout": confirmed,
        "asks": asks,
    }


# ---------------------------------------------------------------------------
# One party, two parts in the same document
# ---------------------------------------------------------------------------

def two_parts_in_one_document(store: Store, bundle: dict | None = None,
                              limit: int = 50) -> list[dict]:
    """One entity recorded under two different roles in a single document.

    The least ambiguous of the three, because it needs no graph at all: the
    same page, the same document, two parts. Often a genuine dual capacity — a
    body that both buys and supplies is ordinary — and occasionally an
    extraction error, which is why the mentions are cited rather than summarised.
    """
    bundle = bundle or bundle_mod.active(store)
    role_property = _role_property(bundle)
    out = []
    for type_id, table in _named_tables(store, bundle):
        columns = {row["name"] for row in store.query(f'PRAGMA table_info("{table}")')}
        if role_property not in columns:
            continue
        rows = store.query(
            f'SELECT m.entity_id, x.document_id, x."{role_property}" AS part, '
            f'       x.instance_id, x.status, e.canonical_name, d.filename '
            f'FROM "{table}" x '
            f"JOIN entity_mentions m ON m.instance_id = x.instance_id "
            f"  AND m.unlinked_at IS NULL "
            f"JOIN entities e ON e.entity_id = m.entity_id "
            f"LEFT JOIN documents d ON d.document_id = x.document_id "
            f'WHERE x."{role_property}" IS NOT NULL AND x."{role_property}" != "" '
            f"AND x.status != 'rejected'")

        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[(row["entity_id"], row["document_id"])].append(row)
        for (entity_id, document_id), mentions in grouped.items():
            parts = {m["part"] for m in mentions}
            if len(parts) < 2:
                continue
            name = mentions[0]["canonical_name"]
            filename = mentions[0]["filename"] or document_id
            out.append(_question(
                "two_parts_in_one_document",
                f"{name} is recorded as {' and '.join(sorted(parts))} in {filename}",
                entities=[{"entity_id": entity_id, "name": name,
                           "type_id": type_id}],
                chain=[{"instance_id": m["instance_id"], "part": m["part"],
                        "document_id": document_id, "filename": filename,
                        "status": m["status"]} for m in mentions],
                asks=("Is this one party in two capacities, which is ordinary, "
                      "or did the extraction read two different parties as one? "
                      "The mentions above say which."),
                confirmed=all(m["status"] in ("confirmed", "amended")
                              for m in mentions),
                documents=[document_id]))
            if len(out) >= limit:
                return out
    return out


def _named_tables(store: Store, bundle: dict | None):
    from .entities import _named_tables as named
    return named(store, bundle)


# ---------------------------------------------------------------------------
# The only thing joining two others
# ---------------------------------------------------------------------------

def shared_counterparty(store: Store, graph: dict | None = None,
                        limit: int = 50) -> list[dict]:
    """Pairs whose only connection runs through one shared third party.

    Two suppliers that never appear together, both dealing with one
    intermediary, is the shape a procurement reviewer is trained to look at. It
    is also the shape of a specialist subcontractor everybody in a small market
    uses, so the question is offered rather than the conclusion.

    Restricted to *articulation points* — the shared party must actually be the
    only route, not merely one of several. Otherwise every well-connected page
    generates a question about every pair around it and the list is noise.
    """
    graph = graph or graph_mod.build(store)
    adjacency, nodes = graph["adjacency"], graph["nodes"]
    cut = {b["entity_id"] for b in graph_mod.articulation_points(graph)}

    edges: dict[tuple, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        pair = (edge["from_entity_id"], edge["to_entity_id"])
        edges[pair].append(edge)
        edges[(pair[1], pair[0])].append(edge)

    out = []
    for middle in sorted(cut):
        neighbours = sorted(adjacency.get(middle, ()))
        for index, left in enumerate(neighbours):
            for right in neighbours[index + 1:]:
                # Directly joined, so the middle is not the only route.
                if right in adjacency.get(left, ()):
                    continue
                chain = []
                confirmed = True
                for a, b in ((left, middle), (middle, right)):
                    edge = max(edges[(a, b)],
                               key=lambda e: (e["n_confirmed"], e["n_documents"]))
                    chain.append({
                        "from_entity_id": a, "from_name": nodes[a]["canonical_name"],
                        "to_entity_id": b, "to_name": nodes[b]["canonical_name"],
                        "link_type_id": edge["link_type_id"],
                        "n_documents": edge["n_documents"],
                        "n_confirmed": edge["n_confirmed"],
                        "documents": edge["documents"],
                    })
                    confirmed = confirmed and bool(edge["n_confirmed"])
                out.append(_question(
                    "shared_counterparty",
                    (f"{nodes[left]['canonical_name']} and "
                     f"{nodes[right]['canonical_name']} are connected only "
                     f"through {nodes[middle]['canonical_name']}"),
                    entities=[{"entity_id": e, "name": nodes[e]["canonical_name"],
                               "type_id": nodes[e]["type_id"],
                               "part": "shared" if e == middle else "side"}
                              for e in (left, middle, right)],
                    chain=chain,
                    asks=("Is the shared party a specialist everybody in this "
                          "market uses, or is it the reason these two are "
                          "closer than they appear? The documents behind each "
                          "hop are named above."),
                    confirmed=confirmed,
                    documents=[d for hop in chain for d in hop["documents"]]))
                if len(out) >= limit:
                    return _rank(out)
    return _rank(out)


# ---------------------------------------------------------------------------
# A chain that comes back
# ---------------------------------------------------------------------------

def circular_relation(store: Store, graph: dict | None = None,
                      max_length: int = MAX_CHAIN, limit: int = 25) -> list[dict]:
    """Relations of one kind that lead back to where they started.

    A subcontracts to B, B subcontracts back to A. Sometimes two genuine
    contracts running opposite ways; sometimes one relationship the extraction
    recorded twice with the ends swapped, which is why the direction of every
    hop is kept.
    """
    graph = graph or graph_mod.build(store)
    nodes = graph["nodes"]
    directed: dict[str, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        directed[edge["from_entity_id"]].append(edge)

    seen: set[tuple] = set()
    out = []
    for start in sorted(directed):
        stack = [(start, [start], [])]
        while stack:
            current, path, hops = stack.pop()
            if len(path) > max_length + 1:
                continue
            for edge in directed.get(current, ()):
                target = edge["to_entity_id"]
                if target == start and len(hops) >= 1:
                    cycle = hops + [edge]
                    # One cycle, however many rotations of it exist.
                    key = tuple(sorted(e["from_entity_id"] for e in cycle))
                    if key in seen:
                        continue
                    seen.add(key)
                    chain = [{
                        "from_entity_id": e["from_entity_id"],
                        "from_name": e["from_name"],
                        "to_entity_id": e["to_entity_id"], "to_name": e["to_name"],
                        "link_type_id": e["link_type_id"],
                        "n_documents": e["n_documents"],
                        "n_confirmed": e["n_confirmed"],
                        "documents": e["documents"],
                    } for e in cycle]
                    names = " → ".join([h["from_name"] for h in chain]
                                       + [chain[0]["from_name"]])
                    out.append(_question(
                        "circular_relation",
                        f"{chain[0]['link_type_id'].replace('_', ' ')} comes "
                        f"back to where it started: {names}",
                        entities=[{"entity_id": h["from_entity_id"],
                                   "name": h["from_name"],
                                   "type_id": nodes.get(h["from_entity_id"], {}).get("type_id")}
                                  for h in chain],
                        chain=chain,
                        asks=("Are these genuinely separate agreements running "
                              "in both directions, or one relationship recorded "
                              "twice with its ends swapped? Compare the "
                              "documents behind each hop."),
                        confirmed=all(h["n_confirmed"] for h in chain),
                        documents=[d for h in chain for d in h["documents"]]))
                    if len(out) >= limit:
                        return _rank(out)
                elif target not in path:
                    stack.append((target, path + [target], hops + [edge]))
    return _rank(out)


def _rank(questions: list[dict]) -> list[dict]:
    """Checked chains first.

    The opposite of a severity sort, and deliberate: a question every hop of
    which somebody has confirmed is worth a person's time, and one assembled
    from unreviewed machine guesses is a reason to check the extraction. Putting
    the loudest first would invert that.
    """
    return sorted(questions, key=lambda q: (not q["confirmed_throughout"],
                                            -len(q["documents"]), q["summary"]))


# ---------------------------------------------------------------------------
# All three
# ---------------------------------------------------------------------------

def raised(store: Store, bundle: dict | None = None,
           graph: dict | None = None) -> dict:
    """Every question the corpus raises, with what it rests on.

    `coverage` comes first for the same reason it does in the topology: two of
    the three checks read the relation graph, and the graph is only as complete
    as the wiki. Questions not raised because the evidence never reached the
    graph are the ones nobody will think to look for.
    """
    graph = graph or graph_mod.build(store)
    found = (two_parts_in_one_document(store, bundle)
             + shared_counterparty(store, graph)
             + circular_relation(store, graph))
    found = _rank(found)
    checked = [q for q in found if q["confirmed_throughout"]]

    if not found:
        note = ("Nothing in the shape of this corpus raises a question. That is "
                "a statement about the relations that reached the graph, not a "
                "clean bill of health -- read the coverage line first.")
    else:
        note = (f"{len(found)} question(s), {len(checked)} of them resting "
                f"entirely on links somebody has confirmed. None of this is a "
                f"finding: each one names a chain and asks what explains it.")
    return {
        "coverage": graph_mod.coverage(store),
        "questions": found,
        "n_questions": len(found),
        "n_confirmed_throughout": len(checked),
        "by_kind": {kind: sum(1 for q in found if q["kind"] == kind)
                    for kind in ("two_parts_in_one_document",
                                 "shared_counterparty", "circular_relation")},
        "note": note,
    }
