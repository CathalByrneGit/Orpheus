"""Reading a document with the machine, a passage at a time.

One property matters more than the rest and most of this file defends it:
**a suggestion is not an extraction.** A companion firing as somebody reads
produces proposals nobody asked for. If those became `unconfirmed` instances
they would flood the review queue, let the wiki build pages out of guesses, and
pollute the extraction-quality number Phase 1 turns on — each quietly, and none
of it visible until the number was already wrong.

The rest is what makes it bearable to use: a page read twice does not re-offer
what was already decided, and reading is recorded separately from finding, so
"nobody opened this page" and "this page holds nothing" stay different facts.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.companion import (accept_suggestion, dismiss_suggestion,
                               get_suggestion, passage, read_passage,
                               reading_progress, suggestion_quality)
from orpheus.entities import propose_entities
from orpheus.quality import extraction_quality
from orpheus.utils import NotFound, OrpheusError

PAGES = {
    1: ("This Agreement is made on 3 March 2024 between Ardmore Digital Ltd "
        "and the Health Service Executive. The Client shall pay EUR 250,000 "
        "per annum."),
    2: ("Term. This Agreement commences on 1 April 2024 and terminates on "
        "31 March 2027."),
    3: "This page says nothing worth recording.",
}


@pytest.fixture
def reading(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.execute(
        "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
        " n_pages, date_added, created_by, visibility, review_status)"
        " VALUES ('doc_1','contract.pdf','h1',100,3,datetime('now'),'act_a',"
        "'private','unreviewed')")
    for page_no, text in PAGES.items():
        store.execute(
            "INSERT INTO document_pages (document_id, page_no, text,"
            " text_source, char_count) VALUES ('doc_1',?,?,'pdf_text',?)",
            (page_no, text, len(text)))
    store.conn.commit()
    return store


# -- a suggestion is not an extraction ---------------------------------------

def test_reading_a_page_writes_no_instance(reading):
    result = read_passage(reading, "doc_1", 1, actor_id="act_a")
    assert result["n_offered"] > 0
    # Nothing has been extracted. Somebody was shown something.
    assert reading.scalar("SELECT COUNT(*) FROM instance_index") == 0
    assert reading.scalar("SELECT COUNT(*) FROM provenance") == 0


def test_offers_do_not_reach_the_extraction_quality_number(reading):
    # The number Phase 1 turns on. Proposals nobody asked for must not move it.
    read_passage(reading, "doc_1", 1, actor_id="act_a")
    read_passage(reading, "doc_1", 2, actor_id="act_a")
    assert extraction_quality(reading)["overall"].get("n_reviewed", 0) == 0


def test_offers_do_not_become_wiki_pages(reading):
    # propose_entities() walks unlinked mentions. An offer is not a mention.
    read_passage(reading, "doc_1", 1, actor_id="act_a")
    assert propose_entities(reading, actor_id="act_a")["proposed"] == 0


def test_accepting_writes_the_instance_through_the_usual_path(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    dates = [s for s in offers if s["type_id"] == "KeyDate"]
    accepted = accept_suggestion(reading, dates[0]["suggestion_id"], "act_a")

    assert accepted["status"] == "accepted"
    instance_id = accepted["instance_id"]
    assert reading.one("SELECT 1 FROM instance_index WHERE instance_id = ?",
                       (instance_id,))
    # Provenance, with the excerpt and the span, exactly as a batch pass writes.
    evidence = reading.one("SELECT * FROM provenance WHERE instance_id = ?",
                           (instance_id,))
    assert evidence["excerpt"] and evidence["page_no"] == 1
    assert evidence["source_label"].startswith("companion:")
    assert evidence["char_start"] is not None


def test_an_accepted_row_says_a_person_vouches_for_it(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    accepted = accept_suggestion(reading, offers[0]["suggestion_id"], "act_a")
    row = reading.one(
        "SELECT source, status FROM instances_KeyDate WHERE instance_id = ? "
        "UNION ALL SELECT source, status FROM instances_MonetaryAmount "
        "WHERE instance_id = ?",
        (accepted["instance_id"], accepted["instance_id"]))
    assert row["source"] == "human"
    assert row["status"] == "confirmed"


# -- correcting on the way in ------------------------------------------------

def test_a_field_can_be_fixed_while_accepting(reading):
    # The common case: the machine spotted the right thing and got one field
    # wrong. Making a person accept-then-amend would be two steps for one act.
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    date = next(s for s in offers if s["type_id"] == "KeyDate")
    accepted = accept_suggestion(reading, date["suggestion_id"], "act_a",
                                 properties={"date_role": "signature"})
    assert accepted["corrected"] == {"date_role": "signature"}
    assert reading.one(
        "SELECT date_role FROM instances_KeyDate WHERE instance_id = ?",
        (accepted["instance_id"],))["date_role"] == "signature"


def test_what_the_machine_offered_survives_the_correction(reading):
    # "What did the machine say before a person fixed it" has to stay
    # answerable, which is the same reason amendments keep the original.
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    date = next(s for s in offers if s["type_id"] == "KeyDate")
    original = date["properties"]["date_role"]
    accept_suggestion(reading, date["suggestion_id"], "act_a",
                      properties={"date_role": "signature"})
    assert get_suggestion(reading, date["suggestion_id"])["properties"]["date_role"] \
        == original


# -- dismissing --------------------------------------------------------------

def test_a_dismissal_is_kept(reading):
    # The only evidence there is about whether the companion is worth having.
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    dismissed = dismiss_suggestion(reading, offers[0]["suggestion_id"], "act_a",
                                   note="a clause number, not a date")
    assert dismissed["status"] == "dismissed"
    assert dismissed["note"]
    assert reading.scalar("SELECT COUNT(*) FROM suggestions") == len(offers)


def test_deciding_twice_is_refused(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    dismiss_suggestion(reading, offers[0]["suggestion_id"], "act_a")
    with pytest.raises(OrpheusError):
        accept_suggestion(reading, offers[0]["suggestion_id"], "act_a")


def test_both_decisions_reach_the_history(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    accept_suggestion(reading, offers[0]["suggestion_id"], "act_a")
    dismiss_suggestion(reading, offers[1]["suggestion_id"], "act_a")
    actions = {r["action"] for r in reading.query(
        "SELECT action FROM edit_history WHERE table_name = 'suggestions'")}
    assert actions == {"accept_suggestion", "dismiss_suggestion"}


# -- reading the same page twice ---------------------------------------------

def test_a_re_read_does_not_re_offer_what_was_decided(reading):
    # Without this, scrolling back re-offers everything already dismissed and
    # the companion becomes something to close.
    first = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    dismiss_suggestion(reading, first[0]["suggestion_id"], "act_a")
    accept_suggestion(reading, first[1]["suggestion_id"], "act_a")

    again = read_passage(reading, "doc_1", 1, actor_id="act_a")
    offered = {s["suggestion_id"] for s in again["suggestions"]}
    assert first[0]["suggestion_id"] not in offered
    assert first[1]["suggestion_id"] not in offered
    assert len(offered) == len(first) - 2


def test_a_re_read_does_not_duplicate_what_is_still_offered(reading):
    first = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    again = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    assert {s["suggestion_id"] for s in first} == {s["suggestion_id"] for s in again}
    assert reading.scalar("SELECT COUNT(*) FROM suggestions") == len(first)


# -- reading is recorded separately from finding -----------------------------

def test_a_page_holding_nothing_still_counts_as_read(reading):
    # "Nobody opened this page" and "this page holds nothing" are different
    # facts, and only the reading record can tell them apart.
    result = read_passage(reading, "doc_1", 3, actor_id="act_a")
    assert result["n_offered"] == 0
    assert passage(reading, "doc_1", 3)["has_been_read"] is True
    assert passage(reading, "doc_1", 2)["has_been_read"] is False


def test_progress_counts_pages_read_not_pages_with_findings(reading):
    read_passage(reading, "doc_1", 3, actor_id="act_a")
    progress = reading_progress(reading, "doc_1", actor_id="act_a")
    assert progress["n_read"] == 1
    assert progress["unread"] == [1, 2]
    assert "1 of 3 page(s) read" in progress["note"]


def test_progress_reports_the_acceptance_rate_once_anything_is_decided(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    accept_suggestion(reading, offers[0]["suggestion_id"], "act_a")
    dismiss_suggestion(reading, offers[1]["suggestion_id"], "act_a")
    progress = reading_progress(reading, "doc_1", actor_id="act_a")
    assert progress["acceptance_rate"] == 0.5
    assert "worth recording" in progress["note"]


def test_nothing_read_says_so_rather_than_reporting_zero(reading):
    assert "None of the 3 page(s)" in reading_progress(reading, "doc_1")["note"]


def test_an_unknown_page_is_not_an_empty_passage(reading):
    with pytest.raises(NotFound):
        read_passage(reading, "doc_1", 99, actor_id="act_a")


# -- measuring the companion itself ------------------------------------------

def test_offers_nobody_looked_at_do_not_count_as_a_score(reading):
    read_passage(reading, "doc_1", 1, actor_id="act_a")
    report = suggestion_quality(reading, "doc_1")
    assert report["by_engine"][0]["acceptance_rate"] is None
    assert "say nothing about whether the companion is useful" in report["note"]


def test_the_acceptance_rate_is_reported_per_engine(reading):
    offers = read_passage(reading, "doc_1", 1, actor_id="act_a")["suggestions"]
    accept_suggestion(reading, offers[0]["suggestion_id"], "act_a")
    dismiss_suggestion(reading, offers[1]["suggestion_id"], "act_a")
    entry = suggestion_quality(reading, "doc_1")["by_engine"][0]
    assert entry["engine"] == "deterministic"
    assert entry["acceptance_rate"] == 0.5
    assert entry["n_decided"] == 2


# -- an offer from somewhere other than a page read --------------------------

def test_a_chat_offer_is_a_suggestion_like_any_other(reading):
    # The point of routing it here: an offer that skips this table cannot be
    # measured, and `suggestion_quality` is the only thing that says whether
    # these are worth a person's attention.
    from orpheus.companion import propose, suggestion_quality

    offer = propose(reading, "doc_1", 1, "Company",
                    {"name": "Ardmore Digital Ltd", "naive_key": "ardmore digital"},
                    quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    reading.conn.commit()

    assert offer["status"] == "offered"
    assert offer["engine"] == "chat"
    assert offer["alignment"], "the quote is located, not taken on trust"
    assert offer["char_start"] is not None

    engines = {row["engine"]: row for row in suggestion_quality(reading)["by_engine"]}
    assert "chat" in engines, "the chat's offers must be measurable per source"
    assert engines["chat"]["offered"] == 1


def test_declining_a_chat_offer_leaves_evidence(reading):
    from orpheus.companion import dismiss_suggestion, propose, suggestion_quality

    offer = propose(reading, "doc_1", 1, "Company",
                    {"name": "Ardmore Digital Ltd", "naive_key": "ardmore digital"},
                    quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    dismiss_suggestion(reading, offer["suggestion_id"], actor_id="act_a",
                       note="declined in chat")
    reading.conn.commit()

    row = reading.one("SELECT status, note FROM suggestions WHERE suggestion_id = ?",
                      (offer["suggestion_id"],))
    assert row["status"] == "dismissed"
    assert row["note"] == "declined in chat"
    assert not reading.query("SELECT 1 FROM instances_Company"), \
        "declining must not write an instance"


def test_an_offer_quoting_text_the_page_lacks_is_refused(reading):
    from orpheus.companion import propose

    with pytest.raises(OrpheusError) as excinfo:
        propose(reading, "doc_1", 1, "Company", {"name": "Elsewhere Ltd"},
                quote="a sentence this page does not contain", engine="chat",
                actor_id="act_a")
    assert "does not contain that quote" in str(excinfo.value)
    assert not reading.query("SELECT 1 FROM suggestions")


def test_the_same_offer_twice_does_not_queue_twice(reading):
    from orpheus.companion import propose

    values = {"name": "Ardmore Digital Ltd", "naive_key": "ardmore digital"}
    first = propose(reading, "doc_1", 1, "Company", values,
                    quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    second = propose(reading, "doc_1", 1, "Company", values,
                     quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    reading.conn.commit()
    assert first["suggestion_id"] == second["suggestion_id"]


def test_re_offering_something_already_settled_is_refused(reading):
    from orpheus.companion import dismiss_suggestion, propose

    values = {"name": "Ardmore Digital Ltd", "naive_key": "ardmore digital"}
    offer = propose(reading, "doc_1", 1, "Company", values,
                    quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    dismiss_suggestion(reading, offer["suggestion_id"], actor_id="act_a")
    reading.conn.commit()

    with pytest.raises(OrpheusError) as excinfo:
        propose(reading, "doc_1", 1, "Company", values,
                quote=PAGES[1][:40], engine="chat", actor_id="act_a")
    assert "already dismissed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The rest of the document, behind the passage
# ---------------------------------------------------------------------------
#
# A clause that only makes sense in the light of an earlier definition was read
# without it, because the model saw one page in isolation. It can now see the
# rest as background -- and the thing that has to hold is that seeing more does
# not widen what it may *report*: the offers are still about this page.

@pytest.fixture
def spy_engine():
    """An engine that records the prompt and returns whatever it is told to."""
    from orpheus.engines import _ENGINES, register_engine

    seen = {}
    plan: list[dict] = []

    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        seen["text"] = text
        return {"entities": list(plan)}

    register_engine("spy", engine)
    try:
        yield seen, plan
    finally:
        _ENGINES.pop("spy", None)


def _offer(name, excerpt):
    return {"type_id": "Company", "properties": {"name": name},
            "excerpt": excerpt, "confidence": 0.9}


def test_by_default_a_model_still_sees_one_page(reading, spy_engine):
    seen, plan = spy_engine
    read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")

    assert seen["text"] == PAGES[2]
    assert PAGES[1] not in seen["text"]


def test_the_pages_before_it_are_what_context_means(reading, spy_engine):
    # A definition comes before the thing it defines, so the budget runs
    # backwards first.
    seen, plan = spy_engine
    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy",
                          context_chars=5000)

    assert PAGES[1] in seen["text"], "the earlier page is the point"
    assert PAGES[2] in seen["text"], "the passage is still there"
    assert seen["text"].index(PAGES[1]) < seen["text"].index(PAGES[2])
    assert result["context_chars"] > 0


def test_the_passage_is_marked_off_from_the_background(reading, spy_engine):
    from orpheus.companion import CONTEXT_MARKER

    seen, _ = spy_engine
    read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy",
                 context_chars=5000)
    before, _, after = seen["text"].partition(CONTEXT_MARKER)
    assert PAGES[1] in before and PAGES[2] in after


def test_a_budget_of_zero_characters_buys_no_context(reading, spy_engine):
    seen, _ = spy_engine
    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy",
                          context_chars=0)
    assert result["context_chars"] == 0
    assert seen["text"] == PAGES[2]


def test_a_budget_too_small_for_a_whole_page_takes_none_of_it(reading, spy_engine):
    # Whole pages only. Half a definition read as a whole one is worse than not
    # seeing it at all.
    seen, _ = spy_engine
    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy",
                          context_chars=10)
    assert result["context_chars"] == 0
    assert seen["text"] == PAGES[2]


def test_an_offer_quoting_the_background_is_not_filed_under_this_page(
        reading, spy_engine):
    # The failure this guards against. An offer from page 1 filed under page 2
    # gets a page-scoped fingerprint, a page-relative offset, and sends a
    # reviewer to the wrong passage to check it.
    seen, plan = spy_engine
    plan.append(_offer("Ardmore Digital Ltd", "Ardmore Digital Ltd"))   # page 1
    plan.append(_offer("Whoever", "This Agreement commences"))          # page 2

    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy",
                          context_chars=5000)

    names = {s["properties"]["name"] for s in result["suggestions"]}
    assert names == {"Whoever"}
    assert result["n_outside_the_page"] == 1


def test_nothing_is_discarded_when_there_is_no_context_to_stray_into(
        reading, spy_engine):
    # Without context the model was only ever shown the page, so an excerpt it
    # invented is an alignment question rather than a scope one -- and the
    # alignment machinery already records that.
    seen, plan = spy_engine
    plan.append(_offer("Ardmore Digital Ltd", "not on this page at all"))

    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")
    assert result["n_outside_the_page"] == 0
    assert len(result["suggestions"]) == 1


def test_the_pattern_pass_is_never_given_context(reading):
    # It matches characters rather than reading, so context is text it could
    # match in and then mislocate, and it cannot use it for understanding.
    result = read_passage(reading, "doc_1", 2, actor_id="act_a",
                          context_chars=5000)
    assert result["context_chars"] == 0


def test_the_context_is_charged_to_the_same_budget_as_the_passage(reading):
    """A model sent eleven times the text is an eleven-times call, and the
    audit is where that has to show up. `prompt_chars` counts what was sent."""
    from orpheus.engines import _ENGINES, register_engine
    from orpheus.llm import record_llm_call

    def engine(*, store, document, bundle, text, tier, opt_in, actor_id):
        record_llm_call(store, tier=tier, purpose="companion",
                        prompt_chars=len(text),
                        document_id=document["document_id"], actor_id=actor_id)
        return {"entities": []}

    register_engine("billed", engine)
    try:
        read_passage(reading, "doc_1", 2, actor_id="act_a", engine="billed")
        alone = reading.scalar("SELECT prompt_chars FROM llm_calls "
                               "ORDER BY rowid DESC LIMIT 1")
        read_passage(reading, "doc_1", 2, actor_id="act_a", engine="billed",
                     context_chars=5000)
        with_context = reading.scalar("SELECT prompt_chars FROM llm_calls "
                                      "ORDER BY rowid DESC LIMIT 1")
    finally:
        _ENGINES.pop("billed", None)

    assert alone == len(PAGES[2])
    assert with_context > alone, "the context was sent and is not being counted"


# ---------------------------------------------------------------------------
# Two findings that carry nothing but their page
# ---------------------------------------------------------------------------

def test_two_clauses_on_one_page_are_two_offers(reading, spy_engine):
    """Found by reading a real contract with a real model.

    It returned four Clauses from one page, each quoting different text, each
    carrying `{"page_no": 4}` and nothing else. All four fingerprinted the same,
    so three were dropped as duplicates of the first -- silently, and the count
    still said four.
    """
    seen, plan = spy_engine
    plan.append({"type_id": "Clause", "properties": {},
                 "excerpt": "This Agreement commences", "confidence": 0.9})
    plan.append({"type_id": "Clause", "properties": {},
                 "excerpt": "terminates on", "confidence": 0.9})

    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")

    assert result["n_offered"] == 2
    assert len({s["suggestion_id"] for s in result["suggestions"]}) == 2


def test_the_same_finding_read_twice_is_still_one_offer(reading, spy_engine):
    # The property the fingerprint exists for, and it has to survive the fix.
    seen, plan = spy_engine
    plan.append({"type_id": "Clause", "properties": {},
                 "excerpt": "This Agreement commences", "confidence": 0.9})

    first = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")
    again = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")
    assert again["n_offered"] == 1
    assert (first["suggestions"][0]["suggestion_id"]
            == again["suggestions"][0]["suggestion_id"])


def test_a_dismissed_finding_is_not_re_offered(reading, spy_engine):
    seen, plan = spy_engine
    plan.append({"type_id": "Clause", "properties": {},
                 "excerpt": "This Agreement commences", "confidence": 0.9})

    first = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")
    dismiss_suggestion(reading, first["suggestions"][0]["suggestion_id"],
                       actor_id="act_a", note="not a clause")
    assert read_passage(reading, "doc_1", 2, actor_id="act_a",
                        engine="spy")["n_offered"] == 0


def test_a_named_thing_is_still_matched_across_engines(reading, spy_engine):
    # Where the properties do identify the finding, the excerpt stays out of
    # the key -- so a second engine quoting different text for the same company
    # does not re-offer it.
    seen, plan = spy_engine
    plan.append({"type_id": "Company", "properties": {"name": "Ardmore Digital Ltd"},
                 "excerpt": "This Agreement commences", "confidence": 0.9})
    first = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")

    plan.clear()
    plan.append({"type_id": "Company", "properties": {"name": "Ardmore Digital Ltd"},
                 "excerpt": "terminates on", "confidence": 0.9})
    again = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")

    assert (first["suggestions"][0]["suggestion_id"]
            == again["suggestions"][0]["suggestion_id"])


def test_the_number_reported_is_the_number_in_the_store(reading, spy_engine):
    # n_offered also lands in reading_passages.n_suggested, and from there in
    # how well the companion is judged to be doing.
    seen, plan = spy_engine
    for excerpt in ("This Agreement commences", "terminates on", "31 March 2027"):
        plan.append({"type_id": "Clause", "properties": {},
                     "excerpt": excerpt, "confidence": 0.9})

    result = read_passage(reading, "doc_1", 2, actor_id="act_a", engine="spy")
    held = reading.scalar(
        "SELECT COUNT(*) FROM suggestions WHERE document_id = 'doc_1' "
        "AND page_no = 2 AND status = 'offered'")
    assert result["n_offered"] == held == 3
    assert reading.scalar(
        "SELECT n_suggested FROM reading_passages WHERE document_id = 'doc_1' "
        "AND page_no = 2 AND engine = 'spy'") == 3
