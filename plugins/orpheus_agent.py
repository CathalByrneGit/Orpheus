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
from orpheus import lint as lint_mod
from orpheus import record as record_mod
from orpheus import search as search_mod
from orpheus.store import Store
from orpheus.utils import OrpheusError

PLUGIN = "orpheus-datasette"

# Said on every tool, because the model reads descriptions and not this module.
_PREFER = ("Use this instead of sql_query: raw SQL over these tables returns "
           "values without the review state that qualifies them, and an answer "
           "written from that reads as settled fact. ")


def _database(datasette):
    config = datasette.plugin_config(PLUGIN) or {}
    name = config.get("database")
    return datasette.get_database(name) if name else None


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


def _actor_id(actor):
    return (actor or {}).get("orpheus_actor_id") or (actor or {}).get("id")


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

async def record(datasette, actor, context, document_id: str, type_id: str,
                 properties: dict, quote: str, note: str = None):
    """Record a fact extraction missed — after the person approves the draft."""
    actor_id = _actor_id(actor)
    if not actor_id:
        return json.dumps({"error": "No signed-in actor, so nothing can be "
                                    "recorded: the row has to belong to somebody."})

    values = "".join(
        f"<li><code>{html.escape(str(k))}</code>: {html.escape(str(v))}</li>"
        for k, v in (properties or {}).items())
    # ask_user raises QuestionPending and re-runs this function once answered,
    # so nothing is written before the person has seen the draft.
    approved = await context.ask_user(
        f"Record this {type_id} in the store?",
        html=(f"<p>Recording writes a <strong>confirmed</strong> row with "
              f"<code>source = human</code>. That is the one source nothing "
              f"downstream questions, so it should be what you read, not what "
              f"anybody inferred.</p><ul>{values}</ul>"
              f"<p><strong>Quoting:</strong> "
              f"<blockquote>{html.escape(quote or '')}</blockquote>"
              f"The quote is located in the document, and refused if it is not "
              f"there.</p>"))
    if not approved:
        return json.dumps({"recorded": False,
                           "note": "Not recorded. The person declined."})

    def run(store):
        return record_mod.record_fact(
            store, document_id, type_id, properties or {}, quote=quote,
            actor_id=actor_id, note=note)

    try:
        return json.dumps(await _write(datasette, run), default=str)
    except OrpheusError as refused:
        return json.dumps({
            "recorded": False, "refused": str(refused),
            "reading": ("Do not retry with a looser quote to get it in. If the "
                        "document does not say it, the entity page's notes "
                        "field is where it belongs.")})


# ---------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {}, "required": []}


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
            name="orpheus_corroboration",
            description=_PREFER + "How many independent sources say the same "
                        "thing about an entity, counted in distinct wordings so "
                        "copied boilerplate is not mistaken for agreement.",
            input_schema={"type": "object", "properties": {
                "entity_id": {"type": "string"}}, "required": ["entity_id"]},
            fn=corroboration_for,
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
            description="Record a fact that extraction missed, quoting the line "
                        "in the document it was read on. Asks the person to "
                        "approve the draft first, and is refused if the document "
                        "does not contain the quote. Draft it and let them "
                        "decide — never record something you inferred rather "
                        "than read.",
            input_schema={"type": "object", "properties": {
                "document_id": {"type": "string"},
                "type_id": {"type": "string",
                            "description": "A bundle type, e.g. Person"},
                "properties": {"type": "object", "additionalProperties": True,
                               "description": "The values to record"},
                "quote": {"type": "string",
                          "description": "Text from the document, verbatim"},
                "note": {"type": "string"}},
                "required": ["document_id", "type_id", "properties", "quote"]},
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
