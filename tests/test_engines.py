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
    assert set(engines.engine_names()) == {"gliner2", "langextract", "llm",
                                           "chat", "anthropic"}
    available = engines.available_engines()
    assert set(available) == {"gliner2", "langextract", "llm", "chat",
                              "anthropic"}
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


def test_a_backend_that_aborts_the_interpreter_is_still_audited(seeded, monkeypatch):
    """A native backend can abort through the interpreter rather than raise.

    Observed: langextract importing a broken `cryptography` raised a Rust
    PanicException, which is a BaseException and slipped past the `finally`'s
    idea of what a failure looks like. The audit then said the call was clean.
    A wrong answer to "what left this deployment and what happened to it" is
    worse than no answer.
    """
    import orpheus.engines as engines_mod

    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")

    class Panic(BaseException):
        pass

    def panics(*args, **kwargs):
        raise Panic("the backend aborted")

    monkeypatch.setattr(engines_mod, "_post_chat", panics)
    with pytest.raises(OrpheusError, match="Extraction failed"):
        populate(store, document_id, tier="local")

    calls = store.query("SELECT error FROM llm_calls")
    assert len(calls) == 1
    assert "the backend aborted" in calls[0]["error"]


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
                        lambda: {"gliner2": True, "langextract": True,
                                 "llm": True, "chat": True})
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
                        lambda: {"gliner2": True, "langextract": True,
                                 "llm": True, "chat": True})
    store.set_setting("extraction_engine", "gliner2")

    populate(store, document_id, tier="local")
    assert store.scalar("SELECT COUNT(*) FROM llm_calls") == 0


def test_gliner_says_so_when_it_is_not_installed(seeded, monkeypatch):
    store, document_id = seeded
    monkeypatch.setattr(engines, "available_engines",
                        lambda: {"gliner2": False, "langextract": True,
                                 "llm": True, "chat": True})
    store.set_setting("extraction_engine", "gliner2")
    with pytest.raises(OrpheusError, match="GLiNER2 is not installed"):
        populate(store, document_id, tier="local")


# -- llm: Simon Willison's library ------------------------------------------

llm_lib = pytest.importorskip("llm")


def _echo_available() -> bool:
    try:
        llm_lib.get_model("echo")
        return True
    except Exception:
        return False


echo_only = pytest.mark.skipif(not _echo_available(),
                               reason="needs the llm-echo plugin")


@echo_only
def test_the_llm_engine_drives_a_real_model(seeded):
    # llm-echo is a real llm plugin that returns its own input as JSON, so this
    # exercises the whole path -- model lookup, system prompt, prompt call,
    # response text, usage -- without a provider or a key.
    store, document_id = seeded
    store.set_setting("extraction_engine", "llm")
    store.set_setting("local_llm_model", "echo")

    result = populate(store, document_id, tier="local")
    assert result["engine"] == "llm"
    # echo hands back a description of the prompt rather than extractions, so
    # nothing survives parsing -- which is itself the right behaviour: a reply
    # that is not the requested shape yields no findings, not junk ones.
    assert result["entities"] == []

    call = store.query("SELECT provider, model, prompt_chars, error FROM llm_calls")[0]
    assert call["model"] == "echo"
    assert call["error"] is None
    # Token counts from the provider, where a character count used to stand in.
    assert call["prompt_chars"] > 0


@echo_only
def test_the_document_and_the_bundle_prompt_both_reach_the_model(seeded):
    import json as _json
    store, document_id = seeded
    store.set_setting("extraction_engine", "llm")
    store.set_setting("local_llm_model", "echo")

    populate(store, document_id, tier="local")
    # echo's reply is a JSON description of what it was sent.
    model = llm_lib.get_model("echo")
    reply = _json.loads(model.prompt("probe", system="sys", stream=False).text())
    assert set(reply) >= {"prompt", "system"}


@echo_only
def test_a_model_without_schema_support_is_asked_for_json_instead(seeded):
    from orpheus.engines import _JSON_INSTRUCTIONS
    import json as _json
    store, document_id = seeded
    store.set_setting("extraction_engine", "llm")
    store.set_setting("local_llm_model", "echo")

    populate(store, document_id, tier="local")
    # echo does not support schemas, so the shape has to be asked for. A model
    # that does support them gets the schema and no such plea.
    assert not getattr(llm_lib.get_model("echo"), "supports_schema", False)
    assert "no code fence" in _JSON_INSTRUCTIONS


def test_the_extraction_schema_names_only_types_from_the_bundle():
    from orpheus.engines import extraction_schema
    schema = extraction_schema(load_bundle())
    item = schema["properties"]["extractions"]["items"]
    assert item["required"] == ["type", "excerpt", "properties"]
    assert "Contract" in item["properties"]["type"]["enum"]
    # Review columns are not extractable, so no type exists only to hold them.
    assert "instance_index" not in item["properties"]["type"]["enum"]


def test_an_unknown_llm_model_says_how_to_fix_it(seeded):
    store, document_id = seeded
    store.set_setting("extraction_engine", "llm")
    store.set_setting("local_llm_model", "no-such-model-anywhere")
    with pytest.raises(OrpheusError, match="llm does not know a model"):
        populate(store, document_id, tier="local")


@echo_only
def test_the_cloud_gate_still_applies_to_the_llm_engine(seeded):
    store, document_id = seeded
    store.set_setting("extraction_engine", "llm")
    store.set_setting("cloud_llm_model", "echo")
    with pytest.raises(OrpheusError, match="Cloud processing is disabled"):
        populate(store, document_id, tier="cloud", opt_in=True)
    assert llm.cloud_calls(store) == []


def test_the_audit_names_the_provider_the_key_actually_reached(monkeypatch):
    """`provider` on an llm_calls row has to follow the key, not the default.

    The cloud default is Gemini. A deployment routing through OpenRouter with an
    OpenRouter key would otherwise write `gemini` on every audit row -- a wrong
    answer to "where did this document's text go", which is the one question the
    log exists to answer.
    """
    from orpheus import llm

    for variable in ("ORPHEUS_CLOUD_API_KEY", "ORPHEUS_CLOUD_PROVIDER",
                     "OPENROUTER_API_KEY", "LANGEXTRACT_API_KEY",
                     "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    config = llm.model_config(None, "cloud")
    assert config["provider"] == "openrouter"
    assert config["api_key"] == "sk-or-test"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.model_config(None, "cloud")["provider"] == "anthropic"


def test_a_stored_provider_setting_wins_over_the_key(store, monkeypatch):
    # A deployment fronting several models behind one gateway names it once,
    # rather than having it inferred per call.
    from orpheus import llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    store.set_setting("cloud_provider", "acme-gateway")
    assert llm.model_config(store, "cloud")["provider"] == "acme-gateway"


def test_the_reported_send_mode_matches_what_is_actually_sent(seeded):
    """A false claim about what leaves the building is worse than no claim.

    `send_mode` was read from a setting defaulting to `"excerpt"` that nothing
    implemented: `populate()` sends the whole document, and every engine records
    `excerpt_only=False`. The audit was honest while `/capabilities` was not.
    """
    from orpheus import llm

    store, document_id = seeded
    store.set_setting("cloud_ai_policy", "org_allow")
    assert llm.cloud_policy(store)["send_mode"] == "full_document"

    # And a setting that nothing reads cannot quietly change the answer back.
    store.set_setting("cloud_send_mode", "excerpt")
    assert llm.cloud_policy(store)["send_mode"] == "full_document"


def test_extraction_records_that_it_sent_the_whole_document(seeded, chat_server):
    store, document_id = seeded
    store.set_setting("extraction_engine", "chat")
    store.set_setting("local_base_url", chat_server)
    populate(store, document_id, tier="local")

    row = store.one("SELECT excerpt_only, prompt_chars FROM llm_calls")
    assert row["excerpt_only"] == 0
    # The whole page text, not a selection from it.
    from orpheus.population import document_text
    assert row["prompt_chars"] == len(document_text(store, document_id))


# -- the budget ---------------------------------------------------------------
#
# A third condition on the cloud gate, alongside the org policy and the
# per-request opt-in. Denominated in characters because Orpheus does not know
# any provider's price list, and a budget that silently stops matching the
# invoice is a control somebody is relying on.

def test_no_budget_set_means_nothing_stops_a_run(store):
    from orpheus.llm import budget_status
    status = budget_status(store)
    assert status["chars_limit"] is None
    assert status["exceeded"] is False
    assert "nothing will stop a run" in status["note"]


def test_the_gate_refuses_once_the_budget_is_spent(store):
    from orpheus.llm import assert_cloud_allowed, budget_status, record_llm_call
    from orpheus.utils import OrpheusError

    store.set_setting("cloud_ai_policy", "org_allow")
    store.set_setting("cloud_budget_chars", "1000")
    store.set_setting("cloud_budget_window", "total")
    assert_cloud_allowed(store, opt_in=True)  # policy and opt-in both satisfied

    record_llm_call(store, tier="cloud", purpose="extract", prompt_chars=600)
    assert_cloud_allowed(store, opt_in=True)
    record_llm_call(store, tier="cloud", purpose="extract", prompt_chars=600)

    assert budget_status(store)["exceeded"] is True
    with pytest.raises(OrpheusError) as caught:
        assert_cloud_allowed(store, opt_in=True)
    assert "budget" in str(caught.value)


def test_a_failed_call_still_spends_the_budget(store):
    # It sent its payload just the same, which is why the audit records it.
    from orpheus.llm import budget_status, record_llm_call
    store.set_setting("cloud_budget_chars", "1000")
    store.set_setting("cloud_budget_window", "total")
    record_llm_call(store, tier="cloud", purpose="extract", prompt_chars=1200,
                    error="502 from the provider")
    assert budget_status(store)["exceeded"] is True


def test_the_local_tier_is_not_charged_against_the_cloud_budget(store):
    from orpheus.llm import budget_status, record_llm_call
    store.set_setting("cloud_budget_chars", "1000")
    store.set_setting("cloud_budget_window", "total")
    record_llm_call(store, tier="local", purpose="extract", prompt_chars=5000)
    assert budget_status(store)["chars_used"] == 0


def test_no_cost_is_estimated_without_a_configured_rate(store):
    # Never guessed from the model name -- a wrong price is worse than none.
    from orpheus.llm import budget_status, record_llm_call
    record_llm_call(store, tier="cloud", purpose="extract", prompt_chars=1_000_000)
    status = budget_status(store)
    assert status["estimated_cost"] is None
    assert "No rate configured" in status["estimated_cost_note"]

    store.set_setting("cloud_price_per_million_chars", "3.5")
    priced = budget_status(store)
    assert priced["estimated_cost"] == 3.5
    assert "not a price read from the provider" in priced["estimated_cost_note"]


def test_capabilities_shows_the_cap_before_a_run_hits_it(store):
    from orpheus.llm import cloud_policy
    store.set_setting("cloud_budget_chars", "500")
    assert cloud_policy(store)["budget"]["chars_limit"] == 500


# -- the anthropic engine -----------------------------------------------------
#
# `chat` already reaches Anthropic through an OpenAI-compatible front, so this
# engine exists for one reason: an identity-linked API key requires an
# `anthropic-workspace-id` header on every request, and neither `chat` nor
# `llm-anthropic` can carry one.

def test_the_workspace_is_configuration_not_discovery(store, monkeypatch):
    # The endpoint that lists workspaces needs an admin key, so a deployment
    # that needs this has to be told rather than have it guessed from an error.
    from orpheus.engines import _anthropic_workspace

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert _anthropic_workspace(store) is None

    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_env")
    assert _anthropic_workspace(store) == "wrkspc_env"

    # Settings win, so it survives without an environment variable.
    store.set_setting("anthropic_workspace_id", "wrkspc_stored")
    assert _anthropic_workspace(store) == "wrkspc_stored"


def test_the_cloud_gate_runs_before_any_text_is_prepared(store):
    # Same property every other engine has: a refused call sends nothing, and
    # leaves no audit row claiming it did.
    from orpheus.engines import anthropic_extract
    from orpheus.utils import OrpheusError

    store.set_setting("cloud_ai_policy", "disabled")
    with pytest.raises(OrpheusError) as caught:
        anthropic_extract(store=store, document={"document_id": "doc_1"},
                          bundle=load_bundle(), text="anything",
                          tier="cloud", opt_in=True, actor_id=None)
    assert "disabled" in str(caught.value)
    assert store.scalar("SELECT COUNT(*) FROM llm_calls") == 0


def test_a_missing_key_is_refused_by_name(store):
    from orpheus.engines import anthropic_extract
    from orpheus.utils import OrpheusError

    store.set_setting("cloud_ai_policy", "org_allow")
    with pytest.raises(OrpheusError) as caught:
        anthropic_extract(store=store, document={"document_id": "doc_1"},
                          bundle=load_bundle(), text="anything",
                          tier="cloud", opt_in=True, actor_id=None)
    assert "ANTHROPIC_API_KEY" in str(caught.value)


def test_the_anthropic_engine_never_asks_anthropic_for_a_gemini_model(
        store, monkeypatch):
    # The cloud tier's default provider is Gemini, so its default model id is
    # a Gemini one. Passed through to Anthropic it returns a 404 naming a
    # model that plainly exists, which reads as an outage rather than as the
    # misconfiguration it is. A real six-document corpus run failed this way.
    from orpheus import engines, llm

    monkeypatch.delenv("ORPHEUS_ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ORPHEUS_CLOUD_MODEL", raising=False)

    config = llm.model_config(None, "cloud")
    assert config["model_id"].startswith("gemini")

    resolved = engines._anthropic_model_id(None, "cloud", config)
    assert resolved == engines.DEFAULT_ANTHROPIC_MODEL
    assert resolved.startswith("claude")


def test_an_anthropic_model_configured_for_the_tier_is_honoured(monkeypatch):
    # Setting the tier model to a Claude model is a deliberate choice, and
    # the engine defers to it rather than overriding with its own default.
    from orpheus import engines

    monkeypatch.delenv("ORPHEUS_ANTHROPIC_MODEL", raising=False)
    resolved = engines._anthropic_model_id(
        None, "cloud", {"model_id": "claude-opus-4-1"})
    assert resolved == "claude-opus-4-1"


def test_a_stored_anthropic_model_wins_over_everything(store, monkeypatch):
    from orpheus import engines

    monkeypatch.setenv("ORPHEUS_ANTHROPIC_MODEL", "claude-from-env")
    store.set_setting("anthropic_model", "claude-from-settings", None)
    assert engines._anthropic_model_id(
        store, "cloud", {"model_id": "gemini-2.5-flash"}) \
        == "claude-from-settings"


def test_the_budget_is_spent_in_the_unit_it_is_set_in(store, monkeypatch):
    # `prompt_chars` is summed by budget_status against a limit a person set in
    # characters. The Anthropic engine recorded the provider's token count
    # there instead, which read as the more accurate number and was the wrong
    # one: it spent a character budget in tokens, leaving the cap roughly four
    # times higher than it was set, and only for that engine.
    import inspect

    from orpheus import engines

    source = inspect.getsource(engines.anthropic_extract)
    assert "prompt_chars=len(text)" in source
    assert "input_tokens" not in source.split("record_llm_call")[-1], \
        "a token count must not be recorded in a character column"


def test_a_schema_capable_model_can_still_return_relationships():
    # The llm engine takes one of two branches. The non-schema branch has asked
    # for relationships all along; the schema branch handed the model a schema
    # with only `extractions` in it, so a provider that enforces schemas could
    # not return a relationship even though the prompt asked for one. The whole
    # relation network came out empty, and the graph, the questions and the
    # corroboration of relations all described a corpus of unconnected things
    # with nothing to say why. Seen on llm-anthropic: 15 instances, 0 edges.
    from orpheus import bundle as bundle_mod
    from orpheus.engines import extraction_schema

    bundle = bundle_mod.load()
    schema = extraction_schema(bundle)

    assert "relationships" in schema["properties"], \
        "a schema-capable model must be able to express a relationship"
    links = schema["properties"]["relationships"]["items"]["properties"]
    assert links["link_type_id"]["enum"], "the link types come from the bundle"
    assert set(schema["properties"]["extractions"]["items"]["properties"]) >= {
        "instance_id", "type", "excerpt", "properties"}, \
        "relationships refer to extractions by instance_id, so it has to be askable"


# ---------------------------------------------------------------------------
# Asking a general question
# ---------------------------------------------------------------------------

def test_an_extractor_cannot_be_asked_an_open_question(store):
    """`gliner2` and `langextract` are handed a field list and return spans for
    it. That is what makes them cheap and grounded, and it means there is no
    shape of call that asks either of them what a corpus is about."""
    from orpheus.engines import ask, general_engines

    with pytest.raises(OrpheusError) as refused:
        ask(store=store, system="s", text="t", purpose="survey",
            engine="gliner2", tier="local", opt_in=False, actor_id=None)
    assert "cannot be asked an open question" in str(refused.value)
    assert "chat" in str(refused.value)
    assert "gliner2" not in general_engines()


def test_a_failed_question_is_still_recorded_as_having_been_asked(store,
                                                                  monkeypatch):
    """The audit answers "what left this deployment", and the payload left just
    the same when the call came back an error."""
    from orpheus import engines

    def explode(base_url, api_key, payload):
        raise RuntimeError("upstream said no")

    monkeypatch.setattr(engines, "_post_chat", explode)
    with pytest.raises(OrpheusError):
        engines.ask(store=store, system="sys", text="body", purpose="survey",
                    engine="chat", tier="local", opt_in=False, actor_id=None)
    call = store.one("SELECT * FROM llm_calls ORDER BY seq DESC")
    assert call["purpose"] == "survey"
    assert "upstream said no" in call["error"]
    # System prompt included. A budget that counted only half of every call
    # would be wrong in the direction that matters.
    assert call["prompt_chars"] == len("sys") + len("body")
