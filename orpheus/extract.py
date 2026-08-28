"""Steps 4 and 5: what the engines found becomes rows a person can correct.

This is where an extraction stops being a result and becomes a record. Three
things happen to every finding on the way in, and none of them is optional:

**Provenance is written beside it.** The instance row carries the *current*
values; the provenance row carries what the machine originally said, and never
changes. After a correction the instance says `human` and the provenance still
says `ai_local` at whatever confidence it claimed — which is the only reason
extraction quality can be measured later at all.

**Confidence is snapped to the rubric**, downward-biased, whatever number the
engine offered.

**Undeclared properties become schema amendments** rather than being dropped.
The bundle is a hypothesis about the documents; a property that keeps turning up
and has nowhere to go is the evidence against it.
"""

from __future__ import annotations

import sqlite3

from . import bundle as bundle_mod
from . import llm
from .amendments import record_schema_amendment
from .audit import record_edit
from .deterministic import (AMOUNT_ROLE_CUES, DATE_ROLE_CUES, find_amounts,
                            find_dates, infer_role)
from .ingest import document_pages, get_document, has_text
from .align import MATCH_EXACT
from .population import page_offsets, populate
from .rubric import EXCLUDED_STATUSES, RESERVED_PROPS, snap_confidence
from .store import Store
from .utils import OrpheusError, from_json, naive_key, new_id, now, to_json


def source_for_tier(tier: str) -> str:
    return "ai_local" if tier == "local" else "ai_cloud"


def active_bundle(store: Store) -> dict:
    row = store.one("SELECT bundle_json FROM bundles WHERE is_active = 1")
    if row is None:
        raise OrpheusError("No active ontology bundle. Register one first.")
    return bundle_mod.normalise(from_json(row["bundle_json"]))


# ---------------------------------------------------------------------------
# Writing one instance
# ---------------------------------------------------------------------------

def insert_instance(store: Store, bundle: dict, type_id: str, instance_id: str,
                    document_id: str, properties: dict, source: str,
                    confidence: float, status: str = "unconfirmed",
                    actor_id: str | None = None) -> str | None:
    """Write one instance, or record why it could not be written."""
    obj = bundle_mod.object_type(bundle, type_id)
    if obj is None:
        record_schema_amendment(
            store, document_id, "new_type", type_id, None,
            observed_value=to_json(properties) or "",
            rationale=f"Extraction produced unknown object type '{type_id}'.",
            actor_id=actor_id)
        return None

    table = bundle_mod.table_name(obj)
    declared = set(bundle_mod.property_ids(obj))
    values: dict = {
        "instance_id": instance_id,
        "document_id": document_id,
        "source": source,
        "confidence": snap_confidence(confidence),
        "status": status,
        "created_at": now(),
    }

    for name, value in (properties or {}).items():
        if value is None or value == "" or name in RESERVED_PROPS:
            continue
        if name in declared:
            values[name] = ("; ".join(str(v) for v in value)
                            if isinstance(value, (list, tuple)) else value)
        else:
            record_schema_amendment(
                store, document_id, "new_property", type_id, name,
                observed_value=("; ".join(str(v) for v in value)
                                if isinstance(value, (list, tuple)) else str(value)),
                rationale="Property seen during population but not declared in the bundle.",
                actor_id=actor_id)

    # Derived, never model output. It has to stay a pure function of `name`, or
    # cross-document matching quietly varies by extraction run.
    if "naive_key" in declared and values.get("name"):
        # In the style the bundle asks for: a personal name drops an honorific,
        # an organisation drops a trailing legal form, and applying either to
        # the other merges things that are not the same.
        values["naive_key"] = naive_key(values["name"], bundle_mod.name_style(obj))

    try:
        store.insert(table, values)
    except sqlite3.IntegrityError as exc:
        # A model omitting a property the bundle declares NOT NULL is ordinary,
        # and losing the whole document's extraction over one such instance is
        # not. It is skipped and the reason recorded, in the same place every
        # other "the bundle had no room for this" finding goes -- so it shows up
        # in review rather than in a stack trace.
        record_schema_amendment(
            store, document_id, "new_property", type_id, None,
            observed_value=to_json(properties) or "",
            rationale=f"Instance of '{type_id}' could not be stored: {exc}",
            actor_id=actor_id)
        return None

    store.insert("instance_index", {
        "instance_id": instance_id, "type_id": type_id, "table_name": table,
        "document_id": document_id, "created_at": now(),
    })
    return instance_id


def write_provenance(store: Store, instance_id: str, document_id: str,
                     source_label: str, source: str, page_no: int | None,
                     excerpt: str, confidence: float,
                     alignment: str | None = None,
                     char_start: int | None = None,
                     char_end: int | None = None) -> None:
    """The immutable record of what the machine said, and where it found it.

    Separate from the instance row on purpose. Amending an instance overwrites
    its values and sets `source = 'human'`, correctly — it is ground truth now.
    Without this row there would then be nothing left saying what the machine
    had claimed, and no way to ask afterwards whether it was any good.

    `alignment` is why the confidence is what it is. The two are related but not
    interchangeable: an engine may report low confidence in a quotation the
    document plainly contains, and may report high confidence in one it does
    not. Only the second is a fabrication, and only this column can tell them
    apart afterwards.
    """
    store.insert("provenance", {
        "provenance_id": new_id("prov"),
        "instance_id": instance_id,
        "document_id": document_id,
        "source_label": source_label,
        "page_no": page_no,
        "excerpt": excerpt,
        "confidence": snap_confidence(confidence),
        "source": source,
        "alignment": alignment,
        "char_start": char_start,
        "char_end": char_end,
        "created_at": now(),
    })


# ---------------------------------------------------------------------------
# Persisting a whole population pass
# ---------------------------------------------------------------------------

def persist_population(store: Store, document_id: str, bundle: dict,
                       population: dict, source: str,
                       actor_id: str | None = None) -> dict:
    n_entities = n_edges = 0
    id_map: dict[str, str] = {}

    with store.transaction():
        for entity in population.get("entities", []):
            new = new_id("inst")
            written = insert_instance(store, bundle, entity["type_id"], new,
                                      document_id, entity.get("properties") or {},
                                      source, entity.get("confidence"),
                                      actor_id=actor_id)
            if written is None:
                continue
            id_map[entity["instance_id"]] = new
            n_entities += 1

            write_provenance(store, new, document_id,
                             entity.get("source_label") or "",
                             source, entity.get("page_no"),
                             entity.get("excerpt") or "", entity.get("confidence"),
                             alignment=entity.get("alignment"),
                             char_start=entity.get("char_start"),
                             char_end=entity.get("char_end"))

            obj = bundle_mod.object_type(bundle, entity["type_id"])
            record_edit(store, bundle_mod.table_name(obj), new, document_id, "extract",
                        new={"type_id": entity["type_id"],
                             "properties": entity.get("properties"),
                             "source": source,
                             "confidence": entity.get("confidence"),
                             # The span, kept in the history too, so a
                             # highlight survives a later amendment.
                             "char_start": entity.get("char_start"),
                             "char_end": entity.get("char_end")},
                        actor_id=actor_id)

        known_links = {l["id"] for l in bundle.get("links", [])}
        for link in population.get("relationships", []):
            source_instance = id_map.get(link.get("from_instance_id"))
            target_instance = id_map.get(link.get("to_instance_id"))
            if not source_instance or not target_instance:
                continue
            if link.get("link_type_id") not in known_links:
                record_schema_amendment(
                    store, document_id, "new_link_type", None,
                    link.get("link_type_id"), observed_value=link.get("evidence") or "",
                    rationale="Link type seen during population but not declared in the bundle.",
                    actor_id=actor_id)
                continue
            edge_id = new_id("edge")
            store.insert("edges", {
                "edge_id": edge_id,
                "from_instance_id": source_instance,
                "to_instance_id": target_instance,
                "link_type_id": link["link_type_id"],
                "document_id": document_id,
                "evidence": link.get("evidence") or "",
                "source": source,
                "confidence": snap_confidence(link.get("confidence")),
                "status": "unconfirmed",
                "created_at": now(),
            })
            record_edit(store, "edges", edge_id, document_id, "extract",
                        new={"link_type_id": link["link_type_id"],
                             "from": source_instance, "to": target_instance},
                        actor_id=actor_id)
            n_edges += 1

        for amendment in population.get("amendments", []):
            record_schema_amendment(
                store, document_id, amendment.get("amendment_type", "new_property"),
                amendment.get("type_id"), amendment.get("property_id"),
                observed_value=amendment.get("observed_value", ""),
                inferred_type=amendment.get("inferred_type", "string"),
                rationale=amendment.get("rationale", ""), actor_id=actor_id)

        link_deterministic_to_primary(store, bundle, document_id)

    n_amendments = store.scalar(
        "SELECT COUNT(*) FROM schema_amendments WHERE document_id = ? AND status = 'pending'",
        (document_id,)) or 0

    return {"n_entities": n_entities, "n_edges": n_edges,
            "n_amendments": int(n_amendments),
            "dropped_edges": population.get("dropped_edges", 0)}


def link_deterministic_to_primary(store: Store, bundle: dict, document_id: str) -> bool:
    """Attach this document's findings to the thing the document is about.

    Which type is primary and which property points at it come from the
    bundle's domain block, not from here. A planning-application bundle names
    Application and its own container property, and the same code links to that.
    """
    domain = bundle_mod.domain(bundle)
    primary_id = domain.get("primaryObjectType")
    key = domain.get("containerProperty")
    if not primary_id or not key:
        return False

    primary = bundle_mod.object_type(bundle, primary_id)
    table = bundle_mod.table_name(primary) if primary else None
    if not table or not store.table_exists(table):
        return False

    anchor = store.one(
        f'SELECT instance_id FROM "{table}" WHERE document_id = ? '
        "AND status != 'rejected' ORDER BY confidence DESC LIMIT 1",
        (document_id,))
    if anchor is None:
        return False

    for obj in bundle_mod.managed_object_types(bundle):
        if obj["id"] == primary_id:
            continue
        child = bundle_mod.table_name(obj)
        if not child or not store.table_exists(child):
            continue
        if key not in store.columns(child):
            continue
        store.execute(
            f'UPDATE "{child}" SET "{key}" = ? WHERE document_id = ? '
            f'AND ("{key}" IS NULL OR "{key}" = \'\')',
            (anchor["instance_id"], document_id))
    return True


# ---------------------------------------------------------------------------
# The deterministic pass
# ---------------------------------------------------------------------------

def deterministic_finding_exists(store: Store, table: str, document_id: str,
                                 raw_text: str, page_no: int) -> bool:
    """Has this exact finding already been recorded for this document?

    The deterministic pass commits in its own transaction, before the model
    pass runs — deliberately, because a pattern-matched date is worth keeping
    even if the model call then fails. But that means a retry after a failure
    would write the same findings again, and the guard in `extract()` does not
    catch it because that only refuses a tier that already *succeeded*.

    Matched on what a finding is: the same raw text on the same page of the
    same document is the same finding. A row a reviewer rejected does not block
    a fresh one, so a deliberate re-run still refreshes.
    """
    if not store.table_exists(table):
        return False
    return store.one(
        f'SELECT instance_id FROM "{table}" WHERE document_id = ? AND raw_text = ? '
        "AND page_no = ? AND status != 'rejected' LIMIT 1",
        (document_id, raw_text, page_no)) is not None


def excerpt_around(text: str, position: int, needle: str, window: int = 120) -> str:
    if position is None or position < 0:
        return needle
    start = max(0, position - window)
    end = min(len(text), position + len(needle) + window)
    return text[start:end].strip()


def run_deterministic_pass(store: Store, document_id: str, bundle: dict,
                           actor_id: str | None = None) -> int:
    """Dates and money, found by pattern, recorded at the top of the rubric.

    Runs before the model and commits separately, so its findings survive a
    failed model call.
    """
    written = 0
    # Where each page's text begins in the whole-document string. The
    # deterministic pass reads a page at a time, so its offsets are page-local;
    # the model pass works on the joined document. Both write to the same
    # char_start/char_end columns, so both have to mean the same thing —
    # otherwise a reading UI highlights the right span on the wrong page.
    text_starts = {
        page_no: start + len(f"--- Page {page_no} ---\n")
        for page_no, start, _ in page_offsets(store, document_id)
    }

    with store.transaction():
        for page in document_pages(store, document_id):
            text = page["text"] or ""
            page_no = page["page_no"]
            if not text.strip():
                continue
            offset = text_starts.get(page_no, 0)

            for found in find_dates(text):
                if deterministic_finding_exists(store, "instances_KeyDate",
                                                document_id, found["raw_text"], page_no):
                    continue
                role = infer_role(text, found["position"], DATE_ROLE_CUES)
                instance_id = new_id("inst")
                if insert_instance(store, bundle, "KeyDate", instance_id, document_id,
                                   {"value": found["value"], "raw_text": found["raw_text"],
                                    "date_role": role, "page_no": page_no},
                                   "ai_local", found["confidence"], actor_id=actor_id) is None:
                    continue
                write_provenance(store, instance_id, document_id, "deterministic:date",
                                 "ai_local", page_no,
                                 excerpt_around(text, found["position"], found["raw_text"]),
                                 found["confidence"],
                                 # Found *by* matching the text, so it is
                                 # grounded by construction: the pattern pass
                                 # cannot assert something the page does not
                                 # contain, which is exactly what separates it
                                 # from a model.
                                 alignment=MATCH_EXACT,
                                 char_start=offset + found["position"],
                                 char_end=offset + found["position"]
                                 + len(found["raw_text"]))
                record_edit(store, "instances_KeyDate", instance_id, document_id, "extract",
                            new={"value": found["value"], "date_role": role,
                                 "source": "ai_local"},
                            actor_id=actor_id,
                            note=("Day/month order is ambiguous; recorded day-first."
                                  if found.get("ambiguous") else None))
                written += 1

            for found in find_amounts(text):
                if deterministic_finding_exists(store, "instances_MonetaryAmount",
                                                document_id, found["raw_text"], page_no):
                    continue
                role = infer_role(text, found["position"], AMOUNT_ROLE_CUES)
                instance_id = new_id("inst")
                if insert_instance(store, bundle, "MonetaryAmount", instance_id, document_id,
                                   {"amount": found["amount"], "currency": found["currency"],
                                    "raw_text": found["raw_text"], "role": role,
                                    "page_no": page_no},
                                   "ai_local", found["confidence"], actor_id=actor_id) is None:
                    continue
                write_provenance(store, instance_id, document_id, "deterministic:amount",
                                 "ai_local", page_no,
                                 excerpt_around(text, found["position"], found["raw_text"]),
                                 found["confidence"],
                                 # Found *by* matching the text, so it is
                                 # grounded by construction: the pattern pass
                                 # cannot assert something the page does not
                                 # contain, which is exactly what separates it
                                 # from a model.
                                 alignment=MATCH_EXACT,
                                 char_start=offset + found["position"],
                                 char_end=offset + found["position"]
                                 + len(found["raw_text"]))
                record_edit(store, "instances_MonetaryAmount", instance_id, document_id,
                            "extract",
                            new={"amount": found["amount"], "currency": found["currency"],
                                 "role": role, "source": "ai_local"},
                            actor_id=actor_id)
                written += 1
    return written


# ---------------------------------------------------------------------------
# Re-running a tier
# ---------------------------------------------------------------------------

def supersede_tier_instances(store: Store, document_id: str, source: str,
                             actor_id: str | None = None) -> int:
    """Retire the unreviewed output of an earlier run of the same tier.

    Rejected rather than deleted, in keeping with the amendment model: the
    superseded rows stay queryable as evidence about extraction quality. Rows a
    person confirmed or amended are untouched — a re-run must never discard a
    human judgement.
    """
    bundle = active_bundle(store)
    superseded = 0
    with store.transaction():
        for obj in bundle_mod.managed_object_types(bundle):
            table = bundle_mod.table_name(obj)
            if not table or not store.table_exists(table):
                continue
            stale = store.query(
                f'SELECT instance_id FROM "{table}" WHERE document_id = ? '
                "AND source = ? AND status = 'unconfirmed'",
                (document_id, source))
            if not stale:
                continue
            store.execute(
                f'UPDATE "{table}" SET status = \'rejected\', amended_at = ? '
                "WHERE document_id = ? AND source = ? AND status = 'unconfirmed'",
                (now(), document_id, source))
            for row in stale:
                record_edit(store, table, row["instance_id"], document_id, "superseded",
                            previous={"status": "unconfirmed"},
                            new={"status": "rejected"}, actor_id=actor_id,
                            note="Superseded by a later extraction run of the same tier.")
                superseded += 1
        store.execute(
            "UPDATE edges SET status = 'rejected', amended_at = ? "
            "WHERE document_id = ? AND source = ? AND status = 'unconfirmed'",
            (now(), document_id, source))
    return superseded


# ---------------------------------------------------------------------------
# The step itself
# ---------------------------------------------------------------------------

def extract(store: Store, document_id: str, tier: str = "local",
            actor_id: str | None = None, opt_in: bool = False,
            deterministic: bool = True, force: bool = False,
            engine_name: str | None = None) -> dict:
    """Run extraction over one document and write what it found."""
    store.assert_writable()
    if tier not in ("local", "cloud"):
        raise OrpheusError(f"Unknown tier {tier!r}.")
    document = get_document(store, document_id)
    if document is None:
        raise OrpheusError(f"No document {document_id!r}.")

    prior = store.one(
        "SELECT run_id FROM extraction_runs WHERE document_id = ? AND tier = ? "
        "AND status = 'succeeded' LIMIT 1", (document_id, tier))
    if prior and not force:
        raise OrpheusError(
            f"The {tier} tier has already run on {document_id}. Re-running would "
            "write a second copy of every instance, leaving a reviewer to work "
            "out which of two identical rows is current. Pass force=True to "
            "supersede the unreviewed results of the earlier run."
        )

    superseded = 0
    if prior and force:
        superseded = supersede_tier_instances(store, document_id,
                                              source_for_tier(tier), actor_id)

    bundle = active_bundle(store)
    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)

    source = source_for_tier(tier)
    run_id = new_id("run")
    store.insert("extraction_runs", {
        "run_id": run_id, "document_id": document_id, "tier": tier,
        "actor_id": actor_id, "bundle_id": bundle["bundleId"],
        "bundle_version": bundle["bundleVersion"], "started_at": now(),
        "status": "running",
    })

    n_deterministic = 0
    model_error: str | None = None
    population: dict = {}
    written = {"n_entities": 0, "n_edges": 0, "n_amendments": 0}

    try:
        if tier == "local" and deterministic:
            n_deterministic = run_deterministic_pass(store, document_id, bundle, actor_id)

        if not has_text(store, document_id):
            raise OrpheusError(
                f"Document {document_id} has no text to extract from. Its pages "
                "may need OCR — check text_source on document_pages."
            )

        # The model pass gets its own failure handling. The deterministic pass
        # finds dates and amounts by pattern and needs no model at all, so a
        # missing key, an unreachable Ollama or a backend that will not import
        # should cost the model's findings and nothing else. Discarding the
        # deterministic instances too would mean that the one part of the
        # pipeline guaranteed to work offline only ever survives when the part
        # that isn't also worked.
        try:
            population = populate(store, document_id, bundle=bundle, tier=tier,
                                  opt_in=opt_in, actor_id=actor_id,
                                  engine_name=engine_name)
            written = persist_population(store, document_id, bundle, population,
                                         source, actor_id)
        except BaseException as exc:  # noqa: BLE001
            # BaseException, not Exception: a native extraction backend can
            # abort through the interpreter (a Rust panic surfaces as
            # BaseException), and this process holds the only write connection
            # to the store. Losing it would take the whole service down over an
            # optional dependency.
            if not n_deterministic:
                raise
            # The core writes its messages for a person, so an OrpheusError
            # is passed on as written rather than prefixed with its class.
            model_error = (str(exc) if isinstance(exc, OrpheusError)
                           else f"{type(exc).__name__}: {exc}")

        status = "partial" if model_error else "succeeded"
        store.execute(
            "UPDATE extraction_runs SET finished_at = ?, status = ?, error = ?, "
            "n_entities = ?, n_edges = ?, n_amendments = ? WHERE run_id = ?",
            (now(), status, model_error, written["n_entities"] + n_deterministic,
             written["n_edges"], written["n_amendments"], run_id))
    except Exception as exc:
        store.execute(
            "UPDATE extraction_runs SET finished_at = ?, status = 'failed', "
            "error = ? WHERE run_id = ?", (now(), str(exc), run_id))
        raise

    return {**written, "run_id": run_id, "tier": tier, "document_id": document_id,
            "n_deterministic": n_deterministic, "superseded": superseded,
            "engine": population.get("engine"), "model_error": model_error}


# ---------------------------------------------------------------------------
# Reading instances back
# ---------------------------------------------------------------------------

def document_instances(store: Store, document_id: str, type_id: str | None = None,
                       include_rejected: bool = False) -> list[dict]:
    """Every instance extracted from a document, with its provenance."""
    sql = ("SELECT i.instance_id, i.type_id, i.table_name, p.excerpt, p.page_no, "
           "       p.source_label, p.confidence AS provenance_confidence, "
           "       p.source AS provenance_source "
           "FROM instance_index i "
           "LEFT JOIN provenance p ON p.instance_id = i.instance_id "
           "WHERE i.document_id = ?")
    params: list = [document_id]
    if type_id:
        sql += " AND i.type_id = ?"
        params.append(type_id)
    rows = store.query(sql + " ORDER BY i.type_id, i.created_at", tuple(params))

    out = []
    for row in rows:
        # Property values live in per-type tables, so they are fetched per type
        # rather than forced into one wide, mostly-empty result.
        instance = store.one(
            f'SELECT * FROM "{row["table_name"]}" WHERE instance_id = ?',
            (row["instance_id"],)) or {}
        row["status"] = instance.get("status")
        row["properties"] = {k: v for k, v in instance.items()
                             if k not in ("instance_id", "table_name")}
        if not include_rejected and row["status"] in EXCLUDED_STATUSES:
            continue
        out.append(row)
    return out
