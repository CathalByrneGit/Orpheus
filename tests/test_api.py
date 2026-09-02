"""The HTTP surface.

Called directly rather than over a socket: `handle()` takes a method, a path and
a body, so the routing, the permission checks and the error mapping are all
testable without standing up a server. The Datasette plugin mounts the same
function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.api import handle
from orpheus.auth import create_actor, create_token, share_document
from orpheus.population import set_populator

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"

COMPANY = {"type": "Company", "excerpt": "Ardmore Digital Limited",
           "properties": {"name": "Ardmore Digital Limited"}}


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def api(store, tmp_path):
    owner = create_actor(store, "Owner", actor_id="act_owner")
    other = create_actor(store, "Other", actor_id="act_other")
    admin = create_actor(store, "Admin", actor_id="act_admin", is_admin=True)
    tokens = {name: create_token(store, actor)["token"]
              for name, actor in (("owner", owner), ("other", other),
                                  ("admin", admin))}
    bundle_mod.register(store, bundle_mod.load(), actor_id=admin)

    def call(method, path, body=None, who="owner"):
        return handle(store, method, path, body or {},
                      token=tokens[who] if who else None)

    status, document = call("POST", "/documents",
                            {"path": str(PDF), "storage_root": str(tmp_path / "s")})
    assert status == 200
    return store, call, document["document_id"], tokens


# -- routing and auth -------------------------------------------------------

def test_health_needs_no_token(api):
    _, call, _, _ = api
    assert call("GET", "/health", who=None) == (200, {"status": "ok"})


def test_every_other_route_needs_one(api):
    _, call, document_id, _ = api
    status, payload = call("GET", "/documents", who=None)
    assert status == 401
    assert payload["error"]["message"] == "Authentication required."


def test_an_unknown_route_is_a_404(api):
    _, call, _, _ = api
    status, payload = call("GET", "/nowhere")
    assert status == 404
    assert "No route" in payload["error"]["message"]


def test_a_bad_token_is_not_an_actor(api):
    store, _, _, _ = api
    assert handle(store, "GET", "/documents", token="rubbish")[0] == 401


# -- per-document permission ------------------------------------------------

def test_a_stranger_cannot_read_someone_elses_document(api):
    _, call, document_id, _ = api
    status, payload = call("GET", f"/documents/{document_id}", who="other")
    assert status == 403
    assert "Not permitted to view" in payload["error"]["message"]


def test_a_missing_document_looks_the_same_as_a_forbidden_one(api):
    # Distinguishing them would tell an actor which documents exist.
    _, call, _, _ = api
    assert call("GET", "/documents/doc_nope", who="other")[0] == 403


def test_a_share_grants_access_through_the_api(api):
    store, call, document_id, _ = api
    assert call("POST", f"/documents/{document_id}/share",
                {"actor_id": "act_other", "role": "viewer"})[0] == 200
    assert call("GET", f"/documents/{document_id}", who="other")[0] == 200
    # A viewer share does not grant edit.
    assert call("POST", f"/documents/{document_id}/extract", who="other")[0] == 403


def test_only_the_owner_may_share(api):
    _, call, document_id, _ = api
    assert call("POST", f"/documents/{document_id}/share",
                {"actor_id": "act_admin", "role": "editor"}, who="other")[0] == 403


# -- the pipeline over HTTP -------------------------------------------------

def test_the_whole_loop_runs_through_the_api(api):
    store, call, document_id, _ = api
    set_populator(lambda **kwargs: {"extractions": [COMPANY]})

    status, extracted = call("POST", f"/documents/{document_id}/extract",
                             {"tier": "local"})
    assert status == 200 and extracted["n_entities"] == 1

    status, listed = call("GET", f"/documents/{document_id}/instances")
    assert status == 200
    company = next(i for i in listed["instances"] if i["type_id"] == "Company")

    assert call("POST", f"/instances/{company['instance_id']}/amend",
                {"changes": {"name": "Ardmore Digital Ltd"},
                 "note": "Matches the register"})[0] == 200

    status, document = call("GET", f"/documents/{document_id}")
    assert document["review"]["amended"] == 1

    status, history = call("GET", f"/documents/{document_id}/history")
    assert {h["action"] for h in history["history"]} >= {"ingest", "extract", "amend"}


def test_an_amendment_the_bundle_forbids_comes_back_with_the_reason(api):
    store, call, document_id, _ = api
    set_populator(lambda **kwargs: {"extractions": [COMPANY]})
    call("POST", f"/documents/{document_id}/extract", {"tier": "local"})
    instance_id = store.scalar("SELECT instance_id FROM instances_Company")

    status, payload = call("POST", f"/instances/{instance_id}/amend",
                           {"changes": {"favourite_colour": "blue"}})
    # The core writes its messages for a person; they are surfaced verbatim.
    assert status == 400
    assert "not a declared property" in payload["error"]["message"]
    assert "schema amendment" in payload["error"]["message"]


def test_amending_without_changes_says_what_is_missing(api):
    store, call, document_id, _ = api
    set_populator(lambda **kwargs: {"extractions": [COMPANY]})
    call("POST", f"/documents/{document_id}/extract", {"tier": "local"})
    instance_id = store.scalar("SELECT instance_id FROM instances_Company")
    status, payload = call("POST", f"/instances/{instance_id}/amend", {})
    assert status == 400
    assert "`changes`" in payload["error"]["message"]


def test_reviewing_an_instance_needs_edit_on_its_document(api):
    store, call, document_id, _ = api
    set_populator(lambda **kwargs: {"extractions": [COMPANY]})
    call("POST", f"/documents/{document_id}/extract", {"tier": "local"})
    instance_id = store.scalar("SELECT instance_id FROM instances_Company")
    # The instance route names no document, so the permission is resolved
    # through the instance — a route that forgot would serve someone else's.
    assert call("POST", f"/instances/{instance_id}/confirm", who="other")[0] == 403


# -- the gate ---------------------------------------------------------------

def test_the_cloud_tier_is_refused_with_the_reason(api):
    _, call, document_id, _ = api
    status, payload = call("POST", f"/documents/{document_id}/extract",
                           {"tier": "cloud", "cloud_opt_in": True})
    assert status == 400
    assert "Cloud processing is disabled" in payload["error"]["message"]


def test_capabilities_says_what_this_server_can_do(api):
    _, call, _, _ = api
    status, capabilities = call("GET", "/capabilities")
    assert status == 200
    assert capabilities["cloud"]["available"] is False
    assert capabilities["cloud"]["requires_explicit_opt_in"] is True
    assert set(capabilities["extraction_engines"]) >= {"llm", "chat"}


# -- administrator routes ---------------------------------------------------

def test_administrator_routes_refuse_ordinary_actors(api):
    _, call, _, _ = api
    for method, path, body in [
        ("POST", "/admin/settings", {"key": "k", "value": "v"}),
        ("POST", "/admin/concepts/setup", {}),
        ("GET", "/audit/llm", {}),
        ("POST", "/admin/concept-parameters",
         {"template_id": "value_threshold", "parameter": "threshold", "value": 1}),
    ]:
        assert call(method, path, body, who="owner")[0] == 403, path


def test_an_administrator_can_set_up_concepts_and_change_a_threshold(api):
    store, call, _, _ = api
    status, result = call("POST", "/admin/concepts/setup", who="admin")
    assert status == 200 and result["concepts"]

    status, changed = call("POST", "/admin/concept-parameters",
                           {"template_id": "value_threshold",
                            "parameter": "threshold", "value": 250000},
                           who="admin")
    assert status == 200
    status, parameters = call("GET", "/concept-parameters", who="admin")
    assert parameters["parameters"][0]["source"] == "deployment_override"


def test_accepting_a_schema_amendment_is_an_administrator_decision(api):
    store, call, document_id, _ = api
    set_populator(lambda **kwargs: {"extractions": [
        {"type": "Contract", "excerpt": "x",
         "properties": {"name": "A", "renewal_notice_period": "90 days"}}]})
    call("POST", f"/documents/{document_id}/extract", {"tier": "local"})

    status, listed = call("GET", "/schema-amendments")
    amendment_id = listed["amendments"][0]["amendment_id"]

    assert call("POST", f"/schema-amendments/{amendment_id}/review",
                {"decision": "accepted"}, who="owner")[0] == 403
    status, result = call("POST", f"/schema-amendments/{amendment_id}/review",
                          {"decision": "accepted"}, who="admin")
    assert status == 200 and result["applied_to_bundle"] is True


def test_the_quality_report_is_reachable(api):
    _, call, _, _ = api
    status, report = call("GET", "/quality", who="admin")
    assert status == 200
    assert "Not enough to say anything" in report["headline"]


def test_the_corpus_quality_report_is_administrator_only(api):
    """It aggregates over documents the caller may not be able to read.

    The per-document report is the one a reviewer wants, and it needs only
    `view` on that document.
    """
    _, call, document_id, _ = api
    assert call("GET", "/quality", who="owner")[0] == 403
    assert call("GET", f"/documents/{document_id}/quality", who="owner")[0] == 200


# -- entities ----------------------------------------------------------------

@pytest.fixture
def with_entities(api):
    store, call, document_id, _ = api
    from orpheus.utils import naive_key
    for instance_id, name, registration in (
            ("i1", "Halloran Instruments, Inc.", "482991"),
            ("i2", "Halloran Instruments Inc", "482991")):
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, registration_number, source, confidence, status,"
            " created_at) VALUES (?,?,?,?,?,'ai_cloud',0.9,'unconfirmed',"
            " datetime('now'))",
            (instance_id, document_id, name, naive_key(name), registration))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',"
            " ?,datetime('now'))", (instance_id, document_id))
    store.conn.commit()
    return store, call, document_id


def test_proposing_and_reading_a_page_over_the_api(with_entities):
    _, call, _ = with_entities
    status, proposed = call("POST", "/entities/propose", {})
    assert status == 200 and proposed["proposed"] == 1

    entity_id = proposed["entities"][0]["entity_id"]
    status, page = call("GET", f"/entities/{entity_id}")
    assert status == 200
    assert page["counts"]["mentions"] == 2
    assert page["aliases"] == ["Halloran Instruments Inc"]


def test_the_propose_route_is_not_shadowed_by_the_entity_id_route(with_entities):
    # `/entities/propose` and `/entities/<id>` share a prefix; dispatch is
    # ordered, so this is worth pinning rather than assuming.
    _, call, _ = with_entities
    assert call("POST", "/entities/propose", {})[0] == 200


def test_confirming_a_link_and_hiding_proposals(with_entities):
    _, call, _ = with_entities
    entity_id = call("POST", "/entities/propose", {})[1]["entities"][0]["entity_id"]
    assert call("POST", f"/entities/{entity_id}/mentions/i1/confirm", {})[0] == 200

    _, everything = call("GET", f"/entities/{entity_id}")
    _, asserted = call("GET", f"/entities/{entity_id}",
                       {"include_unconfirmed": "0"})
    assert everything["counts"]["mentions"] == 2
    assert asserted["counts"]["mentions"] == 1


def test_unlinking_over_the_api_returns_the_mention_to_the_queue(with_entities):
    _, call, _ = with_entities
    entity_id = call("POST", "/entities/propose", {})[1]["entities"][0]["entity_id"]
    assert call("POST", f"/entities/{entity_id}/mentions/i2/unlink",
                {"note": "different company"})[0] == 200
    _, queue = call("GET", "/mentions/unlinked")
    assert "i2" in {m["instance_id"] for m in queue["mentions"]}


def test_candidates_are_offered_for_an_unlinked_mention(with_entities):
    _, call, _ = with_entities
    status, created = call("POST", "/entities",
                           {"type_id": "Company",
                            "canonical_name": "Halloran Instruments, Inc."})
    assert status == 200
    call("POST", f"/entities/{created['entity_id']}/mentions",
         {"instance_id": "i1"})

    _, result = call("GET", "/mentions/i2/candidates")
    assert result["candidates"][0]["entity_id"] == created["entity_id"]
    assert result["candidates"][0]["basis"] == "identifier"


def test_a_document_lists_the_entities_it_is_evidence_about(with_entities):
    _, call, document_id = with_entities
    call("POST", "/entities/propose", {})
    status, result = call("GET", f"/documents/{document_id}/entities")
    assert status == 200
    assert result["entities"]


def test_merging_over_the_api(with_entities):
    _, call, _ = with_entities
    keep = call("POST", "/entities", {"type_id": "Company",
                                      "canonical_name": "Halloran"})[1]["entity_id"]
    gone = call("POST", "/entities", {"type_id": "Company",
                                      "canonical_name": "Halloran Inc"})[1]["entity_id"]
    call("POST", f"/entities/{gone}/mentions", {"instance_id": "i1"})

    status, result = call("POST", f"/entities/{keep}/merge", {"merge_id": gone})
    assert status == 200 and result["mentions_moved"] == 1
    # The merged id still resolves, which is why its row is kept.
    assert call("GET", f"/entities/{gone}")[1]["entity"]["entity_id"] == keep


def test_a_bad_entity_request_is_a_message_not_a_traceback(with_entities):
    _, call, _ = with_entities
    assert call("POST", "/entities", {"type_id": "Company"})[0] == 400
    assert call("GET", "/entities/ent_nope")[0] == 404


# ---------------------------------------------------------------------------
# The ontology itself
# ---------------------------------------------------------------------------

def test_surveying_is_an_administrator_decision(api):
    """Not because a survey is dangerous, but because of what it is for: the
    queue it fills is the input to a decision that shapes every row the store
    will ever hold."""
    _, call, _, _ = api
    status, body = call("POST", "/ontology/survey", who="owner")
    assert status == 403
    assert "administrator" in body["error"]["message"]


def test_a_survey_proposes_and_a_person_decides(api):
    store, call, document_id, _ = api
    store.execute(
        "INSERT INTO document_pages (document_id, page_no, text, text_source, "
        "char_count) VALUES (?, 99, ?, 'native', 60)",
        (document_id, "Reference: A/1\nOwner: Ada Lovelace\nStatus: Open\n"))
    store.conn.commit()

    status, surveyed = call("POST", "/ontology/survey", {"min_support": 1},
                            who="admin")
    assert status == 200
    assert surveyed["n_candidates"] >= 1

    status, listed = call("GET", "/ontology/candidates", who="owner")
    assert status == 200
    # Counted, not claimed -- and the route says so, because a number between
    # 0 and 1 next to a machine's proposal reads as a confidence.
    assert "not a confidence" in listed["reading"]

    candidate = next(c for c in listed["candidates"]
                     if c["kind"] == "object_type")
    status, refused = call(
        "POST", f"/ontology/candidates/{candidate['candidate_id']}/review",
        {"decision": "accepted"}, who="owner")
    assert status == 403

    status, decided = call(
        "POST", f"/ontology/candidates/{candidate['candidate_id']}/review",
        {"decision": "accepted", "accepted_as": "Matter"}, who="admin")
    assert status == 200
    assert (decided["status"], decided["accepted_as"]) == ("amended", "Matter")


def test_drafting_returns_a_bundle_and_does_not_install_it(api):
    """The one property this route exists to keep. Registering an ontology is a
    deliberate act; a drafting route that also installed it would be the place
    an ontology arrived without anybody choosing it."""
    store, call, document_id, _ = api
    store.execute(
        "INSERT INTO document_pages (document_id, page_no, text, text_source, "
        "char_count) VALUES (?, 99, ?, 'native', 60)",
        (document_id, "Reference: A/1\nOwner: Ada Lovelace\nSubject: Roads\n"))
    store.conn.commit()
    call("POST", "/ontology/survey", {"min_support": 1}, who="admin")
    _, listed = call("GET", "/ontology/candidates", who="admin")
    for candidate in listed["candidates"]:
        call("POST",
             f"/ontology/candidates/{candidate['candidate_id']}/review",
             {"decision": "accepted"}, who="admin")

    before = bundle_mod.active(store)["bundleId"]
    status, drafted = call("POST", "/ontology/draft",
                           {"bundle_id": "matters-core"}, who="admin")
    assert status == 200
    assert drafted["bundle"]["bundleId"] == "matters-core"
    assert bundle_mod.active(store)["bundleId"] == before


def test_the_topology_route_caps_and_can_be_uncapped(api):
    """`?list_cap=0` is what a script wants and a page does not."""
    _, call, _, _ = api
    status, capped = call("GET", "/graph/topology", {"list_cap": "2"},
                          who="admin")
    assert status == 200 and capped["list_cap"] == 2

    status, everything = call("GET", "/graph/topology", {"list_cap": "0"},
                              who="admin")
    assert status == 200 and everything["list_cap"] is None
    assert len(everything["isolates"]) >= len(capped["isolates"])
    # Whatever is shown, the totals are the true ones.
    assert everything["counts"]["components"] == capped["counts"]["components"]


def test_exact_betweenness_is_asked_for_rather_than_waited_for(api):
    """It is the only computation on that page that does not finish in about a
    second on a corpus-sized graph -- 51 seconds on 3,000 pages against 1.3 for
    everything else combined."""
    _, call, _, _ = api
    _, sampled = call("GET", "/graph/topology", who="admin")
    _, exact = call("GET", "/graph/topology", {"exact": "1"}, who="admin")
    assert exact["centrality_method"] in ("betweenness_exact", "degree_only")
    assert sampled["centrality_method"] in ("betweenness_exact",
                                            "betweenness_sampled",
                                            "degree_only")


def test_the_ontology_queue_hands_over_a_front_and_a_total(api):
    """A queue is not a listing: the front of it plus a count of what is behind
    is what a reviewer needs."""
    store, call, document_id, _ = api
    for i in range(6):
        store.insert("ontology_candidates", {
            "candidate_id": f"cnd_api{i}", "survey_id": "srv", "kind": "object_type",
            "type_id": f"Type{i}", "n_documents": 6 - i, "n_sampled": 6,
            "engine": "deterministic", "source": "ai_local", "status": "proposed",
            "created_at": "2026-01-01T00:00:00Z"})
    status, page = call("GET", "/ontology/candidates", {"limit": "2"},
                        who="admin")
    assert status == 200
    assert len(page["candidates"]) == 2 and page["n_total"] == 6
    assert page["limit"] == 2

    _, whole = call("GET", "/ontology/candidates", {"limit": "0"}, who="admin")
    assert len(whole["candidates"]) == 6 and whole["limit"] is None
