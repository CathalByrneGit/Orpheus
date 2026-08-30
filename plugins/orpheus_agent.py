"""Orpheus tools for `datasette-agent`, so the chat speaks the store's language.

The chat surface is a good fit for the work: somebody reading a contract wants
to ask about a name in front of them without leaving the page. What does not fit
is answering those questions with `sql_query`.

A `SELECT address FROM instances_Company WHERE name LIKE 'NETGEAR%'` returns
`4401 Great America Parkway` and nothing else. The columns that say a machine
produced it, how sure the machine was, and that nobody has checked it are all
sitting in the same table, and prose written from that row drops them. When a
person runs the same query in Datasette those columns are on screen, and the
metadata explains the rubric; that is why raw SQL is fine for a reader and not
for a writer of sentences.

So every tool here returns the review state and the provenance **in the payload
itself**, where the model cannot answer without having seen them, and the tool
descriptions say plainly that they exist to be used instead of `sql_query`.
That is a nudge and not a guarantee: `datasette-agent` builds its system prompt
in one hardcoded function with no hook to extend it, so nothing here can stop a
model reaching for raw SQL. Closing that needs an upstream change.

Writes are narrower still. `record` is the only one, it goes through
`context.ask_user()` first, and the wording put in front of the person is the
draft itself -- type, values and the exact span -- because `human` is the one
source nothing downstream questions.
"""

from __future__ import annotations

import html
import json

from datasette import hookimpl

try:  # optional: `pip install 'orpheus[agent]'`
    from datasette_agent.tools import AgentTool
except ImportError:  # pragma: no cover - exercised by not installing the extra
    AgentTool = None

from orpheus import companion as companion_mod
from orpheus import corroboration as corroboration_mod
from orpheus import entities as entities_mod
from orpheus import graph as graph_mod
from orpheus.rubric import RESOLUTION_STATUSES
from orpheus import lint as lint_mod
from orpheus import ontology as ontology_mod
from orpheus import record as record_mod
from orpheus import registers as registers_mod
from orpheus import search as search_mod
from orpheus.store import Store
from orpheus.utils import OrpheusError

PLUGIN = "orpheus-datasette"

# What `engine` a chat's offers are filed under, so suggestion_quality can
# answer per source rather than in aggregate.
ENGINE = "chat"

# Said on every tool, because the model reads descriptions and not this module.
_PREFER = ("Use this instead of sql_query: raw SQL over these tables returns "
           "values without the review state that qualifies them, and an answer "
           "written from that reads as settled fact. ")


def _database(datasette):
    """The Orpheus database, or None if this Datasette is not serving one.

    `get_database` raises `KeyError` for a name it does not have, and every
    caller here is written to treat None as "not configured" -- so a
    misconfigured name produced a traceback where a message was intended.
    """
    config = datasette.plugin_config(PLUGIN) or {}
    name = config.get("database")
    if not name:
        return None
    try:
        return datasette.get_database(name)
    except KeyError:
        return None


async def _read(datasette, fn):
    """Run a core function against the store on Datasette's read connection."""
    database = _database(datasette)
    if database is None:
        return {"error": "No Orpheus database is configured for this Datasette."}

    def run(conn):
        store = Store.adopt(conn, path=database.path)
        store.assert_current()
        return fn(store)

    return await database.execute_fn(run)


async def _write(datasette, fn):
    database = _database(datasette)
    if database is None:
        return {"error": "No Orpheus database is configured for this Datasette."}

    def run(conn):
        store = Store.adopt(conn, path=database.path, owns_transaction=False)
        store.assert_current()
        return fn(store)

    return await database.execute_write_fn(run)


def _sibling_plugin():
    """The Orpheus Datasette plugin, however it was loaded.

    Not `import orpheus_datasette`. `--plugins-dir` builds each file with
    `types.ModuleType` and never puts it in `sys.modules`, so that import
    raises under the only configuration this runs in -- it succeeded in tests
    only because they put `plugins/` on the path. Pluggy has the real module
    object, so ask it, and fall back to the import for the test case.
    """
    try:
        from datasette.plugins import pm

        for module in pm.get_plugins():
            if hasattr(module, "_datasette_identity") and \
                    hasattr(module, "_resolve_actor"):
                return module
    except Exception:  # pragma: no cover - pluggy always present in practice
        pass
    try:
        import orpheus_datasette

        return orpheus_datasette
    except ImportError:
        return None


class _AsRequest:
    """Just enough of a request for `_datasette_identity`, which reads only
    `.actor`. A tool is given the actor and never the request."""

    def __init__(self, actor):
        self.actor = actor


async def _actor_id(datasette, actor):
    """The Orpheus actor behind a Datasette identity.

    Not `actor["id"]`. Datasette answers "who is this" in its own terms -- for
    `--root` that is the literal string `root`, which is nobody here. Writing
    it into `decided_by` attributed a decision to an actor that does not exist,
    and it went in quietly because Datasette does not switch foreign keys on,
    so the reference the schema declares was never checked.

    Built by the plugin's own `_datasette_identity` rather than by hand. That
    dict has a shape -- pinned, is_admin, idp -- and a second copy of it here
    would drift from the first, which is how this went wrong to begin with.
    """
    if not (actor or {}).get("id"):
        return None
    database = _database(datasette)
    if database is None:
        return None
    plugin = _sibling_plugin()
    if plugin is None:
        return None
    identity = plugin._datasette_identity(datasette, _AsRequest(actor))
    if not identity:
        return None
    resolved = await plugin._resolve_actor(database, identity)
    return (resolved or {}).get("actor_id")


# ---------------------------------------------------------------------------
# Looking something up
# ---------------------------------------------------------------------------

async def find_entity(datasette, actor, query: str):
    """Is this name in the store, and in what state."""
    def run(store):
        pages = [
            {"entity_id": row["entity_id"], "name": row["canonical_name"],
             "type_id": row["type_id"], "page_status": row["status"]}
            for row in store.query(
                "SELECT entity_id, canonical_name, type_id, status FROM entities "
                "WHERE merged_into IS NULL AND canonical_name LIKE ? "
                "ORDER BY canonical_name LIMIT 25", (f"%{query}%",))]
        # Separates the two ways a name can be "not there": extracted but not
        # yet on a page, versus never picked up at all. Only the second is a
        # case for `orpheus_record`; the first is wiki work.
        #
        # The full-text index is optional and a deployment may not have built
        # one. Saying so beats letting the question go unanswered silently:
        # without it, "the extractor missed this" cannot be distinguished from
        # "nothing in the corpus says it", and those want opposite replies.
        try:
            missed = search_mod.unextracted_mentions(store, query, limit=10)
        except OrpheusError as unavailable:
            missed = {"unlinked": None, "linked_documents": None,
                      "caveat": (f"{unavailable} Until then this cannot tell a "
                                 "name the extractor missed from one no "
                                 "document mentions — do not report either.")}
        return {
            "query": query,
            "pages": pages,
            "documents_naming_it_with_no_extraction": missed["unlinked"],
            "documents_already_carrying_it": missed["linked_documents"],
            "caveat": missed["caveat"],
            "reading": (
                "Three different answers, and they need different words. A page "
                "exists → say its state. Extracted but no page → it is in the "
                "corpus and not in the wiki, which is not 'absent'. Named in a "
                "document with no extraction → the extractor missed it, and "
                "orpheus_record is the way in. `page_status` is whether anybody "
                "confirmed the page is one real thing, not whether the facts on "
                "it are checked."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def entity_page(datasette, actor, entity_id: str):
    """Everything the store holds about one page, with its sources."""
    def run(store):
        page = entities_mod.entity_page(store, entity_id)
        return {
            "page": page,
            "reading": (
                "Every property here comes from a document. `mentions` carries "
                "each one's document, page, excerpt and review status. Report "
                "the status: an unconfirmed extraction is what one machine said "
                "once, and saying it plainly is the failure this store exists to "
                "prevent."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def connections(datasette, actor, entity_id: str, depth: int = 2):
    """What surrounds a page in the relation network, and how checked it is."""
    def run(store):
        graph = graph_mod.build(store)
        return {
            "coverage": graph_mod.coverage(store),
            "neighbourhood": graph_mod.neighbourhood(
                store, entity_id, depth=depth, graph=graph),
            "reading": (
                "Read `coverage` first: it says how much of the corpus reached "
                "the graph at all. A connection is a question, never a finding — "
                "report the chain, the documents behind each hop, and how much "
                "of it anybody has confirmed. Two parties sharing a "
                "counterparty is not an allegation about either."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def compare_pages(datasette, actor, entity_id: str, other_entity_id: str):
    """Everything bearing on whether two pages are one thing."""
    def run(store):
        evidence = entities_mod.resolution_evidence(store, entity_id,
                                                    other_entity_id)
        verdict = entities_mod.resolution_verdict(store, entity_id,
                                                  other_entity_id)
        return {
            "evidence": evidence,
            # Somebody may already have looked. Saying so stops a second
            # opinion being offered as if it were the first, and `stale` says
            # whether what they decided still rests on what is known.
            "already_reviewed": verdict,
            "reading": (
                "Assembled, never judged, and you must not judge it either — "
                "recommend, with the evidence you are recommending on. "
                "`n_pages_sharing` is the whole of what a shared value is "
                "worth: a value three pages of this type carry is evidence, "
                "one that 64 of 74 carry is not, and both look identical "
                "without that number. Appearing in the same document is not "
                "evidence of being the same thing -- measured on this corpus, "
                "two different companies share a document and a neighbouring "
                "page, because naming two different parties is what a contract "
                "does. Read `passages` and say what the documents actually "
                "show. State nothing that is not in this payload: asked to "
                "compare two companies it had only been told were "
                "`private_company`, a model called them 'both Delaware "
                "corporations', which was plausible, unsupported, and would "
                "have been read as a fact from the file. Nothing here merges "
                "anything: a person does that."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def review_register(datasette, actor, register_id: str = "",
                          limit: int = 25):
    """A staged register, so a person and a model can look it over together."""
    def run(store):
        if not register_id:
            return {"registers": registers_mod.list_registers(store),
                    "reading": ("A staged register is readable and is not "
                                "evidence. Ask for one by id to see its rows.")}
        return {
            "register": registers_mod.get_register(store, register_id),
            "rows": registers_mod.rows(store, register_id, limit=limit),
            "reading": (
                "Look for rows that would match the wrong thing: a blank or "
                "boilerplate name, an identifier in the name column, a row "
                "that is a header read as data. Say which row numbers look "
                "wrong and why, and let the person reject them -- "
                "`orpheus_reject_register_row` records a rejection somebody "
                "has decided on, and promoting the register is theirs alone. "
                "A register matched on the wrong column produces confident "
                "nonsense, so check the name and identifier columns first."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def register_columns(datasette, actor):
    """Which register keys are queryable, and which are only readable."""
    def run(store):
        exposed = registers_mod.exposed_columns(store)
        return {
            "exposed": exposed,
            "n_rows": store.scalar("SELECT COUNT(*) FROM register_rows") or 0,
            "reading": (
                "`name`, `naive_key` and `identifier` are lifted out of every "
                "register when it loads, because matching needs them. "
                "Everything else sits in `values_json`, readable and "
                "unqueryable, until somebody exposes it.\n"
                "So a filter on an un-exposed key is not slow, it is "
                "impossible -- say that rather than offering to scan. "
                "Exposing one is a person's decision (it alters the table for "
                "every register in the store) and you have no tool for it: "
                "`orpheus register --expose <key>`, or the control on "
                "/-/orpheus/registers."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def reject_register_row(datasette, actor, register_id: str, row_no: int,
                              note: str):
    """Mark one register row as not to be used, with a reason."""
    actor_id = await _actor_id(datasette, actor)

    def run(store):
        return registers_mod.review_row(store, register_id, int(row_no),
                                        "rejected", note=note,
                                        actor_id=actor_id)

    try:
        result = await _write(datasette, run)
    except OrpheusError as refused:
        return json.dumps({"rejected": False, "refused": str(refused)})
    if isinstance(result, dict) and result.get("error"):
        return json.dumps(result)
    return json.dumps({
        "rejected": dict(result),
        "reading": ("The row stays readable and stops counting, because a bad "
                    "row is evidence about the register. Promoting the "
                    "register is a person's decision and is not yours to "
                    "make."),
    }, default=str)


async def record_comparison(datasette, actor, entity_id: str,
                            other_entity_id: str, status: str, rationale: str):
    """Write down what was decided about two pages, and why."""
    actor_id = await _actor_id(datasette, actor)

    def run(store):
        return entities_mod.review_resolution(
            store, entity_id, other_entity_id, status, rationale,
            actor_id=actor_id)

    try:
        result = await _write(datasette, run)
    except OrpheusError as refused:
        # A vocabulary the store does not have, a missing reason, or a page
        # paired with itself. Said back rather than raised, so the model can
        # correct it instead of the conversation ending.
        return json.dumps({"recorded": False, "refused": str(refused)})
    if isinstance(result, dict) and result.get("error"):
        return json.dumps(result)
    return json.dumps({
        "recorded": dict(result),
        "reading": (
            "Recorded against a digest of the evidence it rested on, so it "
            "comes back if that changes and not before. `same` does not merge "
            "the pages -- it records that somebody decided they are one thing, "
            "and a person still performs the merge."),
    }, default=str)


async def corroboration_for(datasette, actor, entity_id: str):
    """How many independent sources say the same thing about this page."""
    def run(store):
        return {
            "corroboration": corroboration_mod.for_entity(store, entity_id),
            "reading": (
                "Counted in distinct wordings across distinct documents. The "
                "same sentence in three documents is one source copied twice, "
                "not three agreeing, and is reported as copied. Corroboration "
                "never raises a confidence value."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def needs_review(datasette, actor, limit: int = 20):
    """What in the store is currently misleading a reader."""
    def run(store):
        report = lint_mod.lint(store)
        report["findings"] = report.get("findings", [])[:limit]
        return report
    return json.dumps(await _read(datasette, run), default=str)


async def passage(datasette, actor, document_id: str, page_no: int):
    """The page somebody is reading, and what already stands on it."""
    def run(store):
        found = companion_mod.passage(store, document_id, int(page_no),
                                      status="all")
        return {
            "passage": found,
            "reading": (
                "This is the text of the page, so a quote taken from it is "
                "verbatim — which is what orpheus_record needs. Anything under "
                "`suggestions` is offered and not in the store: a suggestion is "
                "not an extraction until a person accepts it, so do not report "
                "one as something the document holds."),
        }
    return json.dumps(await _read(datasette, run), default=str)


# ---------------------------------------------------------------------------
# The one write
# ---------------------------------------------------------------------------

def _offer_card(suggestion: dict) -> str:
    """One offer, as the person sees it in the conversation."""
    rows = "".join(
        f"<tr><td style='color:#666;padding-right:.6em'>{html.escape(str(k))}</td>"
        f"<td>{html.escape(str(v))}</td></tr>"
        for k, v in (suggestion.get("properties") or {}).items()
        if k != "page_no")
    excerpt = html.escape(suggestion.get("excerpt") or "")
    sid = html.escape(suggestion.get("suggestion_id") or "")
    return (
        f"<div style='border-left:3px solid #1d4ed8;background:#fafafa;"
        f"padding:.7em 1em;margin:.6em 0'>"
        f"<p style='margin:0'><strong>{html.escape(suggestion.get('type_id') or '')}"
        f"</strong> <span style='color:#666'>&middot; "
        f"{html.escape(str(suggestion.get('confidence')))} &middot; "
        f"{html.escape(suggestion.get('engine') or '')}</span></p>"
        f"<table style='margin:.3em 0'>{rows}</table>"
        f"<blockquote style='color:#444;border-left:3px solid #ccc;"
        f"padding-left:1em;margin:.4em 0'>{excerpt}</blockquote>"
        f"<button type='button' data-orpheus-settle='record {sid}'>Record this</button> "
        f"<button type='button' data-orpheus-settle='dismiss {sid}'>Not worth it</button>"
        f"</div>")


# One listener for every card, however many arrive: a button fills the chat box
# and sends it, so settling goes back through a tool and the decision is still
# taken by ask_user rather than by a click the model could have caused.
_CARD_SCRIPT = (
    "<script>if(!window.__orpheusSettle){window.__orpheusSettle=1;"
    "document.addEventListener('click',function(e){"
    "var b=e.target.closest('[data-orpheus-settle]');if(!b)return;"
    "var i=document.getElementById('message-input');"
    "var f=document.getElementById('chat-form');if(!i||!f)return;"
    "i.value=b.getAttribute('data-orpheus-settle');f.requestSubmit();});}</script>")


async def read_page(datasette, actor, document_id: str, page_no: int):
    """Run the pattern pass over a page and show what it offers."""
    actor_id = await _actor_id(datasette, actor)

    def run(store):
        # The pattern pass only. It cannot offer something the page does not
        # contain, so it needs no opt-in and sends nothing anywhere -- and the
        # model pass is this conversation, which is already reading the page.
        return companion_mod.read_passage(
            store, document_id, int(page_no), actor_id=actor_id,
            engine=companion_mod.DEFAULT_ENGINE)

    result = await _write(datasette, run)
    if isinstance(result, dict) and result.get("error"):
        return json.dumps(result)

    offers = result.get("suggestions") or []
    cards = "".join(_offer_card(s) for s in offers)
    return json.dumps({
        "_html": (_CARD_SCRIPT + cards) if cards else "",
        "n_offered": len(offers),
        "offers": [{"suggestion_id": s["suggestion_id"], "type_id": s["type_id"],
                    "properties": s.get("properties"),
                    "confidence": s.get("confidence")} for s in offers],
        "reading": (
            "These are offered and not in the store. A suggestion is not an "
            "extraction until a person accepts it, so do not report one as "
            "something the document holds. The person has a Record / Not worth "
            "it button on each; settle one only when they ask you to."),
    }, default=str)


async def settle(datasette, actor, context, suggestion_id: str,
                 decision: str, note: str = None):
    """Accept or dismiss an outstanding offer, once the person says so."""
    actor_id = await _actor_id(datasette, actor)
    if not actor_id:
        return json.dumps({"error": "No signed-in actor, so no decision can be "
                                    "attributed."})
    if decision not in ("record", "dismiss"):
        return json.dumps({"error": "decision must be 'record' or 'dismiss'."})

    def load(store):
        return companion_mod.get_suggestion(store, suggestion_id)

    offer = await _read(datasette, load)
    if isinstance(offer, dict) and offer.get("error"):
        return json.dumps(offer)

    values = "".join(
        f"<li><code>{html.escape(str(k))}</code>: {html.escape(str(v))}</li>"
        for k, v in (offer.get("properties") or {}).items() if k != "page_no")
    verb = "Record" if decision == "record" else "Dismiss"
    approved = await context.ask_user(
        f"{verb} this {offer.get('type_id')}?",
        html=(f"<ul>{values}</ul><blockquote>"
              f"{html.escape(offer.get('excerpt') or '')}</blockquote>"
              + ("<p>Recording writes a <strong>confirmed</strong> row with "
                 "<code>source = human</code>.</p>" if decision == "record" else
                 "<p>Dismissing keeps the offer, marked dismissed. That is what "
                 "makes these measurable at all.</p>")))
    if not approved:
        return json.dumps({"settled": False,
                           "reading": "Left outstanding. Nothing changed."})

    def act(store):
        if decision == "record":
            return companion_mod.accept_suggestion(
                store, suggestion_id, actor_id=actor_id, note=note)
        return companion_mod.dismiss_suggestion(
            store, suggestion_id, actor_id=actor_id, note=note)

    return json.dumps({"settled": True, "decision": decision,
                       "suggestion": await _write(datasette, act)}, default=str)


async def record(datasette, actor, context, document_id: str, page_no: int,
                 type_id: str, properties: dict, quote: str, note: str = None):
    """Offer a fact extraction missed, through the same queue as a page read.

    Not a direct write. The offer becomes a suggestion first, so it is the same
    kind of thing the companion makes and is measured the same way -- and so a
    declined offer leaves evidence, which is the only measure there is of
    whether these are worth reading.
    """
    actor_id = await _actor_id(datasette, actor)
    if not actor_id:
        return json.dumps({"error": "No signed-in actor, so nothing can be "
                                    "offered: a decision has to belong to somebody."})

    # Offered before the question is asked, because ask_user re-runs everything
    # above it once answered -- and because an offer nobody accepts is still a
    # thing that happened.
    def make(store):
        return companion_mod.propose(
            store, document_id, int(page_no), type_id, properties or {},
            quote=quote, engine=ENGINE, actor_id=actor_id)

    try:
        suggestion = await _write(datasette, make)
    except OrpheusError as refused:
        return json.dumps({
            "offered": False, "refused": str(refused),
            "reading": ("Do not retry with a looser quote to get it in. If the "
                        "page does not say it, the entity page's notes field is "
                        "where it belongs.")})
    if isinstance(suggestion, dict) and suggestion.get("error"):
        return json.dumps(suggestion)

    values = "".join(
        f"<li><code>{html.escape(str(k))}</code>: {html.escape(str(v))}</li>"
        for k, v in (properties or {}).items())
    approved = await context.ask_user(
        f"Record this {type_id}?",
        html=(f"<p>Recording writes a <strong>confirmed</strong> row with "
              f"<code>source = human</code>: the one source nothing downstream "
              f"questions, so it should be what you read.</p><ul>{values}</ul>"
              f"<p><strong>Quoting page {html.escape(str(page_no))}:</strong>"
              f"<blockquote>{html.escape(suggestion.get('excerpt') or '')}"
              f"</blockquote>Located in the page, not taken on trust.</p>"
              f"<p style='color:#666'>Either way this is kept. Saying no is "
              f"what tells anybody whether these offers are worth reading.</p>"))

    def settle(store):
        if approved:
            return companion_mod.accept_suggestion(
                store, suggestion["suggestion_id"], actor_id=actor_id, note=note)
        return companion_mod.dismiss_suggestion(
            store, suggestion["suggestion_id"], actor_id=actor_id,
            note=note or "declined in chat")

    settled = await _write(datasette, settle)
    return json.dumps({
        "offered": True,
        "accepted": bool(approved),
        "suggestion": settled,
        "reading": ("Recorded, and confirmed because a person read it."
                    if approved else
                    "Not recorded. The offer is kept as dismissed, which is "
                    "what makes these measurable at all."),
    }, default=str)


# ---------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {}, "required": []}


async def review_ontology(datasette, actor, status: str = "proposed",
                          kind: str = ""):
    """What a survey proposed about a corpus nobody has modelled yet."""
    def run(store):
        rows = ontology_mod.candidates(store, status=status,
                                       kind=kind or None)
        return {
            "candidates": rows,
            "reading": (
                "`n_documents` of `n_sampled` is how many documents show this, "
                "counted rather than claimed -- it is not a confidence and the "
                "model was never asked for one. Read the evidence: every "
                "candidate carries quotations located in the documents they "
                "came from.\n"
                "The useful things to say are the ones the counts cannot: "
                "which two of these are the same thing under different names, "
                "which property is really an attribute of a type nobody "
                "proposed, and which type is a role rather than a thing. Say "
                "what you think and why, and let the person decide -- "
                "accepting an object type fixes the shape of every row that "
                "will ever be filed under it, and that decision is theirs."),
        }
    return json.dumps(await _read(datasette, run), default=str)


async def decide_ontology_candidate(datasette, actor, candidate_id: str,
                                    decision: str, accepted_as: str = "",
                                    note: str = ""):
    """Record a person's decision about one proposed type, property or link."""
    actor_id = await _actor_id(datasette, actor)

    def run(store):
        return ontology_mod.review_candidate(
            store, candidate_id, decision, actor_id,
            accepted_as=accepted_as or None, note=note or None)

    try:
        result = await _write(datasette, run)
    except OrpheusError as refused:
        return json.dumps({"decided": False, "refused": str(refused)})
    if isinstance(result, dict) and result.get("error"):
        return json.dumps(result)
    return json.dumps({
        "decided": {k: v for k, v in dict(result).items() if k != "evidence"},
        "reading": ("Recorded. Nothing is in the ontology yet: a bundle is "
                    "drafted from the accepted candidates as a separate step, "
                    "and registering it is a person's decision."),
    }, default=str)


def _tools():
    return [
        AgentTool(
            name="orpheus_find_entity",
            description=_PREFER + "Look up whether a person or organisation is "
                        "in the Orpheus store, returning any entity pages and "
                        "how many mentions exist. Start here for 'is X in the "
                        "database'.",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "A name or part of one"}},
                "required": ["query"]},
            fn=find_entity,
        ),
        AgentTool(
            name="orpheus_entity_page",
            description=_PREFER + "Everything the store holds about one entity "
                        "page: what the documents say, each mention with its "
                        "document, page, excerpt and review status, recorded "
                        "conflicts, and which properties are corroborated.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string",
                              "description": "An ent_... id"}},
                "required": ["entity_id"]},
            fn=entity_page,
        ),
        AgentTool(
            name="orpheus_connections",
            description=_PREFER + "What an entity is connected to in the "
                        "relation network, with the documents behind each hop "
                        "and how much of the chain anybody has confirmed. "
                        "Includes coverage, which says how much of the corpus "
                        "the graph represents.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "depth": {"type": "integer",
                          "description": "Hops to follow, default 2"}},
                "required": ["entity_id"]},
            fn=connections,
        ),
        AgentTool(
            name="orpheus_compare_pages",
            description=_PREFER + "Everything the store holds bearing on "
                        "whether two entity pages are the same thing: shared "
                        "identifiers, shared property values with how many "
                        "pages carry each one, the name analysis, and the "
                        "passages naming each. Use it before suggesting a "
                        "merge, and read the passages rather than the scores.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "other_entity_id": {"type": "string"}},
                "required": ["entity_id", "other_entity_id"]},
            fn=compare_pages,
        ),
        AgentTool(
            name="orpheus_record_comparison",
            description="Write down what was decided about two pages that "
                        "might be one thing, and why. `same`, `different` or "
                        "`unsure`, with a reason in every case. This does not "
                        "merge anything -- it records a judgement, so the pair "
                        "stops being offered until the evidence changes. Only "
                        "record what a person has decided, never your own "
                        "opinion.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string"},
                "other_entity_id": {"type": "string"},
                "status": {"type": "string",
                           "enum": list(RESOLUTION_STATUSES)},
                "rationale": {"type": "string",
                              "description": "Why. Required for every status, "
                                             "including unsure."}},
                "required": ["entity_id", "other_entity_id", "status",
                             "rationale"]},
            fn=record_comparison,
        ),
        AgentTool(
            name="orpheus_review_register",
            description=_PREFER + "A register of reference data staged for "
                        "review, with its rows. Use it to help somebody check "
                        "a register before they promote it: a staged register "
                        "is readable and does not count as evidence until a "
                        "person vouches for it. Omit register_id to list them.",
            input_schema={"type": "object", "properties": {
                "register_id": {"type": "string"},
                "limit": {"type": "integer"}},
                "required": []},
            fn=review_register,
        ),
        AgentTool(
            name="orpheus_register_columns",
            description=_PREFER + "Which columns of a register can actually be "
                        "filtered on. Everything except name and identifier is "
                        "inside a JSON blob no query reaches until somebody "
                        "exposes it — so check here before offering to filter "
                        "a register by anything else.",
            input_schema={"type": "object", "properties": {}, "required": []},
            fn=register_columns,
        ),
        AgentTool(
            name="orpheus_reject_register_row",
            description="Mark one row of a staged register as not to be used, "
                        "with a reason. Only for a row a person has decided "
                        "against -- promoting the register is theirs alone, "
                        "and this tool cannot do it.",
            input_schema={"type": "object", "properties": {
                "register_id": {"type": "string"},
                "row_no": {"type": "integer"},
                "note": {"type": "string",
                         "description": "Why this row should not be used."}},
                "required": ["register_id", "row_no", "note"]},
            fn=reject_register_row,
        ),
        AgentTool(
            name="orpheus_corroboration",
            description=_PREFER + "How many independent sources say the same "
                        "thing about an entity, counted in distinct wordings so "
                        "copied boilerplate is not mistaken for agreement.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string"}}, "required": ["entity_id"]},
            fn=corroboration_for,
        ),
        AgentTool(
            name="orpheus_review_ontology",
            description=_PREFER + "What an ontology survey proposed for a "
                        "corpus that has no bundle yet: object types, "
                        "properties and links, each with quotations and a "
                        "count of how many documents show it. Use it to help "
                        "somebody decide what their documents are about — "
                        "especially which proposals are the same thing twice, "
                        "and which property should have been a type.",
            input_schema={"type": "object", "properties": {
                "status": {"type": "string",
                           "enum": ["proposed", "accepted", "amended",
                                    "rejected"]},
                "kind": {"type": "string",
                         "enum": ["object_type", "property", "link_type"]}},
                "required": []},
            fn=review_ontology,
        ),
        AgentTool(
            name="orpheus_decide_ontology_candidate",
            description="Record what the person decided about one proposed "
                        "type, property or link. `accepted_as` renames it, "
                        "which is the ordinary move — a survey notices that "
                        "something recurs and cannot know what it is called. "
                        "Call it when they say to. Accepting a type shapes "
                        "every row that will ever be filed under it, so it is "
                        "never yours to decide on your own reading.",
            input_schema={"type": "object", "properties": {
                "candidate_id": {"type": "string"},
                "decision": {"type": "string",
                             "enum": ["accepted", "rejected"]},
                "accepted_as": {"type": "string",
                                "description": "Accept it under this name"},
                "note": {"type": "string"}},
                "required": ["candidate_id", "decision"]},
            fn=decide_ontology_candidate,
        ),
        AgentTool(
            name="orpheus_needs_review",
            description="Where the Orpheus store is currently misleading a "
                        "reader: uncited pages, quotations that are not in the "
                        "document they cite, conflicts nobody recorded, "
                        "groupings nobody checked.",
            input_schema={"type": "object", "properties": {
                "limit": {"type": "integer"}}, "required": []},
            fn=needs_review,
        ),
        AgentTool(
            name="orpheus_read_page",
            description=_PREFER + "Run the pattern pass over a page and show what it "
                        "offers. Local, instant, and it cannot offer something "
                        "the page does not contain, so it needs no opt-in. Each "
                        "offer is shown to the person with its own Record / Not "
                        "worth it buttons; nothing is in the store until one is "
                        "pressed.",
            input_schema={"type": "object", "properties": {
                "document_id": {"type": "string"},
                "page_no": {"type": "integer"}},
                "required": ["document_id", "page_no"]},
            fn=read_page,
        ),
        AgentTool(
            name="orpheus_settle_suggestion",
            description="Accept or dismiss one outstanding offer. The person is "
                        "asked to confirm either way. Call it when they say to "
                        "— not to tidy a queue, and never on your own reading of "
                        "whether an offer looks right.",
            input_schema={"type": "object", "properties": {
                "suggestion_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["record", "dismiss"]},
                "note": {"type": "string"}},
                "required": ["suggestion_id", "decision"]},
            fn=settle,
        ),
        AgentTool(
            name="orpheus_passage",
            description=_PREFER + "The full text of one page of a document, "
                        "with what has already been extracted from it and what "
                        "the reading companion has offered. Use it to read the "
                        "page somebody is looking at, and to take a verbatim "
                        "quote for orpheus_record.",
            input_schema={"type": "object", "properties": {
                "document_id": {"type": "string"},
                "page_no": {"type": "integer"}},
                "required": ["document_id", "page_no"]},
            fn=passage,
        ),
        AgentTool(
            name="orpheus_record",
            description="Offer a fact that extraction missed, quoting the line "
                        "on the page it was read on. The offer joins the same "
                        "queue a page read fills, the person is asked to "
                        "approve it, and it is refused if the page does not "
                        "contain the quote. Saying no is kept too. Draft it and "
                        "let them decide — never offer something you inferred "
                        "rather than read.",
            input_schema={"type": "object", "properties": {
                "document_id": {"type": "string"},
                "page_no": {"type": "integer",
                            "description": "The page the quote is on"},
                "type_id": {"type": "string",
                            "description": "A bundle type, e.g. Person"},
                "properties": {"type": "object", "additionalProperties": True,
                               "description": "The values to record"},
                "quote": {"type": "string",
                          "description": "Text from that page, verbatim"},
                "note": {"type": "string"}},
                "required": ["document_id", "page_no", "type_id", "properties",
                             "quote"]},
            fn=record,
        ),
    ]


@hookimpl
def register_agent_tools(datasette):
    # Absent when the extra is not installed, which is the normal case: the
    # plugin's own pages do not need it and must keep working without it.
    if AgentTool is None:
        return []
    return _tools()
