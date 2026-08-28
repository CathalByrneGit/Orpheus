"""Extraction becoming a record.

Three things happen to every finding on the way in, and each is tested for its
own reason: provenance is written beside the instance and never changes,
confidence is snapped to the rubric whatever the engine offered, and a property
the bundle has no place for becomes a schema amendment rather than disappearing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.audit import row_history
from orpheus.extract import (deterministic_finding_exists, document_instances,
                             extract, run_deterministic_pass,
                             supersede_tier_instances)
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.rubric import CONFIDENCE
from orpheus.utils import OrpheusError

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


@pytest.fixture
def ready(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    return store, document_id


def populator(entities):
    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        return {"extractions": list(entities)}
    return engine


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


COMPANY = {"type": "Company", "excerpt": "Ardmore Digital Limited",
           "properties": {"name": "Ardmore Digital Limited", "role": "supplier"}}
CONTRACT = {"type": "Contract", "excerpt": "SERVICES AGREEMENT",
            "properties": {"name": "Services Agreement", "reference": "DOH-2026-0431"}}


# -- the schema comes from the bundle ---------------------------------------

def test_registering_a_bundle_creates_one_table_per_managed_type(ready):
    store, _ = ready
    expected = {bundle_mod.table_name(o)
                for o in bundle_mod.managed_object_types(bundle_mod.load())}
    for table in expected:
        assert store.table_exists(table)


def test_created_at_is_store_bookkeeping_not_a_bundle_property(ready):
    # Declaring it as a property would put it in every object-set projection,
    # where it means nothing to anyone reading the data.
    store, _ = ready
    assert "created_at" in store.columns("instances_KeyDate")
    contract = bundle_mod.object_type(bundle_mod.load(), "Contract")
    assert "created_at" not in bundle_mod.property_ids(contract)


def test_applying_a_bundle_that_gained_a_property_adds_the_column(ready):
    store, _ = ready
    grown = bundle_mod.load()
    contract = next(o for o in grown["objects"] if o["id"] == "Contract")
    contract["properties"].append({"id": "renewal_notice", "type": "string"})
    grown["bundleVersion"] = "0.2.1"
    bundle_mod.register(store, grown, actor_id="act_test")
    assert "renewal_notice" in store.columns("instances_Contract")


# -- what extraction writes -------------------------------------------------

def test_extraction_writes_instances_provenance_and_history(ready):
    store, document_id = ready
    set_populator(populator([COMPANY, CONTRACT]))

    result = extract(store, document_id, tier="local", actor_id="act_test")
    assert result["n_entities"] == 2
    assert result["n_deterministic"] == 5      # four dates and one amount

    rows = document_instances(store, document_id)
    assert {r["type_id"] for r in rows} == {"Company", "Contract", "KeyDate",
                                            "MonetaryAmount"}
    assert all(r["status"] == "unconfirmed" for r in rows)

    # One provenance row per instance, and it carries the tier.
    assert store.scalar("SELECT COUNT(*) FROM provenance") == len(rows)
    assert {r["provenance_source"] for r in rows} == {"ai_local"}

    company = next(r for r in rows if r["type_id"] == "Company")
    assert row_history(store, "instances_Company", company["instance_id"])[0]["action"] == "extract"


def test_the_run_is_recorded_whether_it_succeeds_or_fails(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")
    run = store.one("SELECT * FROM extraction_runs")
    assert run["status"] == "succeeded"
    # Read from the shipped bundle rather than pinned: what is under test is
    # that the run records which ontology produced it, not the number itself.
    assert run["bundle_version"] == bundle_mod.load()["bundleVersion"]
    assert run["finished_at"]


def test_a_model_failure_leaves_the_run_partial_not_failed(ready):
    store, document_id = ready

    def broken(**kwargs):
        raise RuntimeError("the model fell over")

    set_populator(broken)
    result = extract(store, document_id, tier="local", actor_id="act_test")
    run = store.one("SELECT status, error FROM extraction_runs")
    assert run["status"] == "partial"
    assert "fell over" in run["error"]
    assert "fell over" in result["model_error"]


def test_a_run_that_salvages_nothing_still_fails(ready):
    # Nothing to keep, so nothing to be partial about: the caller is told.
    store, document_id = ready

    def broken(**kwargs):
        raise RuntimeError("the model fell over")

    set_populator(broken)
    with pytest.raises(RuntimeError):
        extract(store, document_id, tier="local", actor_id="act_test",
                deterministic=False)
    assert store.one("SELECT status FROM extraction_runs")["status"] == "failed"


def test_a_native_backend_that_aborts_the_interpreter_is_survived(ready):
    """A Rust panic arrives as BaseException, not Exception.

    Under Datasette the extracting process holds the only write connection to
    the store, so letting one escape would take the service down over an
    optional dependency.
    """
    store, document_id = ready

    class Panic(BaseException):
        pass

    def panics(**kwargs):
        raise Panic("backend aborted")

    set_populator(panics)
    result = extract(store, document_id, tier="local", actor_id="act_test")
    assert "backend aborted" in result["model_error"]
    assert store.scalar("SELECT COUNT(*) FROM instances_KeyDate") == 4


def test_a_refused_cloud_request_never_starts_a_run(ready):
    # Being refused is not a backend failure, and must not read as a partial
    # success. The gate is checked before the run row exists, so there is
    # nothing to salvage and nothing recorded as having tried.
    store, document_id = ready
    store.set_setting("cloud_ai_policy", "disabled", "act_test")
    with pytest.raises(OrpheusError):
        extract(store, document_id, tier="cloud", actor_id="act_test", opt_in=True)
    assert store.scalar("SELECT COUNT(*) FROM extraction_runs") == 0


def test_deterministic_findings_survive_a_failed_model_call(ready):
    # The deterministic pass finds dates and amounts by pattern and needs no
    # model at all. A pattern-matched date is worth keeping when the model call
    # then fails -- and under Datasette, where the whole request is one
    # transaction, keeping it means not raising through it.
    store, document_id = ready

    def broken(**kwargs):
        raise RuntimeError("no model")

    set_populator(broken)
    extract(store, document_id, tier="local", actor_id="act_test")
    assert store.scalar("SELECT COUNT(*) FROM instances_KeyDate") == 4


def test_a_retry_after_a_partial_run_needs_no_force_and_does_not_duplicate(ready):
    store, document_id = ready

    def broken(**kwargs):
        raise RuntimeError("no model")

    set_populator(broken)
    extract(store, document_id, tier="local", actor_id="act_test")

    set_populator(populator([CONTRACT]))
    # The guard refuses a tier that already *succeeded*; a partial run is
    # exactly the case a retry is for, so it is not in its way.
    extract(store, document_id, tier="local", actor_id="act_test")
    # Findings are matched on what they are, so the deterministic pass running
    # twice does not write them twice.
    assert store.scalar("SELECT COUNT(*) FROM instances_KeyDate") == 4


def test_a_finding_is_matched_on_its_text_and_page(ready):
    store, document_id = ready
    run_deterministic_pass(store, document_id, bundle_mod.load(), "act_test")
    assert deterministic_finding_exists(store, "instances_KeyDate", document_id,
                                        "4 February 2026", 1)
    assert not deterministic_finding_exists(store, "instances_KeyDate", document_id,
                                            "4 February 2026", 2)


# -- confidence and provenance ----------------------------------------------

def test_confidence_is_snapped_to_the_rubric_whatever_the_engine_said(ready):
    store, document_id = ready
    set_populator(populator([
        {"type": "Company", "excerpt": "Ardmore Digital Limited",
         "confidence": 0.83, "properties": {"name": "Ardmore Digital Limited"}},
    ]))
    extract(store, document_id, tier="local", actor_id="act_test")
    assert store.scalar("SELECT confidence FROM instances_Company") == CONFIDENCE["implied"]


def test_provenance_records_what_the_machine_said_not_what_is_current(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")

    instance_id = store.scalar("SELECT instance_id FROM instances_Company")
    provenance = store.one("SELECT * FROM provenance WHERE instance_id = ?",
                           (instance_id,))
    assert provenance["source"] == "ai_local"
    assert provenance["excerpt"] == "Ardmore Digital Limited"
    assert provenance["page_no"] == 1

    # The instance row is what changes; this row is what does not.
    store.execute("UPDATE instances_Company SET name = ?, source = 'human' "
                  "WHERE instance_id = ?", ("Corrected Name", instance_id))
    unchanged = store.one("SELECT source, confidence FROM provenance WHERE instance_id = ?",
                          (instance_id,))
    assert unchanged["source"] == "ai_local"
    assert unchanged["confidence"] == provenance["confidence"]


# -- undeclared properties --------------------------------------------------

def test_an_undeclared_property_becomes_a_schema_amendment(ready):
    store, document_id = ready
    set_populator(populator([
        {"type": "Contract", "excerpt": "SERVICES AGREEMENT",
         "properties": {"name": "Services Agreement", "renewal_notice": "90 days"}},
    ]))
    extract(store, document_id, tier="local", actor_id="act_test")

    amendment = store.one("SELECT * FROM schema_amendments WHERE status = 'pending'")
    assert amendment["amendment_type"] == "new_property"
    assert amendment["type_id"] == "Contract"
    assert amendment["property_id"] == "renewal_notice"
    assert amendment["observed_value"] == "90 days"


def test_the_same_amendment_seen_twice_is_counted_not_repeated(ready, tmp_path):
    # A property appearing in one document out of two hundred is a different
    # proposition from one appearing in all of them.
    store, document_id = ready
    set_populator(populator([
        {"type": "Contract", "excerpt": "x",
         "properties": {"name": "One", "renewal_notice": "90 days"}},
    ]))
    extract(store, document_id, tier="local", actor_id="act_test")

    second = tmp_path / "second.txt"
    second.write_text("Another agreement entirely, dated 1 May 2026.")
    other = ingest(store, second, actor_id="act_test",
                   storage_root=tmp_path / "storage")["document_id"]
    extract(store, other, tier="local", actor_id="act_test")

    rows = store.query("SELECT property_id, occurrences FROM schema_amendments")
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2


def test_an_instance_missing_a_required_property_is_skipped_not_fatal(ready):
    # A model omitting a NOT NULL property is ordinary. Losing the whole
    # document's extraction over one such instance is not, so it is skipped and
    # the reason recorded where a reviewer will see it.
    store, document_id = ready
    set_populator(populator([
        {"type": "Contract", "excerpt": "x", "properties": {"reference": "no name"}},
        COMPANY,
    ]))
    result = extract(store, document_id, tier="local", actor_id="act_test")

    assert result["n_entities"] == 1                       # the Company survived
    assert store.scalar("SELECT COUNT(*) FROM instances_Contract") == 0
    reason = store.scalar("SELECT rationale FROM schema_amendments "
                          "WHERE rationale LIKE '%could not be stored%'")
    assert "Contract" in reason


def test_an_unknown_object_type_becomes_an_amendment_not_a_crash(ready):
    store, document_id = ready
    set_populator(populator([
        {"type": "Vessel", "excerpt": "MV Something", "properties": {"name": "MV Something"}},
    ]))
    result = extract(store, document_id, tier="local", actor_id="act_test")
    assert result["n_entities"] == 0
    assert store.one("SELECT amendment_type, type_id FROM schema_amendments "
                     "WHERE amendment_type = 'new_type'")["type_id"] == "Vessel"


# -- the domain block does the linking --------------------------------------

def test_deterministic_findings_are_linked_to_the_documents_primary_instance(ready):
    store, document_id = ready
    set_populator(populator([CONTRACT]))
    extract(store, document_id, tier="local", actor_id="act_test")

    contract_id = store.scalar("SELECT instance_id FROM instances_Contract")
    linked = store.query("SELECT contract_instance_id FROM instances_KeyDate")
    assert linked and all(r["contract_instance_id"] == contract_id for r in linked)


def test_the_naive_key_is_derived_not_taken_from_the_model(ready):
    # It must stay a pure function of `name`, or cross-document matching quietly
    # varies by extraction run.
    store, document_id = ready
    set_populator(populator([
        {"type": "Company", "excerpt": "Ardmore Digital Limited",
         "properties": {"name": "Ardmore Digital Limited", "naive_key": "NONSENSE"}},
    ]))
    extract(store, document_id, tier="local", actor_id="act_test")
    assert store.scalar("SELECT naive_key FROM instances_Company") == "ardmore digital"


# -- re-running a tier ------------------------------------------------------

def test_re_running_a_tier_is_refused_by_default(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")
    with pytest.raises(OrpheusError, match="already run"):
        extract(store, document_id, tier="local", actor_id="act_test")


def test_forcing_a_re_run_supersedes_the_unreviewed_rows(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")
    first = store.scalar("SELECT instance_id FROM instances_Company")

    result = extract(store, document_id, tier="local", actor_id="act_test", force=True)
    assert result["superseded"] > 0
    # Rejected rather than deleted: the superseded rows stay queryable as
    # evidence about extraction quality.
    assert store.scalar("SELECT status FROM instances_Company WHERE instance_id = ?",
                        (first,)) == "rejected"
    assert store.scalar("SELECT COUNT(*) FROM instances_Company") == 2
    assert len(document_instances(store, document_id, type_id="Company")) == 1


def test_a_re_run_never_discards_a_human_judgement(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")
    reviewed = store.scalar("SELECT instance_id FROM instances_Company")
    store.execute("UPDATE instances_Company SET status = 'confirmed' WHERE instance_id = ?",
                  (reviewed,))

    supersede_tier_instances(store, document_id, "ai_local", "act_test")
    assert store.scalar("SELECT status FROM instances_Company WHERE instance_id = ?",
                        (reviewed,)) == "confirmed"


# -- the gate ---------------------------------------------------------------

def test_the_cloud_tier_is_refused_before_anything_is_written(ready):
    store, document_id = ready
    set_populator(populator([COMPANY]))
    with pytest.raises(OrpheusError, match="Cloud processing is disabled"):
        extract(store, document_id, tier="cloud", opt_in=True, actor_id="act_test")
    assert store.scalar("SELECT COUNT(*) FROM extraction_runs") == 0
    assert store.scalar("SELECT COUNT(*) FROM instances_Company") == 0
