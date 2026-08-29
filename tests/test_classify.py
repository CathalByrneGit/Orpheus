"""Classification as a proposal, not a verdict."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.audit import row_history
from orpheus.classify import classify, classify_prompt, confirm_classification
from orpheus.ingest import ingest
from orpheus.llm import cloud_calls
from orpheus.rubric import CONFIDENCE
from orpheus.utils import OrpheusError

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


class _Handler(BaseHTTPRequestHandler):
    reply = "{}"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(
            {"choices": [{"message": {"content": _Handler.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def model_server(store):
    """A real HTTP model on a real socket, reached the way a deployment does.

    Nothing is monkeypatched: `chat` is configured as the engine and the local
    tier's base URL points here, so the request goes through `engines.ask()`
    exactly as it would in production. The old fixture stubbed out the
    transport, which is why the transport was where the bug lived.
    """
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store.set_setting("extraction_engine", "chat", None)
    store.set_setting("local_base_url",
                      f"http://127.0.0.1:{server.server_port}/v1", None)
    store.conn.commit()
    try:
        yield
    finally:
        server.shutdown()


@pytest.fixture
def ingested(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    return store, document_id


def test_the_vocabulary_comes_from_the_bundle():
    # A closed list is the point: an open one produces a new doc_type per
    # document and the column stops being worth grouping by.
    prompt = classify_prompt(bundle_mod.document_types(bundle_mod.load()))
    assert "contract, amendment, tender, correspondence, other" in prompt
    assert "1.0 stated explicitly" in prompt


def test_a_classification_lands_unconfirmed(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "sector": "health",
                                 "jurisdiction": "Ireland", "confidence": 0.9,
                                 "rationale": "Titled agreement with named parties."})
    result = classify(store, document_id, actor_id="act_test")

    assert result["doc_type"] == "contract"
    assert result["confidence"] == CONFIDENCE["named"]
    row = store.one("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    # Like any other AI-sourced value, it waits for a person.
    assert row["classification_status"] == "unconfirmed"
    assert row["classification_source"] == "ai_local"
    assert row["sector"] == "health"


def test_the_rationale_is_kept_in_the_history(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "confidence": 1.0,
                                 "rationale": "The heading says SERVICES AGREEMENT."})
    classify(store, document_id, actor_id="act_test")
    entry = next(h for h in row_history(store, "documents", document_id)
                 if h["action"] == "classify")
    assert "SERVICES AGREEMENT" in entry["note"]


def test_a_type_outside_the_vocabulary_becomes_other(ingested, model_server):
    # A model naming a type the bundle does not have is answering a different
    # question; "other" is the honest place for it.
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "shipping_manifest", "confidence": 0.9})
    assert classify(store, document_id, actor_id="act_test")["doc_type"] == "other"


def test_confidence_is_snapped_to_the_rubric(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "confidence": 0.84})
    assert classify(store, document_id)["confidence"] == CONFIDENCE["implied"]


def test_a_reply_that_is_not_json_is_refused_rather_than_guessed_at(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = "I think this is probably a contract of some kind."
    with pytest.raises(OrpheusError, match="not JSON"):
        classify(store, document_id, actor_id="act_test")
    # Nothing was written from a reply that could not be read.
    assert store.scalar("SELECT doc_type FROM documents") is None


def test_a_fenced_reply_is_still_read(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = '```json\n{"doc_type": "tender", "confidence": 0.7}\n```'
    assert classify(store, document_id)["doc_type"] == "tender"


def test_every_call_is_audited(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "confidence": 1.0})
    classify(store, document_id, actor_id="act_test")
    call = store.one("SELECT purpose, tier, document_id FROM llm_calls")
    assert call["purpose"] == "classify"
    assert call["tier"] == "local"
    assert call["document_id"] == document_id


def test_a_document_with_no_text_is_not_sent_anywhere(store, tmp_path):
    store.insert("actors", {"actor_id": "act_test", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle_mod.load(), actor_id="act_test")
    blank = tmp_path / "blank.txt"
    blank.write_text("   ")
    document_id = ingest(store, blank, actor_id="act_test",
                         storage_root=tmp_path / "s")["document_id"]

    with pytest.raises(OrpheusError, match="no extracted text"):
        classify(store, document_id, actor_id="act_test")
    assert store.scalar("SELECT COUNT(*) FROM llm_calls") == 0


def test_the_cloud_tier_is_gated(ingested):
    store, document_id = ingested
    with pytest.raises(OrpheusError, match="Cloud processing is disabled"):
        classify(store, document_id, tier="cloud", opt_in=True)
    assert cloud_calls(store) == []


def test_confirming_leaves_the_values_and_records_the_check(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "confidence": 0.9})
    classify(store, document_id, actor_id="act_test")

    confirm_classification(store, document_id, "act_test")
    row = store.one("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    assert row["classification_status"] == "confirmed"
    assert row["classification_source"] == "ai_local"   # origin unchanged


def test_correcting_the_classification_takes_responsibility_for_it(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = json.dumps({"doc_type": "contract", "confidence": 0.5})
    classify(store, document_id, actor_id="act_test")

    confirm_classification(store, document_id, "act_test", {"doc_type": "tender"})
    row = store.one("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    assert row["doc_type"] == "tender"
    assert row["classification_status"] == "amended"
    assert row["classification_source"] == "human"
    assert row["classification_confidence"] == 1.0


# -- which model actually gets asked -----------------------------------------

def test_the_engine_the_deployment_configured_is_the_one_asked(store):
    """The bug this replaced: classification reached for the `llm` library
    whenever it imported, resolved the tier's model id -- which for the cloud
    tier names a Gemini model -- and failed on every document of a deployment
    that had `llm` and not `llm-gemini`. Two whole corpora, 88 of 88."""
    from orpheus.engines import general_engine_for

    store.set_setting("extraction_engine", "anthropic", None)
    assert general_engine_for(store) == "anthropic"

    # An extractor cannot answer an open question, so it is not silently used
    # and not silently guessed around either: the fallback is the one transport
    # that needs nothing installed.
    store.set_setting("extraction_engine", "gliner2", None)
    assert general_engine_for(store) == "chat"

    store.set_setting("extraction_engine", "auto", None)
    assert general_engine_for(store) == "chat"


def test_naming_an_extractor_is_refused_rather_than_worked_around(store):
    from orpheus.engines import general_engine_for

    with pytest.raises(OrpheusError) as refused:
        general_engine_for(store, "langextract")
    assert "cannot be asked an open question" in str(refused.value)


def test_a_truncated_document_is_recorded_as_an_excerpt(ingested, model_server):
    """`excerpt_only` answers "did all of this document leave the building".
    The transport this now shares defaulted it to False, and classification is
    the one caller that truncates."""
    store, document_id = ingested
    _Handler.reply = '{"doc_type": "contract", "confidence": 1.0}'
    classify(store, document_id, actor_id="act_test", max_chars=200)
    call = store.one("SELECT * FROM llm_calls WHERE purpose = 'classify' "
                     "ORDER BY seq DESC")
    assert call["excerpt_only"] == 1
    assert call["error"] is None
    # The system prompt counts too: it is text this deployment sent, and a
    # budget that counted half of every call would be wrong the wrong way.
    assert call["prompt_chars"] > 200


# -- a field with no vocabulary is not asked about ---------------------------

def test_an_open_field_is_the_one_thing_a_closed_list_exists_to_stop():
    """`sector` was free text, and on forty-eight documents of one corpus it
    came back as thirteen spellings of a single answer — "software/open-source
    governance", "open-source software governance", "open source software
    governance" and ten more. That is exactly the harm `documentTypes` is a
    closed list to prevent, one field over."""
    bare = classify_prompt(["a", "b"])
    assert "sector" not in bare and "jurisdiction" not in bare
    listed = classify_prompt(["a"], ["health", "transport"], ["Ireland"])
    assert "sector        one of: health, transport, or null" in listed
    assert "jurisdiction  one of: Ireland, or null" in listed


def test_a_value_outside_the_list_is_dropped(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = ('{"doc_type": "contract", "sector": "software/open '
                      'source governance", "jurisdiction": "Python Software '
                      'Foundation", "confidence": 0.9}')
    result = classify(store, document_id, actor_id="act_test")
    # Both are outside the shipped vocabularies. A model answering a different
    # question than the one asked lands as null, the same way an unknown
    # doc_type lands as `other`.
    assert result["sector"] is None
    assert result["jurisdiction"] is None


def test_a_value_on_the_list_is_kept_however_it_is_cased(ingested, model_server):
    store, document_id = ingested
    _Handler.reply = ('{"doc_type": "contract", "sector": "Health", '
                      '"jurisdiction": "ireland", "confidence": 0.9}')
    result = classify(store, document_id, actor_id="act_test")
    assert (result["sector"], result["jurisdiction"]) == ("health", "Ireland")


def test_a_bundle_with_no_vocabulary_records_nothing_rather_than_guesses(
        store, tmp_path):
    """A domain with no jurisdictions worth naming should not be invited to
    invent them: asked openly about software governance minutes, a model
    answered "Python Software Foundation", which is an organisation."""
    starter = bundle_mod.load(bundle_mod.BUNDLE_DIR / "starter-0.1.0.json")
    assert bundle_mod.sectors(starter) == []
    assert bundle_mod.jurisdictions(starter) == []
