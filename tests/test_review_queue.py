"""The ranked queue, with the evidence attached.

`triage()` decides *which* extraction to look at and is tested in
`test_triage.py`. What is tested here is what turns that ranking into something
a person can act on without a page load per decision: the evidence travels with
the card, only work the reviewer can actually do is offered, and a card that has
gone missing since the ranking was computed does not take the batch with it.

The measurement that justifies the feature is `tests/e2e/measure_review.py`,
which reviews the same findings both ways against a live server and checks the
store agreed each time.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus import api, auth, ingest as ingest_mod, redact, review
from orpheus.utils import naive_key

LEVELS = [1.0, 0.9, 0.7, 0.5]


@pytest.fixture
def corpus(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.owner = auth.create_actor(store, "Nuala Ryan", idp="d", external_id="n")
    store.stranger = auth.create_actor(store, "Sean Kelly", idp="d", external_id="s")
    store.admin = auth.create_actor(store, "Root", idp="d", external_id="r",
                                    is_admin=True)
    store.documents = []
    n = 0
    for i in range(1, 4):
        path = tmp_path / f"contract-{i}.txt"
        path.write_text(f"Agreement {i} between Halloran Instruments, Inc. "
                        "and Kestrel Medical Group PLC.")
        document_id = ingest_mod.ingest(
            store, path, actor_id=store.owner,
            storage_root=tmp_path / "storage")["document_id"]
        store.documents.append(document_id)
        for j in range(4):
            n += 1
            _finding(store, f"inst_{n:02d}", document_id,
                     "Halloran Instruments, Inc.", LEVELS[j])
    return store


def _finding(store, instance_id, document_id, name, confidence,
             excerpt="This Agreement is made between Halloran Instruments, Inc."):
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, "
        "naive_key, source, confidence, status, created_at) VALUES "
        "(?,?,?,?,'ai_local',?,'unconfirmed',datetime('now'))",
        (instance_id, document_id, name, naive_key(name), confidence))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, "
        "document_id, created_at) VALUES (?,'Company','instances_Company',?,"
        "datetime('now'))", (instance_id, document_id))
    if excerpt is not None:
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id, "
            "excerpt, page_no, source, confidence, alignment, created_at) "
            "VALUES (?,?,?,?,1,'ai_local',?,'exact',datetime('now'))",
            (f"prov_{instance_id}", instance_id, document_id, excerpt, confidence))
    return instance_id


def owner(store):
    return {"actor_id": store.owner, "is_admin": False}


def stranger(store):
    return {"actor_id": store.stranger, "is_admin": False}


def admin(store):
    return {"actor_id": store.admin, "is_admin": True}


# -- the same ranking, with the evidence ------------------------------------

def test_the_queue_is_the_triage_ranking_and_not_a_second_opinion(corpus):
    """If these ever disagree, one of them is deciding what to review on a rule
    nobody wrote down."""
    ranked = review.triage(corpus, limit=60)
    queued = review.review_queue(corpus, actor=admin(corpus), limit=60)
    assert [c["instance_id"] for c in queued["cards"]] == \
        [e["instance_id"] for e in ranked["queue"]]
    assert queued["headline"] == ranked["headline"]


def test_a_card_carries_what_a_decision_needs(corpus):
    card = review.review_queue(corpus, actor=admin(corpus))["cards"][0]
    # The sentence the value was read from, and where it is. Without these the
    # card is a name and a shrug.
    assert card["excerpt"].startswith("This Agreement is made between")
    assert card["page_no"] == 1
    assert card["filename"].startswith("contract-")
    assert card["values"]["name"] == "Halloran Instruments, Inc."
    assert card["confidence_label"]
    assert card["reason"]


def test_a_card_offers_the_properties_amend_will_accept(corpus):
    card = review.review_queue(corpus, actor=admin(corpus))["cards"][0]
    assert "name" in card["amendable"]
    # Never the bookkeeping columns.
    assert not {"status", "source", "confidence"} & set(card["amendable"])
    assert set(card["values"]) == set(card["amendable"])


def test_a_derived_property_is_not_offered_as_a_box_to_edit(corpus):
    """`amend_instance` recomputes `naive_key` from `name`, so a box for it is
    a box whose value is overwritten the moment its source changes."""
    card = review.review_queue(corpus, actor=admin(corpus))["cards"][0]
    assert "naive_key" not in card["amendable"]
    assert "naive_key" not in card["values"]


def test_the_best_located_excerpt_is_the_one_shown(corpus):
    """An instance can carry several provenance rows -- the deterministic pass
    and a model pass both finding it. The reviewer gets the best-located one."""
    instance_id = _finding(corpus, "inst_99", corpus.documents[0],
                           "Ardmore Digital Ltd", 0.9, excerpt="a weak reading")
    corpus.execute(
        "INSERT INTO provenance (provenance_id, instance_id, document_id, "
        "excerpt, page_no, source, confidence, alignment, created_at) VALUES "
        "('prov_best',?,?,'the exact sentence',2,'deterministic',1.0,'exact',"
        "datetime('now'))", (instance_id, corpus.documents[0]))

    cards = review.review_queue(corpus, actor=admin(corpus), limit=60)["cards"]
    card = next(c for c in cards if c["instance_id"] == instance_id)
    assert card["excerpt"] == "the exact sentence"
    assert card["page_no"] == 2


def test_a_row_with_no_provenance_is_not_offered_for_review(store, tmp_path):
    """Not an oversight, and worth a test so it cannot become one.

    The ranking reads `collect_review_outcomes`, which joins through
    `provenance` on purpose: a row with no provenance row was not offered by
    the extractor. Concept-raised flags and facts a person typed in are the two
    kinds, and reviewing either would walk the extraction accuracy number
    upward without the extractor having done anything.
    """
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    path = tmp_path / "one.txt"
    path.write_text("Sionna Analytics Limited is a party.")
    document_id = ingest_mod.ingest(
        store, path, storage_root=tmp_path / "storage")["document_id"]
    _finding(store, "inst_98", document_id, "Sionna Analytics Limited", 0.5,
             excerpt=None)

    queue = review.review_queue(store, actor={"actor_id": "a", "is_admin": True})
    assert queue["cards"] == []
    # And the queue says so rather than implying the corpus is reviewed.
    assert queue["n_unreviewed"] == 0


# -- only work the reviewer can actually do ---------------------------------

def test_the_queue_offers_only_documents_the_reviewer_may_correct(corpus):
    """Filtered on `edit`, not `view`: a queue whose every second card is
    refused when the person acts on it wastes the thing it exists to save."""
    auth.share_document(corpus, corpus.documents[0], corpus.stranger,
                        "viewer", corpus.owner)

    queue = review.review_queue(corpus, actor=stranger(corpus), limit=60)
    assert queue["cards"] == []
    assert queue["n_withheld"] > 0
    assert "may read but not correct" in queue["withheld_note"]


def test_an_editor_gets_the_cards_for_what_they_may_edit(corpus):
    auth.share_document(corpus, corpus.documents[1], corpus.stranger,
                        "editor", corpus.owner)

    queue = review.review_queue(corpus, actor=stranger(corpus), limit=60)
    assert queue["cards"]
    assert {c["document_id"] for c in queue["cards"]} == {corpus.documents[1]}


def test_an_administrator_is_offered_the_whole_corpus(corpus):
    queue = review.review_queue(corpus, actor=admin(corpus), limit=60)
    assert {c["document_id"] for c in queue["cards"]} == set(corpus.documents)
    assert queue["n_withheld"] == 0
    assert queue["withheld_note"] is None


def test_the_owner_of_a_document_may_review_it(corpus):
    queue = review.review_queue(corpus, actor=owner(corpus), limit=60)
    assert {c["document_id"] for c in queue["cards"]} == set(corpus.documents)


# -- the batch survives contact with a moving store -------------------------

def test_a_card_that_vanished_since_the_ranking_does_not_take_the_batch(corpus):
    """A redaction between the ranking and the hydration. One removed document
    must not empty a reviewer's queue."""
    redact.redact_document(corpus, corpus.documents[0],
                           actor_id=corpus.admin, note="subject access request")

    queue = review.review_queue(corpus, actor=admin(corpus), limit=60)
    assert queue["cards"]
    assert corpus.documents[0] not in {c["document_id"] for c in queue["cards"]}


def test_an_unextracted_corpus_returns_an_empty_queue_rather_than_raising(store):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    queue = review.review_queue(store, actor={"actor_id": "a", "is_admin": True})
    assert queue["cards"] == []
    assert queue["headline"] == "Nothing has been extracted yet."


def test_the_batch_is_short_on_purpose(corpus):
    """The ranking changes as review happens, so a long page of cards is a page
    that is stale by the time it is halfway read."""
    assert review.QUEUE_BATCH < 50
    assert len(review.review_queue(corpus, actor=admin(corpus))["cards"]) \
        <= review.QUEUE_BATCH


# -- over the API -----------------------------------------------------------

def test_the_api_serves_the_queue(corpus):
    status, payload = api.handle(corpus, "GET", "/review/queue", {},
                                 actor=admin(corpus))
    assert status == 200
    assert payload["cards"] and payload["n_unreviewed"] == 12


def test_scoping_to_a_document_you_cannot_correct_is_refused(corpus):
    auth.share_document(corpus, corpus.documents[0], corpus.stranger,
                        "viewer", corpus.owner)
    status, payload = api.handle(
        corpus, "GET", "/review/queue", {"document_id": corpus.documents[0]},
        actor=stranger(corpus))
    assert status == 403
    assert "correct" in payload["error"]["message"]


def test_the_queue_needs_an_actor(corpus):
    status, _ = api.handle(corpus, "GET", "/review/queue", {})
    assert status == 401


def test_a_limit_is_honoured(corpus):
    status, payload = api.handle(corpus, "GET", "/review/queue", {"limit": "3"},
                                 actor=admin(corpus))
    assert status == 200 and len(payload["cards"]) == 3
