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


def _actor_for(datasette, request) -> dict | None:
    """Map a Datasette actor onto an Orpheus one.

    Datasette answers "who is this"; Orpheus answers "what may they see". The
    two meet here, on the actor id, which is the seam `datasette-accounts` would
    slot into.
    """
    actor = request.actor
    if not actor:
        return None
    mapped = _config(datasette).get("actor_map", {}).get(actor.get("id"))
    return {"actor_id": mapped or actor.get("id"),
            "display_name": actor.get("name") or actor.get("id"),
            "is_admin": bool(actor.get("is_admin")
                             or actor.get("id") == _config(datasette).get("admin_id")
                             or actor.get("id") == "root")}


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
    actor = _actor_for(datasette, request)
    database = _database(datasette)
    writing = method != "GET"

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
        (r"^/-/orpheus/api/(?P<rest>.*)$", api_route),
    ]


@hookimpl
def menu_links(datasette, actor):
    if not actor:
        return []
    return [{"href": datasette.urls.path("/-/orpheus"), "label": "Documents"}]


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
