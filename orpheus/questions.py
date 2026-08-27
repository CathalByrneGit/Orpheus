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

import hashlib
import json
from collections import defaultdict
from typing import Any

from . import bundle as bundle_mod
from . import graph as graph_mod
from .audit import record_edit
from .rubric import QUESTION_STATUSES
from .store import Store
from .utils import (NotFound, OrpheusError, from_json, new_id, now,
                    require_choice, require_string, to_json)

# Where a bundle names the property that says what part something played in a
# document. Falls back to `role`, which is what the shipped contract bundle
# calls it; a bundle for another domain names its own.
ROLE_PROPERTY_HINT = "roleProperty"
DEFAULT_ROLE_PROPERTY = "role"

# A chain longer than this is not a question anybody can act on. Everything is
# connected to everything at six hops.
MAX_CHAIN = 3

# Every shape this asks about. Ordered as a reviewer meets them: the ones that
# need no graph first, then the ones the relation graph makes possible.
KINDS = ("two_parts_in_one_document", "shared_detail", "person_bridges",
         "shared_counterparty", "circular_relation")


def _role_property(bundle: dict | None) -> str:
    extensions = ((bundle or {}).get("extensions", {}) or {}).get("orpheus", {}) or {}
    return extensions.get(ROLE_PROPERTY_HINT) or DEFAULT_ROLE_PROPERTY


def fingerprint(kind: str, entity_ids: list[str]) -> str:
    """What makes two runs raise the same question.

    Its kind and the pages involved, order-independent -- so a reviewer's
    judgement survives the graph being rebuilt, another document arriving, or
    the chain being walked from the other end.
    """
    payload = json.dumps({"kind": kind, "entities": sorted(set(entity_ids))},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _chain_digest(chain: list[dict]) -> str:
    """The evidence, digested, so a judgement does not outlive it.

    A question about the same two pages resting on different documents is a
    different question, and a review made before that evidence arrived should
    not silently keep applying to it.
    """
    payload = json.dumps(sorted(
        json.dumps({k: v for k, v in hop.items() if k != "documents"},
                   sort_keys=True, default=str)
        + json.dumps(sorted(hop.get("documents") or []))
        for hop in chain))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _question(kind: str, summary: str, entities: list[dict], chain: list[dict],
              asks: str, confirmed: bool, documents: list[str]) -> dict:
    return {
        "kind": kind, "summary": summary, "entities": entities,
        "fingerprint": fingerprint(kind, [e["entity_id"] for e in entities]),
        "chain_digest": _chain_digest(chain),
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
# The same details on two different pages
# ---------------------------------------------------------------------------

def shared_detail(store: Store, bundle: dict | None = None,
                  properties: tuple[str, ...] = ("address", "registration_number"),
                  limit: int = 50) -> list[dict]:
    """Two pages whose documents give them the same stated detail.

    A shared **address** is the workhorse of this kind of review and it is also
    extremely common for dull reasons -- serviced offices, company formation
    agents, accountants who register a hundred clients at their own door. The
    question says so rather than implying otherwise.

    A shared **registration number** is different in kind, and stronger: a
    company number identifies one legal entity. Two pages carrying the same one
    are either one company under two names that resolution has not merged, or an
    extraction error. Both are worth knowing, and neither is wrongdoing.
    """
    bundle = bundle or bundle_mod.active(store)
    out = []
    for type_id, table in _named_tables(store, bundle):
        columns = {row["name"] for row in store.query(f'PRAGMA table_info("{table}")')}
        for prop in properties:
            if prop not in columns:
                continue
            rows = store.query(
                f'SELECT x."{prop}" AS detail, m.entity_id, e.canonical_name, '
                f"       x.instance_id, x.document_id, x.status, d.filename "
                f'FROM "{table}" x '
                f"JOIN entity_mentions m ON m.instance_id = x.instance_id "
                f"  AND m.unlinked_at IS NULL "
                f"JOIN entities e ON e.entity_id = m.entity_id "
                f"  AND e.merged_into IS NULL "
                f"LEFT JOIN documents d ON d.document_id = x.document_id "
                f'WHERE x."{prop}" IS NOT NULL AND TRIM(x."{prop}") != "" '
                f"AND x.status != 'rejected'")

            grouped: dict[str, list[dict]] = defaultdict(list)
            for row in rows:
                grouped[str(row["detail"]).strip().lower()].append(row)
            for detail, mentions in grouped.items():
                pages = {m["entity_id"] for m in mentions}
                if len(pages) < 2:
                    continue
                names = sorted({m["canonical_name"] for m in mentions})
                stated = mentions[0]["detail"]
                out.append(_question(
                    "shared_detail",
                    (f"{' and '.join(names[:3])}"
                     + (f" and {len(names) - 3} more" if len(names) > 3 else "")
                     + f" are all given the same {prop}: {stated}"),
                    entities=[{"entity_id": e, "type_id": type_id,
                               "name": next(m["canonical_name"] for m in mentions
                                            if m["entity_id"] == e)}
                              for e in sorted(pages)],
                    chain=[{"instance_id": m["instance_id"],
                            "part": f"{prop} = {m['detail']}",
                            "document_id": m["document_id"],
                            "filename": m["filename"] or m["document_id"],
                            "status": m["status"],
                            "documents": [m["document_id"]]} for m in mentions],
                    asks=(("A registration number identifies one legal entity, so "
                           "these are either one company under two names that has "
                           "not been merged, or an extraction error. Compare the "
                           "documents.")
                          if prop == "registration_number" else
                          ("Shared addresses are common and usually dull -- a "
                           "serviced office, a formation agent, an accountant. Is "
                           "this that, or are these parties less separate than the "
                           "documents suggest?")),
                    confirmed=all(m["status"] in ("confirmed", "amended")
                                  for m in mentions),
                    documents=[m["document_id"] for m in mentions]))
                if len(out) >= limit:
                    return _rank(out)
    return _rank(out)


# ---------------------------------------------------------------------------
# One person, two sides
# ---------------------------------------------------------------------------

def person_bridges(store: Store, graph: dict | None = None,
                   limit: int = 50) -> list[dict]:
    """One person connected to two organisations that deal with each other.

    The shape a reviewer is actually looking for, and the one the relation graph
    was worth building to reach. It is still not a finding -- a person can
    legitimately sit on both sides, and often the document says why -- but it is
    the question most worth putting in front of somebody.

    Read from the graph, so it needs both relations to have been extracted and
    both endpoints to have entity pages. Where the wiki is thin this finds
    nothing, which is what `coverage` is for.
    """
    graph = graph or graph_mod.build(store)
    nodes, adjacency = graph["nodes"], graph["adjacency"]
    edges: dict[tuple, list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        edges[(edge["from_entity_id"], edge["to_entity_id"])].append(edge)
        edges[(edge["to_entity_id"], edge["from_entity_id"])].append(edge)

    people = [e for e, node in nodes.items() if node["type_id"] == "Person"]
    out = []
    for person in sorted(people):
        attached = sorted(n for n in adjacency.get(person, ())
                          if nodes.get(n, {}).get("type_id") != "Person")
        for index, left in enumerate(attached):
            for right in attached[index + 1:]:
                # The two organisations must themselves be related -- otherwise
                # this is just somebody with two jobs, which is not a question.
                between = edges.get((left, right))
                if not between:
                    continue
                chain = []
                confirmed = True
                for a, b in ((person, left), (person, right), (left, right)):
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
                    "person_bridges",
                    (f"{nodes[person]['canonical_name']} is connected to both "
                     f"{nodes[left]['canonical_name']} and "
                     f"{nodes[right]['canonical_name']}, which deal with each "
                     f"other"),
                    entities=[{"entity_id": e, "name": nodes[e]["canonical_name"],
                               "type_id": nodes[e]["type_id"],
                               "part": "person" if e == person else "organisation"}
                              for e in (person, left, right)],
                    chain=chain,
                    asks=("Does a document explain the dual role -- a declared "
                          "directorship, a secondment, a signature given on "
                          "somebody's behalf? If nothing does, that absence is "
                          "the thing to ask about."),
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
    order = {"standing": 0, "open": 1, "explained": 2, "dismissed": 3}
    return sorted(questions, key=lambda q: (
        # What a person left standing comes first: they looked, and it stayed.
        order.get(q.get("status") or "open", 1),
        not q["confirmed_throughout"],
        -len(q["documents"]), q["summary"]))


# ---------------------------------------------------------------------------
# All three
# ---------------------------------------------------------------------------

def raised(store: Store, bundle: dict | None = None,
           graph: dict | None = None, open_only: bool = False) -> dict:
    """Every question the corpus raises, with what it rests on.

    `coverage` comes first for the same reason it does in the topology: two of
    the three checks read the relation graph, and the graph is only as complete
    as the wiki. Questions not raised because the evidence never reached the
    graph are the ones nobody will think to look for.
    """
    graph = graph or graph_mod.build(store)
    found = _attach_reviews(store, _rank(
        two_parts_in_one_document(store, bundle)
        + shared_detail(store, bundle)
        + person_bridges(store, graph)
        + shared_counterparty(store, graph)
        + circular_relation(store, graph)))

    if open_only:
        found = [q for q in found if q["status"] == "open"]
    checked = [q for q in found if q["confirmed_throughout"]]
    standing = [q for q in found if q["status"] == "standing"]
    outstanding = [q for q in found if q["status"] == "open"]
    stale = [q for q in found if q["review_stale"]]

    if not found:
        note = ("Nothing in the shape of this corpus raises a question. That is "
                "a statement about the relations that reached the graph, not a "
                "clean bill of health -- read the coverage line first.")
    else:
        note = (f"{len(found)} question(s): {len(standing)} somebody has looked "
                f"at and left standing, {len(outstanding)} nobody has ruled on. "
                f"{len(checked)} rest entirely on links somebody confirmed. None "
                f"of this is a finding -- each one names a chain and asks what "
                f"explains it.")
        if stale:
            note += (f" {len(stale)} carry a judgement made against different "
                     f"evidence, and are open again.")
    return {
        "coverage": graph_mod.coverage(store),
        "questions": found,
        "n_questions": len(found),
        "n_open": len(outstanding),
        "n_standing": len(standing),
        "n_stale_reviews": len(stale),
        "n_confirmed_throughout": len(checked),
        "by_kind": {kind: sum(1 for q in found if q["kind"] == kind)
                    for kind in KINDS},
        "note": note,
    }


# ---------------------------------------------------------------------------
# What a person decided
# ---------------------------------------------------------------------------

def review_question(store: Store, question_fingerprint: str, status: str,
                    rationale: str, actor_id: str | None = None,
                    kind: str | None = None, summary: str | None = None,
                    subjects: list[dict] | None = None,
                    chain_digest: str | None = None) -> dict:
    """Record what somebody decided about a question, and why.

    `rationale` is required for every state including `standing`, because the
    reason is the part that is worth anything later. "Shared subcontractor,
    specialist welder, three other suppliers use them too" is a fact somebody
    established; without it the next reviewer establishes it again, and the one
    after that.

    A previous judgement is superseded rather than overwritten -- what somebody
    decided last time stays readable after the evidence moved, for the same
    reason nothing else in this store overwrites.
    """
    store.assert_writable()
    require_choice(status, QUESTION_STATUSES, "status")
    if status == "open":
        raise OrpheusError(
            "`open` is the absence of a judgement, not one to record. To undo a "
            "review, record a new one saying what changed.")
    require_string(rationale, "rationale")
    require_string(question_fingerprint, "fingerprint")

    review_id = new_id("qrv")
    with store.transaction():
        previous = store.one(
            "SELECT * FROM question_reviews WHERE fingerprint = ? "
            "AND superseded_at IS NULL", (question_fingerprint,))
        if previous:
            store.execute(
                "UPDATE question_reviews SET superseded_at = ? WHERE review_id = ?",
                (now(), previous["review_id"]))
        store.insert("question_reviews", {
            "review_id": review_id,
            "fingerprint": question_fingerprint,
            "kind": kind or (previous or {}).get("kind") or "unknown",
            "summary": summary or (previous or {}).get("summary"),
            "subjects_json": to_json(subjects) if subjects
                             else (previous or {}).get("subjects_json"),
            "chain_digest": chain_digest or (previous or {}).get("chain_digest"),
            "status": status,
            "rationale": rationale,
            "reviewed_by": actor_id,
            "reviewed_at": now(),
            "superseded_at": None,
        })
        record_edit(store, "question_reviews", review_id, None, "review_question",
                    previous={"status": previous["status"]} if previous else None,
                    new={"status": status, "fingerprint": question_fingerprint},
                    actor_id=actor_id, note=rationale)
    return get_review(store, review_id)


def get_review(store: Store, review_id: str) -> dict:
    row = store.one("SELECT * FROM question_reviews WHERE review_id = ?",
                    (review_id,))
    if row is None:
        raise NotFound(f"No review {review_id!r}.")
    return {**row, "subjects": from_json(row["subjects_json"]) or []}


def reviews(store: Store, status: str | None = None) -> list[dict]:
    """Every live judgement, newest first."""
    clause = " AND status = ?" if status else ""
    params = (status,) if status else ()
    return [{**r, "subjects": from_json(r["subjects_json"]) or []}
            for r in store.query(
                f"SELECT * FROM question_reviews WHERE superseded_at IS NULL"
                f"{clause} ORDER BY reviewed_at DESC", params)]


def review_history(store: Store, question_fingerprint: str) -> list[dict]:
    """Every judgement ever made about this question, oldest first."""
    return store.query(
        "SELECT * FROM question_reviews WHERE fingerprint = ? "
        "ORDER BY reviewed_at", (question_fingerprint,))


def _attach_reviews(store: Store, found: list[dict]) -> list[dict]:
    """Hang the live judgement on each question, and say if it went stale.

    A review made against different evidence is not a judgement about the
    question in front of you. Rather than silently keeping it or silently
    dropping it, the question carries both: what was decided, and that the
    chain has changed since.
    """
    if not found:
        return found
    marks = ",".join("?" * len(found))
    rows = {r["fingerprint"]: r for r in store.query(
        f"SELECT * FROM question_reviews WHERE superseded_at IS NULL "
        f"AND fingerprint IN ({marks})",
        tuple(q["fingerprint"] for q in found))}
    for question in found:
        review = rows.get(question["fingerprint"])
        if review is None:
            question["status"] = "open"
            question["review"] = None
            question["review_stale"] = False
            continue
        stale = bool(review["chain_digest"]
                     and review["chain_digest"] != question["chain_digest"])
        question["review"] = {**review, "stale": stale}
        question["review_stale"] = stale
        # A stale judgement does not settle the question in front of you.
        question["status"] = "open" if stale else review["status"]
    return found
