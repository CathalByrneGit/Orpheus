"""Two different pasts, kept apart.

An amendment dated 3 March and recorded on 14 November is true from March and
known from November. Ask what a contract said on 1 June and there are two right
answers, and a system that gives one without saying which is lying by omission.

The second question -- *why did the June report say what it said* -- is the one
almost no system can answer, because almost none keeps the record. These tests
hold that it can be answered here, and that the two are never merged.
"""

from __future__ import annotations

import json

import pytest

from orpheus import api, asof, bundle as bundle_mod, review
from orpheus.rubric import CONFIDENCE

TYPE = "KeyDate"


def _instance(store, instance_id, document_id, *, created_at, value=None,
              role=None, confidence=1.0, status="unconfirmed"):
    store.insert(f"instances_{TYPE}", {
        "instance_id": instance_id, "document_id": document_id,
        "value": value, "raw_text": value, "date_role": role,
        "source": "ai_local", "confidence": confidence, "status": status,
        "created_at": created_at})
    store.insert("instance_index", {
        "instance_id": instance_id, "document_id": document_id,
        "type_id": TYPE, "table_name": f"instances_{TYPE}",
        "created_at": created_at})
    store.insert("provenance", {
        "provenance_id": f"prov_{instance_id}", "instance_id": instance_id,
        "document_id": document_id, "excerpt": value or "x",
        "source": "ai_local", "confidence": confidence,
        "created_at": created_at})
    # The `extract` edit the real path writes. Without it a value timeline has
    # no beginning, and the fixture would be testing a store that cannot exist.
    store.insert("edit_history", {
        "id": f"edit_{instance_id}", "table_name": f"instances_{TYPE}",
        "row_id": instance_id, "document_id": document_id, "action": "extract",
        "new_value": json.dumps({"type_id": TYPE, "source": "ai_local",
                                 "properties": {"value": value,
                                                "date_role": role}}),
        "edited_by": "act_r", "edited_at": created_at})


@pytest.fixture
def history(store):
    """A corpus that grew over time, reviewed later than it was ingested.

    March: a contract signed, running to November 2026, nobody has seen it.
    August: it is ingested and extracted.
    September: somebody reviews it.

    Every question below is about which of those three moments you are asking
    from.
    """
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    store.insert("documents", {
        "document_id": "doc_early", "filename": "signed-in-march.txt",
        "file_hash": "h1", "date_added": "2026-08-01T09:00:00Z",
        "created_by": "act_r", "visibility": "private",
        "review_status": "unreviewed"})
    store.insert("documents", {
        "document_id": "doc_late", "filename": "ingested-in-october.txt",
        "file_hash": "h2", "date_added": "2026-10-01T09:00:00Z",
        "created_by": "act_r", "visibility": "private",
        "review_status": "unreviewed"})

    # The March contract, read out of the document in August.
    _instance(store, "kd_start", "doc_early", created_at="2026-08-01T10:00:00Z",
              value="2026-03-03", role="start")
    _instance(store, "kd_end", "doc_early", created_at="2026-08-01T10:00:00Z",
              value="2026-11-15", role="end")
    # A second contract, not ingested until October.
    _instance(store, "kd_late_start", "doc_late",
              created_at="2026-10-01T10:00:00Z", value="2026-01-01",
              role="start", confidence=CONFIDENCE["named"])
    return store


# -- transaction time: what the store believed -------------------------------

def test_before_a_document_was_ingested_it_is_not_in_the_past(history):
    """The October contract was signed in January and this store knew nothing
    about it in September. A report run in September could not have counted
    it, and reconstructing one that does would be a fiction."""
    september = asof.believed_at(history, "2026-09-01")
    assert september["n_documents"] == 1
    assert september["n_instances"] == 2

    november = asof.believed_at(history, "2026-11-01")
    assert november["n_documents"] == 2
    assert november["n_instances"] == 3


def test_a_review_that_had_not_happened_yet_does_not_count(history):
    """The point of the axis. Confirming something in September does not make
    it confirmed in August, and a report that said "0 reviewed" in August said
    so honestly."""
    review.confirm_instance(history, "kd_end", actor_id="act_r", note="checked")

    assert asof.believed_at(history, "2026-08-15")["n_reviewed"] == 0
    assert asof.believed_at(history, "2030-01-01")["n_reviewed"] == 1


def test_the_status_is_replayed_from_the_action_not_the_payload(history):
    """`confirm` means confirmed, `reject` means rejected. The action names the
    outcome, which is why this survives a redaction -- that nulls
    `previous_value` and `new_value` and leaves the actions in place."""
    review.confirm_instance(history, "kd_start", actor_id="act_r")
    review.reject_instance(history, "kd_end", actor_id="act_r", note="a clause number")
    history.execute("UPDATE edit_history SET previous_value = NULL, "
                    "new_value = NULL")

    state = asof.believed_at(history, "2030-01-01")
    assert state["n_reviewed"] == 2
    reviewed = {g["confidence_label"]: g for g in state["by_confidence"]}
    # One confirmed of two reviewed at `explicit`.
    assert reviewed["explicit"]["n_reviewed"] == 2
    assert reviewed["explicit"]["accuracy"] == 0.5


def test_a_date_with_no_time_means_the_whole_of_that_day(history):
    """`2026-08-01` against `2026-08-01T10:00:00Z` compared as strings would
    exclude everything recorded that day, which is not what anybody means."""
    assert asof.believed_at(history, "2026-08-01")["n_instances"] == 2
    assert asof.believed_at(history, "2026-07-31")["n_instances"] == 0


def test_the_verdict_then_is_computed_on_the_same_threshold_as_now(history):
    """So that "the report was silent then and speaks now" is itself a visible
    change, rather than the reason two runs look incomparable."""
    assert asof.believed_at(history, "2026-09-01")["verdict"] == \
        "insufficient_evidence"


def test_asking_about_no_date_is_refused(history):
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        asof.believed_at(history, "")


# -- valid time: what the documents say was running --------------------------

def test_a_contract_is_in_force_between_its_own_dates(history):
    """Nothing to do with when it was extracted or reviewed."""
    # Sorted by when each began: the January contract, then the March one.
    assert [e["document_id"] for e in
            asof.in_force_on(history, "2026-06-01")["in_force"]] == \
        ["doc_late", "doc_early"]

    before = asof.in_force_on(history, "2026-02-01")
    assert [e["document_id"] for e in before["not_yet_begun"]] == ["doc_early"]
    assert [e["document_id"] for e in before["in_force"]] == ["doc_late"]

    after = asof.in_force_on(history, "2026-12-01")
    assert [e["document_id"] for e in after["ended"]] == ["doc_early"]


def test_valid_time_does_not_care_when_the_document_was_ingested(history):
    """The October document is a January contract. It was running in June, and
    this store had never heard of it in June. Both are true."""
    june = asof.in_force_on(history, "2026-06-01")
    believed = asof.believed_at(history, "2026-06-01")
    assert "doc_late" in {e["document_id"] for e in june["in_force"]}
    assert believed["n_documents"] == 0


def test_a_document_with_no_start_date_cannot_be_placed_at_all(history):
    """Neither in force nor out of force. It has no beginning to compare
    against, and calling it either would be inventing a fact about it."""
    history.insert("documents", {
        "document_id": "doc_undated", "filename": "no-dates.txt",
        "file_hash": "h3", "date_added": "2026-01-01T00:00:00Z",
        "created_by": "act_r", "visibility": "private",
        "review_status": "unreviewed"})
    result = asof.in_force_on(history, "2026-06-01")
    assert [e["document_id"] for e in result["unplaceable"]] == ["doc_undated"]
    assert "cannot be placed on a timeline at all" in result["headline"]


def test_a_contract_with_no_end_date_is_still_running(history):
    history.execute("DELETE FROM instances_KeyDate WHERE instance_id = 'kd_end'")
    result = asof.in_force_on(history, "2030-01-01")
    entry = next(e for e in result["in_force"] if e["document_id"] == "doc_early")
    assert entry["ended"] is None


def test_a_rejected_date_does_not_place_a_contract(history):
    review.reject_instance(history, "kd_start", actor_id="act_r",
                           note="that is the signature date")
    result = asof.in_force_on(history, "2026-06-01")
    assert [e["document_id"] for e in result["unplaceable"]] == ["doc_early"]


def test_the_headline_says_how_many_rest_on_an_unchecked_date(history):
    result = asof.in_force_on(history, "2026-06-01")
    assert "rest on a date nobody has confirmed" in result["headline"]

    review.confirm_instance(history, "kd_start", actor_id="act_r")
    review.confirm_instance(history, "kd_end", actor_id="act_r")
    review.confirm_instance(history, "kd_late_start", actor_id="act_r")
    assert "rest on a date nobody has confirmed" not in \
        asof.in_force_on(history, "2026-06-01")["headline"]


def test_a_redacted_document_is_on_neither_timeline(history):
    from orpheus import redact
    redact.redact_document(history, "doc_early", actor_id="act_r",
                           note="Erasure request.")
    result = asof.in_force_on(history, "2026-06-01")
    everything = (result["in_force"] + result["ended"] + result["not_yet_begun"]
                  + result["unplaceable"])
    assert "doc_early" not in {e["document_id"] for e in everything}


# -- and never merged ---------------------------------------------------------

def test_compare_returns_both_and_says_they_are_different_questions(history):
    result = asof.compare(history, "2026-06-01")
    assert result["believed"]["axis"] == "transaction"
    assert result["in_force"]["axis"] == "valid"
    assert "two different pasts" in result["note"]
    # No combined count anywhere: there is no number that is both.
    assert set(result) == {"date", "believed", "in_force", "note", "now"}


# -- over the API -------------------------------------------------------------

def test_the_api_offers_one_axis_or_both(history):
    admin = dict(history.one("SELECT * FROM actors WHERE actor_id = 'act_r'"))
    status, both = api.handle(history, "GET", "/as-of",
                              body={"date": "2026-06-01"}, actor=admin)
    assert status == 200 and "believed" in both and "in_force" in both

    status, one = api.handle(history, "GET", "/as-of",
                             body={"date": "2026-06-01", "axis": "believed"},
                             actor=admin)
    assert status == 200 and one["axis"] == "transaction"

    status, bad = api.handle(history, "GET", "/as-of",
                             body={"date": "2026-06-01", "axis": "sideways"},
                             actor=admin)
    assert status == 400
    assert "what this store held then" in bad["error"]["message"]


def test_a_date_is_required(history):
    admin = dict(history.one("SELECT * FROM actors WHERE actor_id = 'act_r'"))
    status, payload = api.handle(history, "GET", "/as-of", actor=admin)
    assert status == 400 and "YYYY-MM-DD" in payload["error"]["message"]


# -- one value over time ------------------------------------------------------

def test_a_value_reads_as_a_timeline_rather_than_a_list_of_edits(history):
    """The question somebody asks out loud: the contract is for EUR 310,000 --
    what was it before, and who changed it?"""
    review.amend_instance(history, "kd_end", {"value": "2026-12-31"},
                          actor_id="act_r", note="clause 4 extends it")
    result = asof.value_history(history, "kd_end")
    changes = result["properties"]["value"]
    assert [c["became"] for c in changes] == ["2026-11-15", "2026-12-31"]
    assert changes[-1]["was"] == "2026-11-15"
    assert changes[-1]["by"] == "act_r"
    assert changes[-1]["note"] == "clause 4 extends it"
    assert changes[0]["seq"] < changes[-1]["seq"]


def test_one_property_can_be_asked_for_on_its_own(history):
    result = asof.value_history(history, "kd_end", property_id="value")
    assert set(result["properties"]) == {"value"}


def test_a_redacted_document_leaves_no_instance_to_ask_about(history):
    """Redaction deletes the instances themselves, so there is no `instance_id`
    left to name. What survives is the document's history -- that somebody
    amended something on a date -- with the values gone from it, which is
    exactly the split a redaction is for."""
    from orpheus import redact
    from orpheus.utils import NotFound
    review.amend_instance(history, "kd_end", {"value": "2026-12-31"},
                          actor_id="act_r", note="extends it")
    redact.redact_document(history, "doc_early", actor_id="act_r", note="Erasure.")

    with pytest.raises(NotFound):
        asof.value_history(history, "kd_end")

    entries = history.query(
        "SELECT action, new_value FROM edit_history WHERE document_id = ? "
        "ORDER BY seq", ("doc_early",))
    assert [e["action"] for e in entries][-1] == "redact"
    assert any(e["action"] == "amend" for e in entries)
    assert all(e["new_value"] is None for e in entries[:-1])


def test_an_unknown_instance_is_not_found(history):
    from orpheus.utils import NotFound
    with pytest.raises(NotFound):
        asof.value_history(history, "inst_nothing")
