"""Proposing an ontology for a corpus that does not have one yet.

The claim the project rests on is that the engine is domain-neutral and the
contract bundle is an example. `test_domain_neutrality.py` defends that for a
domain somebody has already modelled. This file defends the step before it: a
corpus arrives, nobody has written a bundle for it, and the machine helps.

What is on trial here is the line between helping and deciding. A survey that
wrote a bundle would make the one kind of decision that is expensive to undo --
an object type is every row that will ever be filed under it -- so almost
everything below is a way of asking "did anything reach the bundle that a
person did not put there".
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus import ontology
from orpheus.ontology import (DEFAULT_PRIMARY_TYPE, candidates, draft_bundle,
                              get_candidate, header_fields, infer_data_type,
                              property_id_for, reopen_candidate,
                              review_candidate, survey)
from orpheus.utils import NotFound, OrpheusError

# Deliberately not contracts, and deliberately a real convention: a run of
# `Key: Value` lines at the top of a plain-text document is what mail, PEPs,
# Debian control files and most memo templates all inherit from RFC 822.
PROPOSALS = {
    "prop-1.txt": (
        "PEP: 1\n"
        "Title: PEP Purpose and Guidelines\n"
        "Author: Barry Warsaw, Jeremy Hylton\n"
        "Status: Active\n"
        "Type: Process\n"
        "Created: 13-Jun-2000\n"
        "\n"
        "This PEP describes how proposals are made and decided.\n"),
    "prop-2.txt": (
        "PEP: 8\n"
        "Title: Style Guide for Python Code\n"
        "Author: Guido van Rossum\n"
        "Status: Active\n"
        "Type: Process\n"
        "Created: 05-Jul-2001\n"
        "\n"
        "This document gives coding conventions.\n"),
    "prop-3.txt": (
        "PEP: 20\n"
        "Title: The Zen of Python\n"
        "Author: Tim Peters\n"
        "Status: Active\n"
        "Type: Informational\n"
        "Created: 19-Aug-2004\n"
        "Odd-Field: only this one document has it\n"
        "\n"
        "Beautiful is better than ugly.\n"),
}


@pytest.fixture
def corpus(store):
    """A store with documents and the starter bundle: no object types at all.

    That is the state this module exists for. Registering the contract bundle
    here would be testing the survey against an ontology that already answers
    the question.
    """
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1,
                            "created_at": "2026-01-01T00:00:00Z"})
    starter = bundle_mod.load(bundle_mod.BUNDLE_DIR / "starter-0.1.0.json")
    bundle_mod.register(store, starter, actor_id="act_a")
    bundle_mod.apply_schema(store, starter)
    for index, (filename, text) in enumerate(PROPOSALS.items(), start=1):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, "
            "byte_size, n_pages, date_added, created_by, visibility, "
            "review_status) VALUES (?,?,?,?,1,?,'act_a','private',"
            "'unreviewed')",
            (f"doc_{index}", filename, f"h{index}", len(text),
             f"2026-01-0{index}T00:00:00Z"))
        store.execute(
            "INSERT INTO document_pages (document_id, page_no, text, "
            "text_source, char_count) VALUES (?,1,?,'native',?)",
            (f"doc_{index}", text, len(text)))
    store.conn.commit()
    return store


def _by_property(rows, property_id):
    return next((r for r in rows if r["property_id"] == property_id), None)


# -- the starting state ------------------------------------------------------

def test_a_bundle_with_no_object_types_is_valid(corpus):
    """The schema used to require a primary object type of every bundle, which
    made "nobody has modelled this corpus yet" an invalid state to be in."""
    starter = bundle_mod.load(bundle_mod.BUNDLE_DIR / "starter-0.1.0.json")
    bundle_mod.validate(starter)
    assert starter["objects"] == []


def test_a_bundle_with_object_types_still_needs_a_primary_one():
    with pytest.raises(OrpheusError) as invalid:
        bundle_mod.validate(bundle_mod.normalise({
            "specVersion": "0.1.0", "bundleId": "b", "bundleVersion": "0.1.0",
            "objects": [{"id": "Thing", "primaryKey": "instance_id",
                         "source": {"kind": "table", "table": "instances_Thing"},
                         "properties": [{"id": "instance_id", "type": "string"}]}],
            "extensions": {"orpheus": {"documentTypes": ["other"]}},
        }))
    assert "primaryObjectType" in str(invalid.value)


# -- reading a header block --------------------------------------------------

def test_a_run_of_fields_is_a_header_block():
    fields = header_fields(PROPOSALS["prop-1.txt"])
    assert [f["key"] for f in fields] == [
        "PEP", "Title", "Author", "Status", "Type", "Created"]
    assert fields[1]["value"] == "PEP Purpose and Guidelines"


def test_a_colon_in_prose_is_not_a_field():
    # Two lines with colons happen constantly in prose. Three in a row is where
    # it stops being a coincidence, and that threshold is the whole of what
    # keeps this pass from proposing `Note` as a property of everything.
    prose = ("As the court held in Smith v Jones: the duty is owed.\n"
             "Note: this is discussed below.\n"
             "\nThe rest of the document follows.\n")
    assert header_fields(prose) == []


def test_a_wrapped_value_does_not_end_the_block():
    """RFC 822 folding. Treating an indented continuation as the end of the
    block would split one header in two and then discard both halves for being
    shorter than three lines."""
    folded = ("Title: A very long title that\n"
              "    runs onto a second line\n"
              "Author: Ada\n"
              "Status: Draft\n")
    fields = header_fields(folded)
    assert [f["key"] for f in fields] == ["Title", "Author", "Status"]
    assert fields[0]["value"].endswith("runs onto a second line")


def test_a_field_name_becomes_a_column_name():
    assert property_id_for("Post-History") == "post_history"
    assert property_id_for("PEP") == "pep"


def test_one_disagreeing_value_makes_the_column_a_string():
    # Nine numbers and a word is not a number column: it is a string column
    # whose tenth value is the interesting one.
    assert infer_data_type(["1", "8", "20"]) == "integer"
    assert infer_data_type(["1", "8", "20", "3.11"]) == "number"
    assert infer_data_type(["1", "8", "draft"]) == "string"
    assert infer_data_type(["2000-06-13", "2001-07-05"]) == "date"
    assert infer_data_type([]) == "string"


# -- surveying ---------------------------------------------------------------

def test_a_survey_proposes_the_fields_the_corpus_declares(corpus):
    result = survey(corpus, actor_id="act_a")
    properties = [c for c in result["candidates"] if c["kind"] == "property"]
    assert {c["property_id"] for c in properties} >= {
        "pep", "title", "author", "type", "created"}
    assert result["n_documents_read"] == 3


def test_a_survey_writes_no_bundle_and_no_table(corpus):
    """The one thing that must not happen. An object type is every row that
    will ever be filed under it, and a survey that created one would be making
    that decision on nobody's authority."""
    before = bundle_mod.active(corpus)["objects"]
    survey(corpus, actor_id="act_a")
    assert bundle_mod.active(corpus)["objects"] == before == []
    tables = [r["name"] for r in corpus.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'instances_%'")]
    assert tables == []


def test_a_field_in_one_document_is_not_a_finding(corpus):
    # `Odd-Field` appears in exactly one of the three. A pattern cannot be read
    # off a single observation, and a survey that reported every stray line
    # would hand a reviewer a queue nobody finishes.
    result = survey(corpus, actor_id="act_a")
    assert _by_property(result["candidates"], "odd_field") is None
    assert result["n_below_support"] >= 1


def test_lowering_the_threshold_surfaces_it(corpus):
    result = survey(corpus, actor_id="act_a", min_support=1)
    assert _by_property(result["candidates"], "odd_field") is not None


def test_support_is_counted_not_claimed(corpus):
    result = survey(corpus, actor_id="act_a")
    title = _by_property(result["candidates"], "title")
    assert (title["n_documents"], title["n_sampled"]) == (3, 3)


def test_every_candidate_carries_a_located_quotation(corpus):
    """Grounding computed, not trusted -- the rule the rest of the store
    follows. The quotation is the whole of what a reviewer has to check a
    proposed type against."""
    result = survey(corpus, actor_id="act_a")
    for candidate in result["candidates"]:
        assert candidate["evidence"], candidate["type_id"]
        for item in candidate["evidence"]:
            text = corpus.scalar(
                "SELECT text FROM document_pages WHERE document_id = ?",
                (item["document_id"],))
            assert item["excerpt"] in text
            assert item["char_start"] is not None


def test_a_reserved_column_is_never_proposed(corpus):
    """`Status:` is an extremely common header and `status` is the column the
    store writes review state into. Caught here rather than by bundle
    validation three steps later."""
    result = survey(corpus, actor_id="act_a")
    assert _by_property(result["candidates"], "status") is None


def test_the_type_it_proposes_is_deliberately_colourless(corpus):
    result = survey(corpus, actor_id="act_a")
    types = [c for c in result["candidates"] if c["kind"] == "object_type"]
    assert [t["type_id"] for t in types] == [DEFAULT_PRIMARY_TYPE]
    # It found fields, not a thing. Naming the thing is the reviewer's job and
    # the rationale says so rather than implying the name was read off a page.
    assert "not something a header block says" in types[0]["rationale"]


def test_surveying_twice_does_not_double_the_queue(corpus):
    survey(corpus, actor_id="act_a")
    first = len(candidates(corpus))
    second = survey(corpus, actor_id="act_a")
    assert len(candidates(corpus)) == first
    assert len(second["candidates"]) == first


def test_a_decided_candidate_is_not_offered_again(corpus):
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    review_candidate(corpus, title["candidate_id"], "rejected", "act_a")
    survey(corpus, actor_id="act_a")
    assert get_candidate(corpus, title["candidate_id"])["status"] == "rejected"
    assert _by_property(candidates(corpus), "title") is None


def test_a_store_with_no_documents_has_nothing_to_survey(store):
    with pytest.raises(OrpheusError) as empty:
        survey(store, actor_id="act_a")
    assert "no corpus yet" in str(empty.value)


def test_a_survey_can_be_pointed_at_named_documents(corpus):
    result = survey(corpus, actor_id="act_a", document_ids=["doc_3"],
                    min_support=1)
    assert result["n_documents_read"] == 1
    assert _by_property(result["candidates"], "odd_field") is not None


def test_an_engine_that_only_extracts_is_refused(corpus):
    with pytest.raises(OrpheusError):
        survey(corpus, actor_id="act_a", engine="not_an_engine")


# -- deciding ----------------------------------------------------------------

def test_renaming_keeps_the_evidence_attached(corpus):
    """The ordinary accepting move. A survey is good at noticing that something
    recurs and bad at naming it, and recording a rename as a rejection would
    throw away the quotations that argued for the thing."""
    survey(corpus, actor_id="act_a")
    record = next(c for c in candidates(corpus) if c["kind"] == "object_type")
    decided = review_candidate(corpus, record["candidate_id"], "accepted",
                               "act_a", accepted_as="Proposal")
    assert decided["status"] == "amended"
    assert decided["accepted_as"] == "Proposal"
    assert len(decided["evidence"]) == len(record["evidence"]) >= 1


def test_a_rename_that_is_not_an_identifier_is_refused(corpus):
    survey(corpus, actor_id="act_a")
    record = next(c for c in candidates(corpus) if c["kind"] == "object_type")
    with pytest.raises(OrpheusError):
        review_candidate(corpus, record["candidate_id"], "accepted", "act_a",
                         accepted_as="   ")


def test_a_property_cannot_be_renamed_onto_a_reserved_column(corpus):
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    with pytest.raises(OrpheusError) as refused:
        review_candidate(corpus, title["candidate_id"], "accepted", "act_a",
                         accepted_as="confidence")
    assert "reserves" in str(refused.value)


def test_a_decision_is_made_once(corpus):
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    review_candidate(corpus, title["candidate_id"], "accepted", "act_a")
    with pytest.raises(OrpheusError):
        review_candidate(corpus, title["candidate_id"], "rejected", "act_a")


def test_a_decision_is_recorded_in_the_edit_history(corpus):
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    review_candidate(corpus, title["candidate_id"], "rejected", "act_a",
                     note="two fields, not one")
    edit = corpus.one(
        "SELECT * FROM edit_history WHERE row_id = ?",
        (title["candidate_id"],))
    assert edit["action"] == "ontology_candidate_rejected"
    assert edit["note"] == "two fields, not one"


def test_reviewing_something_that_is_not_there(corpus):
    with pytest.raises(NotFound):
        review_candidate(corpus, "cnd_nope", "accepted", "act_a")


# -- drafting ----------------------------------------------------------------

def _accept_all(store, rename_type="Proposal"):
    survey(store, actor_id="act_a")
    for candidate in candidates(store):
        rename = rename_type if candidate["kind"] == "object_type" else None
        review_candidate(store, candidate["candidate_id"], "accepted", "act_a",
                         accepted_as=rename)


def test_nothing_accepted_means_nothing_to_draft(corpus):
    survey(corpus, actor_id="act_a")
    with pytest.raises(OrpheusError) as nothing:
        draft_bundle(corpus, "proposals-core")
    assert "nothing to draft" in str(nothing.value)


def test_a_drafted_bundle_is_valid_and_registers(corpus):
    _accept_all(corpus)
    drafted = draft_bundle(corpus, "proposals-core", name="Proposals",
                           document_types=["proposal", "other"])
    assert drafted["problems"] == []
    assert drafted["object_types"] == ["Proposal"]
    # The whole point of drafting one: it goes into a store through the same
    # function every other bundle goes through.
    bundle_mod.register(corpus, drafted["bundle"], actor_id="act_a",
                        activate=True)
    bundle_mod.apply_schema(corpus, drafted["bundle"])
    columns = [r["name"] for r in corpus.query(
        "PRAGMA table_info(instances_Proposal)")]
    assert {"instance_id", "title", "author", "document_id", "status",
            "confidence"} <= set(columns)


def test_only_what_a_person_accepted_is_in_the_bundle(corpus):
    survey(corpus, actor_id="act_a")
    for candidate in candidates(corpus):
        if candidate["kind"] == "object_type":
            review_candidate(corpus, candidate["candidate_id"], "accepted",
                             "act_a", accepted_as="Proposal")
        elif candidate["property_id"] == "title":
            review_candidate(corpus, candidate["candidate_id"], "accepted",
                             "act_a")
        else:
            review_candidate(corpus, candidate["candidate_id"], "rejected",
                             "act_a")
    drafted = draft_bundle(corpus, "proposals-core")
    declared = {p["id"] for p in drafted["bundle"]["objects"][0]["properties"]}
    assert "title" in declared
    assert "author" not in declared


def test_a_property_accepted_on_a_type_that_was_not_is_reported(corpus):
    survey(corpus, actor_id="act_a")
    for candidate in candidates(corpus):
        if candidate["kind"] == "object_type":
            review_candidate(corpus, candidate["candidate_id"], "rejected",
                             "act_a")
        else:
            review_candidate(corpus, candidate["candidate_id"], "accepted",
                             "act_a")
    with pytest.raises(OrpheusError):
        # Nothing accepted is a type, so there is no bundle to draft -- but the
        # half-made decision must not vanish silently either.
        draft_bundle(corpus, "proposals-core")


def test_provenance_columns_are_added_without_being_asked_about(corpus):
    _accept_all(corpus)
    drafted = draft_bundle(corpus, "proposals-core")
    declared = {p["id"] for p in drafted["bundle"]["objects"][0]["properties"]}
    assert {"document_id", "source", "confidence", "status", "amended_by",
            "amended_at"} <= declared
    assert "Reviewable" in drafted["bundle"]["objects"][0]["implements"]


def test_a_type_with_a_name_gets_a_key_to_match_on(corpus):
    """Without `naive_key` a bundle produces a wiki of pages that can never be
    the same page as anything, and nothing says so."""
    survey(corpus, actor_id="act_a")
    for candidate in candidates(corpus):
        rename = None
        if candidate["kind"] == "object_type":
            rename = "Author"
        elif candidate["property_id"] == "author":
            rename = "name"
        review_candidate(corpus, candidate["candidate_id"], "accepted",
                         "act_a", accepted_as=rename)
    drafted = draft_bundle(corpus, "proposals-core")
    obj = drafted["bundle"]["objects"][0]
    assert "naive_key" in {p["id"] for p in obj["properties"]}
    assert "Named" in obj["implements"]
    assert any(i["id"] == "Named" for i in drafted["bundle"]["interfaces"])


def test_an_unused_interface_is_not_declared(corpus):
    _accept_all(corpus)
    drafted = draft_bundle(corpus, "proposals-core")
    declared = {i["id"] for i in drafted["bundle"]["interfaces"]}
    assert declared == {"Reviewable"}


def test_a_named_primary_type_has_to_be_one_that_was_accepted(corpus):
    _accept_all(corpus)
    with pytest.raises(OrpheusError):
        draft_bundle(corpus, "proposals-core", primary_type="Contract")


def test_document_scoped_is_a_choice_the_drafter_makes(corpus):
    # It cannot be read off a header block: whether two documents sharing a
    # title are one thing or two is a fact about the domain.
    _accept_all(corpus)
    drafted = draft_bundle(corpus, "proposals-core",
                           document_scoped=["Proposal"])
    assert "DocumentScoped" in drafted["bundle"]["objects"][0]["implements"]


def test_a_field_the_corpus_leaves_blank_is_still_a_field():
    """Ten of forty documents in the calibration corpus carry a bare
    `Post-History:`. Requiring a value counted that field in twenty-four of
    them, which is a claim about how often it is *filled in*, not about whether
    the corpus has it."""
    fields = header_fields("Title: A thing\nPost-History:\nStatus: Draft\n")
    assert [f["key"] for f in fields] == ["Title", "Post-History", "Status"]
    assert fields[1]["value"] == ""


def test_three_bare_headings_are_not_a_header_block():
    """What keeps the empty value above from turning every structured document
    into an ontology: a block has to carry at least one value."""
    headings = "Introduction:\nMotivation:\nSpecification:\n"
    assert header_fields(headings) == []


# -- the model pass ----------------------------------------------------------
#
# The model is doubled and nothing else is. What is under test is everything
# that happens to a reply *after* it arrives: what gets located, what gets
# counted, and what is quietly dropped.

def _reply(**overrides):
    reply = {
        "types": [
            {"id": "Proposal", "display_name": "Proposal",
             "description": "One proposal.", "rationale": "Recurs.",
             "quotes": ["PEP Purpose and Guidelines"],
             "properties": [
                 {"id": "title", "type": "string",
                  "quotes": ["Title: PEP Purpose and Guidelines"]},
                 {"id": "confidence", "type": "number", "quotes": ["Status"]},
             ]},
            {"id": "Person", "name_style": "personal",
             "quotes": ["Tim Peters"],
             "properties": [{"id": "name", "type": "string",
                             "quotes": ["Guido van Rossum"]}]},
        ],
        "links": [{"id": "written_by", "from": "Proposal", "to": "Person",
                   "quotes": ["Author: Tim Peters"]}],
    }
    reply.update(overrides)
    return reply


@pytest.fixture
def model(monkeypatch):
    """Answer every survey with one reply, and record what was sent."""
    import json

    sent = {}

    def fake_ask(*, store, system, text, purpose, engine, tier, opt_in,
                 actor_id, document_id=None):
        sent.update(system=system, text=text, purpose=purpose, engine=engine,
                    tier=tier, opt_in=opt_in)
        return json.dumps(sent.pop("reply", None) or _reply())

    monkeypatch.setattr("orpheus.engines.ask", fake_ask)
    return sent


def test_a_model_can_propose_what_a_header_block_cannot(corpus, model):
    """The reason the expensive pass exists. `Author: Guido van Rossum` is a
    field to the pattern pass and a relation to a Person to a model, and the
    second reading is the one a header block has no way to reach."""
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    kinds = {(c["kind"], c["type_id"], c["property_id"])
             for c in result["candidates"]}
    assert ("object_type", "Person", None) in kinds
    assert ("link_type", "Proposal", "written_by") in kinds


def test_a_quotation_nowhere_in_the_corpus_is_dropped(corpus, model):
    model["reply"] = {"types": [{"id": "Ghost",
                                 "quotes": ["a sentence no document contains"],
                                 "properties": []}]}
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    assert result["candidates"] == []


def test_a_reserved_property_from_a_model_is_dropped_too(corpus, model):
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    assert _by_property(result["candidates"], "confidence") is None


def test_a_reply_that_is_not_json_says_so(corpus, monkeypatch):
    monkeypatch.setattr(
        "orpheus.engines.ask",
        lambda **kwargs: "I'd be happy to help you model these documents!")
    with pytest.raises(OrpheusError) as broken:
        survey(corpus, engine="chat", actor_id="act_a")
    assert "not JSON" in str(broken.value)


def test_the_cheap_pass_measures_what_the_expensive_one_proposed(corpus, model):
    """The model quoted `title` from one document. Every document in the corpus
    has a `Title:` header, and read as "how much of the corpus has this" a
    support of 1 would send the real fields to the bottom of the queue."""
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    title = _by_property(result["candidates"], "title")
    assert title["n_documents"] == 3


def test_a_type_is_supported_as_well_as_its_best_property(corpus, model):
    # You cannot have the title of a proposal in three documents and the
    # proposal itself in one: the property is an attribute of the thing.
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    proposal = next(c for c in result["candidates"]
                    if c["kind"] == "object_type"
                    and c["type_id"] == "Proposal")
    assert proposal["n_documents"] == 3


def test_a_fuzzy_match_is_not_evidence_that_a_document_contains_it(corpus,
                                                                   model):
    """`align` falls back to matching three consecutive words. On the real
    corpus that found a proposed `abstract` property, quoted from one PEP, in
    twenty-one of forty documents that share nothing but an opening phrase."""
    model["reply"] = {"types": [{
        "id": "Thing",
        "quotes": ["PEP: 1\nTitle: PEP Purpose and Guidelines"],
        "properties": [{"id": "loose", "type": "string",
                        "quotes": ["This PEP describes how something else "
                                   "entirely is decided"]}]}]}
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    assert _by_property(result["candidates"], "loose") is None


def test_a_name_style_the_bundle_understands_is_carried_through(corpus, model):
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    person = next(c for c in result["candidates"] if c["type_id"] == "Person"
                  and c["kind"] == "object_type")
    assert person["name_style"] == "personal"
    review_candidate(corpus, person["candidate_id"], "accepted", "act_a")
    name = next(c for c in candidates(corpus)
                if c["type_id"] == "Person" and c["property_id"] == "name")
    review_candidate(corpus, name["candidate_id"], "accepted", "act_a")
    drafted = draft_bundle(corpus, "people-core")
    obj = drafted["bundle"]["objects"][0]
    assert obj["extensions"]["orpheus"]["nameStyle"] == "personal"


def test_an_id_a_model_made_unusable_is_repaired_not_rejected(corpus, model):
    model["reply"] = {"types": [{
        "id": "Proposal (a PEP)",
        "quotes": ["Title: The Zen of Python"],
        "properties": [{"id": "Post-History!", "type": "string",
                        "quotes": ["Type: Informational"]}]}]}
    result = survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    assert {c["type_id"] for c in result["candidates"]} == {"ProposalAPEP"}
    assert _by_property(result["candidates"], "post_history") is not None


def test_the_call_goes_through_the_ordinary_gate(corpus, model):
    survey(corpus, engine="chat", actor_id="act_a", tier="cloud", opt_in=True,
           min_support=1)
    assert (model["tier"], model["opt_in"]) == ("cloud", True)
    # One call over the whole sample, not one per document: "what recurs across
    # these documents" is not answerable from one of them, and asking it twenty
    # times produces twenty ontologies to reconcile.
    assert model["text"].count("--- Document ") == 3


def test_what_was_held_back_is_named_and_not_merely_counted(corpus):
    """On a corpus with no header block to corroborate against, everything sits
    low and "5 were held back" says a threshold did work without saying what it
    did. These are the shapes lowering it would surface."""
    result = survey(corpus, actor_id="act_a")
    held = result["below_support"]
    assert len(held) == result["n_below_support"] >= 1
    assert any(h["property_id"] == "odd_field" for h in held)
    assert all(h["n_documents"] < result["min_support"] for h in held)


def test_a_second_survey_adds_evidence_rather_than_repeating_it(corpus, model):
    """Two runs quoting the same line of the same document is one piece of
    evidence. Showing it twice makes a candidate look better supported than it
    is, on the surface where support is the whole point."""
    survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    survey(corpus, engine="chat", actor_id="act_a", min_support=1)
    for candidate in candidates(corpus):
        quotations = [(e["document_id"], e["excerpt"])
                      for e in candidate["evidence"]]
        assert len(quotations) == len(set(quotations)), candidate["type_id"]


def test_a_type_that_will_never_get_a_page_is_said_so_at_draft_time(corpus):
    """The wiki is built from types implementing `Named`, and the graph is a
    projection over wiki pages. A type without a `name` holds rows, gets no
    page, and orphans every edge through it -- invisible until somebody reads
    graph coverage, by which time the extraction has run. On the council
    minutes that was 625 of 794 edges."""
    _accept_all(corpus)
    drafted = draft_bundle(corpus, "proposals-core")
    assert drafted["problems"] == []
    # No accepted property is called `name` here, so the one type has none.
    assert any("no `name` property" in w for w in drafted["warnings"])
    assert any("never appear in the wiki" in w for w in drafted["warnings"])


def test_a_named_type_draws_no_warning(corpus):
    survey(corpus, actor_id="act_a")
    for candidate in candidates(corpus):
        rename = ("Proposal" if candidate["kind"] == "object_type"
                  else "name" if candidate["property_id"] == "title" else None)
        review_candidate(corpus, candidate["candidate_id"], "accepted", "act_a",
                         accepted_as=rename)
    assert draft_bundle(corpus, "proposals-core")["warnings"] == []


def test_a_decision_can_be_reconsidered(corpus):
    """A warning is only worth having if the decision it warns about can be
    changed, and the warning that matters most -- a type with no `name` gets no
    page -- only becomes visible after the extraction has run."""
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    review_candidate(corpus, title["candidate_id"], "accepted", "act_a")
    assert _by_property(candidates(corpus), "title") is None

    reopened = reopen_candidate(corpus, title["candidate_id"], "act_a",
                                note="it should have been the name")
    assert reopened["status"] == "proposed"
    assert reopened["accepted_as"] is None
    # The evidence stays attached: what is restored is the question, not the
    # state before it was asked.
    assert len(reopened["evidence"]) == len(title["evidence"]) >= 1
    assert _by_property(candidates(corpus), "title") is not None

    again = review_candidate(corpus, title["candidate_id"], "accepted",
                             "act_a", accepted_as="name")
    assert (again["status"], again["accepted_as"]) == ("amended", "name")
    actions = [r["action"] for r in corpus.query(
        "SELECT action FROM edit_history WHERE row_id = ? ORDER BY seq",
        (title["candidate_id"],))]
    assert actions == ["ontology_candidate_accepted",
                       "ontology_candidate_reopened",
                       "ontology_candidate_amended"]


def test_something_nobody_decided_cannot_be_reconsidered(corpus):
    survey(corpus, actor_id="act_a")
    title = _by_property(candidates(corpus), "title")
    with pytest.raises(OrpheusError) as already:
        reopen_candidate(corpus, title["candidate_id"], "act_a")
    assert "already in the queue" in str(already.value)


def test_a_sector_vocabulary_is_supplied_and_never_proposed(corpus):
    """A survey reads what the documents are about; which sectors a deployment
    cares to group by is a decision about the deployment. Omitted means the
    classifier does not ask, which is the point — as an open question `sector`
    produced thirteen spellings of one answer across forty-eight documents."""
    _accept_all(corpus)
    bare = draft_bundle(corpus, "proposals-core")["bundle"]
    assert "sectors" not in bare["extensions"]["orpheus"]

    listed = draft_bundle(corpus, "proposals-core",
                          sectors=["governance", "release"],
                          jurisdictions=["upstream"])["bundle"]
    assert listed["extensions"]["orpheus"]["sectors"] == ["governance",
                                                          "release"]
    assert listed["extensions"]["orpheus"]["jurisdictions"] == ["upstream"]
    bundle_mod.validate(listed)


def test_the_queue_is_capped_and_says_what_is_behind_it(store):
    """A queue is not a listing. You decide the top item, it leaves, and the
    next appears -- so the front of it plus a count of what is behind is what a
    reviewer needs. Paging through it would shift everything under them the
    moment they decided anything."""
    _seed_candidates(store, 40)

    front = ontology.candidates(store, limit=10)
    assert len(front) == 10
    assert ontology.n_candidates(store) == 40
    # Most-supported first, which is why taking the front costs nothing.
    assert [c["n_documents"] for c in front] == sorted(
        (c["n_documents"] for c in front), reverse=True)

    assert len(ontology.candidates(store, limit=None)) == 40


def test_the_default_queue_does_not_hand_over_a_whole_survey(store):
    _seed_candidates(store, ontology.QUEUE_LIMIT + 15)
    assert len(ontology.candidates(store)) == ontology.QUEUE_LIMIT
    assert ontology.n_candidates(store) == ontology.QUEUE_LIMIT + 15


def test_drafting_still_reads_every_decision(store):
    """The cap is a screen concern. A bundle assembled from the first
    twenty-five accepted candidates would silently drop the rest."""
    _seed_candidates(store, 40, status="accepted", kind="object_type")
    store.insert("actors", {"actor_id": "act_d", "display_name": "D",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    drafted = ontology.draft_bundle(store, "wide-core")
    assert len(drafted["bundle"]["objects"]) == 40


def _seed_candidates(store, n, status="proposed", kind="object_type"):
    for i in range(n):
        store.insert("ontology_candidates", {
            "candidate_id": f"cnd_{i:03}", "survey_id": "srv_1", "kind": kind,
            "type_id": f"Type{i:03}", "data_type": None,
            "n_documents": n - i, "n_sampled": n, "status": status,
            "engine": "deterministic", "source": "ai_local",
            "created_at": "2026-01-01T00:00:00Z",
        })
