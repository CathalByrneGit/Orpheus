"""A register: authoritative reference data, held apart from the corpus.

Two properties carry this file.

**Its rows never become facts.** A register import is trivially correct, so
counting its rows as extractions would inflate the number extraction quality
exists to report with work no model did — and a register row has no page and no
excerpt, so calling it an extraction would mean inventing provenance.

**Nothing counts until somebody has looked.** A staged register is present,
readable and not evidence. That is the whole of the review step the upload
plugins do not have.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.entities import create_entity, resolution_evidence
from orpheus.registers import (bearing_on, create_register, expose_column,
                               exposed_columns, get_register, hide_column,
                               list_registers, load_csv, matches_for, promote,
                               review_row, rows, withdraw)
from orpheus.utils import NotFound, OrpheusError

CSV = """name,company_number,address
Ardmore Digital Ltd,482991,12 Ushers Quay
Kestrel Medical Group,551200,3 Fitzwilliam Square
"""


@pytest.fixture
def store_with_bundle(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    return store


@pytest.fixture
def loaded(store_with_bundle):
    store = store_with_bundle
    register_id = create_register(store, "Companies Register",
                                  origin="downloaded 2026-08-01",
                                  actor_id="act_a")
    result = load_csv(store, register_id, CSV, type_id="Company",
                      actor_id="act_a")
    store.conn.commit()
    return store, register_id, result


# -- loading -----------------------------------------------------------------

def test_a_register_arrives_staged_and_is_not_evidence(loaded):
    store, register_id, _ = loaded
    assert get_register(store, register_id)["status"] == "staged"
    # Present and readable...
    assert len(rows(store, register_id)) == 2
    # ...and matching nothing, because nobody has vouched for it.
    assert matches_for(store, "Ardmore Digital Ltd", "Company") == []


def test_promoting_makes_it_evidence(loaded):
    store, register_id, _ = loaded
    assert promote(store, register_id, actor_id="act_a")["rows_accepted"] == 2
    hits = matches_for(store, "Ardmore Digital Limited", "Company")
    assert len(hits) == 1
    assert hits[0]["identifier"] == "482991"
    # Matched on the same weak basis the wiki is built on, and says so.
    assert hits[0]["basis"] == "naive_key"


def test_the_whole_row_is_kept_as_it_arrived(loaded):
    # A person reviewing a row should see the row, not a projection of it.
    store, register_id, _ = loaded
    first = rows(store, register_id)[0]
    assert first["values"] == {"name": "Ardmore Digital Ltd",
                               "company_number": "482991",
                               "address": "12 Ushers Quay"}


def test_which_columns_were_used_is_reported_not_assumed(loaded):
    # A register matched on the wrong column produces confident nonsense, and
    # the only defence is saying which column was used.
    _, _, result = loaded
    assert result["name_column"] == "name"
    assert result["identifier_column"] == "company_number"
    assert "confident nonsense" in result["caveat"]


def test_a_column_can_be_named_when_the_guess_would_be_wrong(store_with_bundle):
    store = store_with_bundle
    register_id = create_register(store, "Odd", actor_id="act_a")
    result = load_csv(store, register_id,
                      "party,ref\nArdmore Digital Ltd,482991\n",
                      name_column="party", identifier_column="ref",
                      actor_id="act_a")
    assert result["name_column"] == "party"


def test_a_file_with_no_recognisable_name_column_says_so(store_with_bundle):
    store = store_with_bundle
    register_id = create_register(store, "Opaque", actor_id="act_a")
    with pytest.raises(OrpheusError) as caught:
        load_csv(store, register_id, "a,b\n1,2\n", actor_id="act_a")
    assert "which column holds the name" in str(caught.value)


def test_names_normalise_by_the_type_the_register_is_about(store_with_bundle):
    # The same rule as everywhere else: a register of people and a register of
    # companies do not share a normalisation.
    store = store_with_bundle
    register_id = create_register(store, "People", actor_id="act_a")
    load_csv(store, register_id, "name,ref\nDr. Mitchell Felder,X1\n",
             type_id="Person", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")
    assert matches_for(store, "Mitchell Felder", "Person")


def test_rows_are_never_instances(loaded):
    # The property this whole design turns on.
    store, register_id, _ = loaded
    promote(store, register_id, actor_id="act_a")
    assert store.scalar("SELECT COUNT(*) FROM instance_index") == 0
    assert store.scalar("SELECT COUNT(*) FROM entities") == 0
    assert store.scalar("SELECT COUNT(*) FROM provenance") == 0


# -- reviewing ---------------------------------------------------------------

def test_a_rejected_row_stays_readable_and_stops_counting(loaded):
    store, register_id, _ = loaded
    review_row(store, register_id, 1, "rejected", note="wrong company",
               actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    assert matches_for(store, "Ardmore Digital Ltd", "Company") == []
    kept = [r for r in rows(store, register_id) if r["row_no"] == 1][0]
    assert kept["status"] == "rejected" and kept["note"] == "wrong company"


def test_promoting_accepts_what_is_still_staged_and_leaves_rejections(loaded):
    store, register_id, _ = loaded
    review_row(store, register_id, 2, "rejected", actor_id="act_a")
    assert promote(store, register_id, actor_id="act_a")["rows_accepted"] == 1
    statuses = {r["row_no"]: r["status"] for r in rows(store, register_id)}
    assert statuses == {1: "accepted", 2: "rejected"}


def test_a_register_cannot_be_promoted_twice(loaded):
    store, register_id, _ = loaded
    promote(store, register_id, actor_id="act_a")
    with pytest.raises(OrpheusError):
        promote(store, register_id, actor_id="act_a")


def test_withdrawing_stops_it_being_evidence_without_deleting_it(loaded):
    # A register somebody relied on and then withdrew is part of the record of
    # how a decision was reached.
    store, register_id, _ = loaded
    promote(store, register_id, actor_id="act_a")
    withdraw(store, register_id, actor_id="act_a", note="superseded")

    assert matches_for(store, "Ardmore Digital Ltd", "Company") == []
    assert len(rows(store, register_id)) == 2
    assert get_register(store, register_id)["status"] == "withdrawn"


def test_rows_only_load_into_a_staged_register(loaded):
    store, register_id, _ = loaded
    promote(store, register_id, actor_id="act_a")
    with pytest.raises(OrpheusError) as caught:
        load_csv(store, register_id, CSV, actor_id="act_a")
    assert "staged register" in str(caught.value)


def test_every_step_is_in_the_edit_history(loaded):
    store, register_id, _ = loaded
    review_row(store, register_id, 1, "rejected", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")
    actions = {r["action"] for r in store.query(
        "SELECT action FROM edit_history WHERE table_name IN "
        "('registers', 'register_rows')")}
    assert {"create", "load", "review", "promote"} <= actions


def test_an_unknown_register_is_a_message_not_a_traceback(store_with_bundle):
    with pytest.raises(NotFound):
        get_register(store_with_bundle, "reg_nope")


# -- what it says about two pages --------------------------------------------

@pytest.fixture
def two_pages(store_with_bundle):
    store = store_with_bundle
    a = create_entity(store, "Company", "Ardmore Digital Ltd",
                      actor_id="act_a", source="ai_local")
    b = create_entity(store, "Company", "Ardmore Digital Limited",
                      actor_id="act_a", source="ai_local")
    store.conn.commit()
    return store, a, b


def test_one_register_row_for_both_pages_argues_for(two_pages):
    store, a, b = two_pages
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    found = bearing_on(store, {"entity_id": a, "canonical_name": "Ardmore Digital Ltd",
                               "type_id": "Company"},
                       {"entity_id": b, "canonical_name": "Ardmore Digital Limited",
                        "type_id": "Company"})
    assert found["shared_identifiers"] == ["482991"]
    assert found["identifiers_conflict"] is False
    assert "strongest evidence for one thing" in found["reading"]


def test_different_registered_numbers_argue_against(store_with_bundle):
    """The first thing in this codebase that can argue *against* a merge with
    something better than a spelling."""
    store = store_with_bundle
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id,
             "name,company_number\n"
             "EFTC Operating Corp,111111\n"
             "K-TEC Operating Corp,222222\n",
             type_id="Company", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    found = bearing_on(
        store, {"entity_id": "ent_a", "canonical_name": "EFTC Operating Corp",
                "type_id": "Company"},
        {"entity_id": "ent_b", "canonical_name": "K-TEC Operating Corp",
         "type_id": "Company"})
    assert found["identifiers_conflict"] is True
    assert found["shared_identifiers"] == []
    assert "two organisations" in found["reading"]
    # And it says how it could be wrong, because a bad match here argues
    # confidently for the wrong answer.
    assert "normalised name" in found["reading"]


def test_no_register_row_is_not_evidence_of_difference(two_pages):
    store, a, b = two_pages
    found = bearing_on(store, {"entity_id": a, "canonical_name": "Someone",
                               "type_id": "Company"},
                       {"entity_id": b, "canonical_name": "Somebody",
                        "type_id": "Company"})
    assert "absence of evidence" in found["reading"]


def test_a_row_for_one_page_only_says_nothing(store_with_bundle):
    store = store_with_bundle
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    found = bearing_on(
        store, {"entity_id": "ent_a", "canonical_name": "Ardmore Digital Ltd",
                "type_id": "Company"},
        {"entity_id": "ent_b", "canonical_name": "Nobody At All",
         "type_id": "Company"})
    assert "rarely complete" in found["reading"]


def test_the_dossier_carries_what_the_register_says(two_pages):
    store, a, b = two_pages
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    evidence = resolution_evidence(store, a, b)
    assert evidence["registers"]["shared_identifiers"] == ["482991"]


def test_promoting_a_register_makes_an_earlier_judgement_stale(two_pages):
    # A judgement does not outlive its evidence, and a register promoted after
    # somebody decided is exactly the new evidence that rule is for.
    from orpheus.entities import resolution_verdict, review_resolution

    store, a, b = two_pages
    review_resolution(store, a, b, "different",
                      rationale="Nothing to tell them apart on.",
                      actor_id="act_a")
    assert resolution_verdict(store, a, b)["stale"] is False

    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")
    promote(store, register_id, actor_id="act_a")

    assert resolution_verdict(store, a, b)["stale"] is True


def test_a_staged_register_changes_nothing(two_pages):
    # It is present and readable, and it is not evidence until somebody says so.
    from orpheus.entities import resolution_verdict, review_resolution

    store, a, b = two_pages
    review_resolution(store, a, b, "different", rationale="Nothing yet.",
                      actor_id="act_a")
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")

    assert resolution_verdict(store, a, b)["stale"] is False
    assert list_registers(store)[0]["n_rows"] == 2


# -- over the API ------------------------------------------------------------

def test_promoting_a_register_is_an_administrator_decision(store_with_bundle):
    """Not because the rows are sensitive. A register is reference data every
    later answer rests on, so vouching for it is taking responsibility for what
    it decides."""
    from orpheus.api import handle

    store = store_with_bundle
    register_id = create_register(store, "Companies", actor_id="act_a")
    load_csv(store, register_id, CSV, type_id="Company", actor_id="act_a")
    store.conn.commit()

    ordinary = {"actor_id": "act_b", "is_admin": 0}
    admin = {"actor_id": "act_a", "is_admin": 1}

    status, refused = handle(store, "POST", f"/registers/{register_id}/promote",
                             {}, actor=ordinary)
    assert status == 403 and "vouch for" in refused["error"]["message"]
    # And reading it is not restricted: a staged register is meant to be looked
    # at, which is the point of the review step.
    assert handle(store, "GET", f"/registers/{register_id}", {},
                  actor=ordinary)[0] == 200

    assert handle(store, "POST", f"/registers/{register_id}/promote", {},
                  actor=admin)[0] == 200
    assert get_register(store, register_id)["status"] == "active"


def test_the_api_says_a_staged_register_is_not_evidence(store_with_bundle):
    from orpheus.api import handle

    store = store_with_bundle
    create_register(store, "Companies", actor_id="act_a")
    store.conn.commit()
    _, payload = handle(store, "GET", "/registers", {},
                        actor={"actor_id": "act_a", "is_admin": 1})
    assert "is not evidence" in payload["reading"]


# ---------------------------------------------------------------------------
# Exposing a column
# ---------------------------------------------------------------------------
#
# `load_csv` lifts three fields into real columns and leaves the rest in
# `values_json`, readable and unqueryable. These defend the way out of that, and
# the two properties that make it safe to offer: the values are never copied,
# and a column nobody exposed cannot be filtered on.

WIDE_CSV = """name,company_number,county,status
Ardmore Digital Ltd,482991,Mayo,Normal
Kestrel Medical Group,551200,Dublin,Normal
Halloran Instruments,771020,Mayo,Dissolved
"""


@pytest.fixture
def wide(store_with_bundle):
    store = store_with_bundle
    register_id = create_register(store, "Companies Register", actor_id="act_a")
    load_csv(store, register_id, WIDE_CSV, type_id="Company", actor_id="act_a")
    store.conn.commit()
    return store, register_id


def test_a_key_in_the_json_is_not_queryable_until_it_is_exposed(wide):
    store, register_id = wide
    assert exposed_columns(store) == []
    with pytest.raises(OrpheusError) as refused:
        rows(store, register_id, column="county", value="Mayo")
    assert "not an exposed column" in str(refused.value)


def test_exposing_a_key_makes_it_filterable(wide):
    store, register_id = wide
    result = expose_column(store, "county", actor_id="act_a")
    assert (result["column"], result["n_rows"], result["n_total"]) == \
        ("county", 3, 3)

    found = rows(store, register_id, column="county", value="Mayo")
    assert len(found) == 2
    assert all(r["values"]["county"] == "Mayo" for r in found)
    # It reads through on the row too, so anything doing dict(row) sees it.
    assert found[0]["county"] == "Mayo"


def test_the_index_is_used_rather_than_a_scan(wide):
    """The whole point. Measured on register-shaped rows the gap is 82x at
    5,000 rows and 96x at 200,000 -- but what makes it a different kind of
    query rather than a faster one is the plan."""
    store, _ = wide
    expose_column(store, "county", actor_id="act_a")
    plan = " ".join(str(r["detail"]) for r in store.query(
        'EXPLAIN QUERY PLAN SELECT * FROM register_rows WHERE "county" = ?',
        ("Mayo",)))
    assert "USING INDEX" in plan and "SCAN" not in plan


def test_nothing_is_copied_and_nothing_is_lost(wide):
    """An exposed column holds no data of its own, which is why hiding one is
    safe in a way `schema_ops.drop_property` is not."""
    store, register_id = wide
    before = [r["values_json"] for r in store.query(
        "SELECT values_json FROM register_rows ORDER BY row_no")]

    expose_column(store, "county", actor_id="act_a")
    assert hide_column(store, "county", actor_id="act_a")["key"] == "county"

    after = [r["values_json"] for r in store.query(
        "SELECT values_json FROM register_rows ORDER BY row_no")]
    assert after == before
    assert exposed_columns(store) == []
    # And it can be exposed again, which is what makes it undoable.
    expose_column(store, "county", actor_id="act_a")
    assert len(rows(store, register_id, column="county", value="Mayo")) == 2


def test_a_key_no_register_carries_says_so_rather_than_looking_fine(wide):
    store, _ = wide
    result = expose_column(store, "Sic Code", actor_id="act_a")
    # The column exists and every value in it is null. Saying so beats letting
    # somebody discover it by filtering and getting nothing back.
    assert (result["column"], result["n_rows"]) == ("sic_code", 0)
    assert "No row carries" in result["reading"]


def test_a_column_the_table_owns_cannot_be_shadowed(wide):
    store, _ = wide
    for key in ("name", "identifier", "values_json", "Status"):
        with pytest.raises(OrpheusError) as refused:
            expose_column(store, key, actor_id="act_a")
        assert "already owns" in str(refused.value)


def test_a_key_a_json_path_cannot_address_is_refused(wide):
    """`$."k"` cannot express a key containing a quote, and the failure is
    silent: the path parses and extracts NULL from every row."""
    store, _ = wide
    with pytest.raises(OrpheusError) as refused:
        expose_column(store, 'we"ird', actor_id="act_a")
    assert "double quote" in str(refused.value)


def test_exposing_twice_is_refused_with_the_key_it_came_from(wide):
    store, _ = wide
    expose_column(store, "county", actor_id="act_a")
    with pytest.raises(OrpheusError) as refused:
        expose_column(store, "County", actor_id="act_a")
    assert "already exposed" in str(refused.value)


def test_what_is_exposed_is_read_from_the_schema_not_a_second_table(wide):
    """No table records this. The column either exists or it does not, and a
    second copy of that fact is a second thing that can be wrong."""
    store, _ = wide
    expose_column(store, "county", actor_id="act_a")
    found = exposed_columns(store)
    assert [(c["column"], c["key"], c["n_rows"]) for c in found] == \
        [("county", "county", 3)]
    # Prove it: drop the column behind the module's back and it stops being
    # reported, because the schema is the only place this is written down.
    # The index has to go first -- SQLite refuses to leave one dangling, which
    # is why `hide_column` drops them in that order.
    store.execute("DROP INDEX idx_register_rows_county")
    store.execute('ALTER TABLE register_rows DROP COLUMN "county"')
    assert exposed_columns(store) == []


def test_the_decision_is_recorded_like_every_other(wide):
    store, _ = wide
    expose_column(store, "county", actor_id="act_a",
                  note="reviewers keep asking for it")
    edit = store.one("SELECT * FROM edit_history WHERE action = 'expose_column'")
    assert edit["note"] == "reviewers keep asking for it"
    assert edit["edited_by"] == "act_a"


def test_hiding_something_that_is_not_exposed(wide):
    store, _ = wide
    with pytest.raises(NotFound):
        hide_column(store, "county", actor_id="act_a")


def test_a_key_whose_name_the_table_owns_can_be_exposed_under_another(wide):
    """`register_rows` has a `status` -- the review state of the row -- and a
    companies register with its own `status` column is the ordinary case, not
    the exotic one. Found by loading a real 20,000-row register."""
    store, register_id = wide
    result = expose_column(store, "status", as_column="company_status",
                           actor_id="act_a")
    assert (result["column"], result["key"]) == ("company_status", "status")
    found = rows(store, register_id, column="company_status", value="Dissolved")
    assert len(found) == 1
    assert found[0]["values"]["status"] == "Dissolved"
    # The row's own review state is untouched, which is the collision this
    # avoided.
    assert found[0]["status"] == "staged"


def test_the_index_only_covers_the_half_that_was_exposed(wide):
    """Worth knowing before designing around this: SQLite uses the index for
    the exposed predicate and then extracts JSON for every row it returns. On a
    real register that made a two-predicate filter 3.6x rather than 58x. A pair
    of predicates is worth a pair of exposed columns."""
    store, register_id = wide
    expose_column(store, "county", actor_id="act_a")
    mixed = store.query(
        'EXPLAIN QUERY PLAN SELECT row_no FROM register_rows '
        'WHERE "county" = ? AND json_extract(values_json, \'$.status\') = ?',
        ("Mayo", "Dissolved"))
    plan = " ".join(str(r["detail"]) for r in mixed)
    assert "USING INDEX" in plan          # the exposed half is a search...
    assert store.query(
        "SELECT row_no FROM register_rows WHERE \"county\" = ? "
        "AND json_extract(values_json, '$.status') = ?",
        ("Mayo", "Dissolved"))            # ...and the other half still works

    # Exposing the second one is what makes both predicates indexed.
    expose_column(store, "status", as_column="company_status", actor_id="act_a")
    both = " ".join(str(r["detail"]) for r in store.query(
        'EXPLAIN QUERY PLAN SELECT row_no FROM register_rows '
        'WHERE "county" = ? AND "company_status" = ?', ("Mayo", "Dissolved")))
    assert "USING INDEX" in both and "SCAN" not in both
