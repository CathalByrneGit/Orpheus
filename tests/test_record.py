"""A person recording something the extractor missed.

The two properties that make this safe to have: it is grounded exactly as a
machine extraction is, and it is not counted as evidence about the extractor.
Both are load-bearing, and neither is obvious from the code that writes the row.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.quality import extraction_quality
from orpheus.record import record_fact
from orpheus.utils import OrpheusError

PAGES = {
    1: "DISTRIBUTOR AGREEMENT between Ardmore Digital Ltd and Kestrel Supply Ltd.",
    2: "Signed for Ardmore Digital Ltd\n\nBy: /s/ Niamh Ronan\nTitle: Director",
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
        " VALUES ('doc_1','contract.pdf','h1',100,2,datetime('now'),'act_a',"
        "'private','unreviewed')")
    for page_no, text in PAGES.items():
        store.execute(
            "INSERT INTO document_pages (document_id, page_no, text,"
            " text_source, char_count) VALUES ('doc_1',?,?,'pdf_text',?)",
            (page_no, text, len(text)))
    store.conn.commit()
    return store


# -- grounded exactly like a machine extraction ------------------------------

def test_a_recorded_fact_is_located_in_the_document(reading):
    result = record_fact(
        reading, "doc_1", "Person",
        {"name": "Niamh Ronan", "naive_key": "niamh ronan",
         "job_title": "Director"},
        quote="By: /s/ Niamh Ronan", actor_id="act_a")

    assert result["alignment"] == "match_exact"
    assert result["page_no"] == 2, "the page is computed from the located span"
    assert result["char_start"] is not None and result["char_end"] is not None
    assert result["source"] == "human"
    assert result["status"] == "confirmed"

    row = reading.query(
        "SELECT p.source AS source, i.status AS status FROM provenance p "
        "JOIN instances_Person i "
        "ON i.instance_id = p.instance_id WHERE p.instance_id = ?",
        (result["instance_id"],))[0]
    assert row["source"] == "human"
    assert row["status"] == "confirmed"


def test_a_quote_the_document_does_not_contain_is_refused(reading):
    # A human claim citing nothing is more dangerous than a machine one, not
    # less: nothing downstream doubts it.
    with pytest.raises(OrpheusError) as excinfo:
        record_fact(reading, "doc_1", "Person",
                    {"name": "Somebody Else", "naive_key": "somebody else"},
                    quote="Niamh Ronan also directs a company in Panama",
                    actor_id="act_a")
    assert "does not contain that quote" in str(excinfo.value)
    assert "notes" in str(excinfo.value), \
        "the refusal should point at where ungrounded knowledge does belong"
    assert not reading.query("SELECT 1 FROM instances_Person")


def test_recording_without_a_quote_is_refused(reading):
    with pytest.raises(OrpheusError):
        record_fact(reading, "doc_1", "Person", {"name": "Niamh Ronan"},
                    quote="   ", actor_id="act_a")


def test_recording_without_a_person_is_refused(reading):
    with pytest.raises(OrpheusError):
        record_fact(reading, "doc_1", "Person", {"name": "Niamh Ronan"},
                    quote="By: /s/ Niamh Ronan", actor_id="")


def test_a_type_the_bundle_does_not_declare_is_refused(reading):
    with pytest.raises(OrpheusError):
        record_fact(reading, "doc_1", "Unicorn", {"name": "x"},
                    quote="By: /s/ Niamh Ronan", actor_id="act_a")


# -- not evidence about the extractor ----------------------------------------

def test_a_recorded_fact_does_not_move_extraction_quality(reading):
    before = extraction_quality(reading)["overall"]

    record_fact(reading, "doc_1", "Person",
                {"name": "Niamh Ronan", "naive_key": "niamh ronan"},
                quote="By: /s/ Niamh Ronan", actor_id="act_a")

    after = extraction_quality(reading)["overall"]
    assert after["n_total"] == before["n_total"], (
        "the extractor never offered this, so counting it as a confirmed "
        "extraction would walk accuracy upward every time somebody filled a "
        "gap the model left")


def test_a_machine_extraction_a_person_confirmed_is_still_counted(reading):
    # The other half of the distinction. Where the machine did offer something
    # and a person agreed, that is exactly what extraction quality measures,
    # and the filter must not swallow it.
    from orpheus.extract import insert_instance, write_provenance

    bundle = bundle_mod.active(reading)
    insert_instance(reading, bundle, "Person", "inst_m", "doc_1",
                    {"name": "Niamh Ronan", "naive_key": "niamh ronan"},
                    "ai_cloud", 1.0, status="confirmed", actor_id="act_a")
    write_provenance(reading, "inst_m", "doc_1", "engine", "ai_cloud", 2,
                     "By: /s/ Niamh Ronan", 1.0, alignment="match_exact")
    reading.conn.commit()

    assert extraction_quality(reading)["overall"]["n_total"] == 1
