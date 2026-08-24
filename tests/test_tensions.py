"""Conflicts that survive review.

The property defended here is that a verified disagreement has somewhere to
live. Every other review verb in the store resolves *towards* one answer, so
without this a page renders two confirmed mentions that contradict each other
in the same voice and reads as though they agree.

Two rules do the work, and both are tested rather than trusted: a tension cites
at least two sides that exist, and `accepted` is a terminal state a reviewer can
actually reach.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.entities import (confirm_link, create_entity, entity_page,
                              link_mention, list_entities)
from orpheus.tensions import (accept_tension, detect_conflicts, get_tension,
                              list_tensions, propose_tensions, raise_tension,
                              resolve_tension, tensions_for_document,
                              tensions_for_entity, withdraw_tension)
from orpheus.utils import NotFound, OrpheusError, naive_key

# One company, two documents, two registered addresses. Both extractions are
# correct; the company moved. This is the case the whole module exists for.
MENTIONS = [
    ("i1", "doc_1", "Ardmore Digital Ltd", "12 Ushers Quay, Dublin 8"),
    ("i2", "doc_2", "Ardmore Digital Limited", "4 Sandwith Street, Dublin 2"),
    ("i3", "doc_3", "Ardmore Digital Ltd", "12 Ushers Quay, Dublin 8"),
]


@pytest.fixture
def corpus(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    for document_id in ("doc_1", "doc_2", "doc_3"):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status)"
            " VALUES (?,?,?,100,1,datetime('now'),'act_a','private','unreviewed')",
            (document_id, f"{document_id}.pdf", document_id))
    for instance_id, document_id, name, address in MENTIONS:
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, address, source, confidence, status, created_at)"
            " VALUES (?,?,?,?,?,'ai_local',0.9,'unconfirmed',datetime('now'))",
            (instance_id, document_id, name, naive_key(name), address))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id,"
            " source_label, page_no, excerpt, confidence, created_at, source,"
            " alignment, char_start, char_end)"
            " VALUES (?,?,?,?,1,?,0.9,datetime('now'),'ai_local','match_exact',10,40)",
            (f"p_{instance_id}", instance_id, document_id,
             f"{document_id}.pdf", f"{name} of {address}"))
    store.conn.commit()
    return store


@pytest.fixture
def contested(corpus):
    """One entity, three confirmed mentions, two of them disagreeing."""
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    for instance_id in ("i1", "i2", "i3"):
        link_mention(corpus, entity_id, instance_id, actor_id="act_a",
                     basis="naive_key")
        confirm_link(corpus, entity_id, instance_id, actor_id="act_a")
    corpus.conn.commit()
    return corpus, entity_id


# -- a tension must be cited -------------------------------------------------

def test_one_side_is_an_assertion_not_a_tension(corpus):
    with pytest.raises(OrpheusError) as caught:
        raise_tension(corpus, "conflicting_value", "Only one view",
                      sides=["i1"], actor_id="act_a", subject_id="ent_x")
    assert "at least 2" in str(caught.value)


def test_a_side_that_does_not_exist_is_refused(corpus):
    with pytest.raises(NotFound):
        raise_tension(corpus, "conflicting_value", "Two views",
                      sides=["i1", "i_nope"], actor_id="act_a",
                      subject_id="ent_x")


def test_the_same_mention_twice_is_still_one_side(corpus):
    # Otherwise a tension could be raised against itself and look cited.
    with pytest.raises(OrpheusError):
        raise_tension(corpus, "conflicting_value", "Two views",
                      sides=["i1", "i1"], actor_id="act_a", subject_id="ent_x")


def test_every_side_carries_its_excerpt(contested):
    corpus, entity_id = contested
    tension_id = raise_tension(
        corpus, "conflicting_value", "Two registered addresses",
        sides=[{"instance_id": "i1", "position": "Ushers Quay"},
               {"instance_id": "i2", "position": "Sandwith Street"}],
        actor_id="act_a", subject_id=entity_id, property_id="address")
    sides = get_tension(corpus, tension_id)["sides"]
    assert len(sides) == 2
    # A tension displayed without its evidence is the thing this prevents.
    assert all(s["excerpt"] and s["page_no"] and s["filename"] for s in sides)
    assert {s["position"] for s in sides} == {"Ushers Quay", "Sandwith Street"}


# -- accepted is somewhere a reviewer can stop -------------------------------

def test_a_conflict_can_be_signed_rather_than_resolved(contested):
    corpus, entity_id = contested
    tension_id = raise_tension(corpus, "conflicting_value", "Two addresses",
                               sides=["i1", "i2"], actor_id="act_a",
                               subject_id=entity_id, property_id="address")
    accepted = accept_tension(corpus, tension_id, "act_a",
                              note="the company moved in 2024")
    assert accepted["status"] == "accepted"
    # Signed, not outstanding: it still stands on the page.
    assert tension_id in {t["tension_id"] for t in
                          tensions_for_entity(corpus, entity_id, standing_only=True)}


def test_resolving_without_saying_how_is_refused(contested):
    corpus, entity_id = contested
    tension_id = raise_tension(corpus, "conflicting_value", "Two addresses",
                               sides=["i1", "i2"], actor_id="act_a",
                               subject_id=entity_id)
    with pytest.raises(OrpheusError):
        resolve_tension(corpus, tension_id, "act_a", "")


def test_a_settled_tension_is_not_reopened(contested):
    corpus, entity_id = contested
    tension_id = raise_tension(corpus, "conflicting_value", "Two addresses",
                               sides=["i1", "i2"], actor_id="act_a",
                               subject_id=entity_id)
    resolve_tension(corpus, tension_id, "act_a", "doc_2 supersedes doc_1")
    with pytest.raises(OrpheusError) as caught:
        accept_tension(corpus, tension_id, "act_a")
    assert "resolved" in str(caught.value)


def test_a_withdrawn_tension_is_kept_as_evidence(contested):
    # Same reason a rejected instance is kept: it measures how well conflict
    # detection works, and deleting it loses the measurement with the mistake.
    corpus, entity_id = contested
    tension_id = raise_tension(corpus, "conflicting_value", "Two addresses",
                               sides=["i1", "i2"], actor_id="act_a",
                               subject_id=entity_id)
    withdraw_tension(corpus, tension_id, "act_a", "same address, formatted twice")
    assert get_tension(corpus, tension_id)["status"] == "withdrawn"
    assert tensions_for_entity(corpus, entity_id, standing_only=True) == []


def test_settling_is_recorded_in_the_history(contested):
    corpus, entity_id = contested
    tension_id = raise_tension(corpus, "conflicting_value", "Two addresses",
                               sides=["i1", "i2"], actor_id="act_a",
                               subject_id=entity_id)
    accept_tension(corpus, tension_id, "act_a", note="the company moved")
    actions = [r["action"] for r in corpus.query(
        "SELECT action FROM edit_history WHERE table_name = 'tensions' "
        "AND row_id = ? ORDER BY seq", (tension_id,))]
    assert actions == ["raise_tension", "accepted_tension"]


# -- detection ---------------------------------------------------------------

def test_disagreeing_reviewed_mentions_are_detected(contested):
    corpus, entity_id = contested
    conflicts = detect_conflicts(corpus, entity_id=entity_id)
    by_property = {c["property_id"]: c for c in conflicts}
    assert "address" in by_property
    assert by_property["address"]["n_values"] == 2
    # Two spellings of the name are not a conflict about the name -- they are
    # what the entity page calls aliases.
    assert "name" not in by_property


def test_unreviewed_disagreement_is_not_a_conflict_by_default(corpus):
    # Two unconfirmed extractions disagreeing is far more likely to be one bad
    # extraction than a real conflict. That is the review queue's job.
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    for instance_id in ("i1", "i2"):
        link_mention(corpus, entity_id, instance_id, actor_id="act_a",
                     basis="naive_key")
    corpus.conn.commit()
    assert detect_conflicts(corpus, entity_id=entity_id) == []
    assert detect_conflicts(corpus, entity_id=entity_id, reviewed_only=False)


def test_a_rejected_mention_is_not_a_side(contested):
    corpus, entity_id = contested
    corpus.execute("UPDATE instances_Company SET status = 'rejected' "
                   "WHERE instance_id = 'i2'")
    corpus.conn.commit()
    # i1 and i3 agree, so with i2 out there is nothing left to argue about.
    assert detect_conflicts(corpus, entity_id=entity_id) == []


def test_proposing_writes_open_tensions_and_does_not_repeat_itself(contested):
    corpus, entity_id = contested
    first = propose_tensions(corpus, actor_id="act_a")
    assert first["n_raised"] == 1
    raised = get_tension(corpus, first["raised"][0])
    # The machine can see the values differ. Whether that matters is the
    # judgement the state exists to hold, so it is never pre-made.
    assert raised["status"] == "open"
    assert raised["source"] == "lint"

    again = propose_tensions(corpus, actor_id="act_a")
    assert again["n_raised"] == 0 and again["already_recorded"] == 1


def test_a_withdrawn_conflict_can_be_raised_again(contested):
    # Withdrawn means "not a conflict", not "never mention this again": if the
    # evidence changes, the detector should be free to say so.
    corpus, entity_id = contested
    first = propose_tensions(corpus, actor_id="act_a")
    withdraw_tension(corpus, first["raised"][0], "act_a", "formatting only")
    assert propose_tensions(corpus, actor_id="act_a")["n_raised"] == 1


# -- the page cannot render a conflict smoothly ------------------------------

def test_the_page_marks_a_contested_property(contested):
    corpus, entity_id = contested
    propose_tensions(corpus, actor_id="act_a")
    page = entity_page(corpus, entity_id)
    assert page["contested_properties"] == ["address"]
    assert page["n_tensions"] == 1
    # Both values still shown -- the page never picked a winner and still does
    # not. What is new is that it says they disagree.
    assert len(page["properties"]["address"]) == 2


def test_the_page_marks_which_mentions_are_sides(contested):
    corpus, entity_id = contested
    propose_tensions(corpus, actor_id="act_a")
    page = entity_page(corpus, entity_id)
    sides = {m["instance_id"] for m in page["mentions"] if m["tensions"]}
    assert sides == {"i1", "i2"}


def test_a_settled_tension_leaves_the_page(contested):
    corpus, entity_id = contested
    result = propose_tensions(corpus, actor_id="act_a")
    resolve_tension(corpus, result["raised"][0], "act_a",
                    "doc_2 is the later filing and supersedes doc_1")
    page = entity_page(corpus, entity_id)
    assert page["contested_properties"] == []
    assert page["n_tensions"] == 0


def test_the_index_shows_a_contested_page_before_it_is_opened(contested):
    # A conflict findable only by clicking through is one most people won't find.
    corpus, entity_id = contested
    propose_tensions(corpus, actor_id="act_a")
    listed = {e["entity_id"]: e for e in list_entities(corpus)}
    assert listed[entity_id]["n_tensions"] == 1


# -- reading from the other end ----------------------------------------------

def test_a_document_finds_the_tensions_it_is_a_side_of(contested):
    corpus, entity_id = contested
    propose_tensions(corpus, actor_id="act_a")
    # Scoped by the sides, not the subject: doc_2's most important conflict is
    # with doc_1, and that hangs off the entity rather than off either document.
    assert len(tensions_for_document(corpus, "doc_2")) == 1
    assert tensions_for_document(corpus, "doc_3") == []


def test_listing_can_be_narrowed(contested):
    corpus, entity_id = contested
    propose_tensions(corpus, actor_id="act_a")
    assert len(list_tensions(corpus, kind="conflicting_value")) == 1
    assert list_tensions(corpus, kind="unexplained") == []
    assert len(list_tensions(corpus, status="open")) == 1
