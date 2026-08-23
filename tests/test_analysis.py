"""Cross-document comparison, and its limits.

The limits are the substance. Every test that asserts a match works is paired
with one asserting the result says how weak the match is — because the whole
module is a stepping stone to entity resolution and the danger is that it gets
mistaken for the real thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.analysis import (compare_primary_values, corpus_analysis,
                              identifier_matches, object_set_by_interface)
from orpheus.extract import extract
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.review import reject_instance
from orpheus.rubric import NAIVE_RESOLUTION
from orpheus.utils import NotFound, OrpheusError

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


def seed(store, tmp_path, documents):
    """Ingest one text document per entry and extract the entities given."""
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    ids = []
    for index, entities in enumerate(documents):
        path = tmp_path / f"doc-{index}.txt"
        path.write_text(f"Agreement number {index}. Dated 1 March 2026.")
        document_id = ingest(store, path, actor_id="act_admin",
                             storage_root=tmp_path / "storage")["document_id"]
        set_populator(lambda entities=entities, **kwargs: {"extractions": entities})
        extract(store, document_id, tier="local", actor_id="act_admin",
                deterministic=False)
        ids.append(document_id)
    return ids


def company(name, registration=None, **extra):
    properties = {"name": name, **extra}
    if registration:
        properties["registration_number"] = registration
    return {"type": "Company", "excerpt": name, "properties": properties}


def contract(name, amount, currency="EUR"):
    return {"type": "Contract", "excerpt": name,
            "properties": {"name": name, "value_amount": amount,
                           "value_currency": currency}}


# -- the interface query ----------------------------------------------------

def test_an_interface_query_spans_every_type_that_implements_it(store, tmp_path):
    # A name is a name, whether it belongs to a Company or a Person, and asking
    # "what else is called this?" should not have to know which.
    seed(store, tmp_path, [[company("Ardmore Digital Limited"),
                            {"type": "Person", "excerpt": "Nuala Ryan",
                             "properties": {"name": "Nuala Ryan"}}]])
    rows = object_set_by_interface(store, "Named")
    assert {r["type_id"] for r in rows} == {"Company", "Person"}
    assert {"name", "naive_key", "type_id"} <= set(rows[0])


def test_an_unknown_interface_lists_the_ones_that_exist(store, tmp_path):
    seed(store, tmp_path, [[company("A")]])
    with pytest.raises(NotFound, match="Known:"):
        object_set_by_interface(store, "Winged")


def test_rejected_rows_are_left_out_of_an_interface_query(store, tmp_path):
    seed(store, tmp_path, [[company("Ardmore Digital Limited")]])
    instance_id = store.scalar("SELECT instance_id FROM instances_Company")
    reject_instance(store, instance_id, "act_admin")
    assert object_set_by_interface(store, "Named") == []
    assert len(object_set_by_interface(store, "Named", include_rejected=True)) == 1


# -- name matching, and its weakness ----------------------------------------

def test_the_same_name_in_two_documents_is_matched(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Ardmore Digital Limited")],
                                  [company("Ardmore Digital Ltd")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")
    assert result["matched_companies"] == 1
    match = result["counterparties"][0]
    assert match["appears_in_documents"] == 1
    # Differing spellings under one key are the clearest signal that this needs
    # real resolution, so they are surfaced rather than smoothed over.
    assert match["spelling_varies"] is True
    assert "Ardmore Digital Ltd" in match["name_variants"]


def test_every_result_is_labelled_unresolved(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Ardmore Digital Limited")],
                                  [company("Ardmore Digital Limited")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")
    assert result["resolution_quality"] == NAIVE_RESOLUTION
    assert "not resolved entities" in result["caveat"]
    stored = store.one("SELECT resolution_quality FROM concept_evaluations "
                       "WHERE kind = 'corpus'")
    assert stored["resolution_quality"] == NAIVE_RESOLUTION


def test_the_known_failure_of_the_naive_key_is_not_hidden(store, tmp_path):
    # "Ernst & Young" and "Ernst and Young" produce different keys. The test
    # exists so the limitation cannot quietly disappear.
    docs = seed(store, tmp_path, [[company("Ernst & Young")],
                                  [company("Ernst and Young")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")
    assert result["matched_companies"] == 0


def test_a_cross_type_match_is_reported_separately(store, tmp_path):
    # A name that is a company here and a person elsewhere is exactly what a
    # reviewer wants to see — and it is weaker than a same-type match.
    docs = seed(store, tmp_path, [
        [company("Morgan Bailey")],
        [{"type": "Person", "excerpt": "Morgan Bailey",
          "properties": {"name": "Morgan Bailey"}}]])
    match = corpus_analysis(store, docs[0], actor_id="act_admin")["counterparties"][0]
    assert match["appears_in_documents"] == 0        # no same-type match
    assert match["cross_type_matches"][0]["type_id"] == "Person"


# -- identifier matching, which is exact ------------------------------------

def test_a_registration_number_matches_where_the_name_does_not(store, tmp_path):
    # The identifier exists precisely to catch what normalisation misses.
    docs = seed(store, tmp_path, [
        [company("Meridian Systems Limited", registration="IE123456")],
        [company("Meridian Sys. Ltd", registration="IE123456")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")

    assert result["identifier_matched"] == 1
    match = result["counterparties"][0]
    assert match["identifier_matches"][0]["registration_number"] == "IE123456"
    # Proven rather than guessed: the same registered entity, written two ways.
    assert match["identifier_matches"][0]["name_differs"] is True


def test_an_identifier_match_is_reported_without_a_name_match(store, tmp_path):
    # Requiring a name match first would make the identifier useless. This
    # regressed once in R and was caught by exactly this case.
    docs = seed(store, tmp_path, [
        [company("Wholly Different Name", registration="IE999")],
        [company("Nothing Alike At All", registration="IE999")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")
    assert result["matched_companies"] == 1
    assert result["counterparties"][0]["appears_in_documents"] == 0
    assert result["counterparties"][0]["identifier_matches"]


def test_identifier_matching_needs_the_column_to_exist(store, tmp_path):
    seed(store, tmp_path, [[company("A")]])
    document_id = store.scalar("SELECT document_id FROM documents")
    assert identifier_matches(store, document_id) == {}


# -- value comparison, driven by the domain block ---------------------------

def test_values_are_compared_against_documents_sharing_a_counterparty(store, tmp_path):
    docs = seed(store, tmp_path, [
        [company("Ardmore Digital Limited"), contract("This one", 3_000_000)],
        [company("Ardmore Digital Limited"), contract("Peer A", 1_000_000)],
        [company("Ardmore Digital Limited"), contract("Peer B", 2_000_000)]])
    comparison = corpus_analysis(store, docs[0],
                                 actor_id="act_admin")["value_comparison"]
    assert comparison["available"] is True
    assert comparison["this_value"] == 3_000_000
    assert comparison["peer_count"] == 2
    assert comparison["peer_median"] == 1_500_000
    assert comparison["ratio_to_median"] == 2.0


def test_values_are_only_compared_within_one_currency(store, tmp_path):
    # Converting would need a rate for the right date, which is not something
    # to invent — so other currencies are excluded, and the result says so.
    docs = seed(store, tmp_path, [
        [company("Ardmore"), contract("This one", 1_000_000, "EUR")],
        [company("Ardmore"), contract("Peer", 900_000, "USD")]])
    comparison = corpus_analysis(store, docs[0],
                                 actor_id="act_admin")["value_comparison"]
    assert comparison["available"] is False
    assert "other currencies" in comparison["reason"]


def test_each_unavailable_comparison_gives_its_own_reason(store, tmp_path):
    # "No comparable value in this domain", "nothing extracted here" and "no
    # peers to compare against" call for different actions, so they are
    # different messages rather than one empty result.
    docs = seed(store, tmp_path, [
        [company("Solo Ltd"), contract("This one", 1_000_000)],
        [company("Entirely Other Ltd"), contract("Unrelated", 2_000_000)]])
    comparison = corpus_analysis(store, docs[0],
                                 actor_id="act_admin")["value_comparison"]
    assert comparison["available"] is False
    assert "share a counterparty" in comparison["reason"]


def test_a_document_with_nothing_extracted_says_that_instead(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Ardmore")], [company("Ardmore")]])
    comparison = corpus_analysis(store, docs[0],
                                 actor_id="act_admin")["value_comparison"]
    assert comparison["available"] is False
    assert "No value_amount has been extracted" in comparison["reason"]


def test_a_domain_with_no_comparable_value_says_so(store, tmp_path):
    seed(store, tmp_path, [[company("A")], [company("B")]])
    bundle = bundle_mod.active(store)
    del bundle["extensions"]["orpheus"]["valueProperty"]
    document_id = store.scalar("SELECT document_id FROM documents LIMIT 1")
    comparison = compare_primary_values(store, bundle, document_id, [])
    assert comparison["available"] is False
    assert "declares no comparable value" in comparison["reason"]


# -- the run itself ---------------------------------------------------------

def test_analysis_needs_more_than_one_document(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Only One")]])
    with pytest.raises(OrpheusError, match="more than one document"):
        corpus_analysis(store, docs[0], actor_id="act_admin")


def test_the_run_is_recorded_as_an_evaluation_with_its_dependencies(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Ardmore Digital Limited")],
                                  [company("Ardmore Digital Limited")]])
    result = corpus_analysis(store, docs[0], actor_id="act_admin")

    evaluation = store.one("SELECT * FROM concept_evaluations WHERE kind = 'corpus'")
    assert evaluation["evaluation_id"] == result["evaluation_id"]
    assert evaluation["scope"] == "database"
    # The instances it read, so an amendment to any of them marks it stale.
    assert store.scalar(
        "SELECT COUNT(*) FROM concept_evaluation_dependencies WHERE evaluation_id = ?",
        (evaluation["evaluation_id"],)) >= 2


def test_a_rejected_company_does_not_match(store, tmp_path):
    docs = seed(store, tmp_path, [[company("Ardmore Digital Limited")],
                                  [company("Ardmore Digital Limited")]])
    other = store.scalar("SELECT instance_id FROM instances_Company "
                         "WHERE document_id = ?", (docs[1],))
    reject_instance(store, other, "act_admin", note="Misread")
    assert corpus_analysis(store, docs[0],
                           actor_id="act_admin")["matched_companies"] == 0
