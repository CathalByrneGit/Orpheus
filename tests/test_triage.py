"""Which unreviewed extraction to look at first.

A store with nine hundred unconfirmed instances and no order on them is a store
nobody reviews, and `review.py` had no ordering on instances at all. That is
not a convenience problem: `orpheus report` answers `insufficient_evidence`
until review has happened, so every quality claim this system makes is
downstream of somebody starting -- and "start anywhere" is the instruction
people do not act on.

The ranking is not a heuristic about importance. It is read off the thing the
report is actually waiting for: `confidence_calibration` needs two confidence
levels each holding `min_reviewed` reviewed instances before it will say
anything at all. So a reviewer can confirm three hundred `explicit`
extractions and move the verdict not one inch, while five at a level nobody has
touched moves it from silence to an answer. That is what these tests hold.
"""

from __future__ import annotations

import pytest

from orpheus import api, bundle as bundle_mod, quality, review
from orpheus.rubric import CONFIDENCE

TYPE = "Clause"


@pytest.fixture
def extracted(store):
    """A corpus shaped the way a real one is: most extractions at `explicit`,
    a thin tail everywhere else. That skew is exactly what makes reviewing in
    the order the extractor produced useless for calibration."""
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})

    plan = ([("explicit", 30)] + [(name, 6) for name in
                                  ("named", "implied", "inferred")])
    n = 0
    for label, count in plan:
        for _ in range(count):
            n += 1
            document_id = f"doc_{n % 4}"
            if not store.one("SELECT 1 FROM documents WHERE document_id = ?",
                             (document_id,)):
                store.insert("documents", {
                    "document_id": document_id, "filename": f"{document_id}.txt",
                    "file_hash": f"hash{n % 4}", "date_added": "2026-01-01T00:00:00Z",
                    "created_by": "act_r", "visibility": "private",
                    "review_status": "unreviewed"})
            instance_id = f"ins_{n:03}"
            store.insert(f"instances_{TYPE}", {
                "instance_id": instance_id, "document_id": document_id,
                "text": f"clause {n}", "source": "ai_local",
                "confidence": CONFIDENCE[label], "status": "unconfirmed",
                "created_at": "2026-01-01T00:00:00Z"})
            store.insert("instance_index", {
                "instance_id": instance_id, "document_id": document_id,
                "type_id": TYPE, "table_name": f"instances_{TYPE}",
                "created_at": "2026-01-01T00:00:00Z"})
            store.insert("provenance", {
                "provenance_id": f"prov_{n:03}", "instance_id": instance_id,
                "document_id": document_id, "excerpt": f"clause {n}",
                "source": "ai_local", "confidence": CONFIDENCE[label],
                "created_at": "2026-01-01T00:00:00Z"})
    return store


def _review(store, instance_ids):
    for instance_id in instance_ids:
        review.confirm_instance(store, instance_id, actor_id="act_r", note="ok")


# -- the claim ----------------------------------------------------------------

def test_the_report_is_silent_until_two_levels_have_enough_behind_them(extracted):
    """The premise. Without this the ranking has nothing to be right about."""
    assert quality.confidence_calibration(extracted)["verdict"] == \
        "insufficient_evidence"


def test_reviewing_in_the_order_the_extractor_produced_does_not_get_there(extracted):
    """Thirty confirmations, all at `explicit`, and the report still will not
    speak. This is the experience that makes people stop reviewing."""
    in_order = [r["instance_id"] for r in extracted.query(
        f"SELECT instance_id FROM instances_{TYPE} ORDER BY instance_id LIMIT 30")]
    _review(extracted, in_order)
    assert quality.confidence_calibration(extracted)["verdict"] == \
        "insufficient_evidence"


def test_ten_from_the_ranked_queue_gets_there(extracted):
    """Two levels, five each. The number is not a guess -- it is what
    `min_reviewed` says, which is why it comes out the same every time."""
    queue = review.triage(extracted, limit=10)["queue"]
    assert len(queue) == 10
    _review(extracted, [item["instance_id"] for item in queue])
    assert quality.confidence_calibration(extracted)["verdict"] != \
        "insufficient_evidence"


# -- how it ranks -------------------------------------------------------------

def test_it_leads_with_the_levels_that_are_short(extracted):
    queue = review.triage(extracted, limit=10)["queue"]
    levels = [item["confidence_label"] for item in queue]
    # Two levels, five apiece, and neither of them `explicit`: `explicit` has
    # thirty waiting and would be the obvious pick by volume, which is exactly
    # the pick that leaves the report silent.
    assert len(set(levels)) == 2
    assert all(levels.count(label) == 5 for label in set(levels))


def test_every_item_says_why_it_is_there(extracted):
    """A queue that cannot explain itself is a queue somebody overrides."""
    queue = review.triage(extracted, limit=10)["queue"]
    for item in queue:
        assert "short of counting toward a verdict" in item["reason"]


def test_the_headline_names_the_shortest_way_to_an_answer(extracted):
    result = review.triage(extracted)
    assert "fewer than two confidence levels" in result["headline"]
    assert "shortest way to an answer" in result["headline"]
    assert result["n_unreviewed"] == 48


def test_the_queue_spreads_across_documents(extracted):
    """A calibration measured on one document is a measurement of that
    document. Spreading costs the reviewer nothing."""
    queue = review.triage(extracted, limit=8)["queue"]
    assert len({item["document_id"] for item in queue}) == 4


def test_once_the_report_can_answer_it_deepens_the_thinnest_level(extracted):
    _review(extracted, [item["instance_id"] for item in
                        review.triage(extracted, limit=10)["queue"]])
    result = review.triage(extracted, limit=5)
    assert "will answer" in result["headline"]
    assert "what is left is depth" in result["headline"]
    for item in result["queue"]:
        assert "least review behind it" in item["reason"]
    # The thinnest level, which is whichever one the first pass did not take.
    counts = {level["confidence_label"]: level["n_reviewed"]
              for level in result["levels"]}
    thinnest = min(counts, key=lambda label: counts[label])
    assert result["queue"][0]["confidence_label"] == thinnest


# -- what it will not claim ---------------------------------------------------

def test_one_reachable_level_is_still_no_way_to_an_answer(extracted):
    """Found by running it on eight real contracts, where the model quoted so
    well that 184 of 189 extractions landed `explicit` and the other two levels
    held 1 and 4 -- neither able to reach five however much anybody reviewed.

    The first version said "reviewing 5 at `explicit` is the shortest way to an
    answer". It is not a way to an answer at all: the report needs *two*
    qualifying levels, and telling somebody otherwise means they do the work
    and get the same silence.
    """
    store = extracted
    # Leave one level with too few waiting to ever qualify.
    store.execute(
        f"DELETE FROM instances_{TYPE} WHERE confidence = ? "
        f"AND instance_id NOT IN (SELECT instance_id FROM instances_{TYPE} "
        f"WHERE confidence = ? LIMIT 2)",
        (CONFIDENCE["named"], CONFIDENCE["named"]))
    store.execute(f"DELETE FROM instances_{TYPE} WHERE confidence = ?",
                  (CONFIDENCE["implied"],))
    store.execute(f"DELETE FROM instances_{TYPE} WHERE confidence = ?",
                  (CONFIDENCE["inferred"],))

    result = review.triage(store)
    assert "no amount of review will make the report speak" in result["headline"]
    assert "1 level(s) that can ever get there" in result["headline"]
    assert "not more review" in result["headline"]


def test_a_level_that_can_never_reach_the_threshold_is_not_offered(store):
    """Offering work that cannot achieve the thing it is offered for is worse
    than offering nothing: the reviewer does it, and the report stays silent."""
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    store.insert("documents", {
        "document_id": "doc_1", "filename": "a.txt", "file_hash": "h",
        "date_added": "2026-01-01T00:00:00Z", "created_by": "act_r",
        "visibility": "private", "review_status": "unreviewed"})
    for n in range(3):        # three at one level, and nothing else at all
        store.insert(f"instances_{TYPE}", {
            "instance_id": f"ins_{n}", "document_id": "doc_1",
            "text": "x", "source": "ai_local", "confidence": 1.0,
            "status": "unconfirmed", "created_at": "2026-01-01T00:00:00Z"})
        store.insert("instance_index", {
            "instance_id": f"ins_{n}", "document_id": "doc_1", "type_id": TYPE,
            "table_name": f"instances_{TYPE}",
            "created_at": "2026-01-01T00:00:00Z"})
        store.insert("provenance", {
            "provenance_id": f"prov_{n}", "instance_id": f"ins_{n}",
            "document_id": "doc_1", "excerpt": "x", "source": "ai_local",
            "confidence": 1.0, "created_at": "2026-01-01T00:00:00Z"})

    result = review.triage(store)
    assert "no amount of review will make the report speak" in result["headline"]
    assert "not more review" in result["headline"]


def test_an_empty_store_says_so_rather_than_ranking_nothing(store):
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    result = review.triage(store)
    assert result["queue"] == [] and result["n_unreviewed"] == 0
    assert "Nothing has been extracted yet" in result["headline"]


# -- over the API -------------------------------------------------------------

def test_the_queue_is_an_administrators_view(extracted):
    extracted.insert("actors", {"actor_id": "act_other", "display_name": "Bo",
                                "is_admin": 0,
                                "created_at": "2026-01-01T00:00:00Z"})
    other = dict(extracted.one("SELECT * FROM actors WHERE actor_id = 'act_other'"))
    status, payload = api.handle(extracted, "GET", "/review/triage", actor=other)
    assert status == 403
    assert "Pass `document_id`" in payload["error"]["message"]


def test_one_document_can_be_triaged_by_anyone_who_may_read_it(extracted):
    admin = dict(extracted.one("SELECT * FROM actors WHERE actor_id = 'act_r'"))
    status, payload = api.handle(extracted, "GET", "/review/triage",
                                 body={"document_id": "doc_1", "limit": "5"},
                                 actor=admin)
    assert status == 200
    assert len(payload["queue"]) == 5
    assert {item["document_id"] for item in payload["queue"]} == {"doc_1"}
