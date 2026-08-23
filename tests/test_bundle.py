"""The bundle format.

Orpheus writes ontologySpecR bundles. Not "something like" them: the shipped
bundle is validated here against that project's own schema, vendored unmodified,
so a change to either surfaces as a failing test rather than as a file two tools
disagree about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.bundle import (DEFAULT_BUNDLE, SPEC_SCHEMA, ddl, domain, load,
                            managed_object_types, normalise, object_type,
                            resolve_template, validate)
from orpheus.utils import OrpheusError

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY = Path(__file__).parent.parent / "inst" / "bundles" / "contract-core-0.1.0.json"


@pytest.fixture
def bundle():
    return load()


# -- the format itself ------------------------------------------------------

def test_the_shipped_bundle_is_a_valid_ontologyspecr_bundle(bundle):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SPEC_SCHEMA.read_text())
    jsonschema.Draft202012Validator(schema).validate(bundle)


def test_the_shipped_bundle_passes_orpheus_validation(bundle):
    assert validate(bundle) is bundle


def test_a_third_party_bundle_loads(bundle):
    # The aviation demo from ontologySpecR: a real bundle written by someone
    # else, about a domain with no documents in it at all. It should normalise
    # without complaint, which is the difference between adopting a format and
    # merely resembling one.
    aviation = load(FIXTURES / "aviation-demo.json")
    assert {o["id"] for o in aviation["objects"]} == {"Airport", "Airline", "FlightRoute"}
    assert aviation["links"][0]["cardinality"] == "many-to-one"
    assert aviation["queries"] and aviation["actions"]


def test_a_third_party_bundle_is_rejected_for_the_reason_it_should_be(bundle):
    # It is a valid ontologySpecR bundle and not a usable Orpheus one, because
    # nothing in it says what a document is about. That must fail loudly.
    aviation = load(FIXTURES / "aviation-demo.json")
    with pytest.raises(OrpheusError, match="extensions.orpheus block"):
        validate(aviation)


# -- normalising the R-era spelling -----------------------------------------

def test_the_legacy_bundle_still_loads(bundle):
    legacy = load(LEGACY)
    assert legacy["bundleId"] == bundle["bundleId"]
    assert [o["id"] for o in legacy["objects"]] == [o["id"] for o in bundle["objects"]]
    validate(legacy)


def test_normalising_is_idempotent(bundle):
    assert normalise(bundle) == bundle


def test_the_legacy_double_spelling_is_gone(bundle):
    raw = json.loads(LEGACY.read_text())
    # The R file carried each list twice, once per consuming package.
    assert raw["objects"] == raw["object_types"]
    # The canonical one carries each exactly once.
    for gone in ("object_types", "link_types", "concept_defs", "interfaceTypes",
                 "bundle_id", "x_orpheus"):
        assert gone not in bundle
    assert Path(DEFAULT_BUNDLE).stat().st_size < LEGACY.stat().st_size / 1.5


def test_r_type_names_are_translated(bundle):
    # R names types after its storage modes; the spec names them after JSON's.
    types = {p["type"] for o in bundle["objects"] for p in o["properties"]}
    assert "double" not in types
    assert "number" in types


def test_a_string_primary_key_becomes_the_spec_shape(bundle):
    contract = object_type(bundle, "Contract")
    assert contract["primaryKey"] == {"properties": ["instance_id"],
                                      "strategy": "surrogate"}


def test_display_names_survive_the_move(bundle):
    contract = object_type(bundle, "Contract")
    assert contract["display"]["name"] == "Contract"
    assert "agreement" in contract["display"]["description"]


# -- templated concepts -----------------------------------------------------

def test_a_templated_concept_carries_the_sql_that_will_run(bundle):
    high_value = next(c for c in bundle["concepts"] if c["id"] == "high_value")
    assert high_value["templateId"] == "value_threshold"
    assert high_value["parameterValues"] == {"threshold": 1000000}
    # And the resolved expression, so the file shows what runs.
    assert "1000000" in high_value["sqlExpr"]
    # Not 5e+06. R produced that for a million, which is valid R and invalid
    # SQLite, and the first fix was applied where the SQL was displayed rather
    # than where it was stored.
    assert "e+" not in high_value["sqlExpr"]


def test_a_concept_that_drifts_from_its_template_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    concept = next(c for c in broken["concepts"] if c["id"] == "high_value")
    concept["sqlExpr"] = "value_amount > 42"
    with pytest.raises(OrpheusError, match="does not match template"):
        validate(broken)


def test_a_concept_naming_an_unknown_template_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    next(c for c in broken["concepts"] if c["id"] == "high_value")["templateId"] = "nope"
    with pytest.raises(OrpheusError, match="unknown template"):
        validate(broken)


def test_template_resolution_renders_numbers_for_sqlite(bundle):
    sql = resolve_template(bundle, "value_threshold", {"threshold": 5000000})
    assert "5000000" in sql and "e+" not in sql


# -- semantic checks a schema cannot make -----------------------------------

def test_an_unsatisfied_interface_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    contract = next(o for o in broken["objects"] if o["id"] == "Contract")
    contract["properties"] = [p for p in contract["properties"] if p["id"] != "status"]
    with pytest.raises(OrpheusError, match="implements 'Reviewable' but is missing"):
        validate(broken)


def test_an_unknown_interface_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    next(o for o in broken["objects"] if o["id"] == "Contract")["implements"] = ["Ghost"]
    with pytest.raises(OrpheusError, match="unknown interface"):
        validate(broken)


def test_a_duplicate_property_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    contract = next(o for o in broken["objects"] if o["id"] == "Contract")
    contract["properties"].append(dict(contract["properties"][0]))
    with pytest.raises(OrpheusError, match="duplicate properties"):
        validate(broken)


def test_a_link_to_an_unknown_type_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    broken["links"][0]["to"] = "Nowhere"
    with pytest.raises(OrpheusError, match="references unknown object type"):
        validate(broken)


def test_a_domain_block_naming_a_missing_type_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    broken["extensions"]["orpheus"]["primaryObjectType"] = "Nowhere"
    with pytest.raises(OrpheusError, match="primaryObjectType 'Nowhere'"):
        validate(broken)


def test_a_domain_block_naming_a_missing_value_property_is_rejected(bundle):
    broken = json.loads(json.dumps(bundle))
    broken["extensions"]["orpheus"]["valueProperty"] = "not_a_column"
    with pytest.raises(OrpheusError, match="valueProperty 'not_a_column'"):
        validate(broken)


# -- what the bundle is for -------------------------------------------------

def test_the_domain_block_says_what_a_document_is_about(bundle):
    d = domain(bundle)
    assert d["primaryObjectType"] == "Contract"
    assert d["containerProperty"] == "contract_instance_id"
    assert "contract" in d["documentTypes"]


def test_ddl_is_generated_for_every_managed_type(bundle):
    statements = ddl(bundle)
    tables = [s for s in statements if s.startswith("CREATE TABLE")]
    assert len(tables) == len(managed_object_types(bundle))
    assert any('"instances_Contract"' in s for s in tables)
    # Every instance table is looked up by document, constantly.
    assert any("idx_instances_Contract_doc" in s for s in statements)


def test_queries_moved_out_of_the_code_and_into_the_bundle(bundle):
    ids = {q["id"] for q in bundle["queries"]}
    assert "extraction_accuracy_by_confidence" in ids
    # The one query that cannot be plain SQL says so, rather than the generator
    # knowing which query is special.
    accuracy = next(q for q in bundle["queries"]
                    if q["id"] == "extraction_accuracy_by_confidence")
    assert accuracy["extensions"]["orpheus"]["expand"] == ["instanceUnion"]
    assert "{{instanceUnion}}" in accuracy["definition"]["body"]


def test_actions_declare_the_review_verbs_as_tool_definitions(bundle):
    actions = {a["id"]: a for a in bundle["actions"]}
    assert set(actions) == {"confirm_instance", "amend_instance", "reject_instance"}
    amend = actions["amend_instance"]
    # Reached over HTTP, never as SQL: an agent given these gets the constrained
    # write path rather than a database connection.
    assert amend["implementation"]["kind"] == "http"
    assert {p["id"] for p in amend["parameters"]} == {"instance_id", "changes", "note"}
    assert any(e["kind"] == "emit" for e in amend["effects"])
