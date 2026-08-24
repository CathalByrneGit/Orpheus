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
import urllib.parse

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
        (r"^/-/orpheus/document/(?P<document_id>[^/]+)$", document_page),
        (r"^/-/orpheus/review$", review),
        (r"^/-/orpheus/lint$", lint_page),
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
            {"href": datasette.urls.path("/-/orpheus/wiki"), "label": "Wiki"}]


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
    return Response.json(payload, status=status)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

async def _render(datasette, request, template, context, status=200):
    return Response.html(
        await datasette.render_template(template, context, request=request),
        status=status)


def _redirect(datasette, path: str, **params) -> Response:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return Response.redirect(datasette.urls.path(path) + (f"?{query}" if query else ""))


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

    if has_file:
        # Written to the store's own storage root rather than kept in memory:
        # ingest hashes and content-addresses the original, and it needs a file.
        import tempfile
        from pathlib import Path
        data = await uploaded.read()
        await uploaded.close()
        tmp = Path(tempfile.mkdtemp()) / uploaded.filename
        tmp.write_bytes(data)
        payload = {"path": str(tmp), "filename": uploaded.filename}
    else:
        # Still supported: a watched drop-directory hands over a path rather
        # than pushing bytes through a browser.
        payload = {"path": path}

    payload["storage_root"] = _config(datasette).get("storage_root", "storage")
    status, document = await _call(datasette, request, "POST", "/documents", payload)
    if status != 200:
        return _redirect(datasette, "/-/orpheus",
                         error="Ingest failed: " + document["error"]["message"])

    document_id = document["document_id"]
    target = f"/-/orpheus/document/{document_id}"
    if document.get("duplicate"):
        return _redirect(datasette, target,
                         note="That content was already ingested; showing the "
                              "existing document.")

    # Classification is a convenience; losing it should not stop the person
    # reaching a document that ingested successfully.
    await _call(datasette, request, "POST", f"/documents/{document_id}/classify",
                {"tier": "local"})

    status, extracted = await _call(
        datasette, request, "POST", f"/documents/{document_id}/extract",
        {"tier": tier, "engine": engine, "cloud_opt_in": cloud_opt_in})
    if status != 200:
        return _redirect(datasette, target,
                         error="Ingested, but extraction failed: "
                               + extracted["error"]["message"])
    return _redirect(datasette, target, uploaded="1")


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
