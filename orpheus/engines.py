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
    try:
        annotated = lx.extract(**kwargs)
    except Exception as exc:
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
    return {
        "type": "object",
        "properties": {
            "extractions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
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
        # checked. _parse_chat_json tolerates the fenced reply that follows.
        kwargs["system"] = instructions + "\n\n" + _JSON_INSTRUCTIONS
    if config.get("api_key") and isinstance(model, llm_lib.KeyModel):
        kwargs["key"] = config["api_key"]

    error, content, usage = None, "", None
    try:
        response = model.prompt(text, **kwargs)
        content = response.text()
        usage = response.usage()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate",
            # Real token counts when the provider reported them, rather than a
            # character count standing in for one.
            prompt_chars=getattr(usage, "input", None) or len(text),
            provider="llm:" + str(getattr(model, "needs_key", None) or "local"),
            model=model_id, document_id=document["document_id"],
            actor_id=actor_id, excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")

    return {"extractions": _parse_chat_json(content)}


def _llm_model_id(store: Store | None, tier: str, config: dict) -> str:
    if store is not None:
        configured = store.setting(f"{tier}_llm_model", None)
        if configured:
            return configured
    return os.environ.get(f"ORPHEUS_{tier.upper()}_LLM_MODEL") or config["model_id"]


_JSON_INSTRUCTIONS = (
    "Return JSON only, of the form "
    '{"extractions": [{"type": "<one of the entity types above>", '
    '"excerpt": "<verbatim text from the document>", "properties": {...}}]}. '
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
        schema_lines.append(f'  {{"type": "{type_id}", "excerpt": "<verbatim text '
                            f'from the document>", "properties": {{{fields}}}}}')

    instructions = (
        prompt_for(bundle)
        + "\n\nReturn JSON only, of the form:\n"
          '{"extractions": [\n' + ",\n".join(schema_lines[:3]) + "\n]}\n"
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
    try:
        content = _post_chat(base_url, config.get("api_key"), payload)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        llm.record_llm_call(
            store, tier=tier, purpose="populate", prompt_chars=len(text),
            provider=config["provider"], model=config["model_id"],
            document_id=document["document_id"], actor_id=actor_id,
            excerpt_only=False, payload=text, error=error)
    if error:
        raise OrpheusError(f"Extraction failed: {error}")

    return {"extractions": _parse_chat_json(content)}


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
    """Get the extractions out, tolerating a model that ignored 'no code fence'."""
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return []
    items = parsed.get("extractions") if isinstance(parsed, dict) else parsed
    return [i for i in (items or []) if isinstance(i, dict)]


register_engine("gliner2", gliner2_extract)
register_engine("llm", llm_extract)
register_engine("langextract", langextract_extract)
register_engine("chat", chat_extract)
