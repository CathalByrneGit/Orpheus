"""Renaming and dropping a property on a table that already holds rows.

`apply_schema()` is additive by design, so before this a mistake in an ontology
was permanent in any store that had already run it. The property these tests
guard is that the table and the bundle move together: a store where they
disagree is worse than one that cannot rename, because extraction would then
write to a column the ontology does not declare.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.ingest import ingest
from orpheus.schema_ops import (drop_property, force_drop_property,
                                rename_property)
from orpheus.utils import OrpheusError

pytest.importorskip("sqlite_utils")

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


@pytest.fixture
def seeded(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "T",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    bundle_mod.apply_schema(store, bundle_mod.load())
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, naive_key,"
        " registration_number, source, confidence, status, created_at)"
        " VALUES ('i1',?,'Ardmore Digital Limited','ardmore digital','612884',"
        " 'ai_local',1.0,'confirmed',datetime('now'))", (document_id,))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, document_id,"
        " created_at) VALUES ('i1','Company','instances_Company',?,datetime('now'))",
        (document_id,))
    return store, document_id


# -- rename ------------------------------------------------------------------

def test_renaming_keeps_the_values(seeded):
    store, _ = seeded
    before = store.scalar("SELECT registration_number FROM instances_Company")

    result = rename_property(store, "Company", "registration_number",
                             "company_number", actor_id="act_test")

    assert store.scalar("SELECT company_number FROM instances_Company") == before
    assert result["bundle_version"] != "0.2.0"


def test_the_bundle_moves_with_the_table(seeded):
    """The invariant. A store whose two disagree is worse than one that cannot
    rename, because extraction would write to an undeclared column."""
    store, _ = seeded
    rename_property(store, "Company", "registration_number", "company_number",
                    actor_id="act_test")

    obj = bundle_mod.object_type(bundle_mod.active(store), "Company")
    properties = bundle_mod.property_ids(obj)
    assert "company_number" in properties
    assert "registration_number" not in properties

    columns = {r[1] for r in store.execute("PRAGMA table_info(instances_Company)")}
    assert "company_number" in columns and "registration_number" not in columns
    # And the property still points at the column it is stored in.
    renamed = next(p for p in obj["properties"] if p["id"] == "company_number")
    assert renamed["source"]["column"] == "company_number"


def test_indexes_survive_a_rename(seeded):
    store, _ = seeded
    before = {r[0] for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='instances_Company'")}
    rename_property(store, "Company", "address", "registered_address",
                    actor_id="act_test")
    after = {r[0] for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='instances_Company'")}
    assert before == after


def test_provenance_still_points_at_the_row(seeded):
    # transform() drops and recreates the table with foreign keys on.
    store, document_id = seeded
    store.execute(
        "INSERT INTO provenance (provenance_id, instance_id, document_id,"
        " source_label, page_no, excerpt, confidence, created_at, source)"
        " VALUES ('p1','i1',?,'a.pdf',1,'Ardmore',1.0,datetime('now'),'ai_local')",
        (document_id,))
    rename_property(store, "Company", "registration_number", "company_number",
                    actor_id="act_test")
    assert store.scalar("SELECT COUNT(*) FROM provenance WHERE instance_id='i1'") == 1
    assert list(store.execute("PRAGMA foreign_key_check")) == []


def test_the_rename_is_recorded(seeded):
    store, _ = seeded
    rename_property(store, "Company", "registration_number", "company_number",
                    actor_id="act_test")
    row = store.one("SELECT note, previous_value, new_value FROM edit_history "
                    "WHERE action = 'schema' ORDER BY seq DESC LIMIT 1")
    assert "registration_number" in row["note"] and "company_number" in row["note"]


# -- refusals ----------------------------------------------------------------

def test_provenance_columns_cannot_be_renamed(seeded):
    store, _ = seeded
    for reserved in ("source", "confidence", "status", "document_id"):
        with pytest.raises(OrpheusError, match="provenance the store owns"):
            rename_property(store, "Company", reserved, "something",
                            actor_id="act_test")


def test_renaming_onto_an_existing_property_is_refused(seeded):
    store, _ = seeded
    with pytest.raises(OrpheusError, match="already has a property"):
        rename_property(store, "Company", "registration_number", "name",
                        actor_id="act_test")


def test_an_unknown_property_or_type_is_refused(seeded):
    store, _ = seeded
    with pytest.raises(OrpheusError, match="no property"):
        rename_property(store, "Company", "nonexistent", "x", actor_id="act_test")
    with pytest.raises(OrpheusError, match="no object type"):
        rename_property(store, "Nonexistent", "name", "x", actor_id="act_test")


# -- drop --------------------------------------------------------------------

def test_dropping_a_populated_property_is_refused_by_default(seeded):
    """Nothing else in this store deletes anything.

    Rejected rows are kept precisely because they are evidence, so a silent
    destructive default here would be out of character with everything around
    it.
    """
    store, _ = seeded
    with pytest.raises(OrpheusError, match="still holds 1 value"):
        drop_property(store, "Company", "registration_number", actor_id="act_test")
    # And nothing was changed on the way to refusing.
    assert store.scalar("SELECT registration_number FROM instances_Company") == "612884"


def test_dropping_an_empty_property_is_allowed(seeded):
    store, _ = seeded
    result = drop_property(store, "Company", "address", actor_id="act_test")
    assert result["values_discarded"] == 0
    columns = {r[1] for r in store.execute("PRAGMA table_info(instances_Company)")}
    assert "address" not in columns
    assert "address" not in bundle_mod.property_ids(
        bundle_mod.object_type(bundle_mod.active(store), "Company"))


def test_forcing_a_drop_says_how_much_was_discarded(seeded):
    store, _ = seeded
    result = force_drop_property(store, "Company", "registration_number",
                                 actor_id="act_test")
    assert result["values_discarded"] == 1
    row = store.one("SELECT note, previous_value FROM edit_history "
                    "WHERE action = 'schema' ORDER BY seq DESC LIMIT 1")
    assert "discarding 1 value" in row["note"]
