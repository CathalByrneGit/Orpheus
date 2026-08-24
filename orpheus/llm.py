"""Which model may be called, by whom, and the record that it was.

Orpheus does not implement prompting or model transport — LangExtract does that
better, and `docs/prior-art.md` says so. What Orpheus keeps is the part no
library can keep for it: **two independent conditions before any document text
leaves the building**, and a row in `llm_calls` saying that it did.

That division is the whole argument. A library that resolves its own API keys
and calls its own provider routes around the org policy, the per-request opt-in
and the audit log in one step — the same failure `docs/datasette-ecosystem.md`
identifies in `datasette-llm`. So the library is called *through* this module:
Orpheus decides whether a call may happen and which model serves it, the library
does the calling, and the attempt is recorded either way.
"""

from __future__ import annotations

import hashlib
import os

from .rubric import CLOUD_POLICIES
from .store import Store
from .utils import OrpheusError, new_id, now


def cloud_policy(store: Store) -> dict:
    """What a deployment is allowed to send, and what it actually sends.

    `send_mode` used to be read from a `cloud_send_mode` setting defaulting to
    `"excerpt"`, and nothing implemented it: `populate()` sends
    `document_text()` -- the whole document -- to whichever engine, and all
    three engines record `excerpt_only=False` on the audit row. So the audit was
    telling the truth while this was not, and a deployment reading
    `/capabilities` would have been told its contracts left in fragments when
    they left whole.

    A false claim about what leaves the building is worse than no claim, so this
    reports the code's behaviour. Excerpt selection existed in the R
    implementation and was not ported; until it is, `full_document` is the
    honest answer. See open-decisions.
    """
    policy = store.setting("cloud_ai_policy", "disabled")
    return {
        "policy": policy,
        "available": policy != "disabled",
        "send_mode": "full_document",
        # What a run may still send. `/capabilities` is where a deployment
        # checks what it is allowed to do, and a cap it cannot see is a cap it
        # discovers by hitting it mid-corpus.
        "budget": budget_status(store),
        "send_mode_note": ("The whole document text is sent. Excerpt selection "
                           "is not implemented; classification is the only pass "
                           "that truncates, and it says so per call."),
        "requires_explicit_opt_in": True,
    }


def assert_cloud_allowed(store: Store, opt_in: bool,
                         actor_id: str | None = None) -> None:
    """Both conditions, checked separately, in this order.

    They are deliberately independent. An organisation enabling cloud
    processing is not a person consenting to send *this* document, and a person
    ticking a box cannot override an organisation that has not enabled it.
    Collapsing the two into one setting is the obvious simplification and it
    silently removes one of the two protections.
    """
    policy = store.setting("cloud_ai_policy", "disabled")
    if policy not in CLOUD_POLICIES:
        raise OrpheusError(
            f"Stored cloud_ai_policy {policy!r} is not a recognised policy."
        )
    if policy == "disabled":
        raise OrpheusError(
            "Cloud processing is disabled for this deployment. An administrator "
            "sets cloud_ai_policy to 'per_user' or 'org_allow'."
        )
    if not opt_in:
        raise OrpheusError(
            "Cloud processing needs an explicit per-request opt-in. It is never "
            "inferred from the policy."
        )

    # The third condition. Checked here rather than at the call site so it
    # cannot be forgotten by a new engine, and before any text is prepared so a
    # refused call sends nothing.
    budget = budget_status(store)
    if budget["exceeded"]:
        raise OrpheusError(
            f"The cloud budget for this deployment is spent: {budget['note']} "
            "An administrator raises `cloud_budget_chars` or clears the window."
        )


# ---------------------------------------------------------------------------
# What may leave the building
# ---------------------------------------------------------------------------
#
# Denominated in characters sent, not in currency, and that is deliberate.
#
# Orpheus talks to OpenRouter and the rest over plain HTTP and knows nothing
# about their price lists. A cap in euro would need a hardcoded table of
# per-model rates that goes stale the week a provider changes one, and a budget
# that silently stops matching the invoice is worse than no budget -- it is a
# control somebody is relying on.
#
# Characters are exact, always available, and measure the thing a public body
# actually has to answer for: how much of its material left the building. The
# spend question is downstream of that one. A deployment that knows its own
# rate can set `cloud_price_per_million_chars` and get an estimate, labelled as
# an estimate, on top of a number that is not one.

WINDOWS = ("total", "day", "month")


def budget_status(store: Store) -> dict:
    """How much has been sent to the cloud tier, against the cap.

    Counts every `llm_calls` row in the window, failures included: a call that
    errored sent its payload just the same, which is the same reason the audit
    records it.
    """
    raw = store.setting("cloud_budget_chars")
    window = store.setting("cloud_budget_window", "month")
    if window not in WINDOWS:
        window = "month"

    clause = {
        "total": "",
        "day": " AND created_at >= date('now', 'start of day')",
        "month": " AND created_at >= date('now', 'start of month')",
    }[window]
    used = store.scalar(
        "SELECT COALESCE(SUM(prompt_chars), 0) FROM llm_calls "
        f"WHERE tier = 'cloud'{clause}") or 0

    limit = None
    if raw not in (None, ""):
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = None

    estimate = None
    price = store.setting("cloud_price_per_million_chars")
    if price not in (None, ""):
        try:
            # Four decimal places, not two: a rate per *million* characters
            # over a small corpus rounds to 0.00 at two, and a budget report
            # that says a run cost nothing is worse than one that says nothing.
            estimate = round(float(price) * used / 1_000_000, 4)
        except (TypeError, ValueError):
            estimate = None

    if limit is None:
        note = (f"{used:,} character(s) sent to the cloud tier this {window}. "
                f"No cap is set, so nothing will stop a run.")
    elif used >= limit:
        note = (f"{used:,} of {limit:,} character(s) allowed this {window}.")
    else:
        note = (f"{used:,} of {limit:,} character(s) allowed this {window}; "
                f"{limit - used:,} left.")

    return {
        "window": window, "chars_used": used, "chars_limit": limit,
        "chars_remaining": None if limit is None else max(0, limit - used),
        "exceeded": limit is not None and used >= limit,
        # Present only where a deployment has told us its own rate. Never
        # guessed from the model name.
        "estimated_cost": estimate,
        "estimated_cost_note": (
            "An estimate from the rate this deployment configured, not a price "
            "read from the provider." if estimate is not None else
            "No rate configured, so no cost is estimated. Set "
            "`cloud_price_per_million_chars` to get one."),
        "note": note,
    }


# ---------------------------------------------------------------------------
# Which model serves a tier
# ---------------------------------------------------------------------------

DEFAULTS = {
    "local": {
        "model_id": "gemma2:2b",
        "model_url": "http://localhost:11434",
        "provider": "ollama",
    },
    "cloud": {
        "model_id": "gemini-2.5-flash",
        "model_url": None,
        "provider": "gemini",
    },
}


def model_config(store: Store | None, tier: str) -> dict:
    """What the extraction library should be told to use for this tier.

    The local tier is always on and needs no key; the cloud tier is the one
    behind the gate. Configuration is read from settings first so a deployment
    can change models without a release, then from the environment.
    """
    if tier not in ("local", "cloud"):
        raise OrpheusError(f"Unknown tier {tier!r}.")
    config = dict(DEFAULTS[tier])

    def setting(key, fallback):
        if store is None:
            return fallback
        return store.setting(key, fallback)

    config["model_id"] = os.environ.get(
        f"ORPHEUS_{tier.upper()}_MODEL",
        setting(f"{tier}_model", config["model_id"]))
    if tier == "local":
        config["model_url"] = os.environ.get(
            "ORPHEUS_OLLAMA_HOST",
            setting("local_model_url", config["model_url"]))
        config["api_key"] = None
    else:
        # Read only when the gate has already allowed the call.
        config["api_key"] = (os.environ.get("ORPHEUS_CLOUD_API_KEY")
                             or os.environ.get("OPENROUTER_API_KEY")
                             or os.environ.get("LANGEXTRACT_API_KEY")
                             or os.environ.get("GEMINI_API_KEY")
                             or os.environ.get("ANTHROPIC_API_KEY")
                             or os.environ.get("OPENAI_API_KEY"))
        # Which provider is serving the tier is recorded on every `llm_calls`
        # row, so it has to follow the key that was actually used rather than
        # stay at the default. An audit saying `gemini` for a call that went to
        # OpenRouter is a wrong answer to "where did this document's text go".
        config["provider"] = setting("cloud_provider", _provider_for(config))
    return config


def _provider_for(config: dict) -> str:
    """Name the provider from the key in hand, defaulting to the tier's."""
    if os.environ.get("ORPHEUS_CLOUD_PROVIDER"):
        return os.environ["ORPHEUS_CLOUD_PROVIDER"]
    for variable, provider in (("OPENROUTER_API_KEY", "openrouter"),
                               ("ANTHROPIC_API_KEY", "anthropic"),
                               ("OPENAI_API_KEY", "openai"),
                               ("GEMINI_API_KEY", "gemini")):
        if os.environ.get(variable):
            return provider
    return config["provider"]


def status(store: Store | None = None) -> dict:
    """What the running server could call, without calling anything."""
    local = model_config(store, "local")
    cloud = model_config(store, "cloud")
    return {
        "local_provider": local["provider"],
        "local_model": local["model_id"],
        "local_url": local["model_url"],
        "cloud_provider": cloud["provider"],
        "cloud_model": cloud["model_id"],
        "cloud_key_present": bool(cloud["api_key"]),
        "extraction_backend": "langextract" if _have_langextract() else None,
    }


def _have_langextract() -> bool:
    import importlib.util
    return importlib.util.find_spec("langextract") is not None


# ---------------------------------------------------------------------------
# The audit row
# ---------------------------------------------------------------------------

def record_llm_call(store: Store, tier: str, purpose: str,
                    prompt_chars: int, provider: str | None = None,
                    model: str | None = None, document_id: str | None = None,
                    actor_id: str | None = None, excerpt_only: bool = False,
                    payload: str | None = None, error: str | None = None) -> str:
    """Record that a call was attempted.

    Written whether or not the call succeeded, because the question the log
    answers is "what left this deployment", and a failed call sent the payload
    just the same. The payload is digested rather than stored: enough to prove
    two calls sent the same text, not enough to reconstruct the document from
    the audit log.
    """
    store.assert_writable()
    call_id = new_id("llm")
    store.insert("llm_calls", {
        "call_id": call_id,
        "document_id": document_id,
        "actor_id": actor_id,
        "tier": tier,
        "provider": provider,
        "model": model,
        "purpose": purpose,
        "prompt_chars": prompt_chars,
        "excerpt_only": 1 if excerpt_only else 0,
        "payload_digest": (hashlib.sha256(payload.encode()).hexdigest()
                           if payload else None),
        "created_at": now(),
        "error": error,
    })
    return call_id


def cloud_calls(store: Store, document_id: str | None = None) -> list[dict]:
    sql = ("SELECT seq, call_id, created_at, purpose, document_id, actor_id, "
           "model, prompt_chars, excerpt_only, error "
           "FROM llm_calls WHERE tier = 'cloud'")
    params: tuple = ()
    if document_id:
        sql += " AND document_id = ?"
        params = (document_id,)
    return store.query(sql + " ORDER BY seq DESC", params)
