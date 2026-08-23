"""Versioned rules, the flags they raise, and scores that can be decomposed."""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.concepts import (concept_parameters, document_evaluations,
                              evaluate_concepts, evaluate_score,
                              review_evaluation, set_concept_parameter,
                              setup_concepts, setup_scores)
from orpheus.extract import extract
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.review import amend_instance, reject_instance
from orpheus.utils import NotFound, OrpheusError

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"

RISKY = {"type": "Contract", "excerpt": "SERVICES AGREEMENT", "properties": {
    "name": "Services Agreement", "value_amount": 1480000, "value_currency": "EUR",
    "procurement_procedure": "direct", "signature_block_present": "no"}}
TAME = {"type": "Contract", "excerpt": "SERVICES AGREEMENT", "properties": {
    "name": "Small Agreement", "value_amount": 500, "value_currency": "EUR",
    "procurement_procedure": "open", "signature_block_present": "yes",
    "end_date": "2027-01-01"}}


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


def build(store, tmp_path, entity):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    setup_concepts(store, actor_id="act_admin")
    setup_scores(store, actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "storage")["document_id"]
    set_populator(lambda **kwargs: {"extractions": [entity]})
    extract(store, document_id, tier="local", actor_id="act_admin")
    return store, document_id


@pytest.fixture
def risky(store, tmp_path):
    return build(store, tmp_path, RISKY)


# -- registration and versioning --------------------------------------------

def test_setup_registers_every_concept_in_the_bundle(store):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    result = setup_concepts(store, actor_id="act_admin")
    assert {r["action"] for r in result} == {"created"}
    assert store.scalar("SELECT COUNT(*) FROM concept_versions WHERE status = 'active'") \
        == len(bundle_mod.load()["concepts"])


def test_running_setup_twice_changes_nothing(store):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    setup_concepts(store, actor_id="act_admin")
    again = setup_concepts(store, actor_id="act_admin")
    assert {r["action"] for r in again} == {"unchanged"}


def test_changing_a_threshold_adds_a_version_and_deprecates_the_old_one(risky):
    # Never edits in place: an evaluation made under the old threshold must
    # still point at a definition that exists and still explain itself.
    store, _ = risky
    before = store.one("SELECT version, sql_expr FROM concept_versions "
                       "WHERE concept_id = 'high_value' AND status = 'active'")

    set_concept_parameter(store, "value_threshold", "threshold", 5000000, "act_admin")

    after = store.one("SELECT version, sql_expr FROM concept_versions "
                      "WHERE concept_id = 'high_value' AND status = 'active'")
    assert after["version"] == before["version"] + 1
    assert "5000000" in after["sql_expr"]
    # Not 5e+06: valid in R, not valid in SQLite, and the first fix was applied
    # where the SQL was displayed rather than where it was stored.
    assert "e+" not in after["sql_expr"]

    superseded = store.one("SELECT status FROM concept_versions WHERE concept_id = "
                           "'high_value' AND version = ?", (before["version"],))
    assert superseded["status"] == "deprecated"


def test_the_effective_parameter_shows_which_number_is_in_force(risky):
    store, _ = risky
    before = concept_parameters(store)[0]
    assert before["source"] == "bundle_default"
    assert before["default"] == before["effective"] == 1000000

    set_concept_parameter(store, "value_threshold", "threshold", 250000, "act_admin")
    after = concept_parameters(store)[0]
    assert after["source"] == "deployment_override"
    assert after["default"] == 1000000          # the bundle is not edited
    assert str(after["effective"]) == "250000"


def test_an_unknown_template_or_parameter_is_refused(risky):
    store, _ = risky
    with pytest.raises(NotFound, match="No concept template"):
        set_concept_parameter(store, "nope", "threshold", 1, "act_admin")
    with pytest.raises(OrpheusError, match="has no parameter"):
        set_concept_parameter(store, "value_threshold", "nope", 1, "act_admin")


# -- evaluation -------------------------------------------------------------

def test_a_concept_that_fires_raises_a_flag_in_the_same_queue(risky):
    # A parallel queue for rule findings would mean rules never get reviewed,
    # and a rule that over-fires would never be visible as over-firing.
    store, document_id = risky
    results = evaluate_concepts(store, document_id, actor_id="act_admin")
    fired = {r["concept_id"] for r in results if r.get("n_true")}
    assert {"high_value", "direct_award", "missing_signature"} <= fired

    flags = store.query("SELECT flag_type, status, raised_by_pass FROM instances_Flag")
    assert {f["flag_type"] for f in flags} == fired
    assert all(f["status"] == "unconfirmed" for f in flags)
    assert all(f["raised_by_pass"] == "concept" for f in flags)


def test_a_concept_that_does_not_fire_raises_nothing(store, tmp_path):
    store, document_id = build(store, tmp_path, TAME)
    results = evaluate_concepts(store, document_id, actor_id="act_admin")
    assert all(r.get("n_true") == 0 for r in results if "n_true" in r)
    assert store.scalar("SELECT COUNT(*) FROM instances_Flag") == 0


def test_evaluating_twice_does_not_duplicate_a_flag(risky):
    store, document_id = risky
    evaluate_concepts(store, document_id, actor_id="act_admin")
    first = store.scalar("SELECT COUNT(*) FROM instances_Flag")
    evaluate_concepts(store, document_id, actor_id="act_admin")
    assert store.scalar("SELECT COUNT(*) FROM instances_Flag") == first


def test_a_rejected_instance_does_not_raise_flags(risky):
    # A fact a reviewer threw out must not come back as a rule finding.
    store, document_id = risky
    instance_id = store.scalar("SELECT instance_id FROM instances_Contract")
    reject_instance(store, instance_id, "act_admin", note="Wrong document")

    results = evaluate_concepts(store, document_id, actor_id="act_admin")
    assert all(r.get("n_true", 0) == 0 for r in results)
    assert store.scalar("SELECT COUNT(*) FROM instances_Flag") == 0


def test_one_broken_concept_does_not_stop_the_others(risky):
    store, document_id = risky
    store.execute("UPDATE concept_versions SET sql_expr = 'no_such_column = 1' "
                  "WHERE concept_id = 'high_value' AND status = 'active'")
    results = evaluate_concepts(store, document_id, actor_id="act_admin")

    broken = next(r for r in results if r["concept_id"] == "high_value")
    assert "error" in broken
    # The rest still ran.
    assert any(r.get("n_true") for r in results if r["concept_id"] != "high_value")


# -- staleness --------------------------------------------------------------

def test_amending_an_instance_marks_the_evaluations_that_read_it_stale(risky):
    # The point of recording dependencies: staleness is automatic rather than
    # something a person has to notice.
    store, document_id = risky
    evaluate_concepts(store, document_id, actor_id="act_admin")
    assert store.scalar("SELECT COUNT(*) FROM concept_evaluations WHERE stale = 0") > 0

    instance_id = store.scalar("SELECT instance_id FROM instances_Contract")
    amend_instance(store, instance_id, {"value_amount": 100}, "act_admin")

    stale = store.query("SELECT stale, stale_reason FROM concept_evaluations")
    assert all(r["stale"] == 1 for r in stale)
    assert "amended" in stale[0]["stale_reason"]


# -- scores -----------------------------------------------------------------

def test_a_score_pins_each_component_to_a_live_version(risky):
    store, _ = risky
    components = store.query("SELECT concept_id, version, weight FROM "
                             "composite_score_components WHERE score_id = 'contract_risk'")
    assert len(components) == 4
    # conceptR's own default of "whichever version is active at evaluation
    # time" could never insert, because the column is NOT NULL and part of the
    # key. Pinning is both correct and better: the score records what it scored.
    assert all(c["version"] == 1 for c in components)


def test_a_score_can_be_decomposed(risky):
    # A score nobody can decompose is no better than a model's opinion.
    store, document_id = risky
    evaluate_concepts(store, document_id, actor_id="act_admin")
    result = evaluate_score(store, document_id, "contract_risk", actor_id="act_admin")

    scored = result["results"][0]
    assert scored["score"] == 6.0
    assert scored["max_possible"] == 6.0
    assert scored["tier"] == "high"
    assert {c["concept_id"] for c in scored["contributions"]} == {
        "high_value", "direct_award", "missing_signature", "open_ended_term"}


def test_a_tame_contract_scores_at_the_bottom_tier(store, tmp_path):
    store, document_id = build(store, tmp_path, TAME)
    evaluate_concepts(store, document_id, actor_id="act_admin")
    scored = evaluate_score(store, document_id, "contract_risk",
                            actor_id="act_admin")["results"][0]
    assert scored["score"] == 0
    assert scored["tier"] == "low"
    assert scored["contributions"] == []


def test_an_unknown_score_is_reported(risky):
    store, document_id = risky
    with pytest.raises(NotFound, match="No composite score"):
        evaluate_score(store, document_id, "no_such_score")


# -- step 8: reviewing an interpretation ------------------------------------

def test_an_evaluation_is_reviewable_like_any_other_finding(risky):
    store, document_id = risky
    evaluate_concepts(store, document_id, actor_id="act_admin")
    evaluation = document_evaluations(store, document_id, kind="rule")[0]

    review_evaluation(store, evaluation["evaluation_id"], "rejected", "act_admin",
                      note="The rule is too blunt here")
    assert store.scalar("SELECT status FROM concept_evaluations WHERE evaluation_id = ?",
                        (evaluation["evaluation_id"],)) == "rejected"


def test_evaluations_can_be_read_back_by_kind(risky):
    store, document_id = risky
    evaluate_concepts(store, document_id, actor_id="act_admin")
    evaluate_score(store, document_id, "contract_risk", actor_id="act_admin")
    assert {e["kind"] for e in document_evaluations(store, document_id)} == {"rule", "score"}
    assert all(e["kind"] == "score"
               for e in document_evaluations(store, document_id, kind="score"))
