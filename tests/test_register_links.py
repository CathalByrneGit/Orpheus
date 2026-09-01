"""Getting a register's identifier as far as the page.

`registers.py` opens with the measurement this exists for: *"Only 2 of 74
companies in the calibration corpus state a registered number, and a shared
registered number is the decisive, rare value that resolution otherwise never
gets."* An active register holds those numbers, `matches_for` could already find
them, and nothing joined the two. The number reached a merge comparison and
never reached the page.

What is built here is not the join. It is the *proposal* of one, and the record
of somebody agreeing. `bearing_on` already warns that a wrong match into a
register "argues confidently for the wrong answer"; applied automatically that
warning becomes the design, because every page would get a number, some of them
wrong, and the wrong ones would be indistinguishable from the right ones for
good.
"""

from __future__ import annotations

import pytest

from orpheus import api, bundle as bundle_mod, entities, registers, registry
from orpheus.utils import NotFound, OrpheusError

CSV = """name,number,address
Ardmore Digital Limited,482991,"12 Ushers Quay, Dublin 8"
Kestrel Medical Group PLC,331240,"Bergamot House, Dublin 2"
Halloran Instruments Inc,908112,"Sligo"
Halloran Instruments Inc,554003,"Cork"
"""


@pytest.fixture
def with_register(store):
    """A corpus with pages, and a register nobody has linked to them yet."""
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    store.insert("documents", {
        "document_id": "doc_1", "filename": "a.txt", "file_hash": "h",
        "date_added": "2026-01-01T00:00:00Z", "created_by": "act_r",
        "visibility": "private", "review_status": "unreviewed"})

    for n, name in enumerate(("Ardmore Digital Limited",
                              "Kestrel Medical Group PLC",
                              "Halloran Instruments Inc",
                              "Bergamot Holdings"), start=1):
        instance_id = f"ins_{n}"
        store.insert("instances_Company", {
            "instance_id": instance_id, "document_id": "doc_1", "name": name,
            "naive_key": bundle_mod.key_for(bundle, "Company", name),
            "entity_kind": "company", "source": "ai_local", "confidence": 1.0,
            "status": "unconfirmed", "created_at": "2026-01-01T00:00:00Z"})
        store.insert("instance_index", {
            "instance_id": instance_id, "document_id": "doc_1",
            "type_id": "Company", "table_name": "instances_Company",
            "created_at": "2026-01-01T00:00:00Z"})
        store.insert("provenance", {
            "provenance_id": f"prov_{n}", "instance_id": instance_id,
            "document_id": "doc_1", "excerpt": name, "source": "ai_local",
            "confidence": 1.0, "created_at": "2026-01-01T00:00:00Z"})
    entities.propose_entities(store, actor_id="act_r")

    register_id = registers.create_register(
        store, "CRO extract", origin="cro.ie bulk file", actor_id="act_r")
    registers.load_csv(store, register_id, CSV, type_id="Company",
                       actor_id="act_r")
    return {"store": store, "register_id": register_id}


def _page(store, name):
    return store.one("SELECT entity_id FROM entities WHERE canonical_name = ?",
                     (name,))["entity_id"]


def _admin(store):
    return dict(store.one("SELECT * FROM actors WHERE actor_id = 'act_r'"))


# -- the gap this closes ------------------------------------------------------

def test_a_staged_register_proposes_nothing(with_register):
    """A staged register is present and not vouched for. Proposing links from
    one would rest every later comparison on reference data nobody promoted."""
    result = registers.identifier_candidates(with_register["store"])
    assert result["proposals"] == []
    assert "absence of evidence" in result["headline"]


def test_a_promoted_register_offers_the_pages_it_could_identify(with_register):
    store, register_id = with_register["store"], with_register["register_id"]
    registers.promote(store, register_id, actor_id="act_r")

    result = registers.identifier_candidates(store, type_id="Company")
    offered = {p["canonical_name"]: p["identifier"] for p in result["proposals"]}
    assert offered == {"Ardmore Digital Limited": "482991",
                       "Kestrel Medical Group PLC": "331240"}
    # Bergamot is in the corpus and not in the register. That is the absence of
    # evidence, not evidence it has no number.
    assert "Bergamot Holdings" not in offered


def test_a_name_matching_two_identifiers_is_reported_and_not_proposed(with_register):
    """A coin toss between them is worse than leaving the page unidentified: an
    absent identifier is missing evidence, a wrong one is evidence pointing the
    wrong way."""
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")

    result = registers.identifier_candidates(store)
    assert [e["canonical_name"] for e in result["ambiguous"]] == \
        ["Halloran Instruments Inc"]
    assert "Halloran Instruments Inc" not in \
        {p["canonical_name"] for p in result["proposals"]}
    assert "more than one organisation" in result["ambiguous"][0]["reading"]
    assert "guessing between them is worse" in result["headline"]


# -- a person decides ---------------------------------------------------------

def test_confirming_a_link_records_who_and_why(with_register):
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")
    page = _page(store, "Ardmore Digital Limited")

    result = registers.link_row(store, page, with_register["register_id"], 1,
                                "confirmed", actor_id="act_r",
                                note="Address matches the signature block.")
    assert result["identifier"] == "482991"

    linked = registers.links_for(store, page)
    assert len(linked) == 1 and linked[0]["identifier"] == "482991"
    assert linked[0]["note"] == "Address matches the signature block."

    history = store.query(
        "SELECT action, edited_by FROM edit_history "
        "WHERE table_name = 'entity_register_links'")
    assert [h["action"] for h in history] == ["register_link_confirmed"]
    assert history[0]["edited_by"] == "act_r"


def test_a_decided_pair_stops_being_offered(with_register):
    """The same reason a settled merge stops being offered: a reviewer who said
    no yesterday should not be asked again today."""
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")
    page = _page(store, "Ardmore Digital Limited")

    registers.link_row(store, page, with_register["register_id"], 1, "rejected",
                       actor_id="act_r", note="A different Ardmore.")
    offered = {p["canonical_name"] for p in
               registers.identifier_candidates(store)["proposals"]}
    assert "Ardmore Digital Limited" not in offered
    # And a rejection is not a link: nothing downstream may use it.
    assert registers.links_for(store, page) == []


def test_a_page_cannot_be_linked_to_a_register_nobody_promoted(with_register):
    store = with_register["store"]
    page = _page(store, "Ardmore Digital Limited")
    with pytest.raises(OrpheusError, match="Promote it before linking"):
        registers.link_row(store, page, with_register["register_id"], 1,
                           "confirmed", actor_id="act_r")


def test_an_unknown_page_or_row_is_not_found(with_register):
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")
    with pytest.raises(NotFound):
        registers.link_row(store, "ent_nothing", with_register["register_id"],
                           1, "confirmed", actor_id="act_r")
    with pytest.raises(NotFound):
        registers.link_row(store, _page(store, "Ardmore Digital Limited"),
                           with_register["register_id"], 99, "confirmed",
                           actor_id="act_r")


# -- what the link buys -------------------------------------------------------

def test_a_confirmed_link_makes_the_merge_evidence_rest_on_a_number(with_register):
    """The point of all of it. Before the links, comparing two pages rests on
    two normalised names happening to agree. After them it rests on two rows a
    person checked."""
    store, register_id = with_register["store"], with_register["register_id"]
    registers.promote(store, register_id, actor_id="act_r")

    ardmore = _page(store, "Ardmore Digital Limited")
    kestrel = _page(store, "Kestrel Medical Group PLC")
    a = {"entity_id": ardmore, "canonical_name": "Ardmore Digital Limited",
         "type_id": "Company"}
    b = {"entity_id": kestrel, "canonical_name": "Kestrel Medical Group PLC",
         "type_id": "Company"}

    before = registers.bearing_on(store, a, b)
    assert before["basis"] == "naive_key"

    registers.link_row(store, ardmore, register_id, 1, "confirmed",
                       actor_id="act_r")
    registers.link_row(store, kestrel, register_id, 2, "confirmed",
                       actor_id="act_r")

    after = registers.bearing_on(store, a, b)
    assert after["basis"] == "confirmed_links"
    assert after["identifiers_conflict"] is True
    assert "somebody has confirmed both rows" in after["reading"]


def test_two_pages_confirmed_onto_one_row_is_the_strongest_thing_it_holds(
        with_register):
    store, register_id = with_register["store"], with_register["register_id"]
    registers.promote(store, register_id, actor_id="act_r")
    ardmore = _page(store, "Ardmore Digital Limited")
    bergamot = _page(store, "Bergamot Holdings")

    registers.link_row(store, ardmore, register_id, 1, "confirmed",
                       actor_id="act_r")
    registers.link_row(store, bergamot, register_id, 1, "confirmed",
                       actor_id="act_r", note="Trading name of the same company.")

    reading = registers.bearing_on(
        store,
        {"entity_id": ardmore, "canonical_name": "Ardmore Digital Limited",
         "type_id": "Company"},
        {"entity_id": bergamot, "canonical_name": "Bergamot Holdings",
         "type_id": "Company"})
    assert reading["shared_identifiers"] == ["482991"]
    assert "nothing stronger" in reading["reading"]
    # A name comparison would never have found this pair: they share no token.


# -- the fetch adapter --------------------------------------------------------

GLEIF_PAYLOAD = {
    "data": [
        {"type": "lei-records",
         "attributes": {
             "lei": "635400ABCDEFGHIJ1234",
             "entity": {
                 "legalName": {"name": "ARDMORE DIGITAL LIMITED"},
                 "registeredAs": "482991",
                 "jurisdiction": "IE",
                 "status": "ACTIVE",
                 "legalAddress": {"addressLines": ["12 Ushers Quay"],
                                  "city": "Dublin", "country": "IE"}}}},
        # No identifier of any kind: a row that cannot settle anything, which
        # is the one thing rows are for.
        {"type": "lei-records",
         "attributes": {"entity": {"legalName": {"name": "NAMELESS PLC"}}}},
    ]
}


def test_the_adapter_reads_the_national_number_not_just_the_lei():
    """`registeredAs` is what an Irish contract means when it says "company
    number 482991". The LEI is kept beside it because it is the one identifier
    that is the same in every jurisdiction."""
    rows = registry.gleif_rows(GLEIF_PAYLOAD)
    assert len(rows) == 1
    assert rows[0]["identifier"] == "482991"
    assert rows[0]["lei"] == "635400ABCDEFGHIJ1234"
    assert rows[0]["address"] == "12 Ushers Quay, Dublin, IE"


def test_a_fetch_returns_the_raw_payload_beside_the_rows():
    """These adapters are written to a documented shape rather than to a
    captured response, so the first thing anyone wiring one up should do is
    read what actually came back."""
    import json
    result = registry.fetch("Ardmore", opener=lambda url: json.dumps(
        GLEIF_PAYLOAD).encode())
    assert result["raw"] == GLEIF_PAYLOAD
    assert result["rows"][0]["identifier"] == "482991"
    assert "filter%5Bentity.legalName%5D=Ardmore" in result["url"]


def test_a_fetch_goes_back_through_the_file_path(with_register):
    """Routed through CSV deliberately. `load_csv` guesses the columns and says
    what it guessed, rows land staged, and somebody promotes them. Writing
    straight to `register_rows` would skip all three, and the thing skipped is
    the review."""
    store = with_register["store"]
    text = registry.to_csv(registry.gleif_rows(GLEIF_PAYLOAD))
    assert text.splitlines()[0].startswith("name,identifier,lei")

    register_id = registers.create_register(store, "GLEIF", actor_id="act_r")
    loaded = registers.load_csv(store, register_id, text, type_id="Company",
                                actor_id="act_r")
    assert loaded["n_rows"] == 1
    assert loaded["identifier_column"] == "identifier"
    rows = registers.rows(store, register_id)
    assert rows[0]["status"] == "staged"


def test_an_unknown_source_names_the_better_path():
    with pytest.raises(OrpheusError, match="needs no adapter at all"):
        registry.fetch("Ardmore", source="nowhere")


def test_a_register_that_will_not_answer_is_a_message_not_a_traceback():
    def refuse(url):
        raise OSError("Connection refused")
    with pytest.raises(OrpheusError, match="needs no network at lookup time"):
        registry.fetch("Ardmore", opener=refuse)


# -- over the API -------------------------------------------------------------

def test_linking_is_an_administrators_decision(with_register):
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")
    store.insert("actors", {"actor_id": "act_other", "display_name": "Bo",
                            "is_admin": 0, "created_at": "2026-01-01T00:00:00Z"})
    other = dict(store.one("SELECT * FROM actors WHERE actor_id = 'act_other'"))
    page = _page(store, "Ardmore Digital Limited")

    status, _ = api.handle(store, "GET", "/registers/identifiers", actor=other)
    assert status == 403
    status, _ = api.handle(
        store, "POST", f"/entities/{page}/register-link",
        body={"register_id": with_register["register_id"], "row_no": 1},
        actor=other)
    assert status == 403


def test_the_route_is_not_shadowed_by_the_general_register_route(with_register):
    """`/registers/identifiers` would otherwise be dispatched as a register
    nobody created -- the same first-match-wins trap `/registers/columns` hit."""
    store = with_register["store"]
    registers.promote(store, with_register["register_id"], actor_id="act_r")
    status, payload = api.handle(store, "GET", "/registers/identifiers",
                                 actor=_admin(store))
    assert status == 200 and "proposals" in payload
