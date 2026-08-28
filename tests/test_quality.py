"""The report Phase 1 exists to produce.

These tests build a corpus with known review outcomes and then check the report
says the true thing about it — including when the honest answer is "not enough
evidence yet", which is the answer it will give most often in practice.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.concepts import evaluate_concepts, setup_concepts
from orpheus.extract import extract, run_deterministic_pass
from orpheus.ingest import ingest
from orpheus.population import set_populator
from orpheus.quality import (codelist_violations, concept_precision,
                             grounding,
                             confidence_calibration, extraction_quality,
                             property_corrections, quality_report)
from orpheus.review import amend_instance, confirm_instance, reject_instance
from orpheus.rubric import CONFIDENCE

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def corpus(store, tmp_path):
    """Ten companies at two confidence levels, with known review outcomes."""
    store.insert("actors", {"actor_id": "act_admin", "display_name": "Admin",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "storage")["document_id"]

    entities = []
    for i in range(6):        # explicit: mostly right
        entities.append({"type": "Company", "excerpt": "Ardmore Digital Limited",
                         "confidence": 1.0, "properties": {"name": f"Explicit {i}"}})
    for i in range(6):        # inferred: mostly wrong
        entities.append({"type": "Company", "excerpt": "not in the document",
                         "confidence": 0.5, "properties": {"name": f"Inferred {i}"}})
    set_populator(lambda **kwargs: {"extractions": entities})
    extract(store, document_id, tier="local", actor_id="act_admin",
            deterministic=False)

    rows = store.query("SELECT instance_id, name, confidence FROM instances_Company "
                       "ORDER BY name")
    explicit = [r for r in rows if r["name"].startswith("Explicit")]
    inferred = [r for r in rows if r["name"].startswith("Inferred")]

    for row in explicit[:5]:
        confirm_instance(store, row["instance_id"], "act_admin")
    amend_instance(store, explicit[5]["instance_id"], {"name": "Corrected"},
                   "act_admin")
    for row in inferred[:2]:
        confirm_instance(store, row["instance_id"], "act_admin")
    for row in inferred[2:4]:
        amend_instance(store, row["instance_id"], {"name": "Fixed"}, "act_admin")
    for row in inferred[4:]:
        reject_instance(store, row["instance_id"], "act_admin", note="Invented")

    return store, document_id


# -- accuracy ---------------------------------------------------------------

def test_nothing_extracted_says_so_rather_than_reporting_zero(store):
    result = extraction_quality(store)
    assert result["overall"]["n_total"] == 0
    assert result["overall"]["accuracy"] is None
    assert "Nothing has been extracted" in result["note"]


def test_outcomes_are_split_three_ways_not_two(corpus):
    # "amended" is wrong-in-detail-but-worth-keeping. Collapsing it into
    # "wrong" would make a working extractor with a sloppy field look broken.
    store, _ = corpus
    overall = extraction_quality(store)["overall"]
    assert overall["n_reviewed"] == 12
    assert overall["n_confirmed"] == 7
    assert overall["n_amended"] == 3
    assert overall["n_rejected"] == 2
    assert overall["accuracy"] == round(7 / 12, 3)
    assert overall["amend_rate"] == round(3 / 12, 3)


def test_coverage_says_how_much_of_the_population_was_reviewed(corpus):
    store, document_id = corpus
    # Add an unreviewed instance and coverage should fall.
    set_populator(lambda **kwargs: {"extractions": [
        {"type": "Company", "excerpt": "Ardmore Digital Limited",
         "properties": {"name": "Unreviewed"}}]})
    extract(store, document_id, tier="local", actor_id="act_admin",
            force=True, deterministic=False)
    overall = extraction_quality(store)["overall"]
    assert overall["coverage"] < 1.0


def test_a_rate_from_too_few_rows_is_suppressed_not_shown(corpus):
    # A rate computed from three rows is noise wearing a number's clothes.
    store, _ = corpus
    strict = extraction_quality(store, min_reviewed=50)
    assert all(row["accuracy"] is None for row in strict["by_type"])
    assert strict["overall"]["accuracy"] is not None      # overall is not suppressed


# -- calibration ------------------------------------------------------------

def test_the_rubric_is_checked_for_actually_ranking(corpus):
    store, _ = corpus
    result = confidence_calibration(store, min_reviewed=5)
    assert result["verdict"] == "monotonic"
    assert result["inversions"] == []

    levels = {row["confidence_label"]: row["accuracy"] for row in result["levels"]}
    assert levels["explicit"] > levels["inferred"]


def test_an_inverted_rubric_is_called_out(store, tmp_path):
    # A rubric that does not rank is worse than no rubric, because people trust
    # it. This must be loud.
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "s")["document_id"]

    entities = ([{"type": "Company", "excerpt": "x", "confidence": 1.0,
                  "properties": {"name": f"High {i}"}} for i in range(6)]
                + [{"type": "Company", "excerpt": "y", "confidence": 0.5,
                    "properties": {"name": f"Low {i}"}} for i in range(6)])
    set_populator(lambda **kwargs: {"extractions": entities})
    extract(store, document_id, tier="local", actor_id="act_admin",
            deterministic=False)

    rows = store.query("SELECT instance_id, name FROM instances_Company ORDER BY name")
    for row in rows:
        if row["name"].startswith("High"):
            reject_instance(store, row["instance_id"], "act_admin")   # high, wrong
        else:
            confirm_instance(store, row["instance_id"], "act_admin")  # low, right

    result = confidence_calibration(store, min_reviewed=5)
    assert result["verdict"] == "inverted"
    assert result["inversions"][0]["higher_level"] == "explicit"
    assert "not ranking reliability" in result["note"]


def test_too_little_evidence_is_reported_as_such(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    result = confidence_calibration(store)
    assert result["verdict"] == "insufficient_evidence"
    assert "Review more before trusting the rubric" in result["note"]


# -- concept precision ------------------------------------------------------

def test_a_rule_that_keeps_getting_dismissed_shows_up_as_low_precision(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    setup_concepts(store, actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "s")["document_id"]
    set_populator(lambda **kwargs: {"extractions": [{
        "type": "Contract", "excerpt": "SERVICES AGREEMENT",
        "properties": {"name": "Agreement", "value_amount": 2000000,
                       "value_currency": "EUR", "procurement_procedure": "direct",
                       "signature_block_present": "no"}}]})
    extract(store, document_id, tier="local", actor_id="act_admin", deterministic=False)
    evaluate_concepts(store, document_id, actor_id="act_admin")

    flags = store.query("SELECT instance_id, flag_type FROM instances_Flag")
    for flag in flags:
        if flag["flag_type"] == "direct_award":
            reject_instance(store, flag["instance_id"], "act_admin", note="Was open")
        else:
            confirm_instance(store, flag["instance_id"], "act_admin")

    precision = {row["concept_id"]: row for row in
                 concept_precision(store, min_reviewed=1)}
    assert precision["direct_award"]["precision"] == 0.0
    assert precision["high_value"]["precision"] == 1.0
    # Worst first: the rules worth fixing are the ones people keep dismissing.
    assert concept_precision(store, min_reviewed=1)[0]["concept_id"] == "direct_award"


def test_rule_flags_are_kept_out_of_the_extraction_numbers(store, tmp_path):
    # A concept flag has no provenance row, because it is not an extraction.
    # Give concept flags provenance and this quietly starts reporting rule
    # precision as extraction accuracy.
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    setup_concepts(store, actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "s")["document_id"]
    set_populator(lambda **kwargs: {"extractions": [{
        "type": "Contract", "excerpt": "SERVICES AGREEMENT",
        "properties": {"name": "Agreement", "value_amount": 2000000,
                       "value_currency": "EUR"}}]})
    extract(store, document_id, tier="local", actor_id="act_admin", deterministic=False)
    evaluate_concepts(store, document_id, actor_id="act_admin")

    assert store.scalar("SELECT COUNT(*) FROM instances_Flag") > 0
    types = {row["type_id"] for row in extraction_quality(store)["by_type"]}
    assert "Flag" not in types


# -- property corrections ---------------------------------------------------

def test_the_fields_people_keep_fixing_are_reported_with_an_example(corpus):
    store, _ = corpus
    corrections = property_corrections(store)
    name = next(c for c in corrections if c["property_id"] == "name")
    assert name["n_corrections"] == 3
    # A worked example is worth more than a count: it shows what kind of
    # mistake this is.
    assert name["example_was"] is not None
    assert name["example_now"] in ("Corrected", "Fixed")


# -- codelists --------------------------------------------------------------

def test_a_value_outside_a_closed_codelist_is_reported(store, tmp_path):
    store.insert("actors", {"actor_id": "act_admin", "display_name": "A",
                            "is_admin": 1, "created_at": "t"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_admin")
    document_id = ingest(store, PDF, actor_id="act_admin",
                         storage_root=tmp_path / "s")["document_id"]
    set_populator(lambda **kwargs: {"extractions": [{
        "type": "Contract", "excerpt": "SERVICES AGREEMENT",
        "properties": {"name": "Agreement", "procurement_procedure": "by_carrier_pigeon"}}]})
    extract(store, document_id, tier="local", actor_id="act_admin", deterministic=False)

    violations = codelist_violations(store)
    assert len(violations) == 1
    assert violations[0]["property_id"] == "procurement_procedure"
    assert violations[0]["value"] == "by_carrier_pigeon"
    assert "open" in violations[0]["allowed"]


# -- the whole report -------------------------------------------------------

def test_the_report_says_the_verdict_out_loud(corpus):
    store, _ = corpus
    report = quality_report(store, min_reviewed=5)
    assert "confirmed as extracted" in report["headline"]
    assert report["calibration"]["verdict"] == "monotonic"
    assert set(report) == {"headline", "extraction", "calibration", "grounding",
                           "concept_precision", "property_corrections",
                           "codelist_violations"}


def test_the_report_refuses_to_pronounce_on_too_little_evidence(store):
    report = quality_report(store)
    assert "Not enough to say anything" in report["headline"]


# -- grounding ---------------------------------------------------------------

def test_grounding_separates_a_fabrication_from_an_unsure_model(store, tmp_path):
    """`confidence` alone cannot tell the two apart, and they are opposites.

    A row at `inferred` may be there because the engine reported low confidence
    in a quotation the document plainly contains, or because it invented one.
    The first is a calibrated model; the second cannot be trusted with a
    citation.
    """
    store.insert("actors", {"actor_id": "act_test", "display_name": "T",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]

    set_populator(lambda **kw: {"extractions": [
        # Verbatim -- the fixture really says this.
        {"type": "Company", "excerpt": "Ardmore Digital Limited",
         "properties": {"name": "Ardmore Digital Limited"}},
        # Nothing like this is anywhere in the document.
        {"type": "Company", "excerpt": "Invented Holdings Incorporated of Atlantis",
         "properties": {"name": "Invented Holdings Incorporated"}},
    ]})
    extract(store, document_id, tier="local", actor_id="act_test")

    report = grounding(store)
    ai = next(e for e in report["by_source"] if e["source"] == "ai_local")
    assert ai["n_grounded"] >= 1
    assert ai["n_fabricated"] == 1
    assert 0 < ai["fabrication_rate"] < 1
    assert "does not contain" in report["note"]

    # And the two are distinguishable in the store, not merely in the summary.
    rows = {r["alignment"]: r["excerpt"] for r in
            store.query("SELECT alignment, excerpt FROM provenance "
                        "WHERE excerpt LIKE '%Ardmore Digital Limited%' "
                        "   OR excerpt LIKE 'Invented%'")}
    assert rows[None].startswith("Invented")
    assert "match_exact" in rows


def test_the_deterministic_pass_is_grounded_by_construction(store, tmp_path):
    # It finds a value *by* matching the text, so it cannot assert something
    # the page does not contain. That is exactly what separates it from a model.
    store.insert("actors", {"actor_id": "act_test", "display_name": "T",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    run_deterministic_pass(store, document_id, bundle_mod.load(), "act_test")

    rows = store.query("SELECT alignment, char_start, char_end, excerpt "
                       "FROM provenance WHERE source_label LIKE 'deterministic:%'")
    assert rows
    assert all(r["alignment"] == "match_exact" for r in rows)
    assert all(r["char_start"] is not None and r["char_end"] > r["char_start"]
               for r in rows)
    assert grounding(store)["by_source"][0]["fabrication_rate"] == 0


def test_the_span_points_at_the_document_not_the_page(store, tmp_path):
    """Both passes write to one pair of columns, so both must mean the same.

    The deterministic pass reads a page at a time and the model pass reads the
    joined document. Page-local offsets in the same column would highlight the
    right span on the wrong page.
    """
    from orpheus.population import document_text

    store.insert("actors", {"actor_id": "act_test", "display_name": "T",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    run_deterministic_pass(store, document_id, bundle_mod.load(), "act_test")

    text = document_text(store, document_id)
    for row in store.query("SELECT p.char_start, p.char_end, k.raw_text "
                           "FROM provenance p JOIN instances_KeyDate k "
                           "  ON k.instance_id = p.instance_id"):
        assert text[row["char_start"]:row["char_end"]] == row["raw_text"]


def test_the_report_names_the_fields_people_keep_fixing(store):
    # `orpheus report` crashed with KeyError('type_id') the first time anything
    # was ever amended: the CLI read type_id/property/n while the function
    # returns table_name/property_id/n_corrections. The block could not run
    # until a store had an amendment in it, so nothing caught it until a real
    # review corrected an invented clause heading.
    from orpheus.cli import cmd_report
    from orpheus.quality import property_corrections

    from orpheus.audit import record_edit
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1,
                            "created_at": "2026-01-01T00:00:00Z"})
    record_edit(store, "instances_Clause", "inst_1", None, "amend",
                previous={"heading": "Governing Law"}, new={"heading": ""},
                actor_id="act_a")
    store.conn.commit()

    rows = property_corrections(store)
    assert rows, "an amendment should be reported as a correction"
    for key in ("table_name", "property_id", "n_corrections"):
        assert key in rows[0], f"the CLI reads {key} and would crash without it"
