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
