"""Extraction measured against labelled data rather than against itself.

Built on a small synthetic CUAD-format file, because the real corpus is 510
contracts and this is testing the scorer, not the model.
"""

from __future__ import annotations

import json

import pytest

import orpheus.bundle as bundle_mod
from orpheus.benchmark import (benchmark_extraction, cuad_category, load_cuad,
                               load_map, score_document)
from orpheus.population import set_populator
from orpheus.utils import OrpheusError

CONTEXT = ("This Agreement shall be governed by the laws of Ireland. "
           "The Supplier's aggregate liability shall not exceed the fees paid. "
           "Either party may terminate on ninety days notice.")


def cuad_file(tmp_path, entries=None):
    path = tmp_path / "cuad.json"
    path.write_text(json.dumps({"data": entries or [{
        "title": "Alpha Services Agreement",
        "paragraphs": [{
            "context": CONTEXT,
            "qas": [
                {"question": 'Highlight the parts related to "Governing Law".',
                 "answers": [{"text": "governed by the laws of Ireland",
                              "answer_start": 30}]},
                {"question": 'Highlight the parts related to "Cap On Liability".',
                 "answers": [{"text": "aggregate liability shall not exceed the fees paid",
                              "answer_start": 90}]},
                # Genuinely absent from this contract: a true negative.
                {"question": 'Highlight the parts related to "Audit Rights".',
                 "answers": [], "is_impossible": True},
                # Mapped by no entry in the clause map.
                {"question": 'Highlight the parts related to "Third Party Beneficiary".',
                 "answers": [{"text": "no third party", "answer_start": 0}]},
            ],
        }],
    }]}))
    return path


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


# -- reading the corpus -----------------------------------------------------

def test_a_category_is_recovered_from_the_question():
    assert cuad_category(
        'Highlight the parts (if any) related to "Governing Law" that should '
        "be reviewed.") == "Governing Law"
    # A question naming nothing in quotes still has to yield something.
    assert cuad_category("What is the term?") == "What is the term?"


def test_the_vocabulary_comes_from_the_data_not_from_a_constant(tmp_path):
    # A different CUAD release must not silently score against a stale list.
    cuad = load_cuad(cuad_file(tmp_path))
    assert cuad["categories"] == ["Audit Rights", "Cap On Liability",
                                  "Governing Law", "Third Party Beneficiary"]


def test_an_absent_clause_is_kept_as_a_true_negative(tmp_path):
    # is_impossible means the clause is genuinely absent. Dropping those makes
    # precision unmeasurable.
    cuad = load_cuad(cuad_file(tmp_path))
    audit = [l for l in cuad["labels"] if l["category"] == "Audit Rights"]
    assert len(audit) == 1
    assert audit[0]["present"] is False


def test_a_missing_or_empty_file_is_reported(tmp_path):
    with pytest.raises(OrpheusError, match="No CUAD file"):
        load_cuad(tmp_path / "nope.json")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"data": []}))
    with pytest.raises(OrpheusError, match="no entries"):
        load_cuad(empty)


def test_the_clause_map_is_read_out_of_its_provenance_wrapper():
    mapping = load_map()
    assert mapping["Governing Law"] == "governing_law"
    assert "source" not in mapping


# -- scoring ----------------------------------------------------------------

@pytest.fixture
def scored(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    cuad = load_cuad(cuad_file(tmp_path))

    set_populator(lambda **kwargs: {"extractions": [
        {"type": "Clause", "excerpt": "governed by the laws of Ireland",
         "properties": {"clause_type": "governing_law",
                        "text": "This Agreement shall be governed by the laws of Ireland."}},
        # The liability clause is extracted but mislabelled, so it should not
        # count towards Cap On Liability.
        {"type": "Clause", "excerpt": "aggregate liability",
         "properties": {"clause_type": "payment",
                        "text": "The Supplier's aggregate liability shall not exceed the fees paid."}},
    ]})
    result = benchmark_extraction(store, cuad, tier="local", limit=5,
                                  actor_id="act_admin",
                                  storage_root=tmp_path / "bench")
    return store, result


def test_a_labelled_span_inside_an_extracted_clause_counts_as_found(scored):
    _, result = scored
    by_category = {row["category"]: row for row in result["by_category"]}
    assert by_category["Governing Law"]["n_found"] == 1
    assert by_category["Governing Law"]["recall"] == 1.0


def test_a_clause_extracted_under_the_wrong_type_does_not_count(scored):
    _, result = scored
    by_category = {row["category"]: row for row in result["by_category"]}
    assert by_category["Cap On Liability"]["n_labelled"] == 1
    assert by_category["Cap On Liability"]["n_found"] == 0
    assert by_category["Cap On Liability"]["recall"] == 0.0


def test_an_unmapped_category_is_named_rather_than_scored_as_a_miss(scored):
    # It is a gap in the benchmark's configuration, and it changes what the
    # recall means — so it is reported, not folded into the denominator.
    _, result = scored
    assert "Third Party Beneficiary" in result["unmapped_categories"]
    unmapped = next(r for r in result["by_category"]
                    if r["category"] == "Third Party Beneficiary")
    assert unmapped["mapped"] is False
    assert unmapped["recall"] is None


def test_overall_recall_counts_only_mapped_categories(scored):
    _, result = scored
    # One of two mapped-and-labelled spans found.
    assert result["overall_recall"] == 0.5
    assert result["n_contracts"] == 1


def test_the_result_says_it_is_recall_only(scored):
    _, result = scored
    assert "Precision needs a judgement" in result["caveat"]


def test_worst_categories_come_first(scored):
    _, result = scored
    recalls = [r["recall"] for r in result["by_category"] if r["recall"] is not None]
    assert recalls == sorted(recalls)


def test_a_contract_that_will_not_extract_does_not_end_the_run(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    cuad = load_cuad(cuad_file(tmp_path))

    def broken(**kwargs):
        raise RuntimeError("no model")

    set_populator(broken)
    result = benchmark_extraction(store, cuad, tier="local", limit=5,
                                  actor_id="act_admin", storage_root=tmp_path / "b")
    assert result["n_contracts"] == 0
    assert result["failures"][0]["title"] == "Alpha Services Agreement"
    assert "No contract was extracted successfully" in result["note"]
