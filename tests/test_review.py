"""Review: a machine reading becoming a checked fact.

The property that matters throughout is that nothing is destructively
overwritten. After every verb here, what the machine originally said is still
recoverable — which is what makes extraction quality measurable later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.audit import row_history
from orpheus.extract import document_instances, extract
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.review import (amend_instance, bump_patch, confirm_instance,
                            mark_document_reviewed, reject_instance,
                            review_progress, review_schema_amendment,
                            schema_amendments)
from orpheus.rubric import CONFIDENCE
from orpheus.utils import NotFound, OrpheusError, from_json

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"

COMPANY = {"type": "Company", "excerpt": "Ardmore Digital Limited",
           "properties": {"name": "Ardmore Digital Limited", "role": "supplier"}}


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def extracted(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]

    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        return {"extractions": [COMPANY]}

    set_populator(engine)
    extract(store, document_id, tier="local", actor_id="act_test")
    instance_id = store.scalar("SELECT instance_id FROM instances_Company")
    return store, document_id, instance_id


# -- confirm ----------------------------------------------------------------

def test_confirming_keeps_the_value_and_records_the_check(extracted):
    store, _, instance_id = extracted
    confirm_instance(store, instance_id, "act_test")
    row = store.one("SELECT * FROM instances_Company WHERE instance_id = ?",
                    (instance_id,))
    assert row["status"] == "confirmed"
    assert row["amended_by"] == "act_test"
    # The value is untouched, and so is its origin.
    assert row["name"] == "Ardmore Digital Limited"
    assert row["source"] == "ai_local"


# -- amend ------------------------------------------------------------------

def test_amending_replaces_the_value_and_takes_responsibility_for_it(extracted):
    store, _, instance_id = extracted
    amend_instance(store, instance_id, {"name": "Ardmore Digital Ltd"},
                   "act_test", note="Matches the register")
    row = store.one("SELECT * FROM instances_Company WHERE instance_id = ?",
                    (instance_id,))
    assert row["name"] == "Ardmore Digital Ltd"
    assert row["status"] == "amended"
    # A human correction is ground truth: leaving the model's confidence in
    # place would mean a corrected row still read as a machine guess.
    assert row["source"] == "human"
    assert row["confidence"] == CONFIDENCE["explicit"]


def test_the_machines_reading_survives_the_correction(extracted):
    store, _, instance_id = extracted
    amend_instance(store, instance_id, {"name": "Ardmore Digital Ltd"}, "act_test")

    provenance = store.one("SELECT source, confidence, excerpt FROM provenance "
                           "WHERE instance_id = ?", (instance_id,))
    assert provenance["source"] == "ai_local"
    assert provenance["excerpt"] == "Ardmore Digital Limited"

    history = row_history(store, "instances_Company", instance_id)
    amendment = next(h for h in history if h["action"] == "amend")
    assert "Ardmore Digital Limited" in amendment["previous_value"]
    assert "Ardmore Digital Ltd" in amendment["new_value"]


def test_a_derived_key_is_recomputed_when_the_name_changes(extracted):
    # Otherwise a corrected name goes on matching under the old key.
    store, _, instance_id = extracted
    amend_instance(store, instance_id, {"name": "Northwind Trading PLC"}, "act_test")
    assert store.scalar("SELECT naive_key FROM instances_Company") == "northwind trading"


def test_amending_a_property_the_bundle_does_not_declare_is_refused(extracted):
    store, _, instance_id = extracted
    with pytest.raises(OrpheusError, match="not a declared property"):
        amend_instance(store, instance_id, {"favourite_colour": "blue"}, "act_test")
    # Adding a property changes the bundle for every document; that is a
    # different review from correcting one row.
    assert "schema amendment" in _last_error(store, instance_id)


def _last_error(store, instance_id):
    try:
        amend_instance(store, instance_id, {"favourite_colour": "blue"}, "act_test")
    except OrpheusError as exc:
        return str(exc)
    return ""


def test_a_reserved_column_cannot_be_amended_as_if_it_were_a_property(extracted):
    store, _, instance_id = extracted
    for reserved in ("status", "source", "confidence", "document_id"):
        with pytest.raises(OrpheusError, match="not a declared property"):
            amend_instance(store, instance_id, {reserved: "x"}, "act_test")


def test_an_empty_amendment_is_refused(extracted):
    store, _, instance_id = extracted
    with pytest.raises(OrpheusError, match="non-empty"):
        amend_instance(store, instance_id, {}, "act_test")


def test_an_unknown_instance_is_reported_as_missing(extracted):
    store, _, _ = extracted
    with pytest.raises(NotFound, match="No instance"):
        confirm_instance(store, "inst_nope", "act_test")


# -- reject -----------------------------------------------------------------

def test_rejecting_excludes_a_row_without_deleting_it(extracted):
    store, document_id, instance_id = extracted
    reject_instance(store, instance_id, "act_test", note="Misread from a letterhead")

    assert store.scalar("SELECT status FROM instances_Company WHERE instance_id = ?",
                        (instance_id,)) == "rejected"
    # Still there: a rejected extraction is evidence about extraction quality,
    # and deleting it would throw away the measurement with the mistake.
    assert store.scalar("SELECT COUNT(*) FROM instances_Company") == 1
    assert not [r for r in document_instances(store, document_id)
                if r["instance_id"] == instance_id]
    assert [r for r in document_instances(store, document_id, include_rejected=True)
            if r["instance_id"] == instance_id]


# -- progress and document state --------------------------------------------

def test_review_progress_counts_by_status(extracted):
    store, document_id, instance_id = extracted
    before = review_progress(store, document_id)
    assert before["unconfirmed"] == before["total"] > 0

    confirm_instance(store, instance_id, "act_test")
    after = review_progress(store, document_id)
    assert after["confirmed"] == 1
    assert after["unconfirmed"] == before["unconfirmed"] - 1
    assert after["total"] == before["total"]


def test_a_document_can_be_marked_reviewed_with_rows_outstanding(extracted):
    # "I have looked at this document" and "every row is confirmed" are
    # different claims. A reviewer may finish with a document while leaving rows
    # they cannot judge -- and the result says so rather than refusing.
    store, document_id, _ = extracted
    result = mark_document_reviewed(store, document_id, "act_test")
    assert result["review_status"] == "reviewed"
    assert result["unconfirmed_instances"] > 0
    assert "still unconfirmed" in result["note"]


def test_marking_a_document_unreviewed_clears_the_reviewer(extracted):
    store, document_id, _ = extracted
    mark_document_reviewed(store, document_id, "act_test")
    mark_document_reviewed(store, document_id, "act_test", reviewed=False)
    row = store.one("SELECT review_status, reviewed_by, reviewed_at FROM documents "
                    "WHERE document_id = ?", (document_id,))
    assert row["review_status"] == "unreviewed"
    assert row["reviewed_by"] is None and row["reviewed_at"] is None


# -- schema amendments ------------------------------------------------------

@pytest.fixture
def with_amendment(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "storage")["document_id"]

    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        return {"extractions": [{
            "type": "Contract", "excerpt": "SERVICES AGREEMENT",
            "properties": {"name": "Services Agreement",
                           "renewal_notice_period": "90 days"},
        }]}

    set_populator(engine)
    extract(store, document_id, tier="local", actor_id="act_admin")
    return store, schema_amendments(store)[0]


def test_accepting_a_new_property_changes_the_bundle_and_bumps_its_version(with_amendment):
    store, amendment = with_amendment
    result = review_schema_amendment(store, amendment["amendment_id"], "accepted",
                                     "act_admin")
    assert result["applied_to_bundle"] is True
    assert result["bundle_version"] == "0.2.1"

    # The column exists now, and the property is amendable where it was refused.
    assert "renewal_notice_period" in store.columns("instances_Contract")
    bundle = bundle_mod.active(store)
    contract = bundle_mod.object_type(bundle, "Contract")
    assert "renewal_notice_period" in bundle_mod.property_ids(contract)


def test_rejecting_an_amendment_leaves_the_bundle_alone(with_amendment):
    store, amendment = with_amendment
    result = review_schema_amendment(store, amendment["amendment_id"], "rejected",
                                     "act_admin", note="Not a real field")
    assert result["applied_to_bundle"] is False
    contract = bundle_mod.object_type(bundle_mod.active(store), "Contract")
    assert "renewal_notice_period" not in bundle_mod.property_ids(contract)


def test_an_amendment_cannot_be_reviewed_twice(with_amendment):
    store, amendment = with_amendment
    review_schema_amendment(store, amendment["amendment_id"], "accepted", "act_admin")
    with pytest.raises(OrpheusError, match="already accepted"):
        review_schema_amendment(store, amendment["amendment_id"], "rejected", "act_admin")


def test_a_new_type_is_recorded_but_not_applied_automatically(store, tmp_path):
    # Adding a type is a modelling decision with a shape to design, not a
    # column to append.
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "storage")["document_id"]

    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        return {"extractions": [{"type": "Vessel", "excerpt": "MV Something",
                                 "properties": {"name": "MV Something"}}]}

    set_populator(engine)
    extract(store, document_id, tier="local", actor_id="act_admin")
    amendment = next(a for a in schema_amendments(store)
                     if a["amendment_type"] == "new_type")
    result = review_schema_amendment(store, amendment["amendment_id"], "accepted",
                                     "act_admin")
    assert result["applied_to_bundle"] is False
    assert "not applied automatically" in result["note"]


def test_version_bumping():
    assert bump_patch("0.2.0") == "0.2.1"
    assert bump_patch("1.0.9") == "1.0.10"
    assert bump_patch("weird") == "weird.1"


def test_an_amendment_that_changes_nothing_is_refused(extracted):
    """A browser form posts every field it renders, changed or not.

    Accepting the unchanged ones would flip `source` to `human` on a value the
    machine got right, and the quality report counts amendments as machine
    errors a human had to fix.
    """
    store, _, instance_id = extracted
    current = store.one("SELECT * FROM instances_Company WHERE instance_id = ?",
                        (instance_id,))
    with pytest.raises(OrpheusError, match="Nothing was changed"):
        amend_instance(store, instance_id, {"name": current["name"]}, "act_test")
    after = store.one("SELECT source, status FROM instances_Company "
                      "WHERE instance_id = ?", (instance_id,))
    assert after["source"] == current["source"]
    assert after["status"] == current["status"]
    assert store.scalar("SELECT COUNT(*) FROM edit_history WHERE action = 'amend'") == 0


def test_unchanged_fields_are_dropped_from_a_real_amendment(extracted):
    # The history should read as what the person actually corrected, not as
    # every field the form happened to contain.
    store, _, instance_id = extracted
    current = store.one("SELECT * FROM instances_Company WHERE instance_id = ?",
                        (instance_id,))
    amend_instance(store, instance_id,
                   {"name": "Ardmore Digital Ltd",
                    "registration_number": current["registration_number"]},
                   "act_test")
    row = store.one("SELECT previous_value, new_value FROM edit_history "
                    "WHERE action = 'amend' ORDER BY seq DESC LIMIT 1")
    assert set(from_json(row["new_value"])) == {"name"}
    assert set(from_json(row["previous_value"])) == {"name"}
