"""The HTTP surface, as a dispatch table over the core.

Deliberately transport-agnostic: `handle()` takes a method, a path and a body
and returns `(status, payload)`. The Datasette plugin mounts it under
`/-/orpheus/api/`, a test calls it directly, and neither has to stand up a
server. It is not a second service — Datasette is the writer, and this runs
inside it.

Two things every route goes through, and the reason there is a single dispatch
table rather than a router per module:

**Authentication and per-document permission are checked here, once.** A route
that forgot would not fail; it would quietly serve someone else's document.

**Errors become status codes with the message the core wrote.** Those messages
are written for a person — they say what to do next — so they are surfaced
verbatim rather than replaced with a generic string.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import analysis, auth, bundle as bundle_mod, classify, concepts
from . import engines, extract as extract_mod, ingest as ingest_mod
from . import llm, quality, review, textract
from .store import Store
from .utils import NotFound, OrpheusError, PermissionDenied

# (method, pattern, handler, permission) — permission is the action required on
# the document named by the path, or None for routes that are not about one.
Route = tuple[str, re.Pattern, Callable, str | None]

_ROUTES: list[Route] = []


def route(method: str, pattern: str, permission: str | None = None):
    def register(fn):
        _ROUTES.append((method, re.compile(f"^{pattern}$"), fn, permission))
        return fn
    return register


class ApiError(OrpheusError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def handle(store: Store, method: str, path: str, body: dict | None = None,
           token: str | None = None, actor: dict | None = None) -> tuple[int, Any]:
    """Dispatch one request. Returns `(status, payload)`."""
    body = body or {}
    path = "/" + path.strip("/")

    for route_method, pattern, handler, permission in _ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if not match:
            continue

        if handler.__dict__.get("public"):
            return _run(handler, store, match, body, None)

        resolved = actor or auth.authenticate(store, token)
        if resolved is None:
            return 401, {"error": {"message": "Authentication required."}}

        if permission:
            document_id = match.group("document_id")
            if not auth.can(store, resolved, document_id, permission):
                # Deliberately the same answer whether the document does not
                # exist or is simply not theirs: distinguishing them tells an
                # actor which documents exist.
                return 403, {"error": {
                    "message": f"Not permitted to {permission} document {document_id}."}}
        return _run(handler, store, match, body, resolved)

    return 404, {"error": {"message": f"No route for {method} {path}."}}


def _run(handler, store, match, body, actor) -> tuple[int, Any]:
    try:
        result = handler(store=store, actor=actor, body=body, **match.groupdict())
    except PermissionDenied as exc:
        return 403, {"error": {"message": str(exc)}}
    except NotFound as exc:
        return 404, {"error": {"message": str(exc)}}
    except ApiError as exc:
        return exc.status, {"error": {"message": str(exc)}}
    except OrpheusError as exc:
        # The core writes its messages for a person, so they are surfaced
        # verbatim rather than replaced with something generic.
        return 400, {"error": {"message": str(exc)}}
    if isinstance(result, tuple):
        return result
    return 200, result


def public(fn):
    fn.public = True
    return fn


def _actor_id(actor: dict | None) -> str | None:
    return actor.get("actor_id") if actor else None


# ---------------------------------------------------------------------------
# Open routes
# ---------------------------------------------------------------------------

@route("GET", "/health")
@public
def health(**_):
    return {"status": "ok"}


@route("GET", "/capabilities")
def capabilities(store, **_):
    return {
        "text_extraction": textract.capabilities(),
        "models": llm.status(store),
        "cloud": llm.cloud_policy(store),
        "extraction_engines": engines.available_engines(),
        "confidence_rubric": __import__("orpheus.rubric", fromlist=["CONFIDENCE"]).CONFIDENCE,
    }


@route("GET", "/bundle")
def get_bundle(store, **_):
    return bundle_mod.active(store) or bundle_mod.load()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@route("GET", "/documents")
def list_documents(store, actor, body, **_):
    return {"documents": auth.visible_documents(store, actor,
                                                limit=int(body.get("limit", 100)))}


@route("POST", "/documents")
def create_document(store, actor, body, **_):
    path = body.get("path")
    if not path:
        raise ApiError(400, "Give a `path` to ingest.")
    return ingest_mod.ingest(store, path, actor_id=_actor_id(actor),
                             storage_root=body.get("storage_root", "storage"),
                             filename=body.get("filename"),
                             visibility=body.get("visibility", "private"))


@route("GET", r"/documents/(?P<document_id>[^/]+)", permission="view")
def get_document(store, document_id, **_):
    document = ingest_mod.get_document(store, document_id)
    if document is None:
        raise NotFound(f"No document {document_id!r}.")
    return {**document, "review": review.review_progress(store, document_id)}


@route("GET", r"/documents/(?P<document_id>[^/]+)/text", permission="view")
def get_text(store, document_id, **_):
    return {"document_id": document_id,
            "pages": ingest_mod.document_pages(store, document_id)}


@route("POST", r"/documents/(?P<document_id>[^/]+)/classify", permission="edit")
def post_classify(store, document_id, actor, body, **_):
    return classify.classify(store, document_id, actor_id=_actor_id(actor),
                             tier=body.get("tier", "local"),
                             opt_in=bool(body.get("cloud_opt_in")))


@route("POST", r"/documents/(?P<document_id>[^/]+)/extract", permission="edit")
def post_extract(store, document_id, actor, body, **_):
    return extract_mod.extract(store, document_id, tier=body.get("tier", "local"),
                               actor_id=_actor_id(actor),
                               opt_in=bool(body.get("cloud_opt_in")),
                               deterministic=body.get("deterministic", True),
                               force=bool(body.get("force")),
                               engine_name=body.get("engine"))


@route("GET", r"/documents/(?P<document_id>[^/]+)/instances", permission="view")
def get_instances(store, document_id, body, **_):
    return {"instances": extract_mod.document_instances(
        store, document_id, type_id=body.get("type_id"),
        include_rejected=bool(body.get("include_rejected")))}


@route("GET", r"/documents/(?P<document_id>[^/]+)/history", permission="view")
def get_history(store, document_id, **_):
    from .audit import document_history
    return {"history": document_history(store, document_id)}


@route("POST", r"/documents/(?P<document_id>[^/]+)/review", permission="edit")
def post_document_review(store, document_id, actor, body, **_):
    return review.mark_document_reviewed(store, document_id, _actor_id(actor),
                                         reviewed=body.get("reviewed", True))


@route("POST", r"/documents/(?P<document_id>[^/]+)/share", permission="share")
def post_share(store, document_id, actor, body, **_):
    return {"share": auth.share_document(store, document_id, body["actor_id"],
                                         body.get("role", "viewer"), actor)}


@route("POST", r"/documents/(?P<document_id>[^/]+)/visibility", permission="share")
def post_visibility(store, document_id, actor, body, **_):
    return {"visibility": auth.set_visibility(store, document_id,
                                              body["visibility"], _actor_id(actor))}


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def _instance_permission(store, actor, instance_id) -> str:
    location = review.locate_instance(store, instance_id)
    auth.require(store, actor, location["document_id"], "edit")
    return location["document_id"]


@route("POST", r"/instances/(?P<instance_id>[^/]+)/confirm")
def post_confirm(store, instance_id, actor, body, **_):
    _instance_permission(store, actor, instance_id)
    return {"instance_id": review.confirm_instance(store, instance_id,
                                                   _actor_id(actor),
                                                   note=body.get("note"))}


@route("POST", r"/instances/(?P<instance_id>[^/]+)/amend")
def post_amend(store, instance_id, actor, body, **_):
    _instance_permission(store, actor, instance_id)
    changes = body.get("changes")
    if not changes:
        raise ApiError(400, "Give `changes` as a mapping of property to value.")
    return {"instance_id": review.amend_instance(store, instance_id, changes,
                                                 _actor_id(actor),
                                                 note=body.get("note"))}


@route("POST", r"/instances/(?P<instance_id>[^/]+)/reject")
def post_reject(store, instance_id, actor, body, **_):
    _instance_permission(store, actor, instance_id)
    return {"instance_id": review.reject_instance(store, instance_id,
                                                  _actor_id(actor),
                                                  note=body.get("note"))}


@route("GET", "/schema-amendments")
def get_amendments(store, body, **_):
    return {"amendments": review.schema_amendments(
        store, status=body.get("status", "pending"))}


@route("POST", r"/schema-amendments/(?P<amendment_id>[^/]+)/review")
def post_amendment_review(store, amendment_id, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Accepting a schema amendment changes the bundle for every "
            "document, so it is an administrator decision.")
    return review.review_schema_amendment(store, amendment_id,
                                          body.get("decision", "rejected"),
                                          _actor_id(actor), note=body.get("note"))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@route("POST", r"/documents/(?P<document_id>[^/]+)/concepts/evaluate",
       permission="edit")
def post_evaluate(store, document_id, actor, **_):
    return {"results": concepts.evaluate_concepts(store, document_id,
                                                  actor_id=_actor_id(actor))}


@route("POST", r"/documents/(?P<document_id>[^/]+)/score", permission="edit")
def post_score(store, document_id, actor, body, **_):
    return concepts.evaluate_score(store, document_id, body.get("score_id"),
                                   actor_id=_actor_id(actor))


@route("GET", r"/documents/(?P<document_id>[^/]+)/evaluations", permission="view")
def get_evaluations(store, document_id, body, **_):
    return {"evaluations": concepts.document_evaluations(
        store, document_id, kind=body.get("kind"),
        include_stale=body.get("include_stale", True))}


@route("POST", r"/evaluations/(?P<evaluation_id>[^/]+)/review")
def post_evaluation_review(store, evaluation_id, actor, body, **_):
    return {"evaluation_id": concepts.review_evaluation(
        store, evaluation_id, body.get("status", "confirmed"),
        _actor_id(actor), note=body.get("note"))}


@route("POST", r"/documents/(?P<document_id>[^/]+)/corpus-analysis",
       permission="edit")
def post_corpus_analysis(store, document_id, actor, body, **_):
    return analysis.corpus_analysis(store, document_id, actor_id=_actor_id(actor),
                                    narrate=bool(body.get("narrate")),
                                    tier=body.get("tier", "cloud"),
                                    opt_in=bool(body.get("cloud_opt_in")))


# ---------------------------------------------------------------------------
# Quality and administration
# ---------------------------------------------------------------------------

@route("GET", "/quality")
def get_quality(store, body, **_):
    return quality.quality_report(store,
                                  min_reviewed=int(body.get("min_reviewed", 5)))


@route("GET", r"/documents/(?P<document_id>[^/]+)/quality", permission="view")
def get_document_quality(store, document_id, body, **_):
    return quality.quality_report(store, document_id,
                                  min_reviewed=int(body.get("min_reviewed", 1)))


@route("GET", "/concept-parameters")
def get_concept_parameters(store, **_):
    return {"parameters": concepts.concept_parameters(store)}


@route("POST", "/admin/concept-parameters")
def post_concept_parameter(store, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied("Changing a concept threshold is an "
                               "administrator decision.")
    return {"result": concepts.set_concept_parameter(
        store, body["template_id"], body["parameter"], body["value"],
        _actor_id(actor))}


@route("POST", "/admin/concepts/setup")
def post_setup_concepts(store, actor, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied("Registering concepts is an administrator decision.")
    return {"concepts": concepts.setup_concepts(store, actor_id=_actor_id(actor)),
            "scores": concepts.setup_scores(store, actor_id=_actor_id(actor))}


@route("POST", "/admin/settings")
def post_setting(store, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied("Changing a setting is an administrator decision.")
    store.set_setting(body["key"], body["value"], _actor_id(actor))
    return {"key": body["key"], "value": body["value"]}


@route("GET", "/audit/llm")
def get_llm_audit(store, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied("The model audit log is an administrator view.")
    return {"calls": llm.cloud_calls(store, document_id=body.get("document_id"))}
