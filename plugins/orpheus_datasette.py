"""Orpheus UI for Datasette.

Adds an upload page and per-row review actions, so a person can drop a document
in and correct what came out without leaving Datasette.

The whole plugin is a thin client over the Orpheus HTTP API. That is a
deliberate constraint, not an accident of how it grew:

  * It never opens its own SQLite connection. Datasette writing to the store
    directly would make it a second writer, and the single-writer guarantee the
    storage design rests on would stop holding.

  * It never calls a model. Doing so would bypass the cloud opt-in gate, the
    org policy, the per-request consent and the llm_calls audit log in one
    step -- a document reaching a cloud model with no record that it did, which
    is the exact failure the opt-in exists to prevent.

Everything below therefore goes through the API, which enforces provenance, the
confidence rubric, the amendment history and permissions on the way in.
"""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from datasette import hookimpl
from datasette.utils.asgi import Response

try:  # Datasette 1.0+
    from datasette.utils.asgi import BadRequest
except ImportError:  # pragma: no cover - older Datasette has no file uploads
    class BadRequest(Exception):
        pass

PLUGIN = "orpheus-datasette"
DEFAULT_API = "http://127.0.0.1:8000"
TIMEOUT = 120


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _config(datasette):
    return (datasette.plugin_config(PLUGIN) or {})


def _api_base(datasette):
    return _config(datasette).get("api_url", DEFAULT_API).rstrip("/")


def _token(datasette, actor):
    """Resolve the Orpheus API token for this viewer.

    Two modes. A per-actor mapping is the honest one: the API then sees the
    real person, so `amended_by` names them and per-document permissions apply
    to them. A single shared token is offered for a single-user deployment and
    is called out as such, because with it every amendment is attributed to one
    identity and the audit trail stops distinguishing people.
    """
    config = _config(datasette)
    tokens = config.get("actor_tokens") or {}
    if actor and actor.get("id") in tokens:
        return tokens[actor["id"]]
    return config.get("token")


def _multipart_body(filename, content_type, data):
    """Build a multipart/form-data body for one file.

    Hand-built rather than pulled from a library because the plugin has no
    dependencies beyond Datasette itself, and this is the only multipart it
    ever needs to produce. The boundary is derived from the payload so it
    cannot collide with the content.
    """
    boundary = "orpheus" + hashlib.sha256(data[:4096] + filename.encode()).hexdigest()[:24]
    head = (
        "--{}\r\n"
        'Content-Disposition: form-data; name="file"; filename="{}"\r\n'
        "Content-Type: {}\r\n\r\n"
    ).format(boundary, filename.replace('"', ""), content_type or "application/octet-stream")
    body = head.encode() + data + "\r\n--{}--\r\n".format(boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _call_upload(datasette, actor, filename, content_type, data):
    """Forward an uploaded file to the API as multipart.

    Forwarded rather than written to a shared temp directory, so the plugin
    keeps working when Datasette and the API are on different hosts -- which is
    the realistic deployment, not the demo one.
    """
    token = _token(datasette, actor)
    if not token:
        raise ApiError(500, "No Orpheus API token is configured for this user.")

    body, content_type_header = _multipart_body(filename, content_type, data)
    request = urllib.request.Request(_api_base(datasette) + "/documents", data=body,
                                     method="POST")
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Content-Type", content_type_header)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        message = raw
        try:
            message = json.loads(raw)["error"]["message"]
        except Exception:
            pass
        raise ApiError(exc.code, message)
    except urllib.error.URLError as exc:
        raise ApiError(503, "Could not reach the Orpheus API: {}".format(exc.reason))


def _call(datasette, actor, method, path, payload=None):
    """Make one API call, surfacing the API's own error message.

    The API's errors are written for a person -- "Cloud processing needs an
    explicit per-request opt-in", not "400" -- so they are passed through
    rather than replaced with something generic.
    """
    token = _token(datasette, actor)
    if not token:
        raise ApiError(500, "No Orpheus API token is configured for this user.")

    url = _api_base(datasette) + path
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if data:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        message = raw
        try:
            message = json.loads(raw)["error"]["message"]
        except Exception:
            pass
        raise ApiError(exc.code, message)
    except urllib.error.URLError as exc:
        raise ApiError(
            503,
            "Could not reach the Orpheus API at {}: {}".format(_api_base(datasette), exc.reason),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@hookimpl
def register_routes():
    return [
        (r"^/-/orpheus$", index_page),
        (r"^/-/orpheus/upload$", upload),
        (r"^/-/orpheus/document/(?P<document_id>[^/]+)$", document_page),
        (r"^/-/orpheus/review$", review),
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
    return [{
        "href": datasette.urls.path("/-/orpheus"),
        "label": "Add a document",
        "description": "Ingest and extract a new document through the Orpheus API",
    }]


async def _render(datasette, request, template, context, status=200):
    return Response.html(
        await datasette.render_template(template, context, request=request),
        status=status,
    )


async def index_page(datasette, request):
    actor = request.actor
    if not actor:
        return Response.text("Sign in to use Orpheus.", status=403)

    error = request.args.get("error")
    context = {"documents": [], "capabilities": None, "error": error,
               "uploaded": request.args.get("uploaded")}
    try:
        context["documents"] = _call(datasette, actor, "GET", "/documents")
        context["capabilities"] = _call(datasette, actor, "GET", "/capabilities")
    except ApiError as exc:
        context["error"] = context["error"] or exc.message
    return await _render(datasette, request, "orpheus_index.html", context)


async def upload(datasette, request):
    """Ingest a document, then extract from it.

    Two API calls rather than one, because they fail differently and a person
    needs to know which happened. A document that ingested but failed to
    extract is still there and still worth retrying; saying only "upload
    failed" would send them to re-upload a file that is already stored.
    """
    actor = request.actor
    if not actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return Response.redirect(datasette.urls.path("/-/orpheus"))

    if hasattr(request, "form"):  # Datasette 1.0+
        max_bytes = int(_config(datasette).get("max_file_size", 50 * 1024 * 1024))
        try:
            # Datasette parses the multipart itself, with limits on file size,
            # request size and free disk. Reimplementing that would mean
            # reimplementing its guards too, badly.
            form = await request.form(files=True, max_file_size=max_bytes)
        except BadRequest as exc:
            # Request.form() turns every parse and limit failure - file too
            # large, request too large, disk too full - into BadRequest, which
            # Datasette would otherwise render as a bare 400 page. Say it on
            # the form instead.
            return Response.redirect(
                datasette.urls.path("/-/orpheus") + "?error=" + urllib.parse.quote(
                    "Upload rejected: {}".format(exc)))
        uploaded = form.get("file")
    else:
        # An older Datasette has no multipart parser. The path field still
        # works, so the page degrades rather than breaking.
        form = await request.post_vars()
        uploaded = None

    path = (form.get("path") or "").strip()
    tier = form.get("tier") or "local"
    cloud_opt_in = form.get("cloud_opt_in") == "on"

    has_file = uploaded is not None and getattr(uploaded, "filename", "")
    if not has_file and not path:
        return Response.redirect(
            datasette.urls.path("/-/orpheus") + "?error=" + urllib.parse.quote(
                "Choose a file, or give a path already on the server."
                if hasattr(request, "form") else
                "Give the server-side path of a document to ingest; "
                "file upload needs Datasette 1.0 or newer."))

    try:
        if has_file:
            data = await uploaded.read()
            await uploaded.close()
            document = _call_upload(datasette, actor, uploaded.filename,
                                    uploaded.content_type, data)
        else:
            # Still supported: a watched drop-directory hands the API a path
            # rather than pushing bytes through a browser.
            document = _call(datasette, actor, "POST", "/documents", {"path": path})
    except ApiError as exc:
        return Response.redirect(
            datasette.urls.path("/-/orpheus") + "?error=" + urllib.parse.quote(
                "Ingest failed: " + exc.message))

    document_id = document.get("document_id")
    target = datasette.urls.path("/-/orpheus/document/" + document_id)

    if document.get("duplicate"):
        return Response.redirect(target + "?note=" + urllib.parse.quote(
            "That content was already ingested; showing the existing document."))

    try:
        _call(datasette, actor, "POST", "/documents/{}/classify".format(document_id))
    except ApiError:
        # Classification is a convenience. Losing it should not stop the user
        # reaching a document that ingested successfully.
        pass

    try:
        _call(datasette, actor, "POST", "/documents/{}/extract".format(document_id),
              {"tier": tier, "cloud_opt_in": cloud_opt_in})
    except ApiError as exc:
        return Response.redirect(target + "?error=" + urllib.parse.quote(
            "Ingested, but extraction failed: " + exc.message))

    return Response.redirect(target + "?uploaded=1")


async def document_page(datasette, request):
    actor = request.actor
    if not actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    # Route captures arrive on request.url_vars rather than as handler
    # parameters; declaring them as parameters makes Datasette refuse to call
    # the handler at all.
    document_id = request.url_vars.get("document_id")
    if not document_id:
        return Response.redirect(datasette.urls.path("/-/orpheus"))

    context = {"document_id": document_id, "note": request.args.get("note"),
               "error": request.args.get("error"), "uploaded": request.args.get("uploaded"),
               "document": None, "instances": [], "review": None}
    try:
        context["document"] = _call(datasette, actor, "GET", "/documents/" + document_id)
        context["instances"] = _call(
            datasette, actor, "GET", "/documents/{}/instances".format(document_id))
        context["review"] = _call(
            datasette, actor, "GET", "/documents/{}/review".format(document_id))
    except ApiError as exc:
        context["error"] = context["error"] or exc.message
        return await _render(datasette, request, "orpheus_document.html", context,
                             status=exc.status if exc.status in (403, 404) else 200)

    for instance in context["instances"]:
        # Properties arrive as a JSON string so one payload can carry rows from
        # differently-shaped tables. Parsed here so the template stays simple.
        try:
            instance["parsed"] = json.loads(instance.get("properties") or "{}")
        except (TypeError, ValueError):
            instance["parsed"] = {}
    return await _render(datasette, request, "orpheus_document.html", context)


async def review(datasette, request):
    """Confirm, amend or reject one extracted instance."""
    actor = request.actor
    if not actor:
        return Response.text("Sign in to use Orpheus.", status=403)
    if request.method != "POST":
        return Response.redirect(datasette.urls.path("/-/orpheus"))

    post = await request.post_vars()
    instance_id = post.get("instance_id")
    document_id = post.get("document_id")
    action = post.get("action")
    target = datasette.urls.path("/-/orpheus/document/" + (document_id or ""))

    if action not in ("confirm", "amend", "reject"):
        return Response.redirect(target + "?error=" + urllib.parse.quote(
            "Unknown review action."))

    payload = None
    if action == "amend":
        changes = {}
        for key, value in post.items():
            if key.startswith("change_") and value != "":
                changes[key[len("change_"):]] = value
        if not changes:
            return Response.redirect(target + "?error=" + urllib.parse.quote(
                "Nothing was changed."))
        payload = {"changes": changes, "note": post.get("note") or None}
    elif action == "reject":
        payload = {"note": post.get("note") or None}

    try:
        _call(datasette, actor, "POST",
              "/instances/{}/{}".format(instance_id, action), payload)
    except ApiError as exc:
        return Response.redirect(target + "?error=" + urllib.parse.quote(exc.message))

    return Response.redirect(target + "?note=" + urllib.parse.quote(
        "Marked {}.".format(action + "ed" if action != "amend" else "amended")))
