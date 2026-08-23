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
