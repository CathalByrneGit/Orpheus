"""Removing a document without removing the record that it was here.

`delete` has been in `rubric.ACTIONS` since the beginning and `auth.can` has
computed it for every actor, and nothing consumed it: there was no way to take
a document out of an Orpheus store short of destroying the store. For a corpus
of contracts -- names, signatures, addresses, third parties who never agreed to
be in anybody's database -- that is not a gap in a feature list. It is the
reason a deployment could not responsibly take a document in.

The assertion this file is built around is `test_nothing_the_document_said_
survives_anywhere`, which walks every text column of every table in the store
looking for phrases only that document contained. Enumerating the tables to
clear by hand and asserting on each one would test the list I wrote rather than
the property I want, and the list is the part most likely to be wrong: it was,
twice, and this test is what said so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orpheus import (auth, api, bundle as bundle_mod, entities,
                     extract as extract_mod, ingest, lint as lint_mod, redact)
from orpheus.population import set_populator
from orpheus.utils import NotFound, OrpheusError

# Every one of these appears in the document below and nowhere else in the
# store, so finding any of them afterwards is a leak with a name.
SECRETS = ("Ardmore Digital Limited", "Nuala Ryan", "12 Ushers Quay",
           "482991", "Bergamot House", "nuala.ryan@example.invalid")

DOCUMENT = f"""SERVICES AGREEMENT

Reference: HSE/2024/0117

This Agreement is made between {SECRETS[0]} (company number {SECRETS[3]}),
of {SECRETS[2]}, Dublin 8, and the Health Service Executive.

The Supplier's registered office is {SECRETS[4]}, Dublin 2.

1. The Client shall pay EUR 250,000 per annum.

Signed for and on behalf of {SECRETS[0]}
{SECRETS[1]}, Managing Director
Contact: {SECRETS[5]}
"""

OTHER = """MEMORANDUM OF UNDERSTANDING

Reference: OGP/2024/0900

Between Kestrel Medical Group PLC and the Office of Government Procurement.

Signed by Peter Halloran, Chief Executive.
"""


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def corpus(store, tmp_path):
    """Two documents, both extracted, with entity pages drawn across them.

    A redaction with nothing else in the store proves nothing: the interesting
    question is what happens to a page, a relation and a history that the
    redacted document *shares* with a document that stays.
    """
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_owner", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})

    def populate(*, text, **kwargs):
        if SECRETS[0] in text:
            return {"extractions": [
                {"type": "Company", "excerpt": SECRETS[0],
                 "properties": {"name": SECRETS[0], "registration_number": SECRETS[3],
                                "address": f"{SECRETS[2]}, Dublin 8",
                                "role": "supplier", "entity_kind": "company"}},
                {"type": "Person", "excerpt": f"{SECRETS[1]}, Managing Director",
                 "properties": {"name": SECRETS[1],
                                "job_title": "Managing Director"}},
                {"type": "Company", "excerpt": "Health Service Executive",
                 "properties": {"name": "Health Service Executive",
                                "role": "buyer", "entity_kind": "public_body"}},
            ]}
        return {"extractions": [
            {"type": "Company", "excerpt": "Kestrel Medical Group PLC",
             "properties": {"name": "Kestrel Medical Group PLC",
                            "role": "supplier", "entity_kind": "company"}},
            {"type": "Company", "excerpt": "Health Service Executive",
             "properties": {"name": "Health Service Executive",
                            "role": "buyer", "entity_kind": "public_body"}},
        ]}

    set_populator(populate)
    ids = {}
    for name, text in (("secret", DOCUMENT), ("other", OTHER)):
        path = tmp_path / f"{name}.txt"
        path.write_text(text)
        result = ingest.ingest(store, path, actor_id="act_owner",
                               storage_root=tmp_path / "storage")
        ids[name] = result["document_id"]
        extract_mod.extract(store, ids[name], tier="local", actor_id="act_owner")
    entities.propose_entities(store, actor_id="act_owner")
    return {"store": store, "ids": ids, "tmp": tmp_path}


def _actor(store, actor_id="act_owner"):
    return dict(store.one("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)))


def _stored(store, document_id) -> Path:
    return Path(store.one("SELECT storage_path FROM documents WHERE "
                          "document_id = ?", (document_id,))["storage_path"])


# -- the assertion the rest of this is in service of --------------------------

def _scan_for(store, needles) -> list[str]:
    """Every text-bearing column of every table, looked at once.

    Not a list of tables to check -- the list is the part most likely to be
    wrong, and a redaction that missed a table is exactly the failure this has
    to catch.
    """
    found = []
    tables = [r["name"] for r in store.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        columns = [c["name"] for c in store.query(f'PRAGMA table_xinfo("{table}")')
                   if c["hidden"] != 1]          # skip hidden generated columns
        for row in store.query(f'SELECT * FROM "{table}"'):
            for column in columns:
                value = row[column] if column in row.keys() else None
                if not isinstance(value, str):
                    continue
                for needle in needles:
                    if needle in value:
                        found.append(f"{table}.{column}: {needle!r} in {value[:70]!r}")
    return found


def test_the_scan_can_see_what_it_is_looking_for(corpus):
    """The test above this one is worthless if the scan finds nothing anywhere.
    Before the redaction, every secret is in the store somewhere."""
    leaks = _scan_for(corpus["store"], SECRETS)
    assert leaks, "the scan found nothing before redaction, so it proves nothing"
    tables = {leak.split(".")[0] for leak in leaks}
    # Spread across the store, not sitting in one column: text, extracted
    # values, provenance excerpts, entity pages and the audit trail.
    assert len(tables) >= 4, tables


def test_nothing_the_document_said_survives_anywhere(corpus):
    store = corpus["store"]
    redact.redact_document(store, corpus["ids"]["secret"],
                           actor_id="act_owner", note="Wrong file uploaded.")
    leaks = _scan_for(store, SECRETS)
    assert leaks == [], "the redacted document survives in:\n  " + "\n  ".join(leaks)


def test_the_file_and_its_page_images_are_gone_from_the_disk(corpus):
    store = corpus["store"]
    original = _stored(store, corpus["ids"]["secret"])
    assert original.exists()
    result = redact.redact_document(store, corpus["ids"]["secret"],
                                    actor_id="act_owner", note="Erasure request.")
    assert not original.exists()
    assert str(original) in result["files"]
    # And nothing else went with it.
    assert _stored(store, corpus["ids"]["other"]).exists()


# -- what survives ------------------------------------------------------------

def test_the_row_survives_as_a_tombstone(corpus):
    """A delete would take the count, the ordering and the account of why with
    it. Those are the parts of the record a redaction is supposed to leave."""
    store = corpus["store"]
    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="Contained a third party's personal data.")
    row = store.one("SELECT * FROM documents WHERE document_id = ?",
                    (corpus["ids"]["secret"],))
    assert row is not None
    assert row["redacted_at"] and row["redacted_by"] == "act_owner"
    assert row["redaction_note"] == "Contained a third party's personal data."
    assert row["filename"] == redact.REDACTED_FILENAME
    assert row["storage_path"] is None and row["byte_size"] is None
    # Kept: a doc_type is a category, not content, and it is what makes "the
    # corpus held two contracts, one since redacted" a sentence anybody can
    # still write.
    assert row["date_added"] and row["created_by"] == "act_owner"
    assert store.scalar("SELECT COUNT(*) FROM documents") == 2


def test_the_history_keeps_its_rows_and_loses_its_payloads(corpus):
    """Dropping the rows would break the `seq` chain that makes the history a
    history. Keeping the payloads would mean the audit trail quietly retained
    what the redaction was for."""
    store = corpus["store"]
    document_id = corpus["ids"]["secret"]
    before = store.scalar("SELECT COUNT(*) FROM edit_history WHERE document_id = ?",
                          (document_id,))
    assert before > 0

    redact.redact_document(store, document_id, actor_id="act_owner",
                           note="Erasure request from the signatory.")

    rows = store.query("SELECT * FROM edit_history WHERE document_id = ? "
                       "ORDER BY seq", (document_id,))
    assert len(rows) == before + 1, "the redaction itself is part of the record"
    for row in rows[:-1]:
        assert row["previous_value"] is None and row["new_value"] is None
        assert row["action"], "what happened survives; what it said does not"
    assert rows[-1]["action"] == "redact"
    assert rows[-1]["note"] == "Erasure request from the signatory."

    # The seq chain is unbroken across the whole store.
    seqs = [r["seq"] for r in store.query("SELECT seq FROM edit_history ORDER BY seq")]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_a_page_two_documents_share_keeps_the_half_that_remains(corpus):
    """The Health Service Executive is in both documents. Redacting one must
    take its mentions and leave the page."""
    store = corpus["store"]
    page = store.one("SELECT entity_id FROM entities WHERE canonical_name = ?",
                     ("Health Service Executive",))
    assert page is not None
    assert store.scalar("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?",
                        (page["entity_id"],)) == 2

    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="Wrong file.")

    assert store.one("SELECT 1 FROM entities WHERE entity_id = ?",
                     (page["entity_id"],)) is not None
    assert store.scalar("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?",
                        (page["entity_id"],)) == 1


def test_a_page_whose_every_source_is_redacted_goes_with_them(corpus):
    """Left standing it would assert something no document says, which
    `lint.uncited_pages` calls the worst failure this model can have."""
    store = corpus["store"]
    assert store.one("SELECT 1 FROM entities WHERE canonical_name = ?",
                     (SECRETS[1],)) is not None

    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="Wrong file.")

    assert store.one("SELECT 1 FROM entities WHERE canonical_name = ?",
                     (SECRETS[1],)) is None
    assert lint_mod.uncited_pages(store) == []


# -- refusing, and looking first ----------------------------------------------

def test_a_dry_run_counts_and_changes_nothing(corpus):
    """The only honest way to offer something nobody can take back."""
    store = corpus["store"]
    original = _stored(store, corpus["ids"]["secret"])
    before = _scan_for(store, SECRETS)

    result = redact.redact_document(store, corpus["ids"]["secret"],
                                    actor_id="act_owner", note="Checking.",
                                    dry_run=True)
    assert result["dry_run"] is True
    assert result["would_remove"]["instances"] == 4
    assert result["would_remove"]["pages"] == 1
    # Counted properly rather than reported as zero because the mentions it
    # would orphan have not been removed yet: Ardmore and Nuala Ryan appear in
    # no other document, the HSE does.
    assert result["would_remove"]["entity_pages"] == 2
    assert original.exists()
    assert _scan_for(store, SECRETS) == before
    assert not redact.is_redacted(store, corpus["ids"]["secret"])


def test_a_redaction_without_a_reason_is_refused(corpus):
    with pytest.raises(OrpheusError, match="saying why"):
        redact.redact_document(corpus["store"], corpus["ids"]["secret"],
                               actor_id="act_owner", note="   ")


def test_redacting_twice_says_there_is_nothing_left(corpus):
    store = corpus["store"]
    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="First.")
    with pytest.raises(OrpheusError, match="already redacted"):
        redact.redact_document(store, corpus["ids"]["secret"],
                               actor_id="act_owner", note="Second.")


def test_an_unknown_document_is_not_found(corpus):
    with pytest.raises(NotFound):
        redact.redact_document(corpus["store"], "doc_nothing",
                               actor_id="act_owner", note="x")


# -- afterwards ---------------------------------------------------------------

def test_reading_a_redacted_document_is_gone_not_empty(corpus):
    """Empty lists would read as "this document said nothing" rather than
    "this document was removed", and the difference is the whole point of
    keeping the row."""
    store, document_id = corpus["store"], corpus["ids"]["secret"]
    redact.redact_document(store, document_id, actor_id="act_owner",
                           note="Erasure request.")
    for path in (f"/documents/{document_id}/text",
                 f"/documents/{document_id}/instances",
                 f"/documents/{document_id}/original"):
        status, payload = api.handle(store, "GET", path, actor=_actor(store))
        assert status == 410, f"{path} answered {status}"
        assert "redacted" in payload["error"]["message"]


def test_a_tombstone_cannot_be_extracted_from_again(corpus):
    store, document_id = corpus["store"], corpus["ids"]["secret"]
    redact.redact_document(store, document_id, actor_id="act_owner", note="x")
    status, _ = api.handle(store, "POST", f"/documents/{document_id}/extract",
                           body={"tier": "local"}, actor=_actor(store))
    assert status == 410


def test_the_history_is_still_readable_which_is_the_point(corpus):
    store, document_id = corpus["store"], corpus["ids"]["secret"]
    redact.redact_document(store, document_id, actor_id="act_owner",
                           note="Erasure request.")
    status, payload = api.handle(store, "GET", f"/documents/{document_id}/history",
                                 actor=_actor(store))
    assert status == 200
    assert any(e["action"] == "redact" for e in payload["history"])


def test_only_the_owner_or_an_administrator_may_redact(corpus):
    """A share is permission to work on a document, never to remove one."""
    store, document_id = corpus["store"], corpus["ids"]["secret"]
    store.insert("actors", {"actor_id": "act_editor", "display_name": "Bo",
                            "is_admin": 0, "created_at": "2026-01-01T00:00:00Z"})
    auth.share_document(store, document_id, "act_editor", "editor",
                        _actor(store, "act_owner"))
    assert auth.can(store, _actor(store, "act_editor"), document_id, "edit")

    status, _ = api.handle(store, "POST", f"/documents/{document_id}/redact",
                           body={"note": "not mine to remove"},
                           actor=_actor(store, "act_editor"))
    assert status == 403
    assert not redact.is_redacted(store, document_id)


# -- the storage audit --------------------------------------------------------

def test_a_redaction_is_not_a_failed_restore(corpus):
    """A gate that is permanently red is a gate nobody reads. Every store that
    ever removed a document would fail `orpheus verify` for good."""
    store = corpus["store"]
    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="Erasure request.")

    audit = ingest.audit_storage(store, verify=True)
    assert audit["n_unavailable"] == 0
    assert audit["n_redacted"] == 1
    assert audit["n_available"] == 1
    # Named every time, never folded into the total: a count that quietly
    # shrank would be the one way a redaction could look like a loss.
    assert "1 were redacted, and are absent on purpose" in audit["headline"]


def test_the_lint_does_not_report_a_deliberate_removal_as_a_problem(corpus):
    """Reporting it would train a reviewer to ignore this check on exactly the
    stores that use redaction properly."""
    store = corpus["store"]
    redact.redact_document(store, corpus["ids"]["secret"], actor_id="act_owner",
                           note="Erasure request.")
    assert lint_mod.unavailable_originals(store) == []

    # But a file that went missing on its own is still a finding.
    _stored(store, corpus["ids"]["other"]).unlink()
    findings = lint_mod.unavailable_originals(store)
    assert len(findings) == 1
    assert findings[0]["where"]["document_id"] == corpus["ids"]["other"]
