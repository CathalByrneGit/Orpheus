"""The Orpheus UI, running inside Datasette.

The R implementation made this a thin HTTP client over a Plumber API, because
the API was the single writer and a plugin opening its own SQLite connection
would have been a second one. In Python that arrangement is inverted on purpose:
**Datasette is the writer**, the core is a library it imports, and there is one
process rather than two.

The invariant that replaces "never opens a connection" is stricter and easier to
check: **nothing writes except through `orpheus` core functions.** No SQL is
written here. Every write goes through `api.handle()`, which applies provenance,
the confidence rubric, the amendment history and per-document permissions on the
way in — and every write is queued through `database.execute_write_fn`, so
Datasette's own write thread serialises them.

It still never calls a model directly. Doing so would bypass the cloud opt-in
gate, the org policy and the `llm_calls` audit in one step; the tier a person
picks on the form is passed to the core, which decides.
"""

from __future__ import annotations

import json
import mimetypes
import re
import tempfile
import urllib.parse
from pathlib import Path, PurePosixPath

from datasette import hookimpl
from datasette.utils.asgi import Response

try:  # Datasette 1.0+
    from datasette.utils.asgi import BadRequest
except ImportError:  # pragma: no cover
    class BadRequest(Exception):
        pass

from orpheus import api as orpheus_api
from orpheus import auth
from orpheus.store import Store

PLUGIN = "orpheus-datasette"
DEFAULT_DATABASE = "orpheus"


# ---------------------------------------------------------------------------
# Reaching the store
# ---------------------------------------------------------------------------

def _config(datasette) -> dict:
    return datasette.plugin_config(PLUGIN) or {}


def _database(datasette):
    name = _config(datasette).get("database", DEFAULT_DATABASE)
    try:
        return datasette.get_database(name)
    except KeyError:
        return datasette.get_database()


# Which identity provider a Datasette actor came from. Recorded on the Orpheus
# actor so two people with the same username under different providers stay
# distinct, and so a deployment can see where its accounts came from.
DEFAULT_IDP = "datasette"


def _datasette_identity(datasette, request) -> dict | None:
    """Who Datasette says this is, before Orpheus has an opinion.

    Datasette answers "who is this"; Orpheus answers "what may they see". This
    is the seam, and it reads whatever `actor_from_request` produced — the
    `--root` actor, `datasette-accounts`, an SSO plugin — without knowing which.
    """
    actor = request.actor
    if not actor:
        return None
    config = _config(datasette)

    # Whether the provider has an opinion about administrators *at all*. One
    # that does not must leave the flag None rather than say False, or signing
    # in would quietly demote someone promoted inside Orpheus.
    is_admin = bool(actor["is_admin"]) if "is_admin" in actor else None
    if actor.get("id") == config.get("admin_id") or actor.get("id") == "root":
        is_admin = True

    return {
        "external_id": str(actor.get("id")),
        "display_name": (actor.get("name") or actor.get("username")
                         or str(actor.get("id"))),
        "email": actor.get("email"),
        "idp": config.get("idp", DEFAULT_IDP),
        # Pins this Datasette identity to a specific Orpheus actor id. Kept for
        # deployments that had actors before they had an auth plugin, and so a
        # person can be bound to the rows they already created.
        "pinned": config.get("actor_map", {}).get(actor.get("id")),
        "is_admin": is_admin,
    }


def _provision(store, identity: dict) -> str:
    """The Orpheus actor this identity belongs to, creating it if it is new.

    `auth.upsert_actor()` exists for exactly this and, until now, nothing called
    it: the plugin looked the Datasette id up in an `actor_map` written by hand.
    That works for three people and not for thirty, and it meant every
    deployment adopting an auth plugin kept a second copy of its user list in
    YAML — where it went stale, and where a typo silently attributed one
    person's corrections to another.

    Keyed on `(idp, external_id)`, so the same person signing in again lands on
    the same row. That is what makes `created_by` and `edited_by` mean a person
    rather than a session.
    """
    if identity["pinned"]:
        # A pin is a deployment decision, so Orpheus's own row governs it: the
        # admin flag is not synced from the provider here. The row is created
        # when it is missing rather than left dangling, because `created_by`
        # references it.
        if auth.get_actor(store, identity["pinned"]) is None:
            auth.create_actor(store, identity["display_name"], identity["email"],
                              identity["idp"], identity["external_id"],
                              is_admin=bool(identity["is_admin"]),
                              actor_id=identity["pinned"])
        return identity["pinned"]
    return auth.upsert_actor(store, identity["idp"], identity["external_id"],
                             identity["display_name"], identity["email"],
                             is_admin=identity["is_admin"])


def _is_stale(row, identity: dict) -> bool:
    """Whether the provider now says something the `actors` row does not.

    A pinned actor is exempt: the pin is a deployment decision and Orpheus's
    own row governs it. For everyone else this is what turns the read-only
    fast path back into a write -- a renamed person, a changed address, an
    admin promoted or demoted upstream. Comparing costs nothing; not comparing
    means the surface shows who someone *used to be*.
    """
    if identity["pinned"]:
        return False
    if identity["is_admin"] is not None and bool(row["is_admin"]) != identity["is_admin"]:
        return True
    return (row["display_name"] != identity["display_name"]
            or row["email"] != identity["email"])


async def _resolve_actor(database, identity: dict) -> dict:
    """Settle this identity onto an Orpheus actor row.

    The row is the authority for `is_admin`, not the identity dict, because
    `permission_sql()` can only read the row -- so taking the flag from
    anywhere else is how the API and the browsing surface come to disagree.
    The provider feeds that column; it does not bypass it.

    Read first and write only when something has to change, so the ordinary
    request stays on a read connection and never queues behind the writer.
    """
    def look_up(conn):
        store = Store.adopt(conn, path=database.path)
        if identity["pinned"]:
            return auth.get_actor(store, identity["pinned"])
        return store.one(
            "SELECT * FROM actors WHERE idp = ? AND external_id = ?",
            (identity["idp"], identity["external_id"]))

    row = await database.execute_fn(look_up)
    if row is None or _is_stale(row, identity):
        def write(conn):
            store = Store.adopt(conn, path=database.path, owns_transaction=False)
            return auth.get_actor(store, _provision(store, identity))
        row = await database.execute_write_fn(write)

    return {"actor_id": row["actor_id"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"])}


class _Rollback(Exception):
    """Carries a failed API result out through Datasette's transaction."""

    def __init__(self, status: int, payload: object):
        super().__init__(f"API returned {status}")
        self.status = status
        self.payload = payload


async def _call(datasette, request, method: str, path: str,
                body: dict | None = None) -> tuple[int, object]:
    """One API call, on Datasette's write thread when it writes.

    Reads go straight through; writes are queued, which is what makes Datasette
    a safe single writer rather than merely the only one that happens to be
    running. `execute_write_fn` opens `BEGIN IMMEDIATE` around the task and
    commits when it returns, so the store is told to join that transaction
    rather than start one of its own.

    That commit-on-return is why a failed write has to be raised rather than
    returned. `api.handle()` turns core exceptions into `(4xx, {"error": ...})`
    — correct over HTTP, where the R implementation's own connection had
    already rolled back before the error was rendered. Here, returning normally
    *is* the instruction to commit, so an ingest that failed halfway would be
    committed halfway. The status comes back out through an exception instead,
    and is unwrapped on the far side.
    """
    identity = _datasette_identity(datasette, request)
    database = _database(datasette)
    writing = method != "GET"

    actor = None
    if identity:
        # Resolving the actor can *create* one, which the read connection a GET
        # runs on cannot do -- so it is settled before dispatch rather than
        # inside it, and every handler downstream can assume the row exists.
        actor = await _resolve_actor(database, identity)

    def run(conn):
        store = Store.adopt(conn, path=database.path, owns_transaction=not writing)
        # Migrations only run on a write open, and Datasette holds a shared
        # connection -- so an upgraded deployment serves a stale schema until
        # somebody runs the CLI. Checked here, once, so the symptom is a
        # sentence naming the fix rather than `no such table` from a route that
        # worked yesterday.
        store.assert_current()
        status, payload = orpheus_api.handle(store, method, path, body or {},
                                             actor=actor)
        if writing and status >= 400:
            raise _Rollback(status, payload)
        return status, payload

    if not writing:
        return await database.execute_fn(run)
    try:
        return await database.execute_write_fn(run)
    except _Rollback as failed:
        return failed.status, failed.payload


# ---------------------------------------------------------------------------
# Datasette hooks
# ---------------------------------------------------------------------------

@hookimpl
def register_routes():
    return [
        (r"^/-/orpheus$", index_page),
        (r"^/-/orpheus/upload$", upload),
        (r"^/-/orpheus/document/(?P<document_id>[^/]+)/redact$", redact_act),
        (r"^/-/orpheus/document/(?P<document_id>[^/]+)$", document_page),
        (r"^/-/orpheus/review$", review),
        (r"^/-/orpheus/read/act$", read_act),
        (r"^/-/orpheus/read/(?P<document_id>[^/]+)$", read_page),
        (r"^/-/orpheus/calendar$", calendar_page),
        (r"^/-/orpheus/lint$", lint_page),
        (r"^/-/orpheus/network$", network_page),
        (r"^/-/orpheus/map$", map_page),
        (r"^/-/orpheus/static/(?P<path>.*)$", static_asset),
        (r"^/-/orpheus/registers$", registers_page),
        (r"^/-/orpheus/registers/act$", registers_act),
        (r"^/-/orpheus/ontology$", ontology_page),
        (r"^/-/orpheus/ontology/act$", ontology_act),
        (r"^/-/orpheus/questions$", questions_page),
        (r"^/-/orpheus/questions/act$", questions_act),
        (r"^/-/orpheus/wiki$", wiki_index),
        (r"^/-/orpheus/wiki/queue$", wiki_queue),
        (r"^/-/orpheus/wiki/act$", wiki_act),
        (r"^/-/orpheus/wiki/(?P<entity_id>ent_[^/]+)$", entity_page),
        (r"^/-/orpheus/api/(?P<rest>.*)$", api_route),
    ]


@hookimpl
def menu_links(datasette, actor):
    if not actor:
        return []
    return [{"href": datasette.urls.path("/-/orpheus"), "label": "Documents"},
            {"href": datasette.urls.path("/-/orpheus/calendar"),
             "label": "Calendar"},
            {"href": datasette.urls.path("/-/orpheus/wiki"), "label": "Wiki"},
            {"href": datasette.urls.path("/-/orpheus/ontology"),
             "label": "Ontology"},
            {"href": datasette.urls.path("/-/orpheus/registers"),
             "label": "Registers"}]


@hookimpl
def table_actions(datasette, actor, database, table):
    """Offer the upload page from any instance table.

    Someone looking at extracted rows and wanting to add another document
    should not have to go looking for the page.
    """
    if not actor or not table.startswith("instances_"):
        return []
    return [{"href": datasette.urls.path("/-/orpheus"),
             "label": "Add a document",
             "description": "Ingest and extract a new document"}]


# ---------------------------------------------------------------------------
# The wiki
# ---------------------------------------------------------------------------

async def read_page(datasette, request):
    """Reading one passage with the machine: the page, and what it offers.

    The surface this project was started for. Everything it shows on the right
    is a proposal, not a row -- the store learns nothing until a person says so.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    document_id = request.url_vars["document_id"]

    status, document = await _call(datasette, request, "GET",
                                   f"/documents/{document_id}")
    if status != 200:
        return _redirect(datasette, "/-/orpheus", error=document["error"]["message"])

    try:
        page_no = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page_no = 1

    _, progress = await _call(datasette, request, "GET",
                              f"/documents/{document_id}/reading")
    status, page = await _call(
        datasette, request, "GET",
        f"/documents/{document_id}/passages/{page_no}", {"status": "all"})
    if status != 200:
        return _redirect(datasette, f"/-/orpheus/document/{document_id}",
                         error=page["error"]["message"])

    # Split here rather than in the template: what is still being offered and
    # what has been settled are different questions, and the page should not
    # have to work that out in a loop.
    offered = [s for s in page["suggestions"] if s["status"] == "offered"]
    decided = [s for s in page["suggestions"] if s["status"] != "offered"]

    _, capabilities = await _call(datasette, request, "GET", "/capabilities")
    available = (capabilities or {}).get("extraction_engines") or {}
    return await _render(datasette, request, "orpheus_read.html", {
        "document": document.get("document", document),
        "page": {**page, "suggestions": offered},
        "decided": decided,
        "progress": progress or {},
        "engines": [name for name, ready in available.items()
                    if ready and name != "deterministic"],
        # The chat panel is only offered where there is a chat to open. The
        # `agent` extra is optional and this page has to read the same without
        # it, so the template asks rather than assuming.
        "agent_available": _agent_available(datasette),
        "chat_passage": _chat_passage(page.get("text") or ""),
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


# Pagination targets roughly a printed page, so a passage is normally a few
# thousand characters. A run of text with no break in it at all is left whole
# rather than cut mid-token, though, and that one can be much longer -- so
# there is a ceiling, and when it bites the chat is told to read the rest
# rather than left believing it has the page.
CHAT_PASSAGE_LIMIT = 8000

#: How much of the rest of the document goes with a passage when a reader asks
#: for it. Roughly a handful of pages: enough to carry the definitions a clause
#: leans on, and bounded because it is charged to the same budget as the page.
CONTEXT_CHARS = 12000


def _chat_passage(text: str) -> dict:
    if len(text) <= CHAT_PASSAGE_LIMIT:
        return {"text": text, "truncated": False}
    return {"text": text[:CHAT_PASSAGE_LIMIT], "truncated": True}


def _agent_available(datasette) -> bool:
    """Is datasette-agent installed and usable by this deployment?

    Installed is not enough: without a model configured for `datasette-llm`
    every chat opens and then fails on the first message, which is a worse
    offer than not making one.
    """
    try:
        import datasette_agent  # noqa: F401
    except ImportError:
        return False
    config = datasette.plugin_config("datasette-llm") or {}
    return bool(config.get("default_model"))


async def read_act(datasette, request):
    """Every write the reading page makes, through one form handler."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus")

    form = await request.post_vars()
    action = form.get("action")
    document_id = form.get("document_id") or ""
    page_no = form.get("page_no") or "1"
    suggestion_id = form.get("suggestion_id") or ""
    back = f"/-/orpheus/read/{document_id}?page={page_no}"

    if action == "read":
        engine = form.get("engine") or "deterministic"
        body = {"engine": engine,
                "tier": "cloud" if engine != "deterministic" else "local",
                "cloud_opt_in": "1" if engine != "deterministic" else "0"}
        if form.get("with_context"):
            body["context_chars"] = CONTEXT_CHARS
        status, result = await _call(
            datasette, request, "POST",
            f"/documents/{document_id}/passages/{page_no}/read", body)
        if status != 200:
            return _redirect(datasette, back, error=result["error"]["message"])
        found = result.get("n_offered", 0)
        note = (f"{found} thing(s) worth a look." if found else
                "Read, and nothing stood out. That is recorded too.")
        # What the context cost and what it caught, said plainly. A reader who
        # turned it on is entitled to know whether it earned the call.
        if result.get("context_chars"):
            note += (f" {result['context_chars']} character(s) of the rest of "
                     "the document went with it")
            outside = result.get("n_outside_the_page", 0)
            note += (f", and {outside} offer(s) about it were discarded."
                     if outside else ".")
        return _redirect(datasette, back, note=note)

    if action in ("accept", "dismiss"):
        body = {"note": form.get("note") or None}
        if action == "accept":
            # Whatever the person left in the fields wins. `properties` is the
            # correction, applied on the way in rather than as a second step.
            body["properties"] = {key[len("prop_"):]: value
                                  for key, value in form.items()
                                  if key.startswith("prop_") and value != ""}
        status, result = await _call(
            datasette, request, "POST",
            f"/suggestions/{suggestion_id}/{action}", body)
        if status != 200:
            return _redirect(datasette, back, error=result["error"]["message"])
        return _redirect(datasette, back, note=(
            "Recorded." if action == "accept" else "Dismissed, and kept."))

    return _redirect(datasette, back, error=f"Unknown action {action!r}.")


async def wiki_index(datasette, request):
    """The wiki's front page: what needs doing, and a way in.

    Deliberately not a listing. Datasette already renders `entities` sortable,
    searchable, faceted by type and status and exportable as JSON or CSV, from
    a config block and no code -- so browsing links there rather than being
    rebuilt worse here. What Datasette cannot do is the actions.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)

    query = request.args.get("q") or ""
    _, listing = await _call(datasette, request, "GET", "/entities",
                             {"q": query, "limit": "50"})
    _, queue = await _call(datasette, request, "GET", "/mentions/unlinked",
                           {"limit": "200"})
    # The split the queue cannot show: once every mention has a home it is
    # empty, and two pages that are one thing sit there unremarked.
    _, dupes = await _call(datasette, request, "GET", "/entities/duplicates",
                           {"limit": "20"})
    # Standing conflicts, corpus-wide. Counted from the tension rows rather
    # than from the listing above, which is capped at 50 pages -- a conflict
    # that falls off the end of a page of results has not gone away.
    _, standing = await _call(datasette, request, "GET", "/tensions",
                              {"standing": "1", "limit": "200"})
    tensions = (standing or {}).get("tensions", [])
    entities = (listing or {}).get("entities", [])
    return await _render(datasette, request, "orpheus_wiki.html", {
        "entities": entities,
        "query": query,
        "queue_size": len((queue or {}).get("mentions", [])),
        "unreviewed": [e for e in entities if e["status"] == "unconfirmed"],
        "duplicates": (dupes or {}).get("pairs", []),
        "contested": [e for e in entities if e.get("n_tensions")],
        "unchecked_tensions": sum(1 for t in tensions if t["status"] == "open"),
        "table_url": datasette.urls.path(
            f"/{_config(datasette).get('database', DEFAULT_DATABASE)}/entities"),
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def entity_page(datasette, request):
    """One page. Confirmed facts asserted, proposals behind a disclosure."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    entity_id = request.url_vars["entity_id"]

    status, page = await _call(datasette, request, "GET", f"/entities/{entity_id}")
    if status != 200:
        return _redirect(datasette, "/-/orpheus/wiki", error=page["error"]["message"])

    # Split here rather than in the template: what the wiki *asserts* and what
    # it is *offering* are different claims, and the page should not have to
    # work that out in a loop.
    confirmed = [m for m in page["mentions"]
                 if m["link"]["status"] in ("confirmed", "amended")]
    proposed = [m for m in page["mentions"] if m not in confirmed]

    _, others = await _call(datasette, request, "GET", "/entities",
                            {"type_id": page["entity"]["type_id"], "limit": "200"})
    return await _render(datasette, request, "orpheus_entity.html", {
        "page": page,
        "entity": page["entity"],
        "confirmed": confirmed,
        "proposed": proposed,
        "mergeable": [e for e in (others or {}).get("entities", [])
                      if e["entity_id"] != page["entity"]["entity_id"]],
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def calendar_page(datasette, request):
    """What falls due, with how much of the corpus can speak to it.

    The one page somebody opens on a Monday without being asked to. Which is
    why the review state is on every row rather than in a filter: a diary of
    unconfirmed machine readings that looks like a diary is the failure this
    page has to work hardest to avoid.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    body = {"within_days": request.args.get("within_days") or "90"}
    if request.args.get("as_of"):
        body["as_of"] = request.args["as_of"]
    status, result = await _call(datasette, request, "GET", "/calendar", body)
    if status != 200:
        return _redirect(datasette, "/-/orpheus", error=result["error"]["message"])
    return await _render(datasette, request, "orpheus_calendar.html", {
        "calendar": result,
        "windows": (30, 90, 180, 365),
    })


async def lint_page(datasette, request):
    """The adversarial pass, where a reviewer will actually see it.

    Administrator-only in the core, so this renders whatever the API says
    rather than deciding for itself -- a second permission rule here is a
    second rule to keep in step with `auth.can()`.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    shallow = request.args.get("shallow") in ("1", "true", "on")
    status, report = await _call(datasette, request, "GET", "/lint",
                                 {"deep": "0" if shallow else "1"})
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error=report["error"]["message"])
    return await _render(datasette, request, "orpheus_lint.html", {
        "report": report,
        "shallow": shallow,
        "error": request.args.get("error"),
    })


async def questions_page(datasette, request):
    """What the shape of the corpus raises. Never a finding."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    status, report = await _call(datasette, request, "GET", "/questions")
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error=report["error"]["message"])
    return await _render(datasette, request, "orpheus_questions.html", {
        "report": report,
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def questions_act(datasette, request):
    """Where the individual decides. One form, one write."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus/questions")

    form = await request.post_vars()
    if not (form.get("rationale") or "").strip():
        return _redirect(datasette, "/-/orpheus/questions",
                         error="Give a reason. It is the part worth anything to "
                               "whoever looks at this next.")
    status, result = await _call(datasette, request, "POST", "/questions/review", {
        "fingerprint": form.get("fingerprint"),
        "status": form.get("status"),
        "rationale": form.get("rationale"),
        "kind": form.get("kind"),
        "summary": form.get("summary"),
        "chain_digest": form.get("chain_digest"),
    })
    if status != 200:
        return _redirect(datasette, "/-/orpheus/questions",
                         error=result["error"]["message"])
    return _redirect(datasette, "/-/orpheus/questions",
                     note=f"Recorded as {result['status']}.")


# ---------------------------------------------------------------------------
# The built map
# ---------------------------------------------------------------------------
#
# `frontend/` is a Svelte + Vite + d3-force application, built into
# `plugins/static/`. It is not committed -- `npm run build` produces it -- so
# everything below has to work with it absent, and the map falls back to the
# template that needs no toolchain.
#
# Datasette's own /-/static-plugins/ mount is not available: a plugin loaded
# with --plugins-dir gets `static_path = None`, because Datasette resolves the
# directory by importing the plugin as a package and a --plugins-dir module was
# never registered as one. So the bundle is served from a route here.

BUNDLE = Path(__file__).parent / "static"
MANIFEST = BUNDLE / "manifest.json"
ENTRY = "src/main.ts"


def _bundle() -> dict | None:
    """The built entry point, or None if nobody has built one.

    Read on each request rather than cached: a rebuild during `vite build`
    changes the hashed filenames, and a cached manifest would serve URLs for
    files that no longer exist until the server was restarted.
    """
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return None
    chunk = manifest.get(ENTRY)
    if not chunk or not chunk.get("file"):
        return None
    return {"js": chunk["file"], "css": list(chunk.get("css") or ())}


async def static_asset(datasette, request):
    """Serve one built asset.

    The path comes from the URL, so it is resolved and then checked to be
    inside the bundle directory. A prefix test on the raw string would pass
    `gen/../../../etc/passwd` and a symlink out of the tree; resolving both
    sides and comparing the result is the check that holds.
    """
    target = (BUNDLE / request.url_vars["path"]).resolve()
    try:
        target.relative_to(BUNDLE.resolve())
    except ValueError:
        return Response.text("Not found.", status=404)
    if not target.is_file():
        return Response.text("Not found.", status=404)

    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(
        # Bytes, not text: `Response.asgi_send` passes them through unchanged,
        # and decoding first would quietly corrupt any asset that is not UTF-8
        # -- a font or an image the build inlined.
        target.read_bytes(),
        content_type=content_type,
        # Vite puts a content hash in every filename, so a given URL never
        # changes what it holds.
        headers={"cache-control": "public, max-age=31536000, immutable"})


async def map_page(datasette, request):
    """The same relations the network page lists, drawn.

    Not a second source of truth: it reads `/graph/map`, which is `graph.build`
    with no separate projection, so the picture cannot show a relation the text
    views would not. The coverage banner leads for the same reason it does
    there, and more so -- a diagram is more persuasive than a table and says
    exactly as much.

    Two renderings, one payload. When `frontend/` has been built the Svelte and
    d3-force application draws it; when it has not, a template with a
    hand-written relaxation does. Both read the same server-rendered JSON, so
    which one a deployment gets changes how the map behaves and never what it
    claims. The fallback is not a courtesy: the bundle is a build artefact and
    is not committed, so a fresh checkout has only the template.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    entity_id = request.args.get("entity") or ""
    params = {"depth": request.args.get("depth") or "2"}
    if entity_id:
        params["entity_id"] = entity_id
    if request.args.get("reviewed_only"):
        params["reviewed_only"] = "1"

    status, payload = await _call(datasette, request, "GET", "/graph/map", params)
    if status != 200:
        return _redirect(datasette, "/-/orpheus/network",
                         error=payload["error"]["message"])

    centre_name = next((n["canonical_name"] for n in payload["nodes"]
                        if n["entity_id"] == entity_id), None)
    bundle = _bundle()
    template = "orpheus_map_app.html" if bundle else "orpheus_map.html"
    return await _render(datasette, request, template, {
        "bundle": bundle,
        # The structure, not a pre-serialised string: `tojson` in the template
        # escapes it for embedding in a <script>, which a plain dump does not.
        "payload": {"nodes": payload["nodes"], "edges": payload["edges"]},
        "coverage": payload["coverage"],
        "centre": entity_id or None,
        "centre_name": centre_name,
        "depth": int(params["depth"]),
        "reviewed_only": bool(request.args.get("reviewed_only")),
        "error": request.args.get("error"),
    })


async def network_page(datasette, request):
    """The structural view. Administrator-only in the core, rendered as told."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    status, report = await _call(datasette, request, "GET", "/graph/topology")
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error=report["error"]["message"])
    # Tracing a connection is a read on two ids the person typed, so a bad one
    # is a message on the page rather than a redirect that loses the rest of it.
    path_from = request.args.get("from") or ""
    path_to = request.args.get("to") or ""
    paths, path_error = None, None
    if path_from and path_to:
        status, found = await _call(datasette, request, "GET", "/graph/paths",
                                    {"from": path_from, "to": path_to})
        if status == 200:
            paths = found
        else:
            path_error = found["error"]["message"]

    return await _render(datasette, request, "orpheus_network.html", {
        "report": report,
        "paths": paths,
        "path_from": path_from,
        "path_to": path_to,
        "error": request.args.get("error") or path_error,
    })


async def registers_page(datasette, request):
    """Reference data, and which of its columns anybody can query.

    Registers had no page at all: they were CLI, API and agent tools only,
    which is a strange place for the one thing in the store a person is asked
    to vouch for. This is that page, and the column control lives on it because
    "I want to filter by county" is a thing you discover while looking at rows.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    register_id = request.args.get("register_id") or ""
    status, listed = await _call(datasette, request, "GET", "/registers")
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error=listed["error"]["message"])
    _, columns = await _call(datasette, request, "GET", "/registers/columns")

    register, rows, filtered = None, [], None
    if register_id:
        body = {"limit": "50"}
        if request.args.get("column"):
            body["column"] = request.args.get("column")
            body["value"] = request.args.get("value") or ""
            filtered = (body["column"], body["value"])
        status, one = await _call(
            datasette, request, "GET", f"/registers/{register_id}", body)
        if status != 200:
            return _redirect(datasette, "/-/orpheus/registers",
                             error=one["error"]["message"])
        register, rows = one["register"], one["rows"]

    return await _render(datasette, request, "orpheus_registers.html", {
        "registers": listed.get("registers", []),
        "reading": listed.get("reading"),
        "exposed": (columns or {}).get("exposed", []),
        "n_rows": (columns or {}).get("n_rows", 0),
        "columns_reading": (columns or {}).get("reading"),
        "register": register, "rows": rows, "filtered": filtered,
        "is_admin": await _is_admin(datasette, request),
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def registers_act(datasette, request):
    """Expose a column, hide one, promote a register, reject a row."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus/registers")

    form = await request.post_vars()
    action = form.get("action")
    register_id = form.get("register_id") or ""
    back = (f"/-/orpheus/registers?register_id={register_id}"
            if register_id else "/-/orpheus/registers")

    routes = {
        "expose": ("/registers/columns/expose",
                   {"key": form.get("key"),
                    "as_column": form.get("as_column") or None,
                    "note": form.get("note") or None}),
        "hide": ("/registers/columns/hide",
                 {"column": form.get("column"), "note": form.get("note") or None}),
        "promote": (f"/registers/{register_id}/promote",
                    {"note": form.get("note") or None}),
        "withdraw": (f"/registers/{register_id}/withdraw",
                     {"note": form.get("note") or None}),
        "reject_row": (f"/registers/{register_id}/rows/{form.get('row_no')}/reject",
                       {"note": form.get("note") or None}),
    }
    if action not in routes:
        return _redirect(datasette, back, error=f"Unknown action {action!r}.")
    path, body = routes[action]
    status, result = await _call(datasette, request, "POST", path, body)
    if status != 200:
        return _redirect(datasette, back, error=result["error"]["message"])

    notes = {
        "expose": lambda r: f"{r['column']} <- {r['key']!r}. {r['reading']}",
        "hide": lambda r: f"{r['column']} is gone. {r['reading']}",
        "promote": lambda r: (f"{r['rows_accepted']} row(s) accepted. It counts "
                              "as evidence now."),
        "withdraw": lambda r: "Withdrawn. It stops counting and stays readable.",
        "reject_row": lambda r: f"Row {r['row_no']} is {r['status']}.",
    }
    return _redirect(datasette, back, note=notes[action](result))


async def _is_admin(datasette, request) -> bool:
    """Whether this signed-in person is an Orpheus administrator.

    The Orpheus row is the authority, not Datasette's actor: a `--root` sign-in
    carries no `is_admin` key at all, so reading it off the request hid the
    decide buttons from the one person allowed to press them. Every route that
    *acts* on the answer re-checks it in `api.py`; this only decides what to
    draw.
    """
    identity = _datasette_identity(datasette, request)
    if not identity:
        return False
    actor = await _resolve_actor(_database(datasette), identity)
    return bool((actor or {}).get("is_admin"))


async def ontology_page(datasette, request):
    """The ontology queue: what a survey proposed, and where somebody decides.

    Without this the loop is CLI-only, and the decision it asks for is the one
    that most needs a person's eyes on the evidence -- an object type shapes
    every row that will ever be filed under it. A queue nobody can see is a
    queue nobody reads.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    status, listed = await _call(datasette, request, "GET",
                                 "/ontology/candidates",
                                 {"status": request.args.get("status")
                                  or "proposed"})
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error=listed["error"]["message"])
    _, decided = await _call(datasette, request, "GET",
                             "/ontology/candidates", {"status": "accepted"})
    _, amended = await _call(datasette, request, "GET",
                             "/ontology/candidates", {"status": "amended"})
    accepted = ((decided or {}).get("candidates", [])
                + (amended or {}).get("candidates", []))
    return await _render(datasette, request, "orpheus_ontology.html", {
        "candidates": listed.get("candidates", []),
        "reading": listed.get("reading"),
        "accepted": accepted,
        # A bundle can only be drafted once something has been accepted, and
        # only from types -- a queue of accepted properties with no type is the
        # half-made decision the drafter refuses.
        "has_types": any(c["kind"] == "object_type" for c in accepted),
        "showing": request.args.get("status") or "proposed",
        "is_admin": await _is_admin(datasette, request),
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def ontology_act(datasette, request):
    """Survey, decide, draft. Three writes, one handler."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus/ontology")

    form = await request.post_vars()
    action = form.get("action")

    if action == "survey":
        status, result = await _call(datasette, request, "POST",
                                     "/ontology/survey", {
                                         "engine": form.get("engine") or None,
                                         "min_support": form.get("min_support"),
                                         "sample": form.get("sample"),
                                         "tier": form.get("tier") or "local",
                                         "cloud_opt_in": bool(
                                             form.get("cloud_opt_in")),
                                     })
        if status != 200:
            return _redirect(datasette, "/-/orpheus/ontology",
                             error=result["error"]["message"])
        held = result.get("n_below_support") or 0
        return _redirect(
            datasette, "/-/orpheus/ontology",
            note=(f"Read {result['n_documents_read']} document(s): "
                  f"{result['n_candidates']} candidate(s)"
                  + (f", {held} below the support threshold." if held
                     else ".")))

    if action == "draft":
        status, result = await _call(datasette, request, "POST",
                                     "/ontology/draft", {
                                         "bundle_id": form.get("bundle_id"),
                                         "name": form.get("name"),
                                         "document_types": [
                                             t.strip() for t in
                                             (form.get("document_types") or ""
                                              ).split(",") if t.strip()],
                                     })
        if status != 200:
            return _redirect(datasette, "/-/orpheus/ontology",
                             error=result["error"]["message"])
        problems = result.get("problems") or []
        # Drafted, never registered. Installing an ontology from a button is
        # the one place it could arrive in a store without anybody choosing it,
        # so the page says what it would be and the CLI installs it.
        summary = (f"Drafted {result['bundle_id']}: "
                   f"{', '.join(result['object_types'])} with "
                   f"{result['n_properties']} propert(ies) and "
                   f"{len(result['links'])} link(s). Register it with "
                   f"`orpheus ontology draft --register`.")
        # Warnings, not problems: a type with no name is a legitimate thing to
        # want. It is also invisible until somebody looks at graph coverage
        # after the extraction has run, so it is said here.
        for warning in (result.get("warnings") or []):
            summary += " " + warning
        return _redirect(datasette, "/-/orpheus/ontology",
                         error="; ".join(problems) if problems else None,
                         note=summary)

    candidate_id = form.get("candidate_id") or ""
    if action == "reopen":
        status, result = await _call(
            datasette, request, "POST",
            f"/ontology/candidates/{candidate_id}/reopen",
            {"note": form.get("note") or None})
        if status != 200:
            return _redirect(datasette, "/-/orpheus/ontology",
                             error=result["error"]["message"])
        return _redirect(datasette, "/-/orpheus/ontology",
                         note=f"{result['type_id']}"
                              + (f".{result['property_id']}"
                                 if result.get("property_id") else "")
                              + " is back in the queue.")

    status, result = await _call(
        datasette, request, "POST",
        f"/ontology/candidates/{candidate_id}/review",
        {"decision": form.get("decision"),
         "accepted_as": (form.get("accepted_as") or "").strip() or None,
         "note": form.get("note") or None})
    if status != 200:
        return _redirect(datasette, "/-/orpheus/ontology",
                         error=result["error"]["message"])
    return _redirect(datasette, "/-/orpheus/ontology",
                     note=f"{result['type_id']}"
                          + (f".{result['property_id']}"
                             if result.get("property_id") else "")
                          + f" \u2014 {result['status']}"
                          + (f" as {result['accepted_as']}"
                             if result.get("accepted_as") else "") + ".")


async def redact_act(datasette, request):
    """Destroy everything read from a document, from the browser.

    Two steps rather than one. A GET shows what would go, counted by a real
    dry run against the store rather than estimated; only a POST does it. An
    irreversible action behind a single click is a trap, and the count is the
    part that makes the choice informed -- "this will also remove 2 entity
    pages" is not something a person can work out from the page they are on.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    document_id = request.url_vars["document_id"]

    if request.method != "POST":
        status, preview = await _call(
            datasette, request, "POST", f"/documents/{document_id}/redact",
            {"dry_run": "1", "note": "(preview)"})
        if status != 200:
            return _redirect(datasette, f"/-/orpheus/document/{document_id}",
                             error=preview["error"]["message"])
        _, document = await _call(datasette, request, "GET",
                                  f"/documents/{document_id}")
        return await _render(datasette, request, "orpheus_redact.html", {
            "document": document.get("document", document),
            "preview": preview,
            "error": request.args.get("error"),
        })

    form = await request.post_vars()
    status, result = await _call(
        datasette, request, "POST", f"/documents/{document_id}/redact",
        {"note": form.get("note") or ""})
    if status != 200:
        return _redirect(datasette, f"/-/orpheus/document/{document_id}/redact",
                         error=result["error"]["message"])
    return _redirect(datasette, f"/-/orpheus/document/{document_id}",
                     note=result["headline"])


async def wiki_queue(datasette, request):
    """Mentions with no page yet, each with the pages it might belong to.

    The screen where a wiki actually gets built: the machine has grouped what
    it could, and everything it could not is here with its candidates.
    """
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)

    _, queue = await _call(datasette, request, "GET", "/mentions/unlinked",
                           {"limit": "50"})
    mentions = (queue or {}).get("mentions", [])
    for found in mentions:
        _, result = await _call(
            datasette, request,
            "GET", f"/mentions/{found['instance_id']}/candidates", {"limit": "5"})
        found["candidates"] = (result or {}).get("candidates", [])
    return await _render(datasette, request, "orpheus_queue.html", {
        "mentions": mentions,
        "error": request.args.get("error"),
        "note": request.args.get("note"),
    })


async def wiki_act(datasette, request):
    """Every write the wiki pages make, through one form handler."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus/wiki")

    form = await request.post_vars()
    action = form.get("action")
    entity_id = form.get("entity_id") or ""
    instance_id = form.get("instance_id") or ""
    tension_id = form.get("tension_id") or ""
    note = form.get("note") or None
    back = form.get("back") or (f"/-/orpheus/wiki/{entity_id}" if entity_id
                                else "/-/orpheus/wiki")

    routes = {
        "propose": ("POST", "/entities/propose", {}),
        "confirm": ("POST", f"/entities/{entity_id}/confirm", {"note": note}),
        "reject": ("POST", f"/entities/{entity_id}/reject", {"note": note}),
        "rename": ("POST", f"/entities/{entity_id}/rename",
                   {"canonical_name": form.get("canonical_name"), "note": note}),
        "describe": ("POST", f"/entities/{entity_id}/describe",
                     {"description": form.get("description"), "note": note}),
        "merge": ("POST", f"/entities/{entity_id}/merge",
                  {"merge_id": form.get("merge_id"), "note": note}),
        "confirm_link": ("POST",
                         f"/entities/{entity_id}/mentions/{instance_id}/confirm",
                         {"note": note}),
        "unlink": ("POST", f"/entities/{entity_id}/mentions/{instance_id}/unlink",
                   {"note": note}),
        "link": ("POST", f"/entities/{entity_id}/mentions",
                 {"instance_id": instance_id, "basis": "human", "note": note}),
        "create": ("POST", "/entities",
                   {"type_id": form.get("type_id"),
                    "canonical_name": form.get("canonical_name")}),
        # Conflicts. `accept` is the one that matters: every other verb on this
        # page makes a disagreement go away, and a reviewer who cannot say "yes,
        # this is real" ends up picking a side to clear the queue.
        "find_conflicts": ("POST", "/tensions/propose", {"entity_id": entity_id}),
        "accept_tension": ("POST", f"/tensions/{tension_id}/accept",
                           {"note": note}),
        "resolve_tension": ("POST", f"/tensions/{tension_id}/resolve",
                            {"resolution": note}),
        "withdraw_tension": ("POST", f"/tensions/{tension_id}/withdraw",
                             {"reason": note}),
        "raise_tension": ("POST", "/tensions",
                          {"kind": form.get("kind") or "unexplained",
                           "summary": form.get("summary"),
                           "scope": "entity", "subject_id": entity_id,
                           "property_id": form.get("property_id") or None,
                           "sides": form.get("sides") or ""}),
    }
    if action not in routes:
        return _redirect(datasette, back, error=f"Unknown action {action!r}.")

    method, path, body = routes[action]
    status, result = await _call(datasette, request, method, path, body)
    if status != 200:
        return _redirect(datasette, back, error=result["error"]["message"])

    if action == "create" and form.get("instance_id"):
        # Creating a page from the queue links the mention that prompted it,
        # so the person who said "this is a new company" does not then have to
        # say which mention made them think so.
        new_id = result["entity_id"]
        link_status, link_result = await _call(
            datasette, request, "POST", f"/entities/{new_id}/mentions",
            {"instance_id": form["instance_id"], "basis": "human"})
        if link_status != 200:
            return _redirect(datasette, back,
                             error=link_result["error"]["message"])
        return _redirect(datasette, f"/-/orpheus/wiki/{new_id}",
                         note="Page created.")

    if action == "propose":
        return _redirect(datasette, "/-/orpheus/wiki",
                         note=f"{result['proposed']} page(s) proposed from "
                              f"{result['linked']} mention(s).")
    if action == "merge":
        return _redirect(datasette, f"/-/orpheus/wiki/{result['kept']}",
                         note=f"Merged, moving {result['mentions_moved']} "
                              "mention(s).")
    if action == "reject":
        return _redirect(datasette, "/-/orpheus/wiki", note="Page rejected.")
    return _redirect(datasette, back, note=f"Done: {action.replace('_', ' ')}.")


# ---------------------------------------------------------------------------
# The JSON API, mounted rather than served separately
# ---------------------------------------------------------------------------

async def api_route(datasette, request):
    """`/-/orpheus/api/...` — the same dispatch table, over HTTP.

    Here so that a script, a CLI or an agent tool has a surface, without a
    second process to deploy and a second writer to reason about.
    """
    rest = request.url_vars["rest"]
    body: dict = {}
    if request.method == "POST":
        raw = await request.post_body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return Response.json(
                    {"error": {"message": "Body must be JSON."}}, status=400)
    else:
        # MultiParams is not a Mapping: it exposes keys()/get()/getlist() and
        # nothing else.
        body = {key: request.args.get(key) for key in request.args.keys()}

    status, payload = await _call(datasette, request, request.method,
                                  "/" + rest, body)
    if isinstance(payload, orpheus_api.FileBody):
        return _send_file(request, payload,
                          download="download" in request.args)
    return Response.json(payload, status=status)


# A filename is user input on its way into a response header, which is two
# problems at once: a newline would end the header and begin another, and a
# separator would suggest a path. Both are replaced rather than rejected --
# somebody's document should still arrive when its name is odd.
_UNSAFE_IN_HEADER = re.compile(r'[\x00-\x1f\x7f"\\]')

# Types a browser renders without running anything the uploader wrote. PDFs and
# images are the point of the feature -- a reviewer wants to *look* at the
# signature block, not find it in their downloads folder. Everything else is an
# attachment, and `image/svg+xml` is deliberately not on this list: an SVG is a
# document that can carry script, and rendering one inline would run it on
# Datasette's own origin with the reviewer's session.
_RENDERABLE = frozenset({
    "application/pdf", "text/plain", "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
})


def _disposition(filename: str, media_type: str, download: bool) -> str:
    name = PurePosixPath((filename or "").replace("\\", "/")).name
    name = _UNSAFE_IN_HEADER.sub("_", name).strip(". ") or "download"
    how = "attachment" if download or media_type not in _RENDERABLE else "inline"
    # RFC 6266: the ASCII `filename` every client understands, and `filename*`
    # for the ones that can spell the name the uploader actually used.
    fallback = name.encode("ascii", "replace").decode("ascii")
    return (f"{how}; filename=\"{fallback}\"; "
            f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}")


def _send_file(request, body, *, download: bool) -> Response:
    """Send a located original back.

    The ETag is the document's SHA-256, which is not a cache heuristic here but
    the document's identity: content-addressed storage means a given digest is
    one sequence of bytes forever, so a client holding it needs nothing else.
    `no-cache` pairs with it deliberately -- revalidate every time, which is
    what makes the permission check run every time. `private` on top, because a
    shared cache holding a permissioned document is the whole problem.
    """
    etag = f'"{body.file_hash}"'
    headers = {
        "etag": etag,
        "cache-control": "private, no-cache",
        "content-disposition": _disposition(body.filename, body.media_type,
                                            download),
        # The media type is the uploader's word for what this is, and it is
        # served back unchanged. `nosniff` is what makes that safe: without it
        # a browser may decide a mislabelled file is HTML and render it here,
        # on this origin, with this reviewer's cookies.
        "x-content-type-options": "nosniff",
    }
    if request.headers.get("if-none-match") == etag:
        return Response("", status=304, headers=headers)
    return Response(body.path.read_bytes(), status=200,
                    content_type=body.media_type, headers=headers)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

async def _render(datasette, request, template, context, status=200):
    return Response.html(
        await datasette.render_template(template, context, request=request),
        status=status)


def _redirect(datasette, path: str, **params) -> Response:
    """Redirect, merging into whatever query string the path already carries.

    `?` unconditionally produced `?page=1?note=...` on any path that already had
    one, so the message never arrived -- silently, because a malformed query
    string is not an error, just a parameter nobody reads.
    """
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    if not query:
        return Response.redirect(datasette.urls.path(path))
    separator = "&" if "?" in path else "?"
    return Response.redirect(datasette.urls.path(path) + separator + query)


async def index_page(datasette, request):
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)

    _, documents = await _call(datasette, request, "GET", "/documents")
    _, capabilities = await _call(datasette, request, "GET", "/capabilities")
    return await _render(datasette, request, "orpheus_index.html", {
        "documents": (documents or {}).get("documents", []),
        "capabilities": capabilities,
        "error": request.args.get("error"),
        "uploaded": request.args.get("uploaded"),
        "note": request.args.get("note"),
    })


async def upload(datasette, request):
    """Ingest a document, then classify and extract from it."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus")

    max_bytes = int(_config(datasette).get("max_file_size", 50 * 1024 * 1024))
    if hasattr(request, "form"):
        try:
            # Datasette parses the multipart itself, with limits on file size,
            # request size and free disk. Reimplementing that would mean
            # reimplementing its guards too, badly.
            form = await request.form(files=True, max_file_size=max_bytes)
        except BadRequest as exc:
            # Request.form() turns every limit failure into BadRequest, which
            # Datasette would otherwise render as a bare 400 page.
            return _redirect(datasette, "/-/orpheus", error=f"Upload rejected: {exc}")
        uploaded = form.get("file")
    else:
        form = await request.post_vars()
        uploaded = None

    path = (form.get("path") or "").strip()
    tier = form.get("tier") or "local"
    engine = form.get("engine") or "auto"
    cloud_opt_in = form.get("cloud_opt_in") == "on"
    has_file = uploaded is not None and getattr(uploaded, "filename", "")

    if not has_file and not path:
        return _redirect(datasette, "/-/orpheus",
                         error="Choose a file, or give a path already on the server.")

    # Held here so `finally` can remove it down every path out of this
    # function, including the failures.
    handover: tempfile.TemporaryDirectory | None = None
    try:
        if has_file:
            # `ingest` hashes and content-addresses the original, so it needs a
            # file rather than the bytes. This is the handover, and it is
            # deliberately not named after what the browser called it.
            #
            # **The uploaded filename is attacker input and is never a path.**
            # Datasette stores it verbatim -- its multipart parser does no path
            # sanitisation at all -- and `Path(tmpdir) / "/etc/passwd"` is
            # `/etc/passwd`: an absolute name discards the directory it was
            # joined to, and `../..` walks out of it. Writing there was an
            # arbitrary file write as whoever the server runs as. The real name
            # travels as `filename`, which is what `ingest` reads the kind and
            # the extension from and what lands in `documents.filename`; the
            # file on disk is called `upload` and nothing depends on that.
            handover = tempfile.TemporaryDirectory(prefix="orpheus-upload-")
            tmp = Path(handover.name) / "upload"
            tmp.write_bytes(await uploaded.read())
            await uploaded.close()
            payload = {"path": str(tmp), "filename": uploaded.filename}
        else:
            # Still supported: a watched drop-directory hands over a path rather
            # than pushing bytes through a browser.
            payload = {"path": path}

        payload["storage_root"] = _config(datasette).get("storage_root", "storage")
        status, document = await _call(datasette, request, "POST", "/documents",
                                       payload)
        if status != 200:
            return _redirect(datasette, "/-/orpheus",
                             error="Ingest failed: " + document["error"]["message"])

        document_id = document["document_id"]
        target = f"/-/orpheus/document/{document_id}"
        if document.get("duplicate"):
            return _redirect(datasette, target,
                             note="That content was already ingested; showing "
                                  "the existing document.")

        # Classification is a convenience; losing it should not stop the person
        # reaching a document that ingested successfully.
        await _call(datasette, request, "POST",
                    f"/documents/{document_id}/classify", {"tier": "local"})

        status, extracted = await _call(
            datasette, request, "POST", f"/documents/{document_id}/extract",
            {"tier": tier, "engine": engine, "cloud_opt_in": cloud_opt_in})
        if status != 200:
            return _redirect(datasette, target,
                             error="Ingested, but extraction failed: "
                                   + extracted["error"]["message"])
        return _redirect(datasette, target, uploaded="1")
    finally:
        # By here `ingest` has copied the bytes into the storage root, which is
        # the copy the store knows about. Leaving this one behind put a second,
        # unrecorded copy of every uploaded document in the system temp
        # directory, at default permissions, outside whatever the deployment
        # locked down -- and it survived the document's own `visibility`.
        if handover is not None:
            handover.cleanup()


async def document_page(datasette, request):
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    document_id = request.url_vars["document_id"]

    status, document = await _call(datasette, request, "GET",
                                   f"/documents/{document_id}")
    if status != 200:
        return _redirect(datasette, "/-/orpheus", error=document["error"]["message"])
    _, instances = await _call(datasette, request, "GET",
                               f"/documents/{document_id}/instances")

    return await _render(datasette, request, "orpheus_document.html", {
        "document": document,
        "review": document.get("review", {}),
        "instances": (instances or {}).get("instances", []),
        "error": request.args.get("error"),
        "note": request.args.get("note"),
        "uploaded": request.args.get("uploaded"),
    })


async def review(datasette, request):
    """Confirm, amend or reject one instance."""
    if not request.actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return _redirect(datasette, "/-/orpheus")

    form = await request.post_vars()
    instance_id = form.get("instance_id")
    document_id = form.get("document_id")
    action = form.get("action")
    target = f"/-/orpheus/document/{document_id}"

    if action not in ("confirm", "amend", "reject"):
        return _redirect(datasette, target, error=f"Unknown action {action!r}.")

    body: dict = {"note": form.get("note") or None}
    if action == "amend":
        # Every rendered field comes back, changed or not; the core drops the
        # ones that match what is stored and refuses an amendment that turns
        # out to be empty, so there is no second opinion about it here. A blank
        # field is skipped rather than sent: it means the person cleared the
        # box, not that they meant to store an empty string.
        body["changes"] = {key[len("change_"):]: value
                           for key, value in form.items()
                           if key.startswith("change_") and value != ""}

    status, result = await _call(datasette, request, "POST",
                                 f"/instances/{instance_id}/{action}", body)
    if status != 200:
        return _redirect(datasette, target, error=result["error"]["message"])
    return _redirect(datasette, target, note=f"Marked {action}ed.")
