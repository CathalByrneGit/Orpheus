"""Step 7: versioned rules over the extracted facts.

A concept is a named boolean SQL expression over one object type — "this
contract is high value", "the liability is uncapped" — evaluated per instance.
Two properties make it more than a saved query:

**Concepts are versioned, and versions are never edited.** Changing a threshold
adds a version and deprecates the old one rather than overwriting it, so an
evaluation made last month still points at a definition that exists and can
still explain itself. A superseded version is deprecated, never deleted.

**A rule finding is a finding.** A concept that comes out true raises a `Flag`
instance, which lands in the same review queue as everything a model found, at
the same four states. Rules that over-fire therefore show up as rejections, and
`quality` can count them.

In R this leaned on `conceptR`. There is no conceptR here; Orpheus owns the
machinery, which is why its five tables became migration 3. One thing improved
in the move: `cpt_add_score_component(version = NULL)` documented its default as
"whichever version is active at evaluation time" but could never insert, because
the column is NOT NULL and part of the primary key. A component now always pins
a concrete version — which is the better behaviour anyway, since a score should
record which definition produced it.
"""

from __future__ import annotations

import sqlite3

from . import bundle as bundle_mod
from .audit import record_edit
from .extract import insert_instance
from .rubric import CONFIDENCE
from .store import Store
from .utils import NotFound, OrpheusError, new_id, now, require_string, to_json


# ---------------------------------------------------------------------------
# Template parameters: policy, not fact
# ---------------------------------------------------------------------------

def parameter_key(template_id: str, parameter: str) -> str:
    return f"concept_param.{template_id}.{parameter}"


def concept_parameters(store: Store, bundle: dict | None = None) -> list[dict]:
    """Every template parameter, its default, and what is actually in force.

    "High value means a million" is a local policy question, not a fact about
    contracts. The bundle carries a default so the pipeline runs out of the box;
    a deployment overrides it without editing the bundle. Both are shown,
    because a reviewer looking at a flag needs to know which number raised it.
    """
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    rows = []
    for template in bundle.get("templates", []):
        defaults = ((template.get("extensions") or {}).get("orpheus") or {}).get("defaults") or {}
        for parameter in template.get("parameters", []):
            name = parameter["id"]
            override = store.setting(parameter_key(template["id"], name), None)
            default = defaults.get(name)
            rows.append({
                "template_id": template["id"],
                "parameter": name,
                "type": parameter.get("type", "string"),
                "default": default,
                "effective": override if override is not None else default,
                "source": "deployment_override" if override is not None else "bundle_default",
                "description": (parameter.get("display") or {}).get("description", ""),
            })
    return rows


def effective_parameters(store: Store, bundle: dict, template_id: str) -> dict:
    values = {}
    for row in concept_parameters(store, bundle):
        if row["template_id"] == template_id:
            values[row["parameter"]] = row["effective"]
    return values


def set_concept_parameter(store: Store, template_id: str, parameter: str,
                          value, actor_id: str) -> list[dict]:
    """Change a threshold for this deployment.

    Takes effect by adding a **new concept version**, never by editing the
    current one, so an evaluation made under the old threshold still points at a
    version that exists and still explains itself. Changing the number is a
    schema-level act and is recorded as one.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    bundle = bundle_mod.active(store) or bundle_mod.load()

    template = next((t for t in bundle.get("templates", [])
                     if t["id"] == template_id), None)
    if template is None:
        raise NotFound(f"No concept template {template_id!r}.")
    if not any(p["id"] == parameter for p in template.get("parameters", [])):
        known = ", ".join(p["id"] for p in template.get("parameters", []))
        raise OrpheusError(
            f"Template {template_id!r} has no parameter {parameter!r}. Known: {known}.")

    with store.transaction():
        store.set_setting(parameter_key(template_id, parameter), value, actor_id)
        record_edit(store, "org_settings", parameter_key(template_id, parameter),
                    None, "concept_parameter_set",
                    new={"template_id": template_id, "parameter": parameter,
                         "value": value}, actor_id=actor_id)
        return setup_concepts(store, bundle, actor_id=actor_id)


def resolve_concept_sql(store: Store, bundle: dict, concept: dict) -> str:
    """The SQL a concept will actually run, with deployment overrides applied."""
    template_id = concept.get("templateId")
    if not template_id:
        return concept.get("sqlExpr") or ""
    values = dict(concept.get("parameterValues") or {})
    values.update({k: v for k, v in
                   effective_parameters(store, bundle, template_id).items()
                   if v is not None})
    resolved = bundle_mod.resolve_template(bundle, template_id, values)
    return resolved or concept.get("sqlExpr") or ""


# ---------------------------------------------------------------------------
# Registering concepts
# ---------------------------------------------------------------------------

def register_templates(store: Store, bundle: dict) -> None:
    for template in bundle.get("templates", []):
        store.execute(
            "INSERT INTO concept_templates (template_id, object_type_id, "
            "base_sql_expr, parameters_json, source_standard, description) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(template_id) DO UPDATE SET "
            "base_sql_expr = excluded.base_sql_expr, "
            "parameters_json = excluded.parameters_json",
            (template["id"], template.get("objectTypeId"),
             template.get("baseSqlExpr", ""), to_json(template.get("parameters", [])),
             template.get("sourceStandard"),
             (template.get("display") or {}).get("description", "")))


def setup_concepts(store: Store, bundle: dict | None = None,
                   actor_id: str | None = None) -> list[dict]:
    """Register the bundle's concepts, versioning anything whose SQL changed."""
    store.assert_writable()
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()

    out: list[dict] = []
    with store.transaction():
        register_templates(store, bundle)

        for concept in bundle.get("concepts", []):
            concept_id = concept["id"]
            scope = concept.get("scope") or "default"
            sql_expr = resolve_concept_sql(store, bundle, concept)

            store.execute(
                "INSERT INTO concept_definitions (concept_id, object_type_id, description) "
                "VALUES (?, ?, ?) ON CONFLICT(concept_id) DO UPDATE SET "
                "object_type_id = excluded.object_type_id",
                (concept_id, concept.get("objectTypeId"),
                 (concept.get("display") or {}).get("description", "")))

            active_version = store.one(
                "SELECT version, sql_expr FROM concept_versions "
                "WHERE concept_id = ? AND scope = ? AND status = 'active'",
                (concept_id, scope))
            if active_version and active_version["sql_expr"].strip() == sql_expr.strip():
                out.append({"concept_id": concept_id, "scope": scope,
                            "version": active_version["version"], "action": "unchanged"})
                continue

            highest = store.scalar(
                "SELECT MAX(version) FROM concept_versions WHERE concept_id = ? AND scope = ?",
                (concept_id, scope)) or 0
            version = highest + 1

            store.insert("concept_versions", {
                "concept_id": concept_id, "scope": scope, "version": version,
                "sql_expr": sql_expr, "status": "active", "stage": "production",
                "rationale": concept.get("rationale"),
                "source_standard": concept.get("sourceStandard"),
                "template_id": concept.get("templateId"),
                "parameter_values_json": to_json(concept.get("parameterValues")),
                "activated_at": now(),
            })
            if active_version:
                # Deprecated, never deleted: evaluations made under it keep
                # pointing at a version that still exists.
                store.execute(
                    "UPDATE concept_versions SET status = 'deprecated', deprecated_at = ? "
                    "WHERE concept_id = ? AND scope = ? AND version = ?",
                    (now(), concept_id, scope, active_version["version"]))

            record_edit(store, "concept_versions",
                        f"{concept_id}/{scope}/{version}", None,
                        "concept_version_added",
                        new={"concept_id": concept_id, "scope": scope,
                             "version": version, "sql_expr": sql_expr},
                        actor_id=actor_id)
            out.append({"concept_id": concept_id, "scope": scope, "version": version,
                        "action": "new_version" if active_version else "created"})
    return out


# ---------------------------------------------------------------------------
# Evaluating them
# ---------------------------------------------------------------------------

def active_concepts(store: Store) -> list[dict]:
    return store.query(
        "SELECT d.concept_id, d.object_type_id, v.scope, v.version, v.sql_expr "
        "FROM concept_definitions d "
        "JOIN concept_versions v ON v.concept_id = d.concept_id "
        "WHERE v.status = 'active' ORDER BY d.concept_id")


def evaluate_concepts(store: Store, document_id: str,
                      actor_id: str | None = None) -> list[dict]:
    """Run every active concept over this document's live instances.

    Rejected rows are excluded, and that is not a detail: a fact a reviewer
    threw out must not come back as a rule finding.
    """
    store.assert_writable()
    bundle = bundle_mod.active(store) or bundle_mod.load()
    results: list[dict] = []

    for concept in active_concepts(store):
        obj = bundle_mod.object_type(bundle, concept["object_type_id"])
        table = bundle_mod.table_name(obj) if obj else None
        if not table or not store.table_exists(table):
            continue

        try:
            rows = store.query(
                f'SELECT instance_id FROM "{table}" WHERE document_id = :document_id '
                f"AND status != 'rejected' AND ({concept['sql_expr']})",
                {"document_id": document_id})
        except sqlite3.Error as exc:
            # A concept whose SQL no longer matches the schema is a broken rule,
            # not a broken document. It is reported and skipped, so one bad
            # definition cannot stop every other concept from running.
            results.append({"concept_id": concept["concept_id"],
                            "scope": concept["scope"], "version": concept["version"],
                            "error": str(exc)})
            continue

        evaluated = store.scalar(
            f'SELECT COUNT(*) FROM "{table}" WHERE document_id = ? '
            "AND status != 'rejected'", (document_id,)) or 0
        if not evaluated:
            continue

        results.append({"concept_id": concept["concept_id"], "scope": concept["scope"],
                        "version": concept["version"],
                        "object_type_id": concept["object_type_id"],
                        "n_evaluated": evaluated, "n_true": len(rows)})

        with store.transaction():
            for row in rows:
                write_evaluation(
                    store, concept_id=concept["concept_id"],
                    concept_version=concept["version"], concept_scope=concept["scope"],
                    kind="rule", scope_level="document", document_id=document_id,
                    result={"concept_id": concept["concept_id"],
                            "object_type_id": concept["object_type_id"],
                            "instance_id": row["instance_id"], "value": True},
                    dependencies=[row["instance_id"]], source="ai_local",
                    confidence=CONFIDENCE["explicit"], actor_id=actor_id)
                raise_flag(store, bundle, document_id, row["instance_id"],
                           concept["concept_id"], actor_id)
    return results


def raise_flag(store: Store, bundle: dict, document_id: str,
               target_instance_id: str, concept_id: str,
               actor_id: str | None = None) -> str | None:
    """A rule finding, in the same queue as everything a model found.

    A parallel queue for rule findings would mean rules never get reviewed, and
    a rule that over-fires would never be visible as over-firing.

    Which object type holds a flag is the bundle's to say, and it may say
    nothing: a domain with no flag type still evaluates its concepts and still
    records the evaluations, it just has nowhere to raise an instance. Returning
    None there is the honest answer -- the alternative, which this once did, was
    to assume `instances_Flag` and fail on any bundle that is not the contract
    one.
    """
    flag_type = bundle_mod.flag_object_type(bundle)
    if flag_type is None:
        return None
    table = bundle_mod.table_name(flag_type)

    existing = store.one(
        f'SELECT instance_id FROM "{table}" WHERE document_id = ? '
        "AND target_instance_id = ? AND flag_type = ? AND status != 'rejected'",
        (document_id, target_instance_id, concept_id))
    if existing:
        return existing["instance_id"]

    instance_id = new_id("inst")
    written = insert_instance(
        store, bundle, flag_type["id"], instance_id, document_id,
        {"target_instance_id": target_instance_id, "flag_type": concept_id,
         "severity": "medium",
         "rationale": f"Rule concept '{concept_id}' evaluated true.",
         "raised_by_pass": "concept"},
        "ai_local", CONFIDENCE["explicit"], actor_id=actor_id)
    if written is None:
        return None
    record_edit(store, table, instance_id, document_id, "extract",
                new={"flag_type": concept_id, "target": target_instance_id},
                actor_id=actor_id)
    return instance_id


def write_evaluation(store: Store, concept_id: str, concept_version, concept_scope,
                     kind: str, scope_level: str, document_id: str | None,
                     result: dict, dependencies, source: str, confidence: float,
                     actor_id: str | None = None, corpus_context=None,
                     resolution_quality=None) -> str:
    """Record one evaluation, and what it read.

    The dependencies are the point of the table: recording which instances an
    evaluation consulted is what lets an amendment mark it stale automatically,
    rather than leaving it quietly wrong.
    """
    evaluation_id = new_id("eval")
    store.insert("concept_evaluations", {
        "evaluation_id": evaluation_id, "concept_id": concept_id,
        "concept_version": concept_version, "concept_scope": concept_scope,
        "kind": kind, "scope": scope_level, "target_document_id": document_id,
        "result": to_json(result),
        "corpus_context_used": to_json(corpus_context) if corpus_context else None,
        "resolution_quality": resolution_quality, "source": source,
        "confidence": confidence, "status": "unconfirmed",
        "generated_at": now(), "generated_by": actor_id, "stale": 0,
    })
    for dependency in dict.fromkeys(dependencies or []):
        if not dependency:
            continue
        store.execute(
            "INSERT OR IGNORE INTO concept_evaluation_dependencies "
            "(evaluation_id, instance_id) VALUES (?, ?)",
            (evaluation_id, dependency))
    record_edit(store, "concept_evaluations", evaluation_id, document_id, "evaluate",
                new={"concept_id": concept_id, "kind": kind, "scope": scope_level},
                actor_id=actor_id)
    return evaluation_id


# ---------------------------------------------------------------------------
# Composite scores
# ---------------------------------------------------------------------------

def setup_scores(store: Store, bundle: dict | None = None,
                 actor_id: str | None = None) -> list[dict]:
    """Register the bundle's scores, pinning each component to a live version."""
    store.assert_writable()
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    out = []

    with store.transaction():
        for score in bundle_mod.scores(bundle):
            store.execute(
                "INSERT INTO composite_scores (score_id, object_type_id, description, "
                "aggregation, thresholds_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(score_id) DO UPDATE SET "
                "thresholds_json = excluded.thresholds_json",
                (score["id"], score.get("objectTypeId"), score.get("description"),
                 score.get("aggregation", "weighted_sum"),
                 to_json(score.get("thresholds"))))

            pinned = 0
            for component in score.get("components", []):
                active = store.one(
                    "SELECT version FROM concept_versions WHERE concept_id = ? "
                    "AND scope = ? AND status = 'active'",
                    (component["conceptId"], component["scope"]))
                if active is None:
                    # A score naming a concept that has no active version is a
                    # bundle mistake worth surfacing, not a crash.
                    out.append({"score_id": score["id"],
                                "concept_id": component["conceptId"],
                                "action": "skipped_no_active_version"})
                    continue
                # Re-pin: components from a superseded version go, so the score
                # scores against what is current.
                store.execute(
                    "DELETE FROM composite_score_components WHERE score_id = ? "
                    "AND concept_id = ? AND scope = ? AND version != ?",
                    (score["id"], component["conceptId"], component["scope"],
                     active["version"]))
                store.execute(
                    "INSERT OR REPLACE INTO composite_score_components "
                    "(score_id, concept_id, scope, version, weight) VALUES (?, ?, ?, ?, ?)",
                    (score["id"], component["conceptId"], component["scope"],
                     active["version"], float(component.get("weight", 1.0))))
                pinned += 1
            out.append({"score_id": score["id"], "components": pinned,
                        "action": "registered"})
    return out


def evaluate_score(store: Store, document_id: str, score_id: str | None = None,
                   actor_id: str | None = None) -> dict:
    """Score this document's primary instances against the pinned concepts."""
    store.assert_writable()
    bundle = bundle_mod.active(store) or bundle_mod.load()
    definitions = bundle_mod.scores(bundle)
    if score_id:
        definitions = [s for s in definitions if s["id"] == score_id]
    if not definitions:
        raise NotFound(f"No composite score {score_id!r}." if score_id
                       else "The bundle declares no composite scores.")

    outputs = []
    for score in definitions:
        obj = bundle_mod.object_type(bundle, score.get("objectTypeId"))
        table = bundle_mod.table_name(obj) if obj else None
        if not table or not store.table_exists(table):
            continue

        components = store.query(
            "SELECT concept_id, scope, version, weight FROM composite_score_components "
            "WHERE score_id = ?", (score["id"],))
        if not components:
            continue
        weights = {c["concept_id"]: c["weight"] for c in components}
        max_possible = sum(weights.values())
        thresholds = score.get("thresholds") or {}

        live = store.query(
            f'SELECT instance_id FROM "{table}" WHERE document_id = ? '
            "AND status != 'rejected'", (document_id,))
        results = []
        with store.transaction():
            for row in live:
                fired = [
                    c["concept_id"] for c in components
                    if store.one(
                        "SELECT 1 FROM concept_evaluations e "
                        "JOIN concept_evaluation_dependencies d "
                        "  ON d.evaluation_id = e.evaluation_id "
                        "WHERE e.concept_id = ? AND e.kind = 'rule' AND e.stale = 0 "
                        "  AND e.status != 'rejected' AND d.instance_id = ?",
                        (c["concept_id"], row["instance_id"]))
                ]
                total = sum(weights[c] for c in fired)
                result = {
                    "score_id": score["id"],
                    "instance_id": row["instance_id"],
                    "score": total,
                    "tier": _tier_for(total, thresholds),
                    "thresholds": thresholds,
                    # Which concepts contributed, and by how much. A score
                    # nobody can decompose is no better than a model's opinion.
                    "contributions": [{"concept_id": c, "weight": weights[c]}
                                      for c in fired],
                    "max_possible": max_possible,
                }
                write_evaluation(
                    store, concept_id=score["id"], concept_version=None,
                    concept_scope=None, kind="score", scope_level="document",
                    document_id=document_id, result=result,
                    dependencies=[row["instance_id"]], source="ai_local",
                    confidence=CONFIDENCE["explicit"], actor_id=actor_id)
                results.append(result)
        outputs.append({"score_id": score["id"], "document_id": document_id,
                        "results": results})
    return outputs[0] if score_id and outputs else {"scores": outputs}


def _tier_for(total: float, thresholds: dict) -> str | None:
    """Highest threshold the score reaches."""
    if not thresholds:
        return None
    reached = [(float(v), name) for name, v in thresholds.items() if total >= float(v)]
    return max(reached)[1] if reached else None


# ---------------------------------------------------------------------------
# Reading evaluations back
# ---------------------------------------------------------------------------

def document_evaluations(store: Store, document_id: str, kind: str | None = None,
                         include_stale: bool = True) -> list[dict]:
    sql = ("SELECT * FROM concept_evaluations WHERE target_document_id = ?")
    params: list = [document_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if not include_stale:
        sql += " AND stale = 0"
    return store.query(sql + " ORDER BY generated_at DESC", tuple(params))


def review_evaluation(store: Store, evaluation_id: str, status: str,
                      actor_id: str, note: str | None = None) -> str:
    """Step 8: an interpretation gets reviewed like any other finding."""
    store.assert_writable()
    require_string(actor_id, "actor_id")
    if status not in ("confirmed", "amended", "rejected"):
        raise OrpheusError("status must be confirmed, amended or rejected.")
    evaluation = store.one(
        "SELECT * FROM concept_evaluations WHERE evaluation_id = ?", (evaluation_id,))
    if evaluation is None:
        raise NotFound(f"No evaluation {evaluation_id!r}.")

    with store.transaction():
        store.execute(
            "UPDATE concept_evaluations SET status = ?, amended_by = ?, amended_at = ? "
            "WHERE evaluation_id = ?", (status, actor_id, now(), evaluation_id))
        record_edit(store, "concept_evaluations", evaluation_id,
                    evaluation["target_document_id"], f"evaluation_{status}",
                    previous={"status": evaluation["status"]},
                    new={"status": status}, actor_id=actor_id, note=note)
    return evaluation_id
