"""The extraction engines, and how to choose between them.

There is no single right engine. A 205M-parameter encoder that runs on a laptop
CPU and a frontier model reached over the internet are good at different things,
cost different amounts, and carry different risks — and for a public-sector
deployment the risk difference is often the deciding one. So the engine is
configuration, not architecture:

| Engine | What it is | Good at | Costs you |
|---|---|---|---|
| `gliner2` | 205M encoder, local, CPU | Named spans against a flat schema. Cannot invent a span — it labels text, so every extraction is grounded by construction. Fast, free, air-gappable. | Little reasoning. Nested or inferred values, and anything needing judgement, are out of reach. |
| `langextract` | Library over a generative model | Chunking, parallel passes, multi-pass recall, its own alignment. Handles long documents properly. | Needs a model behind it — Ollama locally, or a cloud provider. |
| `llm` | Simon Willison's `llm` library | Every provider its plugins cover — Anthropic, Gemini, OpenRouter, Ollama, Mistral — from one dependency, with schema-enforced JSON where the provider supports it and real token counts for the audit log. Shares a model registry with `datasette-llm`. | Same as any general model: it will quote text the document does not contain. |
| `chat` | Any OpenAI-compatible endpoint, no dependency | The same reach as `llm` for anything speaking that shape, with nothing to install. | No schema enforcement, no plugin ecosystem, no token counts. |

Whatever the engine, the output goes through the same door: every span is
located in the source document by `orpheus.align`, and the confidence rubric is
assigned from how well it matched. **Grounding is computed, not trusted.** An
engine that reports its own confidence does not get to set the rubric level, and
an engine that reports nothing is not penalised for it.

That is what makes them swappable rather than merely alternative.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from . import bundle as bundle_mod
from . import llm
from .align import align
from .store import Store
from .utils import OrpheusError

TIMEOUT = 180

_ENGINES: dict[str, Callable[..., dict]] = {}


def register_engine(name: str, fn: Callable[..., dict]) -> None:
    _ENGINES[name] = fn


def engine_names() -> list[str]:
    return sorted(_ENGINES)


def available_engines() -> dict[str, bool]:
    """Which engines this install could actually run."""
    return {
        "gliner2": _installed("gliner2") and _installed("torch"),
        "langextract": _installed("langextract"),
        "llm": _installed("llm"),
        # Needs only an endpoint, and one is configured by default for Ollama.
        "chat": True,
        "anthropic": _installed("anthropic"),
    }


def _installed(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def resolve_engine(store: Store | None, requested: str | None = None) -> str:
    """Which engine to use, in order of preference.

    `auto` picks the best installed, which is deliberately ordered by capability
    rather than by cost: a deployment that installed a heavier engine did so on
    purpose.
    """
    name = requested or (store.setting("extraction_engine", "auto") if store else "auto")
    if name != "auto":
        if name not in _ENGINES:
            raise OrpheusError(
                f"Unknown extraction engine {name!r}. Known: {', '.join(engine_names())}."
            )
        return name

    available = available_engines()
    for candidate in ("langextract", "llm", "gliner2", "chat"):
        if available.get(candidate):
            return candidate
    return "chat"


def get_engine(name: str) -> Callable[..., dict]:
    if name not in _ENGINES:
        raise OrpheusError(
            f"Unknown extraction engine {name!r}. Known: {', '.join(engine_names())}."
        )
    return _ENGINES[name]


# ---------------------------------------------------------------------------
# Shared: what every engine is asked to find
# ---------------------------------------------------------------------------

def _extractable_types(bundle: dict) -> list[tuple[str, list[dict]]]:
    """Object types and the properties a model is allowed to be asked for."""
    from .rubric import RESERVED_PROPS
    withheld = set(RESERVED_PROPS)
    container = bundle_mod.domain(bundle).get("containerProperty")
    if container:
        withheld.add(container)

    out = []
    for obj in bundle_mod.managed_object_types(bundle):
        props = [p for p in obj.get("properties", []) if p["id"] not in withheld]
        if props:
            out.append((obj["id"], props))
    return out


# ---------------------------------------------------------------------------
# gliner2 — local, extractive, cannot hallucinate a span
# ---------------------------------------------------------------------------

_gliner_model = None


def _gliner_field_spec(prop: dict) -> str:
    """GLiNER2's field syntax: name::dtype::choices::description."""
    dtype = {"number": "str", "integer": "str", "boolean": "str"}.get(
        prop.get("type", "string"), "str")
    choices = (prop.get("extensions") or {}).get("values") or []
    description = (prop.get("display") or {}).get("description", "")
    spec = f"{prop['id']}::{dtype}"
    if choices:
        spec += "::" + "|".join(str(c) for c in choices)
    elif description:
        spec += "::"
    if description:
        spec += f"::{description}"
    return spec


def gliner2_extract(*, store: Store, document: dict, bundle: dict, text: str,
                    tier: str, opt_in: bool, actor_id: str | None) -> dict:
    """Extract with a local GLiNER2 encoder.

    Never leaves the machine, so the cloud gate does not apply and no audit row
    is written for a network call that did not happen — `llm_calls` records what
    left the deployment, and nothing did.

    NOT EXERCISED. GLiNER2 requires PyTorch, which would not install in the
    environment this was written in, so this path is written against the
    library's source and has never run. Treat it as untested.
    """
    global _gliner_model
    if not available_engines()["gliner2"]:
        raise OrpheusError(
            "GLiNER2 is not installed. `pip install 'orpheus[gliner]'` (it pulls "
            "in PyTorch), or choose another extraction_engine."
        )
    from gliner2 import GLiNER2

    if _gliner_model is None:
        model_id = (store.setting("gliner2_model", None) if store else None) \
            or os.environ.get("ORPHEUS_GLINER_MODEL", "fastino/gliner2-base-v1")
        _gliner_model = GLiNER2.from_pretrained(model_id)

    structures = {
        type_id: [_gliner_field_spec(p) for p in props]
        for type_id, props in _extractable_types(bundle)
    }
    result = _gliner_model.extract_json(text, structures, include_spans=True,
                                        include_confidence=True)

    extractions = []
    for type_id, rows in (result or {}).items():
        for row in (rows if isinstance(rows, list) else [rows]):
            if not isinstance(row, dict):
                continue
            properties, spans = {}, []
            for key, value in row.items():
                text_value, span = _gliner_value(value)
                if text_value is None:
                    continue
                properties[key] = text_value
                if span:
                    spans.append(span)
            if not properties:
                continue
            extractions.append({
                "type_id": type_id,
                "properties": properties,
                # The whole row's span, so the instance can be highlighted as
                # one thing rather than as a scatter of fields.
                "excerpt": _span_text(text, spans),
                "char_start": min((s[0] for s in spans), default=None),
                "char_end": max((s[1] for s in spans), default=None),
            })
    return {"extractions": extractions}


def _gliner_value(value: Any) -> tuple[str | None, tuple[int, int] | None]:
    """GLiNER2 returns a scalar, or a dict carrying spans and confidence."""
    if isinstance(value, dict):
        text_value = value.get("text") or value.get("value")
        start, end = value.get("start"), value.get("end")
        span = (start, end) if isinstance(start, int) and isinstance(end, int) else None
        return (str(text_value) if text_value is not None else None), span
    if isinstance(value, list):
        return (", ".join(str(v) for v in value) if value else None), None
    return (str(value) if value not in (None, "") else None), None


def _span_text(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return ""
    return text[min(s[0] for s in spans):max(s[1] for s in spans)]


# ---------------------------------------------------------------------------
# langextract — a generative model, with chunking and its own alignment
# ---------------------------------------------------------------------------

def langextract_extract(*, store: Store, document: dict, bundle: dict, text: str,
                        tier: str, opt_in: bool, actor_id: str | None) -> dict:
    try:
        import langextract as lx
    except ImportError as exc:
        raise OrpheusError(
            "LangExtract is not installed. `pip install 'orpheus[llm]'`, or "
            "choose another extraction_engine."
        ) from exc

    from .population import examples_for, prompt_for

    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)
    config = llm.model_config(store, tier)

    kwargs: dict[str, Any] = {
        "text_or_documents": text,
        "prompt_description": prompt_for(bundle),
        "examples": examples_for(bundle),
        "model_id": config["model_id"],
    }
    if config.get("model_url"):
        kwargs["model_url"] = config["model_url"]
    if config.get("api_key"):
        kwargs["api_key"] = config["api_key"]

    error, annotated = None, None
    # BaseException, not Exception: a native backend can abort through the
    # interpreter rather than raise, and `llm_calls` is the audit of what was
    # sent. A call that failed and was recorded as clean is worse than no
    # record at all -- it is a wrong answer to "did this document's text leave
    # the building, and what happened to it".
    try:
        annotated = lx.extract(**kwargs)
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate", prompt_chars=len(text),
            provider=config["provider"], model=config["model_id"],
            document_id=document["document_id"], actor_id=actor_id,
            excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")
    return {"extractions": list(getattr(annotated, "extractions", None) or [])}


# ---------------------------------------------------------------------------
# llm — Simon Willison's llm library and its plugin ecosystem
# ---------------------------------------------------------------------------

def extraction_schema(bundle: dict) -> dict:
    """A JSON Schema for what an extraction pass should return.

    Passed to models that can enforce it, which is the difference between
    asking for JSON and being given it: no fenced reply to strip, no prose to
    parse around, no retry because the model explained itself first.

    `properties` is left open rather than typed per object type. A schema
    strict enough to say which fields belong to which type would have to be a
    union across every type in the bundle, and the providers that enforce
    schemas do not all handle unions the same way; an open object is the shape
    that works everywhere. Validation against the bundle happens on the way
    into the store regardless, where it has to happen anyway.
    """
    type_ids = [type_id for type_id, _ in _extractable_types(bundle)]
    link_ids = [link["id"] for link in (bundle.get("links") or []) if link.get("id")]
    schema = {
        "type": "object",
        "properties": {
            "extractions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "instance_id": {
                            "type": "string",
                            "description": ("A short handle, unique within this "
                                            "reply, for referring to this "
                                            "extraction from `relationships`."),
                        },
                        "type": {"type": "string", "enum": type_ids},
                        "excerpt": {
                            "type": "string",
                            "description": ("The text this was read from, copied "
                                            "character for character from the "
                                            "document."),
                        },
                        "properties": {"type": "object"},
                    },
                    "required": ["type", "excerpt", "properties"],
                },
            },
        },
        "required": ["extractions"],
    }
    # Without this a schema-capable model cannot return a relationship even
    # when the system prompt asks for one, and the whole relation network comes
    # out empty with nothing to say why: the graph, the questions and the
    # corroboration of relations all quietly describe a corpus of unconnected
    # things. The non-schema branch has asked for these all along, which is why
    # it only showed up on providers that enforce schemas.
    if link_ids:
        schema["properties"]["relationships"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from_instance_id": {"type": "string"},
                    "to_instance_id": {"type": "string"},
                    "link_type_id": {"type": "string", "enum": link_ids},
                    "evidence": {
                        "type": "string",
                        "description": ("Verbatim text from the document "
                                        "showing this relationship."),
                    },
                },
                "required": ["from_instance_id", "to_instance_id",
                             "link_type_id"],
            },
        }
    return schema


def llm_extract(*, store: Store, document: dict, bundle: dict, text: str,
                tier: str, opt_in: bool, actor_id: str | None) -> dict:
    """Extract through the `llm` library.

    One dependency reaches every provider its plugin ecosystem covers —
    `llm-anthropic`, `llm-gemini`, `llm-openrouter`, `llm-ollama`, `llm-mistral`
    and the rest — so adding a provider is `pip install llm-<provider>` and a
    model id, not an adapter. It is also the library underneath `datasette-llm`,
    which means the core and the Datasette surface can end up sharing one model
    registry rather than each having their own.

    Keys: Orpheus passes one explicitly when it has one, and otherwise lets
    `llm` resolve from its own keystore — which is a real convenience and worth
    being clear about, because it means a key Orpheus never sees can still serve
    a call. That does not weaken the gate: the gate decides *whether* a call
    happens, and it has already decided by the time this runs.
    """
    try:
        import llm as llm_lib
    except ImportError as exc:
        raise OrpheusError(
            "The llm library is not installed. `pip install 'orpheus[chat]'`, "
            "or choose another extraction_engine."
        ) from exc

    from .population import prompt_for

    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)
    config = llm.model_config(store, tier)
    model_id = _llm_model_id(store, tier, config)

    try:
        model = llm_lib.get_model(model_id)
    except Exception as exc:
        raise OrpheusError(
            f"llm does not know a model called {model_id!r}: {exc}. "
            "Install the provider's plugin (for example `pip install "
            "llm-anthropic`, `llm-gemini`, `llm-openrouter`, `llm-ollama`) or "
            "set the {tier}_model setting to one `llm models` lists."
        ) from exc

    instructions = prompt_for(bundle)
    kwargs: dict[str, Any] = {"system": instructions, "stream": False}
    if getattr(model, "supports_schema", False):
        kwargs["schema"] = extraction_schema(bundle)
    else:
        # No enforcement available, so the shape has to be asked for and then
        # checked. _parse_chat_payload tolerates the fenced reply that follows.
        kwargs["system"] = (instructions + "\n\n" + _JSON_INSTRUCTIONS
                            + _link_instructions(bundle))
    if config.get("api_key") and isinstance(model, llm_lib.KeyModel):
        kwargs["key"] = config["api_key"]

    error, content, usage = None, "", None
    # BaseException, not Exception: a native backend can abort through the
    # interpreter rather than raise, and `llm_calls` is the audit of what was
    # sent. A call that failed and was recorded as clean is worse than no
    # record at all -- it is a wrong answer to "did this document's text leave
    # the building, and what happened to it".
    try:
        response = model.prompt(text, **kwargs)
        content = response.text()
        usage = response.usage()
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate",
            # Characters, because the column is `prompt_chars` and the budget
            # is set in characters. See anthropic_extract.
            prompt_chars=len(text),
            provider="llm:" + str(getattr(model, "needs_key", None) or "local"),
            model=model_id, document_id=document["document_id"],
            actor_id=actor_id, excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")

    return _parse_chat_payload(content)


def _llm_model_id(store: Store | None, tier: str, config: dict) -> str:
    if store is not None:
        configured = store.setting(f"{tier}_llm_model", None)
        if configured:
            return configured
    return os.environ.get(f"ORPHEUS_{tier.upper()}_LLM_MODEL") or config["model_id"]


_JSON_INSTRUCTIONS = (
    "Return JSON only, of the form "
    '{"extractions": [{"instance_id": "<a short id unique within this reply>", '
    '"type": "<one of the entity types above>", '
    '"excerpt": "<verbatim text from the document>", "properties": {...}}]}. '
    "The `instance_id` is a local handle for referring to an extraction from "
    "`relationships` below; it is not stored. "
    "Every `excerpt` must be copied character for character from the document. "
    "Omit any property the document does not state. Return no prose, no "
    "explanation and no code fence."
)


# ---------------------------------------------------------------------------
# chat — any OpenAI-compatible endpoint, with no dependency at all
# ---------------------------------------------------------------------------

def chat_extract(*, store: Store, document: dict, bundle: dict, text: str,
                 tier: str, opt_in: bool, actor_id: str | None) -> dict:
    """Extract by asking a general model for JSON.

    One HTTP shape reaches a lot of places: OpenRouter fronts Anthropic, Google
    and OpenAI models behind it, Ollama serves it locally, and OpenAI serves it
    directly. Which of those is in use is a base URL and a model id, both
    configuration.

    A general model will quote text the document does not contain. That is not
    a reason to avoid it — it is a reason not to take its word for anything.
    Every span it returns is located in the source afterwards, and one that
    cannot be found lands at `inferred` rather than being stored as fact.
    """
    from .population import prompt_for

    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)
    config = llm.model_config(store, tier)
    base_url = config.get("base_url") or _default_base_url(store, tier)

    schema_lines = []
    for type_id, props in _extractable_types(bundle):
        fields = ", ".join(f'"{p["id"]}"' for p in props)
        schema_lines.append(
            f'  {{"instance_id": "<a short id unique within this reply>", '
            f'"type": "{type_id}", "excerpt": "<verbatim text from the '
            f'document>", "properties": {{{fields}}}}}')

    links = _link_instructions(bundle)
    instructions = (
        prompt_for(bundle)
        + "\n\nReturn JSON only, of the form:\n"
          '{"extractions": [\n' + ",\n".join(schema_lines[:3]) + "\n],"
        + links + "\n}\n"
          "Every `excerpt` must be copied character for character from the "
          "document. Omit any property the document does not state. Return no "
          "prose, no explanation and no code fence."
    )

    payload = {
        "model": config["model_id"],
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }

    error, content = None, ""
    # BaseException, not Exception: a native backend can abort through the
    # interpreter rather than raise, and `llm_calls` is the audit of what was
    # sent. A call that failed and was recorded as clean is worse than no
    # record at all -- it is a wrong answer to "did this document's text leave
    # the building, and what happened to it".
    try:
        content = _post_chat(base_url, config.get("api_key"), payload)
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate", prompt_chars=len(text),
            provider=config["provider"], model=config["model_id"],
            document_id=document["document_id"], actor_id=actor_id,
            excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")

    return _parse_chat_payload(content)


def _default_base_url(store: Store | None, tier: str) -> str:
    if store is not None:
        configured = store.setting(f"{tier}_base_url", None)
        if configured:
            return configured
    if tier == "local":
        host = os.environ.get("ORPHEUS_OLLAMA_HOST", "http://localhost:11434")
        return host.rstrip("/") + "/v1"
    return os.environ.get("ORPHEUS_CHAT_BASE_URL", "https://openrouter.ai/api/v1")


def _post_chat(base_url: str, api_key: str | None, payload: dict) -> str:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = json.loads(response.read().decode())
    return body["choices"][0]["message"]["content"] or ""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_chat_json(content: str) -> list[dict]:
    """Just the extractions. Kept for callers that want only those."""
    return _parse_chat_payload(content)["extractions"]


def _parse_chat_payload(content: str) -> dict:
    """Extractions *and* relationships, tolerating a fenced reply.

    Relationships were the missing half. `population.normalise_population()`
    has always accepted them and `extract()` has always written them to
    `edges`, but every engine returned `{"extractions": [...]}` and nothing
    else -- so the table, the normaliser and the writer were all correct and
    permanently unreachable, and the corpus could never have a relation in it.
    """
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {"extractions": [], "relationships": []}
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {"extractions": [], "relationships": []}

    if not isinstance(parsed, dict):
        return {"extractions": [i for i in (parsed or []) if isinstance(i, dict)],
                "relationships": []}
    items = parsed.get("extractions") or parsed.get("entities") or []
    links = parsed.get("relationships") or parsed.get("relations") or []
    return {"extractions": [i for i in items if isinstance(i, dict)],
            "relationships": [l for l in links if isinstance(l, dict)]}


def _link_instructions(bundle: dict) -> str:
    """Ask for relations, in the link types the bundle actually declares.

    Constrained to declared types rather than left open: an undeclared link
    type is dropped by `extract()` and recorded as a schema amendment, so
    inviting free-form relation names produces a pile of amendments instead of
    a graph.
    """
    links = [l for l in bundle.get("links", []) if l.get("id")]
    if not links:
        return ""
    described = "; ".join(
        f'{l["id"]} ({l.get("from", "?")} -> {l.get("to", "?")})' for l in links)
    return (
        '\n"relationships": [\n'
        '  {"from_instance_id": "<an instance_id you returned above>", '
        '"to_instance_id": "<another instance_id you returned above>", '
        '"link_type_id": "<one of the types below>", '
        '"evidence": "<verbatim text from the document>"}\n'
        "]\n"
        f"Link types: {described}.\n"
        "Use only these link types, and only ids you returned in `extractions`. "
        "Omit `relationships` entirely if the document states no relation.")


# ---------------------------------------------------------------------------
# anthropic — the official SDK, because some keys need a header
# ---------------------------------------------------------------------------

def anthropic_extract(*, store: Store, document: dict, bundle: dict, text: str,
                      tier: str, opt_in: bool, actor_id: str | None) -> dict:
    """Extract with Claude, through the Anthropic SDK.

    `chat` already reaches a lot of providers, so this exists for a specific
    reason: an **identity-linked API key** requires an `anthropic-workspace-id`
    header on every request, and neither the OpenAI-shaped `chat` engine nor
    `llm-anthropic` can carry one. The SDK takes `default_headers`, so this can.

    Everything else is the same as every other engine here. The cloud gate runs
    before any text is prepared, the call is recorded whether it succeeds or
    fails, and the spans it returns are located in the document afterwards --
    a model's claim to have quoted something is not evidence that it did.
    """
    from .population import prompt_for

    try:
        import anthropic as anthropic_sdk
    except ImportError as exc:
        raise OrpheusError(
            "The Anthropic SDK is not installed. `pip install "
            "'orpheus[anthropic]'`, or choose another extraction_engine."
        ) from exc

    if tier == "cloud":
        llm.assert_cloud_allowed(store, opt_in=opt_in, actor_id=actor_id)
    config = llm.model_config(store, tier)
    if not config.get("api_key"):
        raise OrpheusError(
            "No API key for the cloud tier. Set ORPHEUS_CLOUD_API_KEY or "
            "ANTHROPIC_API_KEY.")

    headers = {}
    workspace = _anthropic_workspace(store)
    if workspace:
        # Required by identity-linked keys, and harmless on keys that do not
        # need it -- so it is sent whenever a deployment has configured one
        # rather than guessed at from the error.
        headers["anthropic-workspace-id"] = workspace

    model_id = _anthropic_model_id(store, tier, config)
    instructions = (prompt_for(bundle) + "\n\n" + _JSON_INSTRUCTIONS
                    + _link_instructions(bundle))

    error, content, usage = None, "", None
    # BaseException, not Exception: the audit is the record of what left this
    # deployment, and a call recorded as clean when it was not is a wrong
    # answer to the only question that log exists to answer.
    try:
        client = anthropic_sdk.Anthropic(api_key=config["api_key"],
                                         default_headers=headers or None)
        # Streamed because a contract is long input and the reply is a list:
        # a non-streaming request at this size risks the HTTP timeout rather
        # than the model.
        with client.messages.stream(
                model=model_id,
                max_tokens=int(store.setting("anthropic_max_tokens", 16000)
                               if store else 16000),
                system=instructions,
                messages=[{"role": "user", "content": text}]) as stream:
            response = stream.get_final_message()
        usage = response.usage
        content = "".join(block.text for block in response.content
                          if block.type == "text")
        if response.stop_reason == "refusal":
            error = "refusal: the model declined this document"
    except BaseException as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate",
            # Characters, like every other engine, because the column is
            # `prompt_chars` and `budget_status` sums it against a limit a
            # person set in characters. Putting the provider's token count
            # here read as a better number and was a worse one: it spent a
            # character budget in tokens, so the cap sat roughly four times
            # higher than it was set, and only for this engine.
            prompt_chars=len(text),
            provider="anthropic", model=model_id,
            document_id=document["document_id"], actor_id=actor_id,
            excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")
    return _parse_chat_payload(content)


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


def _anthropic_model_id(store: Store | None, tier: str, config: dict) -> str:
    """Which Anthropic model serves this call.

    The tier default names a Gemini model, because the cloud tier's default
    provider is Gemini. Handing that id to Anthropic is never right -- it is
    a 404 naming a model that exists, which reads like an outage rather than
    a misconfiguration. So an explicit setting wins, then the environment,
    then the tier's model but only when it actually names an Anthropic model,
    and finally this engine's own default.
    """
    if store is not None:
        configured = store.setting(f"{tier}_anthropic_model", None) \
            or store.setting("anthropic_model", None)
        if configured:
            return configured
    from_env = os.environ.get("ORPHEUS_ANTHROPIC_MODEL")
    if from_env:
        return from_env
    tier_model = config.get("model_id") or ""
    if tier_model.startswith("claude"):
        return tier_model
    return DEFAULT_ANTHROPIC_MODEL


def _anthropic_workspace(store: Store | None) -> str | None:
    """The workspace an identity-linked key acts in.

    Configuration, not discovery: the endpoint that lists workspaces needs an
    admin key, so a deployment that needs this has to be told. Read from
    settings first so it survives without an environment variable.
    """
    if store is not None:
        configured = store.setting("anthropic_workspace_id", None)
        if configured:
            return configured
    return os.environ.get("ANTHROPIC_WORKSPACE_ID")


register_engine("gliner2", gliner2_extract)
register_engine("llm", llm_extract)
register_engine("langextract", langextract_extract)
register_engine("chat", chat_extract)
register_engine("anthropic", anthropic_extract)
