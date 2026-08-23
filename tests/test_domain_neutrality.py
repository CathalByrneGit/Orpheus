"""The engine is domain-neutral and the contract bundle is an example.

That is easy to claim and easy to quietly break, so it is tested rather than
asserted: a bundle from an unrelated domain runs the same pipeline with no code
changes. If any of these fail, the claim in the README is no longer true.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.analysis import compare_primary_values, object_set_by_interface
from orpheus.concepts import evaluate_concepts, setup_concepts
from orpheus.extract import extract
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.quality import codelist_violations
from orpheus.review import amend_instance
from orpheus.store import Store

PROVENANCE = [
    {"id": "document_id", "type": "string"},
    {"id": "source", "type": "string"},
    {"id": "confidence", "type": "number"},
    {"id": "status", "type": "string"},
    {"id": "amended_by", "type": "string", "nullable": True},
    {"id": "amended_at", "type": "string", "nullable": True},
]


def prop(id, type="string", nullable=True, values=None):
    out = {"id": id, "type": type, "nullable": nullable,
           "source": {"column": id}}
    if values:
        # A codelist is a vendor concern, so it lives under `extensions` like
        # everything else Orpheus adds to the spec.
        out["extensions"] = {"values": values}
    return out


def object_type(id, properties, implements=()):
    return {
        "id": id,
        "display": {"name": id, "description": id},
        "implements": list(implements),
        "primaryKey": "instance_id",
        "source": {"kind": "table", "table": f"instances_{id}"},
        "properties": ([prop("instance_id", nullable=False)]
                       + properties + PROVENANCE),
        "extensions": {"orpheus": {"managed": True}},
    }


def planning_bundle() -> dict:
    """Planning applications: a domain sharing nothing with contracts."""
    return bundle_mod.normalise({
        "specVersion": "0.1.0",
        "bundleId": "planning-core",
        "bundleVersion": "0.1.0",
        "metadata": {"name": "Planning applications",
                     "description": "An unrelated domain, same engine."},
        "objects": [
            object_type("Application", [
                prop("name", nullable=False),
                prop("reference"),
                prop("floor_area", "number"),
                prop("area_unit"),
                prop("decision", values=["granted", "refused", "withdrawn"]),
            ]),
            object_type("Applicant", [
                prop("name", nullable=False),
                prop("naive_key"),
            ], implements=["Named"]),
            object_type("Condition", [
                prop("application_instance_id"),
                prop("text"),
                prop("page_no", "integer"),
            ]),
        ],
        "interfaces": [{
            "id": "Named",
            "display": {"name": "Named", "description": "Has a matchable name"},
            "requiredProperties": [
                {"id": "instance_id", "type": "string"},
                {"id": "document_id", "type": "string"},
                {"id": "name", "type": "string"},
                {"id": "naive_key", "type": "string"},
                {"id": "status", "type": "string"},
            ],
        }],
        "links": [{
            "id": "has_condition",
            "from": "Application", "to": "Condition",
            "display": {"name": "has condition", "description": ""},
            "cardinality": "one-to-many",
            "join": {"fromKeys": ["instance_id"],
                     "toKeys": ["application_instance_id"]},
        }],
        "concepts": [{
            "id": "large_development",
            "objectTypeId": "Application",
            "scope": "planning",
            "version": 1,
            "display": {"name": "Large development",
                        "description": "Floor area at or above the threshold."},
            "sqlExpr": "floor_area IS NOT NULL AND CAST(floor_area AS REAL) >= 1000",
            "status": "draft",
            "rationale": "Placeholder threshold.",
        }],
        # The whole of the domain knowledge the engine needs.
        "extensions": {"orpheus": {
            "primaryObjectType": "Application",
            "containerProperty": "application_instance_id",
            "valueProperty": "floor_area",
            "currencyProperty": "area_unit",
            "documentTypes": ["application", "decision", "objection", "other"],
        }},
    })


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def planning_store(tmp_path):
    store = Store(str(tmp_path / "planning.sqlite"), mode="write")
    bundle = planning_bundle()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_test", "display_name": "Planner",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    yield store, bundle
    store.close()


@pytest.fixture
def planning_document(planning_store, tmp_path):
    store, bundle = planning_store
    path = tmp_path / "application.txt"
    path.write_text("PLANNING APPLICATION P-2024-118\n"
                    "Applicant: Byrne Developments Ltd.\n"
                    "Proposed floor area 1,450 sqm. Decision granted on "
                    "1 March 2025.\n")
    document_id = ingest(store, path, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]

    def populator(**kwargs):
        return {"extractions": [
            {"type": "Application", "confidence": 0.9,
             "excerpt": "PLANNING APPLICATION",
             "properties": {"name": "Extension at 4 Main St",
                            "reference": "P-2024-118", "floor_area": 1450,
                            "area_unit": "sqm", "decision": "granted"}},
            {"type": "Applicant", "confidence": 0.9,
             "excerpt": "Byrne Developments",
             "properties": {"name": "Byrne Developments Ltd"}},
            {"type": "Condition", "confidence": 0.7,
             "excerpt": "Condition 3",
             "properties": {"text": "Works shall not begin before 8am.",
                            "page_no": 1}},
        ]}

    set_populator(populator)
    return store, bundle, document_id


# ---------------------------------------------------------------------------

def test_an_unrelated_domain_validates_and_generates_its_own_schema(planning_store):
    store, bundle = planning_store
    bundle_mod.validate(bundle)

    tables = {r["name"] for r in store.query(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"instances_Application", "instances_Applicant",
            "instances_Condition"} <= tables
    # Nothing from the contract bundle leaked into the code.
    assert "instances_Contract" not in tables

    active = bundle_mod.active(store)
    assert bundle_mod.domain(active)["primaryObjectType"] == "Application"
    assert "application" in bundle_mod.document_types(active)


def test_extraction_and_linking_work_unchanged(planning_document):
    store, _, document_id = planning_document
    result = extract(store, document_id, tier="local", actor_id="act_test")
    assert result["n_entities"] == 3

    # The deterministic pass attached its findings using the container property
    # this bundle declared. No code knows what a planning application is.
    application = store.scalar("SELECT instance_id FROM instances_Application")
    linked = store.scalar(
        "SELECT application_instance_id FROM instances_Condition")
    assert linked == application


def test_review_works_unchanged(planning_document):
    store, _, document_id = planning_document
    extract(store, document_id, tier="local", actor_id="act_test")
    application = store.scalar("SELECT instance_id FROM instances_Application")

    amend_instance(store, application, {"decision": "refused"}, "act_test")
    assert store.scalar("SELECT status FROM instances_Application") == "amended"
    assert store.scalar("SELECT source FROM instances_Application") == "human"


def test_the_codelist_report_reads_this_bundles_own_codelist(planning_document):
    store, _, document_id = planning_document
    extract(store, document_id, tier="local", actor_id="act_test")
    application = store.scalar("SELECT instance_id FROM instances_Application")

    amend_instance(store, application, {"decision": "deferred"}, "act_test")
    violations = codelist_violations(store)
    assert [(v["property_id"], v["value"]) for v in violations] == \
        [("decision", "deferred")]
    assert violations[0]["type_id"] == "Application"


def test_a_concept_from_this_domain_evaluates_and_raises_its_own_flag(
        planning_document):
    """Rule concepts are the bundle's, not the code's.

    A domain with no Flag type still evaluates; it has nowhere to raise one,
    which is a property of that bundle rather than an error.
    """
    store, bundle, document_id = planning_document
    extract(store, document_id, tier="local", actor_id="act_test")
    setup_concepts(store, bundle, actor_id="act_test")

    evaluations = evaluate_concepts(store, document_id, actor_id="act_test")
    by_id = {e["concept_id"]: e for e in evaluations}
    assert by_id["large_development"]["n_true"] == 1      # 1450 >= 1000

    # The evaluation is recorded whether or not there is anywhere to raise a
    # flag, because recording it is the engine's job.
    assert store.scalar("SELECT COUNT(*) FROM concept_evaluations "
                        "WHERE concept_id = 'large_development'") == 1


def test_the_interface_query_spans_this_domains_own_named_types(planning_store):
    store, _ = planning_store
    active = bundle_mod.active(store)
    assert bundle_mod.implementing_types(active, "Named") == ["Applicant"]
    assert object_set_by_interface(store, "Named") == []


def test_a_domain_with_no_comparable_value_says_so_instead_of_failing(tmp_path):
    # Not every domain has a number worth comparing across documents. Omitting
    # it must report itself unavailable rather than break the corpus escalation.
    bundle = planning_bundle()
    domain = bundle["extensions"]["orpheus"]
    domain.pop("valueProperty")
    domain.pop("currencyProperty")
    bundle_mod.validate(bundle)

    store = Store(str(tmp_path / "no-value.sqlite"), mode="write")
    try:
        bundle_mod.register(store, bundle)
        bundle_mod.apply_schema(store, bundle)
        comparison = compare_primary_values(store, bundle_mod.active(store),
                                            "doc_x", [])
        assert comparison["available"] is False
        assert "comparable value" in comparison["reason"]
    finally:
        store.close()
