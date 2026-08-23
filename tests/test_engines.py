"""The extraction engines, and the grounding that makes them interchangeable.

The `chat` backend is exercised against a real HTTP server here — a stub that
speaks the OpenAI-compatible shape, which is what OpenRouter, Ollama and OpenAI
all serve. `gliner2` is exercised through a fake model object; its weights
cannot be fetched in this environment.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orpheus import engines, llm
from orpheus.align import (MATCH_EXACT, MATCH_FUZZY, MATCH_LESSER, UNGROUNDED,
                           align)
from orpheus.bundle import load as load_bundle
from orpheus.ingest import ingest
from orpheus.population import populate
from orpheus.rubric import CONFIDENCE
from orpheus.utils import OrpheusError

DOCUMENT = ("SERVICES AGREEMENT\n\nReference: DOH-2026-0431\n\n"
            "This Agreement is made on 4 February 2026 between Ardmore Digital "
            "Limited and the Department of Health.\n"
            "The total contract value is EUR 1,480,000.")


@pytest.fixture
def seeded(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    path = tmp_path / "agreement.txt"
    path.write_text(DOCUMENT)
    result = ingest(store, path, actor_id="act_test", storage_root=tmp_path / "s")
    return store, result["document_id"]


# -- alignment: the shared floor -------------------------------------------

def test_a_verbatim_quotation_is_explicit():
    start, end, status = align(DOCUMENT, "Ardmore Digital Limited")
    assert status == MATCH_EXACT
    assert DOCUMENT[start:end] == "Ardmore Digital Limited"


def test_retyped_whitespace_and_punctuation_still_count_as_the_document():
    _, _, status = align(DOCUMENT, "ardmore  digital   limited")
    assert status in (MATCH_EXACT, MATCH_LESSER)


def test_a_quotation_the_document_does_not_contain_is_not_grounded():
    assert align(DOCUMENT, "Northwind Trading Limited")[2] is UNGROUNDED


def test_a_quotation_that_starts_real_and_drifts_is_only_fuzzy():
    # The characteristic failure of a general model: it begins by quoting and
    # finishes by inventing. Only the real part is located, and the finding is
    # scored down accordingly.
    start, end, status = align(
        DOCUMENT, "Ardmore Digital Limited and the Ministry of Justice")
    assert status == MATCH_FUZZY
    assert "Ministry of Justice" not in DOCUMENT[start:end]


# -- the registry -----------------------------------------------------------

def test_every_engine_is_registered_and_reports_whether_it_can_run():
    assert set(engines.engine_names()) == {"gliner2", "langextract", "chat"}
    available = engines.available_engines()
    assert set(available) == {"gliner2", "langextract", "chat"}
    # chat needs only an endpoint, so it is always a fallback.
    assert available["chat"] is True


def test_an_unknown_engine_is_refused_by_name(store):
    store.set_setting("extraction_engine", "telepathy")
    with pytest.raises(OrpheusError, match="Unknown extraction engine"):
        engines.resolve_engine(store)


def test_a_configured_engine_overrides_auto(store):
    store.set_setting("extraction_engine", "chat")
    assert engines.resolve_engine(store) == "chat"


# -- chat, against a real server -------------------------------------------

class _StubHandler(BaseHTTPRequestHandler):
    reply = ""
    received: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _StubHandler.received = json.loads(body)
        payload = json.dumps(
            {"choices": [{"message": {"content": _StubHandler.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def chat_server():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


def test_the_chat_engine_reaches_an_openai_compatible_endpoint(seeded, chat_server):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("local_base_url", chat_server)
    _StubHandler.reply = json.dumps({"extractions": [
        {"type": "Company", "excerpt": "Ardmore Digital Limited",
         "properties": {"name": "Ardmore Digital Limited"}},
    ]})

    result = populate(store, document_id, tier="local")
    assert result["engine"] == "chat"
    entity = result["entities"][0]
    assert entity["type_id"] == "Company"
    assert entity["confidence"] == CONFIDENCE["explicit"]
    assert entity["page_no"] == 1

    # The document was actually sent, and the schema came from the bundle.
    sent = _StubHandler.received
    assert sent["messages"][1]["content"].startswith("--- Page 1 ---")
    assert "Contract" in sent["messages"][0]["content"]


def test_a_model_that_invents_a_quotation_is_scored_down_not_believed(
        seeded, chat_server):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("local_base_url", chat_server)
    _StubHandler.reply = json.dumps({"extractions": [
        {"type": "Company", "excerpt": "Northwind Trading Limited",
         "properties": {"name": "Northwind Trading Limited"}},
    ]})

    entity = populate(store, document_id, tier="local")["entities"][0]
    # It is kept -- a reviewer should see what the model claimed -- but it is
    # recorded as inferred, not as something the document says.
    assert entity["confidence"] == CONFIDENCE["inferred"]
    assert entity["char_start"] is None


def test_a_fenced_reply_is_still_parsed(seeded, chat_server):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("local_base_url", chat_server)
    _StubHandler.reply = (
        'Sure! Here is the JSON:\n```json\n'
        '{"extractions": [{"type": "Contract", "excerpt": "SERVICES AGREEMENT"}]}\n'
        "```\n")
    result = populate(store, document_id, tier="local")
    assert [e["type_id"] for e in result["entities"]] == ["Contract"]


def test_the_call_is_audited_even_when_the_endpoint_fails(seeded):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("local_base_url", "http://127.0.0.1:9")   # nothing listens
    with pytest.raises(OrpheusError, match="Extraction failed"):
        populate(store, document_id, tier="local")

    # The log answers "what left this deployment", and a failed call sent the
    # payload just the same.
    calls = store.query("SELECT tier, purpose, error FROM llm_calls")
    assert len(calls) == 1
    assert calls[0]["purpose"] == "populate"
    assert calls[0]["error"]


def test_the_cloud_gate_still_applies_to_the_chat_engine(seeded, chat_server):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("cloud_base_url", chat_server)
    with pytest.raises(OrpheusError, match="Cloud processing is disabled"):
        populate(store, document_id, tier="cloud", opt_in=True)
    assert llm.cloud_calls(store) == []


# -- gliner2 ----------------------------------------------------------------

class FakeGliner:
    """Stands in for a loaded GLiNER2 model.

    Its weights cannot be fetched in this environment (the proxy refuses
    huggingface.co), so the adapter is exercised against the result shape the
    library documents rather than against the model.
    """

    def __init__(self):
        self.structures = None

    def extract_json(self, text, structures, **kwargs):
        self.structures = structures
        offset = text.index("Ardmore Digital Limited")
        return {"Company": [{
            "name": {"text": "Ardmore Digital Limited", "start": offset,
                     "end": offset + len("Ardmore Digital Limited"),
                     "confidence": 0.91},
        }]}


def test_the_gliner_adapter_turns_fields_and_spans_into_an_instance(
        seeded, monkeypatch):
    store, document_id = seeded
    fake = FakeGliner()
    monkeypatch.setattr(engines, "_gliner_model", fake)
    monkeypatch.setattr(engines, "available_engines",
                        lambda: {"gliner2": True, "langextract": True, "chat": True})
    store.set_setting("extraction_engine", "gliner2")

    result = populate(store, document_id, tier="local")
    assert result["engine"] == "gliner2"
    entity = result["entities"][0]
    assert entity["type_id"] == "Company"
    assert entity["properties"]["name"] == "Ardmore Digital Limited"
    # Extractive by construction: the span came out of the document, so it
    # grounds exactly.
    assert entity["confidence"] == CONFIDENCE["explicit"]

    # The schema handed to it came from the bundle, in GLiNER2's field syntax.
    assert "Contract" in fake.structures
    assert any(spec.startswith("reference::") for spec in fake.structures["Contract"])


def test_a_local_engine_writes_no_cloud_audit_row(seeded, monkeypatch):
    # llm_calls records what left the deployment. A local encoder sends nothing,
    # so there is nothing to record.
    store, document_id = seeded
    monkeypatch.setattr(engines, "_gliner_model", FakeGliner())
    monkeypatch.setattr(engines, "available_engines",
                        lambda: {"gliner2": True, "langextract": True, "chat": True})
    store.set_setting("extraction_engine", "gliner2")

    populate(store, document_id, tier="local")
    assert store.scalar("SELECT COUNT(*) FROM llm_calls") == 0


def test_gliner_says_so_when_it_is_not_installed(seeded, monkeypatch):
    store, document_id = seeded
    monkeypatch.setattr(engines, "available_engines",
                        lambda: {"gliner2": False, "langextract": True, "chat": True})
    store.set_setting("extraction_engine", "gliner2")
    with pytest.raises(OrpheusError, match="GLiNER2 is not installed"):
        populate(store, document_id, tier="local")
