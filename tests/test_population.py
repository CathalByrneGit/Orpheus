"""Population, and the grounding it now carries.

No model is called here. LangExtract's own result classes are constructed
directly, which is enough to test the part Orpheus owns: the translation from
grounded extractions into instances, and the mapping from how well a span
matched onto the confidence rubric.
"""

from __future__ import annotations

import pytest

from orpheus import llm
from orpheus.bundle import load as load_bundle
from orpheus.ingest import ingest
from orpheus.population import (confidence_for_alignment, normalise_population,
                                page_for_offset, page_offsets, populate,
                                prompt_for, set_populator)
from orpheus.rubric import CONFIDENCE
from orpheus.utils import OrpheusError

lx = pytest.importorskip("langextract")


@pytest.fixture
def seeded(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    path = tmp_path / "doc.txt"
    path.write_text("SERVICES AGREEMENT\nReference DOH-1.\fSecond page here.")
    result = ingest(store, path, actor_id="act_test", storage_root=tmp_path / "s")
    return store, result["document_id"]


def extraction(text, cls="Contract", start=None, end=None, alignment=None,
               attributes=None):
    return lx.data.Extraction(
        extraction_class=cls,
        extraction_text=text,
        char_interval=(lx.data.CharInterval(start_pos=start, end_pos=end)
                       if start is not None else None),
        alignment_status=alignment,
        attributes=attributes or {},
    )


# -- grounding as the confidence signal -------------------------------------

def test_how_well_a_span_matched_decides_its_rubric_level():
    # LangExtract reports no confidence score, which reads like a gap and is
    # closer to a virtue: a model's opinion of its own certainty is the thing
    # the rubric exists to avoid storing. Alignment is a fact about the text.
    assert confidence_for_alignment(lx.data.AlignmentStatus.MATCH_EXACT) == CONFIDENCE["explicit"]
    assert confidence_for_alignment(lx.data.AlignmentStatus.MATCH_GREATER) == CONFIDENCE["named"]
    assert confidence_for_alignment(lx.data.AlignmentStatus.MATCH_LESSER) == CONFIDENCE["named"]
    assert confidence_for_alignment(lx.data.AlignmentStatus.MATCH_FUZZY) == CONFIDENCE["implied"]


def test_an_extraction_that_cannot_be_found_in_the_text_is_only_inferred():
    # char_interval None means the model asserted something the document does
    # not say. That is the rubric's definition of inferred, not of explicit.
    assert confidence_for_alignment(None) == CONFIDENCE["inferred"]
    result = normalise_population({"extractions": [extraction("not in the document")]})
    entity = result["entities"][0]
    assert entity["confidence"] == CONFIDENCE["inferred"]
    assert entity["char_start"] is None


def test_an_unknown_alignment_value_does_not_get_the_benefit_of_the_doubt():
    assert confidence_for_alignment("something_new") == CONFIDENCE["inferred"]


# -- translation ------------------------------------------------------------

def test_extractions_become_instances_carrying_their_span():
    result = normalise_population(
        {"extractions": [
            extraction("Services Agreement", start=0, end=18,
                       alignment=lx.data.AlignmentStatus.MATCH_EXACT,
                       attributes={"name": "Services Agreement"}),
        ]},
        spans=[(1, 0, 40)], source_label="doc.pdf")

    entity = result["entities"][0]
    assert entity["type_id"] == "Contract"
    assert entity["properties"] == {"name": "Services Agreement"}
    assert entity["confidence"] == CONFIDENCE["explicit"]
    assert entity["page_no"] == 1
    assert entity["source_label"] == "doc.pdf"
    # The span itself, which is what a reading UI needs and what an excerpt
    # string cannot give -- an excerpt can only be searched for, and hoped to
    # occur once.
    assert (entity["char_start"], entity["char_end"]) == (0, 18)
    assert entity["alignment"] == "match_exact"
    assert entity["instance_id"].startswith("inst_")


def test_the_seam_accepts_plain_dicts_from_another_engine():
    # Nothing downstream should have to depend on LangExtract's classes for a
    # test double or a different library to be usable.
    result = normalise_population({"extractions": [{
        "type_id": "Company", "excerpt": "Northwind Ltd",
        "properties": {"name": "Northwind Ltd"}, "confidence": 0.9,
    }]})
    entity = result["entities"][0]
    assert entity["type_id"] == "Company"
    assert entity["confidence"] == 0.9


def test_an_arbitrary_confidence_is_snapped_onto_the_rubric():
    result = normalise_population({"extractions": [
        {"type_id": "Company", "confidence": 0.83},
    ]})
    assert result["entities"][0]["confidence"] == CONFIDENCE["implied"]


def test_an_edge_against_an_instance_that_was_not_returned_is_dropped():
    result = normalise_population({
        "extractions": [{"type_id": "Contract", "instance_id": "a"}],
        "relationships": [
            {"from_instance_id": "a", "to_instance_id": "ghost"},
        ],
    })
    assert result["relationships"] == []
    assert result["dropped_edges"] == 1


# -- offsets to pages -------------------------------------------------------

def test_a_character_offset_maps_to_the_page_it_falls_on(seeded):
    store, document_id = seeded
    spans = page_offsets(store, document_id)
    assert [s[0] for s in spans] == [1, 2]

    assert page_for_offset(spans, 0) == 1
    assert page_for_offset(spans, spans[1][1] + 5) == 2
    assert page_for_offset(spans, None) is None


def test_page_offsets_match_the_text_the_model_is_actually_given(seeded):
    # The offsets are computed by reconstructing that string, not by guessing.
    # R searched the excerpt for a "--- Page n ---" marker, which fails whenever
    # an excerpt does not happen to span one.
    from orpheus.ingest import document_text
    store, document_id = seeded
    text = document_text(store, document_id)
    for page_no, start, end in page_offsets(store, document_id):
        assert text[start:end].startswith(f"--- Page {page_no} ---")


# -- the prompt comes from the bundle ---------------------------------------

def test_the_prompt_is_written_from_the_bundle():
    prompt = prompt_for(load_bundle())
    assert "Contract" in prompt and "value_amount" in prompt
    # The platform owns the review columns and does not ask the model for them.
    asked_for = {line.strip().split(" ")[0]
                 for line in prompt.splitlines() if line.startswith("    ")}
    for reserved in ("instance_id", "document_id", "source", "confidence",
                     "status", "amended_by", "amended_at", "created_at"):
        assert reserved not in asked_for
    # Nor the container property: it holds an internal instance id linking a
    # child to its parent, which the model cannot know and would invent.
    assert "contract_instance_id" not in asked_for
    # The review `status` is excluded; OCDS `contract_status` is a real property
    # and stays. The two collided once and were renamed apart for this reason.
    assert "contract_status" in asked_for


def test_a_closed_codelist_is_given_to_the_model(bundle=None):
    prompt = prompt_for(load_bundle())
    assert "one of:" in prompt
    assert "selective" in prompt      # from the OCDS procurementMethod codelist


# -- the gate is not delegated ----------------------------------------------

def test_the_cloud_tier_is_refused_before_any_text_leaves(seeded):
    store, document_id = seeded
    with pytest.raises(OrpheusError, match="Cloud processing is disabled"):
        populate(store, document_id, tier="cloud", opt_in=True)
    # Refused before the call, so nothing was sent and nothing is logged.
    assert llm.cloud_calls(store) == []


def test_the_cloud_tier_needs_the_request_to_opt_in_too(seeded):
    store, document_id = seeded
    store.set_setting("cloud_ai_policy", "org_allow")
    with pytest.raises(OrpheusError, match="explicit per-request opt-in"):
        populate(store, document_id, tier="cloud", opt_in=False)
    assert llm.cloud_calls(store) == []


def test_a_registered_populator_replaces_the_engine(seeded):
    store, document_id = seeded
    calls = []

    def fake(*, store, document, bundle, text, tier, opt_in, actor_id):
        calls.append(tier)
        return {"extractions": [
            extraction("SERVICES AGREEMENT", start=text.index("SERVICES"),
                       end=text.index("SERVICES") + 18,
                       alignment=lx.data.AlignmentStatus.MATCH_EXACT,
                       attributes={"name": "Services Agreement"}),
        ]}

    previous = set_populator(fake)
    try:
        result = populate(store, document_id, tier="local")
    finally:
        set_populator(previous)

    assert calls == ["local"]
    entity = result["entities"][0]
    assert entity["page_no"] == 1
    assert entity["confidence"] == CONFIDENCE["explicit"]


def test_a_models_claimed_page_is_replaced_by_the_located_one():
    # Grounding is computed rather than trusted, and a page number is
    # grounding. On a real SEC contract the model put the APPOINTMENT clause
    # on page 2 and SOFTWARE LICENSES on page 6; the located spans were pages
    # 1 and 5. Keeping the claim on the instance row put two page numbers on
    # one finding, and the wrong one was the one a reader saw first.
    from orpheus.population import normalise_population

    text = ("--- Page 1 ---\nfirst page body here\n\n"
            "--- Page 2 ---\nAPPOINTMENT of the distributor\n\n"
            "--- Page 3 ---\nthird page body here")
    spans = [(1, 0, 35), (2, 35, 82), (3, 82, len(text))]

    result = normalise_population(
        {"extractions": [{
            "instance_id": "x1",
            "type_id": "Clause",
            "excerpt": "APPOINTMENT of the distributor",
            "properties": {"heading": "APPOINTMENT", "page_no": 99},
        }]},
        text=text, spans=spans)

    entity = result["entities"][0]
    assert entity["page_no"] == 2
    assert entity["properties"]["page_no"] == 2, \
        "the row must not keep a page number the store knows is wrong"


def test_a_type_without_a_page_property_does_not_gain_one():
    from orpheus.population import normalise_population

    text = "--- Page 1 ---\nAcme Ltd is a party\n"
    result = normalise_population(
        {"extractions": [{
            "instance_id": "x1", "type_id": "Company",
            "excerpt": "Acme Ltd",
            "properties": {"name": "Acme Ltd"},
        }]},
        text=text, spans=[(1, 0, len(text))])
    assert "page_no" not in result["entities"][0]["properties"]
