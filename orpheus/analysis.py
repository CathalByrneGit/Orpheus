"""Step 9: what this document looks like against the rest of the corpus.

Deliberately labelled as a stepping stone. Matching is on **normalised raw
text**, not resolved entities, and every result carries `naive_unresolved` so
nothing downstream can mistake it for entity resolution. Two organisations
sharing a name will be merged; one organisation written two ways will not be.
Those are leads to check, not findings.

The one exact path is a stated registration number. A number is an identity
claim where a normalised name is a guess, so identifier matches are gathered and
reported **separately** — a match resting on an identifier is much stronger
evidence than one resting on spelling, and collapsing the two would hide that.

Nothing here knows what a contract is. Which type a document is about, and which
property holds a comparable magnitude, come from the bundle's domain block; a
domain with no comparable value says so rather than being silently absent.
"""

from __future__ import annotations

import statistics

from . import bundle as bundle_mod
from .concepts import write_evaluation
from .ingest import get_document
from .rubric import CONFIDENCE, NAIVE_RESOLUTION
from .store import Store
from .utils import NotFound, OrpheusError, to_json


def object_set_by_interface(store: Store, interface_id: str,
                            bundle: dict | None = None,
                            document_id: str | None = None,
                            include_rejected: bool = False,
                            where: str | None = None,
                            params: tuple = ()) -> list[dict]:
    """Every type implementing an interface, as one set.

    An interface is a promise that several types can answer the same question.
    This is where the promise gets used: a name is a name, whether it belongs to
    a Company or a Person, and asking "what else in the corpus is called this?"
    should not have to know which.
    """
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    interface = bundle_mod.interface(bundle, interface_id)
    if interface is None:
        known = ", ".join(i["id"] for i in bundle.get("interfaces", []))
        raise NotFound(
            f"No interface {interface_id!r} in the bundle. Known: {known}.")

    type_ids = bundle_mod.implementing_types(bundle, interface_id)
    if not type_ids:
        raise OrpheusError(f"No object type implements {interface_id!r}.")

    columns = bundle_mod.interface_property_ids(interface)
    selects: list[str] = []
    all_params: list = []

    for type_id in type_ids:
        obj = bundle_mod.object_type(bundle, type_id)
        table = bundle_mod.table_name(obj) if obj else None
        if not table or not store.table_exists(table):
            continue
        # Validated at registration, but a table can lag its bundle between an
        # amendment being accepted and the schema being applied.
        if not set(columns) <= set(store.columns(table)):
            continue

        clauses = []
        if not include_rejected:
            clauses.append("status != 'rejected'")
        if document_id is not None:
            clauses.append("document_id = ?")
            all_params.append(document_id)
        if where:
            clauses.append(f"({where})")
            all_params.extend(params)

        projection = ", ".join(f'"{c}"' for c in columns)
        selects.append(
            f"SELECT {projection}, '{type_id}' AS type_id FROM \"{table}\""
            + (" WHERE " + " AND ".join(clauses) if clauses else ""))

    if not selects:
        return []
    return store.query("\nUNION ALL\n".join(selects), tuple(all_params))


def identifier_matches(store: Store, document_id: str) -> dict[str, list[dict]]:
    """Companies matched to other documents by stated registration number.

    Exact rather than heuristic, and gathered separately so a caller can attach
    each match to the entity it belongs to.
    """
    if not store.table_exists("instances_Company"):
        return {}
    if "registration_number" not in store.columns("instances_Company"):
        return {}

    mine = store.query(
        "SELECT instance_id, name, registration_number FROM instances_Company "
        "WHERE document_id = ? AND status != 'rejected' "
        "AND registration_number IS NOT NULL AND registration_number != ''",
        (document_id,))

    out: dict[str, list[dict]] = {}
    for row in mine:
        others = store.query(
            "SELECT instance_id, document_id, name, registration_number "
            "FROM instances_Company WHERE registration_number = ? "
            "AND document_id != ? AND status != 'rejected'",
            (row["registration_number"], document_id))
        if not others:
            continue
        out[row["instance_id"]] = [{
            "instance_id": other["instance_id"],
            "document_id": other["document_id"],
            "name": other["name"],
            "registration_number": other["registration_number"],
            # The interesting case: the same registered entity written two
            # different ways is precisely what entity resolution is for, and
            # here it is proven rather than guessed.
            "name_differs": other["name"] != row["name"],
        } for other in others]
    return out


def match_counterparties(store: Store, bundle: dict, document_id: str,
                         type_id: str) -> list[dict]:
    obj = bundle_mod.object_type(bundle, type_id)
    table = bundle_mod.table_name(obj) if obj else None
    if not table or not store.table_exists(table):
        return []

    mine = store.query(
        f'SELECT instance_id, name, naive_key FROM "{table}" '
        "WHERE document_id = ? AND status != 'rejected' "
        "AND naive_key IS NOT NULL AND naive_key != ''", (document_id,))
    if not mine:
        return []

    # Searches every type implementing Named, not just the same type: whether a
    # match lands on a Company or a Person is a finding, not something to filter
    # out in advance.
    named = [row for row in object_set_by_interface(store, "Named", bundle=bundle)
             if row.get("naive_key")]
    registered = identifier_matches(store, document_id)

    out = []
    for row in mine:
        # An entity link, where one exists, is what this question is *for*: a
        # person decided these mentions are the same thing, which is strictly
        # better evidence than the normalised name that was only ever a
        # stand-in for the decision. Re-deriving it from `naive_key` here would
        # ignore the answer and recompute a worse one.
        linked = entity_siblings(store, row["instance_id"], document_id)
        others = linked or [n for n in named
                            if n["naive_key"] == row["naive_key"]
                            and n["document_id"] != document_id]
        by_identifier = registered.get(row["instance_id"], [])
        # Either kind of match is worth reporting. Requiring a name match first
        # would make the identifier useless — it exists precisely to catch the
        # entities normalisation misses.
        if not others and not by_identifier:
            continue

        same_type = [o for o in others if o["type_id"] == type_id]
        other_type = [o for o in others if o["type_id"] != type_id]
        variants = list(dict.fromkeys(
            [o["name"] for o in others] + [m["name"] for m in by_identifier]))

        out.append({
            "instance_id": row["instance_id"],
            "name": row["name"],
            "naive_key": row["naive_key"],
            "appears_in_documents": len({o["document_id"] for o in same_type}),
            "other_document_ids": list(dict.fromkeys(o["document_id"] for o in same_type)),
            "other_instance_ids": list(dict.fromkeys(o["instance_id"] for o in same_type)),
            "name_variants": variants,
            # Surfaced rather than hidden: differing spellings under one key are
            # the clearest signal that this needs real resolution.
            "spelling_varies": bool(set(variants) - {row["name"]}),
            "identifier_matches": by_identifier,
            # Which path produced this match. A reviewed entity link and a
            # normalised name are not the same claim, and a caller that treats
            # them alike loses the only distinction that matters here.
            "matched_via": "entity" if linked else "naive_key",
            # A name that is a company here and a person elsewhere is exactly
            # what a reviewer wants to look at — reported, and reported
            # separately, because it is weaker than a same-type match.
            "cross_type_matches": [
                {"type_id": o["type_id"], "name": o["name"],
                 "instance_id": o["instance_id"], "document_id": o["document_id"]}
                for o in other_type],
        })
    return out


def entity_siblings(store: Store, instance_id: str, document_id: str) -> list[dict]:
    """Other mentions of the same entity, if this mention has been linked.

    Returns nothing when it has not, so the caller falls back to name matching.
    Only links a person has not rejected count: an unlinked or rejected one is
    a decision that these are *not* the same thing, and honouring it is the
    point of having asked.
    """
    if not store.table_exists("entity_mentions"):
        return []
    home = store.one(
        "SELECT entity_id FROM entity_mentions WHERE instance_id = ? "
        "AND unlinked_at IS NULL", (instance_id,))
    if home is None:
        return []

    out = []
    for row in store.query(
            "SELECT m.instance_id, m.document_id, i.type_id, i.table_name "
            "FROM entity_mentions m JOIN instance_index i USING (instance_id) "
            "WHERE m.entity_id = ? AND m.unlinked_at IS NULL "
            "AND m.document_id != ?", (home["entity_id"], document_id)):
        detail = store.one(
            f'SELECT name, naive_key FROM "{row["table_name"]}" '
            "WHERE instance_id = ?", (row["instance_id"],))
        out.append({"instance_id": row["instance_id"],
                    "document_id": row["document_id"],
                    "type_id": row["type_id"],
                    "name": detail["name"] if detail else None,
                    "naive_key": detail["naive_key"] if detail else None})
    return out


def compare_primary_values(store: Store, bundle: dict, document_id: str,
                           counterparties: list[dict]) -> dict:
    """This document's headline value against related documents.

    Every "unavailable" here is a distinct, stated reason rather than an empty
    result, because "no comparable value in this domain" and "no peer documents
    to compare against" call for different actions.
    """
    domain = bundle_mod.domain(bundle)
    value_property = domain.get("valueProperty")
    if not domain.get("primaryObjectType") or not value_property:
        return {"available": False,
                "reason": "This bundle declares no comparable value for its "
                          "primary object type."}

    primary = bundle_mod.object_type(bundle, domain["primaryObjectType"])
    table = bundle_mod.table_name(primary) if primary else None
    if not table or not store.table_exists(table):
        return {"available": False,
                "reason": "No instances of the primary object type exist."}

    currency_property = domain.get("currencyProperty")
    columns = ["instance_id", "document_id", value_property]
    if currency_property:
        columns.append(currency_property)
    projection = ", ".join(f'"{c}"' for c in columns)

    this = store.one(
        f'SELECT {projection} FROM "{table}" WHERE document_id = ? '
        "AND status != 'rejected' ORDER BY confidence DESC LIMIT 1", (document_id,))
    if this is None or this.get(value_property) is None:
        return {"available": False,
                "reason": f"No {value_property} has been extracted for this document."}

    peer_documents = list(dict.fromkeys(
        d for f in counterparties for d in f["other_document_ids"]))
    if not peer_documents:
        return {"available": False,
                "reason": "No other documents share a counterparty name with this one."}

    placeholders = ", ".join("?" for _ in peer_documents)
    peers = store.query(
        f'SELECT {projection} FROM "{table}" WHERE status != \'rejected\' '
        f'AND "{value_property}" IS NOT NULL AND document_id IN ({placeholders})',
        tuple(peer_documents))
    if not peers:
        return {"available": False,
                "reason": "Related documents have no extracted value to compare."}

    mixed = False
    if currency_property:
        # Compared only within a currency. Converting would need a rate for the
        # right date, which is not something to invent here.
        same = [p for p in peers
                if p.get(currency_property) == this.get(currency_property)]
        mixed = len(same) < len(peers)
        peers = same

    values = []
    for peer in peers:
        try:
            values.append(float(peer[value_property]))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"available": False,
                "reason": "Related documents are in other currencies; no "
                          "conversion is applied."}

    try:
        mine = float(this[value_property])
    except (TypeError, ValueError):
        return {"available": False,
                "reason": f"This document's {value_property} is not numeric."}

    median = statistics.median(values)
    return {
        "available": True,
        "object_type": domain["primaryObjectType"],
        "value_property": value_property,
        "this_value": mine,
        "currency": this.get(currency_property) if currency_property else None,
        "peer_count": len(values),
        "peer_median": median,
        "peer_max": max(values),
        "peer_min": min(values),
        "ratio_to_median": round(mine / median, 2) if median > 0 else None,
        "mixed_currencies_excluded": mixed,
    }


CAVEAT = (
    "Name matching is on normalised raw text, not resolved entities. Matches "
    "listed under identifier_matches rest on a stated registration number "
    "instead and are exact. Two different organisations sharing a name will be "
    "merged, and one organisation written two ways will not be. Treat these as "
    "leads to check, not as findings."
)


def _resolution_quality(findings: list[dict]) -> str:
    if findings and all(f["matched_via"] == "entity" for f in findings):
        return "resolved"
    return NAIVE_RESOLUTION


def corpus_analysis(store: Store, document_id: str, actor_id: str | None = None,
                    narrate: bool = False, tier: str = "cloud",
                    opt_in: bool = False) -> dict:
    """Compare one document against the rest of the store."""
    store.assert_writable()
    bundle = bundle_mod.active(store) or bundle_mod.load()
    if get_document(store, document_id) is None:
        raise NotFound(f"No document {document_id!r}.")

    n_documents = store.scalar("SELECT COUNT(*) FROM documents") or 0
    if n_documents < 2:
        raise OrpheusError(
            f"Database-wide analysis needs more than one document; the store "
            f"has {n_documents}. Ingest and extract at least one more first.")

    counterparties = match_counterparties(store, bundle, document_id, "Company")
    people = match_counterparties(store, bundle, document_id, "Person")
    value_comparison = compare_primary_values(store, bundle, document_id,
                                              counterparties)

    result = {
        "document_id": document_id,
        "matched_companies": len(counterparties),
        "matched_people": len(people),
        "counterparties": counterparties,
        "people": people,
        "value_comparison": value_comparison,
        "identifier_matched": sum(1 for f in counterparties if f["identifier_matches"]),
        # `naive_unresolved` unless *every* match came through a reviewed entity
        # link. One name-derived match is enough to make the whole result a
        # stepping stone again, and saying otherwise would let a caller trust
        # the weakest row in the set.
        "resolution_quality": _resolution_quality(counterparties + people),
        "caveat": CAVEAT if any(f["matched_via"] == "naive_key"
                                for f in counterparties + people) else None,
    }

    if narrate:
        result["narrative"] = _narrate(store, result, document_id, actor_id,
                                       tier, opt_in)

    local_ids = [f["instance_id"] for f in counterparties + people]
    context_ids = [i for f in counterparties + people for i in f["other_instance_ids"]]

    with store.transaction():
        evaluation_id = write_evaluation(
            store, concept_id="corpus_comparison", concept_version=None,
            concept_scope=None, kind="corpus", scope_level="database",
            document_id=document_id, result=result,
            dependencies=local_ids + context_ids,
            source="ai_cloud" if (narrate and tier == "cloud") else "ai_local",
            confidence=CONFIDENCE["inferred"], actor_id=actor_id,
            corpus_context={"instance_ids": context_ids,
                            "document_ids": list(dict.fromkeys(
                                d for f in counterparties + people
                                for d in f["other_document_ids"]))},
            resolution_quality=result["resolution_quality"])

    result["evaluation_id"] = evaluation_id
    return result


NARRATIVE_PROMPT = (
    "You are given the result of a naive cross-document comparison. Summarise "
    "what a reviewer should look at. Return JSON only with: summary (one "
    "paragraph), observations (a list of short strings), suggested_checks (a "
    "list of short strings).\n"
    "The name matching is not entity resolution. Do not assert that two "
    "entities are the same organisation; say what would need checking to "
    "establish it."
)


def _narrate(store: Store, result: dict, document_id: str,
             actor_id: str | None, tier: str, opt_in: bool) -> dict:
    from .classify import _ask

    reply = _ask(store, tier, NARRATIVE_PROMPT, to_json(result) or "{}",
                 document_id=document_id, actor_id=actor_id,
                 excerpt_only=True, opt_in=opt_in)
    return {"summary": reply.get("summary"),
            "observations": reply.get("observations") or [],
            "suggested_checks": reply.get("suggested_checks") or []}
