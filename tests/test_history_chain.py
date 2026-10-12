"""The audit trail, and the proof that it was not edited.

`edit_history` was append-only by convention: nothing writes except through
`record_edit`, which is true, enforced and tested — and still a convention, in
a SQLite file anybody holding can open with `sqlite3`. These tests are about
the part that is no longer a convention.

Every tamper here is done the way a tamper would really be done: raw SQL on the
connection, behind the core's back. A test that corrupted the chain through
`record_edit` would be testing nothing.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus import api, audit, auth, ingest as ingest_mod, redact, review
from orpheus.utils import OrpheusError, naive_key


@pytest.fixture
def corpus(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.actor = auth.create_actor(store, "Nuala Ryan", idp="d",
                                    external_id="n", is_admin=True)
    store.documents = []
    for n in (1, 2):
        path = tmp_path / f"doc{n}.txt"
        path.write_text(f"Agreement {n} with Halloran Instruments, Inc.")
        document_id = ingest_mod.ingest(
            store, path, actor_id=store.actor,
            storage_root=tmp_path / "storage")["document_id"]
        store.documents.append(document_id)
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name, "
            "naive_key, source, confidence, status, created_at) VALUES "
            "(?,?,'Halloran Instruments, Inc.',?,'ai_local',1.0,'unconfirmed',"
            "datetime('now'))",
            (f"inst_{n}", document_id, naive_key("Halloran Instruments, Inc.")))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name, "
            "document_id, created_at) VALUES (?,'Company','instances_Company',"
            "?,datetime('now'))", (f"inst_{n}", document_id))
        review.confirm_instance(store, f"inst_{n}", store.actor,
                                note=f"checked document {n}")
    return store


def admin(store):
    return {"actor_id": store.actor, "is_admin": True}


def raw(store, sql, params=()):
    """Behind the core's back, which is the only interesting way to tamper."""
    store.conn.execute(sql, params)


# -- the chain -------------------------------------------------------------

def test_every_recorded_edit_is_linked_to_the_one_before_it(corpus):
    rows = corpus.query("SELECT seq, prev_hash, row_hash FROM edit_history "
                        "ORDER BY seq")
    assert len(rows) >= 4
    assert rows[0]["prev_hash"] is None          # the first row links to nothing
    for earlier, later in zip(rows, rows[1:]):
        assert later["prev_hash"] == earlier["row_hash"]
        assert later["row_hash"]


def test_an_intact_chain_verifies(corpus):
    report = audit.verify_history(corpus)
    assert report["intact"] is True
    assert report["verified"] == report["rows"]
    assert report["broken"] is None


def test_hashing_is_not_fooled_by_where_one_field_ends(corpus):
    """Fields are length-prefixed, not joined by a separator. Without that,
    moving a character across a boundary hashes the same -- the classic
    `"ab"+"c"` versus `"a"+"bc"` collision, and a note is free text."""
    row = dict(corpus.query("SELECT * FROM edit_history ORDER BY seq")[0])
    first = audit.chain_digest(None, {**row, "row_id": "ab", "action": "c"})
    second = audit.chain_digest(None, {**row, "row_id": "a", "action": "bc"})
    assert first != second


# -- what a tamper looks like ----------------------------------------------

def test_rewriting_a_row_is_caught_and_the_row_is_named(corpus):
    raw(corpus, "UPDATE edit_history SET note = 'checked by somebody else' "
                "WHERE seq = 3")
    report = audit.verify_history(corpus)

    assert report["intact"] is False
    assert report["broken"]["seq"] == 3
    assert report["broken"]["kind"] == "content"
    assert "changed after it was written" in report["broken"]["message"]
    assert "broken at row 3" in report["headline"]


def test_putting_a_rewritten_value_back_makes_it_verify_again(corpus):
    """The hash is of the content, not a one-way mark on the row. Restoring
    what was there restores the proof -- which is what makes a break evidence
    about the content rather than about somebody having run an UPDATE."""
    before = corpus.one("SELECT note FROM edit_history WHERE seq = 3")["note"]
    raw(corpus, "UPDATE edit_history SET note = 'tampered' WHERE seq = 3")
    assert audit.verify_history(corpus)["intact"] is False

    raw(corpus, "UPDATE edit_history SET note = ? WHERE seq = 3", (before,))
    assert audit.verify_history(corpus)["intact"] is True


def test_deleting_a_row_breaks_the_link_at_the_next_one(corpus):
    raw(corpus, "DELETE FROM edit_history WHERE seq = 3")
    report = audit.verify_history(corpus)

    assert report["intact"] is False
    assert report["broken"]["kind"] == "link"
    assert report["broken"]["seq"] == 4
    assert "inserted, removed or reordered" in report["broken"]["message"]


def test_changing_who_made_an_edit_is_caught(corpus):
    """The one a tamper would most want: the same decision, somebody else's
    name on it."""
    other = auth.create_actor(corpus, "Sean Kelly", idp="d", external_id="s")
    raw(corpus, "UPDATE edit_history SET edited_by = ? WHERE seq = 3", (other,))
    assert audit.verify_history(corpus)["intact"] is False


def test_only_the_first_break_is_reported(corpus):
    """Every row after a tampered one fails too. Listing them all would bury
    the one that matters."""
    raw(corpus, "UPDATE edit_history SET note = 'x' WHERE seq = 2")
    report = audit.verify_history(corpus)
    assert report["broken"]["seq"] == 2
    assert isinstance(report["broken"], dict)       # one finding, not a list


# -- anchors, and what the chain cannot do on its own -----------------------

def test_an_unanchored_chain_does_not_claim_more_than_it_proved(corpus):
    report = audit.verify_history(corpus)
    assert report["anchor"] is None
    assert "internally consistent and not that nobody rewrote it" in \
        report["headline"]


def test_an_anchor_still_holds_over_an_untouched_chain(corpus):
    audit.anchor(corpus, actor_id=corpus.actor, note="before the sprint")
    review.confirm_instance(corpus, "inst_1", corpus.actor, note="again")

    report = audit.verify_history(corpus)
    assert report["intact"] and report["anchor_holds"] is True
    assert "still matches the anchor" in report["headline"]


def test_a_rewritten_chain_is_consistent_and_the_anchor_catches_it(corpus):
    """The whole reason anchors exist. Somebody who recomputes every hash
    produces a chain that verifies -- and cannot produce one that matches a
    head hash written down last Tuesday."""
    taken = audit.anchor(corpus, actor_id=corpus.actor)

    raw(corpus, "UPDATE edit_history SET note = 'rewritten' WHERE seq = 3")
    # Now re-chain the whole table, exactly as a careful attacker would.
    prev = None
    for row in corpus.query("SELECT * FROM edit_history ORDER BY seq"):
        row_hash = audit.chain_digest(prev, row)
        raw(corpus, "UPDATE edit_history SET prev_hash = ?, row_hash = ? "
                    "WHERE seq = ?", (prev, row_hash, row["seq"]))
        prev = row_hash

    report = audit.verify_history(corpus)
    assert report["intact"] is True                 # the chain itself is fine
    assert report["anchor_holds"] is False          # and the anchor says no
    assert "does NOT match the anchor" in report["headline"]
    assert taken["head_hash"] != report["head"]["row_hash"]


def test_an_anchor_says_to_copy_it_out_of_the_store(corpus):
    result = audit.anchor(corpus, actor_id=corpus.actor)
    assert result["head_hash"] and result["seq"]
    assert "copy this out of the store" in result["note"].lower()


def test_anchoring_an_empty_history_is_refused_by_name(store):
    bundle_mod.register(store, bundle_mod.load())
    with pytest.raises(OrpheusError, match="no chained history"):
        store.execute("DELETE FROM edit_history")
        audit.anchor(store)


# -- redaction is not tampering --------------------------------------------

def test_a_redaction_does_not_read_as_a_broken_trail(corpus):
    """Redaction nulls payloads on rows already written, by design. Without
    the mark, every subject access request would look like an attack."""
    audit.anchor(corpus, actor_id=corpus.actor, note="before redaction")
    redact.redact_document(corpus, corpus.documents[0], actor_id=corpus.actor,
                           note="subject access request")

    report = audit.verify_history(corpus)
    assert report["intact"] is True
    assert report["redacted_payloads"] > 0
    assert report["anchor_holds"] is True
    assert "gone on purpose" in report["headline"]


def test_a_redacted_row_keeps_the_chain_running_through_it(corpus):
    redact.redact_document(corpus, corpus.documents[0], actor_id=corpus.actor,
                           note="subject access request")
    rows = corpus.query("SELECT seq, prev_hash, row_hash, payload_redacted_at "
                        "FROM edit_history ORDER BY seq")
    assert any(r["payload_redacted_at"] for r in rows)
    # Linkage undisturbed: `row_hash` is never recomputed on a redaction, so
    # everything after a redacted row still points where it pointed.
    for earlier, later in zip(rows, rows[1:]):
        assert later["prev_hash"] == earlier["row_hash"]


def test_a_redacted_row_cannot_be_used_to_hide_an_edit_elsewhere(corpus):
    redact.redact_document(corpus, corpus.documents[0], actor_id=corpus.actor,
                           note="subject access request")
    raw(corpus, "UPDATE edit_history SET action = 'reject' WHERE seq = 4")
    assert audit.verify_history(corpus)["intact"] is False


# -- enrolling a store that predates the chain ------------------------------

def test_sealing_says_what_it_can_and_cannot_attest(store, tmp_path):
    """A store upgraded into migration 16 gets its existing rows sealed. What
    that attests is their content at enrolment, never that they are what was
    written -- hashing an already-tampered row would bless the tampering."""
    from orpheus.schema import _seal_history

    bundle_mod.register(store, bundle_mod.load())
    store.execute(
        "INSERT INTO edit_history (id, table_name, row_id, document_id, action,"
        " previous_value, new_value, edited_by, edited_at, note) "
        "VALUES ('edit_old','documents','doc_1',NULL,'ingest',NULL,NULL,NULL,"
        "'2026-01-01T00:00:00Z','written before the chain existed')")
    store.execute("UPDATE edit_history SET prev_hash = NULL, row_hash = NULL")

    assert _seal_history(store) >= 1
    report = audit.verify_history(store)
    assert report["intact"] is True
    note = store.one("SELECT note FROM edit_history WHERE action = 'migrate' "
                     "ORDER BY seq DESC LIMIT 1")["note"]
    assert "not as it was written" in note
    assert "Every row after it can" in note


def test_a_fresh_store_records_no_sealing_event(store):
    """There is nothing to enrol and no caveat to record: the chain reaches
    back to the beginning."""
    bundle_mod.register(store, bundle_mod.load())
    assert store.one("SELECT 1 FROM edit_history WHERE action = 'migrate'") is None


def test_a_row_written_behind_the_core_s_back_is_counted_as_unprotected(corpus):
    """Not a broken link -- an unchained row, which is a different finding and
    is reported as one."""
    corpus.execute(
        "INSERT INTO edit_history (id, table_name, row_id, document_id, action,"
        " previous_value, new_value, edited_by, edited_at, note) "
        "VALUES ('edit_raw','documents','doc_x',NULL,'ingest',NULL,NULL,NULL,"
        "'2026-01-01T00:00:00Z','slipped in')")
    report = audit.verify_history(corpus)
    assert report["unlinked"] == 1
    assert "not protected" in report["headline"]


# -- over the API -----------------------------------------------------------

def test_verifying_the_trail_is_an_administrator_view(corpus):
    person = auth.create_actor(corpus, "Sean Kelly", idp="d", external_id="s")
    status, _ = api.handle(corpus, "GET", "/audit/history", {},
                           actor={"actor_id": person, "is_admin": False})
    assert status == 403

    status, payload = api.handle(corpus, "GET", "/audit/history", {},
                                 actor=admin(corpus))
    assert status == 200 and payload["intact"] is True


def test_an_anchor_can_be_recorded_and_listed_over_the_api(corpus):
    status, recorded = api.handle(corpus, "POST", "/audit/history/anchor",
                                  {"note": "quarter end"}, actor=admin(corpus))
    assert status == 200 and recorded["head_hash"]

    status, listed = api.handle(corpus, "GET", "/audit/history/anchors", {},
                                actor=admin(corpus))
    assert status == 200
    assert listed["anchors"][0]["head_hash"] == recorded["head_hash"]
    assert listed["anchors"][0]["note"] == "quarter end"
