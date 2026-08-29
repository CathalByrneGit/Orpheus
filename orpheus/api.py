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
from . import entities as entities_mod
from . import engines, extract as extract_mod, ingest as ingest_mod
from . import export_md
from . import companion as companion_mod
from . import corroboration as corroboration_mod
from . import graph as graph_mod
from . import ontology
from . import registers as registers_mod
from . import questions as questions_mod
from . import lint as lint_mod
from . import llm, quality, review, rubric, tensions as tensions_mod
from . import textract
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


def _int(body: dict, key: str, default: int) -> int:
    """A number out of a query string, or a message saying it was not one.

    Every count and depth on this surface arrives as text, and `int("")` --
    which is what an empty form field sends -- is a traceback, not an answer.
    A caller who mistypes a limit should be told so.
    """
    raw = body.get(key, default)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} must be a whole number, not {raw!r}.")


# ---------------------------------------------------------------------------
# Open routes
# ---------------------------------------------------------------------------

@route("GET", "/health")
@public
def health(**_):
    return {"status": "ok"}


@route("GET", "/capabilities")
def capabilities(store, **_):
    """What this install can do, without doing any of it.

    Includes the active bundle, because "what can this store do" is only half
    answered by the engines: the other half is which ontology is loaded, and a
    store serving a planning bundle answers different questions from one
    serving contracts.
    """
    active = bundle_mod.active(store)
    return {
        "text_extraction": textract.capabilities(),
        "models": llm.status(store),
        "cloud": llm.cloud_policy(store),
        "extraction_engines": engines.available_engines(),
        "bundle": _bundle_summary(active),
        "confidence_rubric": rubric.CONFIDENCE,
    }


def _bundle_summary(active: dict | None) -> dict | None:
    if not active:
        return None
    metadata = active.get("metadata") or {}
    return {"bundle_id": active.get("bundleId"),
            "version": active.get("bundleVersion"),
            "name": metadata.get("name"),
            "object_types": [o["id"] for o in
                             bundle_mod.managed_object_types(active)]}


@route("GET", "/bundle")
def get_bundle(store, **_):
    return bundle_mod.active(store) or bundle_mod.load()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@route("GET", "/documents")
def list_documents(store, actor, body, **_):
    return {"documents": auth.visible_documents(store, actor,
                                                limit=_int(body, "limit", 100))}


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
    # The runs come with the document because "what has been tried on this, and
    # did any of it fail" is part of reading it. A partial run -- deterministic
    # findings kept, model pass failed -- is otherwise invisible to anyone who
    # arrives after the person who started it.
    runs = store.query(
        "SELECT run_id, tier, status, error, n_entities, started_at, finished_at "
        "FROM extraction_runs WHERE document_id = ? ORDER BY started_at DESC",
        (document_id,))
    return {**document, "review": review.review_progress(store, document_id),
            "runs": [dict(r) for r in runs]}


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
# Entities: the wiki
# ---------------------------------------------------------------------------

@route("GET", "/entities")
def get_entities(store, body, **_):
    return {"entities": entities_mod.list_entities(
        store, type_id=body.get("type_id"), status=body.get("status"),
        query=body.get("q"), limit=_int(body, "limit", 100))}


@route("POST", "/entities")
def post_entity(store, actor, body, **_):
    if not body.get("type_id") or not body.get("canonical_name"):
        raise ApiError(400, "Give `type_id` and `canonical_name`.")
    entity_id = entities_mod.create_entity(
        store, body["type_id"], body["canonical_name"],
        actor_id=_actor_id(actor), description=body.get("description"))
    return {"entity_id": entity_id}


@route("GET", "/entities/duplicates")
def get_duplicate_pages(store, body, **_):
    """Pages that look like one thing. The queue cannot show these: every
    mention has a home, so the split is invisible exactly when the machine has
    finished."""
    return {"pairs": entities_mod.duplicate_pages(
        store, type_id=body.get("type_id"), limit=_int(body, "limit", 50))}


@route("GET", r"/entities/(?P<entity_id>[^/]+)")
def get_entity_page(store, entity_id, body, **_):
    """The page. A projection of mentions, so every line carries a source."""
    return entities_mod.entity_page(
        store, entity_id,
        include_unconfirmed=body.get("include_unconfirmed", "1") not in
        ("0", "false", "False", False))


@route("POST", r"/entities/(?P<entity_id>[^/]+)/rename")
def post_entity_rename(store, entity_id, actor, body, **_):
    if not body.get("canonical_name"):
        raise ApiError(400, "Give `canonical_name`.")
    entities_mod.rename_entity(store, entity_id, body["canonical_name"],
                               _actor_id(actor), note=body.get("note"))
    return {"entity_id": entity_id}


@route("POST", r"/entities/(?P<entity_id>[^/]+)/describe")
def post_entity_describe(store, entity_id, actor, body, **_):
    entities_mod.describe_entity(store, entity_id, body.get("description") or "",
                                 _actor_id(actor), note=body.get("note"))
    return {"entity_id": entity_id}


@route("POST", r"/entities/(?P<entity_id>[^/]+)/confirm")
def post_entity_confirm(store, entity_id, actor, body, **_):
    entities_mod.confirm_entity(store, entity_id, _actor_id(actor),
                                note=body.get("note"))
    return {"entity_id": entity_id, "status": "confirmed"}


@route("POST", r"/entities/(?P<entity_id>[^/]+)/reject")
def post_entity_reject(store, entity_id, actor, body, **_):
    entities_mod.reject_entity(store, entity_id, _actor_id(actor),
                               note=body.get("note"))
    return {"entity_id": entity_id, "status": "rejected"}


@route("POST", r"/entities/(?P<entity_id>[^/]+)/merge")
def post_entity_merge(store, entity_id, actor, body, **_):
    if not body.get("merge_id"):
        raise ApiError(400, "Give `merge_id`: the entity being merged away.")
    return entities_mod.merge_entities(store, entity_id, body["merge_id"],
                                       _actor_id(actor), note=body.get("note"))


@route("POST", r"/entities/(?P<entity_id>[^/]+)/mentions")
def post_entity_link(store, entity_id, actor, body, **_):
    if not body.get("instance_id"):
        raise ApiError(400, "Give `instance_id`.")
    return entities_mod.link_mention(
        store, entity_id, body["instance_id"], actor_id=_actor_id(actor),
        basis=body.get("basis", "human"), note=body.get("note"))


@route("POST", r"/entities/(?P<entity_id>[^/]+)/mentions/(?P<instance_id>[^/]+)/confirm")
def post_link_confirm(store, entity_id, instance_id, actor, body, **_):
    return entities_mod.confirm_link(store, entity_id, instance_id,
                                     _actor_id(actor), note=body.get("note"))


@route("POST", r"/entities/(?P<entity_id>[^/]+)/mentions/(?P<instance_id>[^/]+)/unlink")
def post_link_unlink(store, entity_id, instance_id, actor, body, **_):
    return entities_mod.unlink_mention(store, entity_id, instance_id,
                                       _actor_id(actor), note=body.get("note"))


@route("POST", "/entities/propose")
def post_entities_propose(store, actor, body, **_):
    """Turn a pile of mentions into a reviewable queue.

    Everything it makes is unconfirmed and linked on the weakest basis there
    is. It decides nothing; a person confirming a page is what makes it real.
    """
    return entities_mod.propose_entities(store, type_id=body.get("type_id"),
                                         actor_id=_actor_id(actor))


@route("GET", "/mentions/unlinked")
def get_unlinked_mentions(store, body, **_):
    return {"mentions": entities_mod.unlinked_mentions(
        store, type_id=body.get("type_id"), document_id=body.get("document_id"),
        limit=_int(body, "limit", 200))}


@route("GET", r"/mentions/(?P<instance_id>[^/]+)/candidates")
def get_mention_candidates(store, instance_id, body, **_):
    return {"instance_id": instance_id,
            "candidates": entities_mod.candidates_for_mention(
                store, instance_id, limit=_int(body, "limit", 10))}


@route("GET", r"/documents/(?P<document_id>[^/]+)/entities", permission="view")
def get_document_entities(store, document_id, **_):
    return {"entities": entities_mod.entities_in_document(store, document_id)}


# ---------------------------------------------------------------------------
# Tensions: conflicts that survive review
# ---------------------------------------------------------------------------
#
# Registered before nothing and after the entity routes on purpose: the paths
# here are all `/tensions...`, which no earlier pattern can swallow. The entity
# routes learned that lesson the hard way -- `/entities/duplicates` registered
# after `/entities/<id>` is a page called "duplicates".

@route("GET", "/tensions")
def get_tensions(store, body, **_):
    return {"tensions": tensions_mod.list_tensions(
        store, scope=body.get("scope"), subject_id=body.get("subject_id"),
        status=body.get("status"), kind=body.get("kind"),
        open_only=body.get("standing") in ("1", "true", "True", True),
        limit=_int(body, "limit", 200))}


@route("POST", "/tensions")
def post_tension(store, actor, body, **_):
    """Record a conflict. Needs at least two cited sides, and says why if not."""
    if not body.get("kind") or not body.get("summary"):
        raise ApiError(400, "Give `kind` and `summary`.")
    sides = body.get("sides") or []
    if isinstance(sides, str):
        sides = [s.strip() for s in sides.split(",") if s.strip()]
    tension_id = tensions_mod.raise_tension(
        store, kind=body["kind"], summary=body["summary"], sides=sides,
        actor_id=_actor_id(actor), scope=body.get("scope", "entity"),
        subject_id=body.get("subject_id"), property_id=body.get("property_id"),
        detail=body.get("detail"))
    return {"tension_id": tension_id}


@route("GET", "/tensions/conflicts")
def get_conflicts(store, body, **_):
    """Properties whose reviewed mentions disagree, whether or not recorded.

    Reads only. Turning these into rows is `POST /tensions/propose`, because
    the machine can see that two values differ and cannot see whether the
    difference matters.
    """
    return {"conflicts": tensions_mod.detect_conflicts(
        store, entity_id=body.get("entity_id"), type_id=body.get("type_id"),
        reviewed_only=body.get("reviewed_only", "1") not in
        ("0", "false", "False", False))}


@route("POST", "/tensions/propose")
def post_tensions_propose(store, actor, body, **_):
    return tensions_mod.propose_tensions(
        store, actor_id=_actor_id(actor), entity_id=body.get("entity_id"),
        type_id=body.get("type_id"),
        reviewed_only=body.get("reviewed_only", "1") not in
        ("0", "false", "False", False))


@route("GET", r"/tensions/(?P<tension_id>tns_[^/]+)")
def get_tension(store, tension_id, **_):
    return tensions_mod.get_tension(store, tension_id)


@route("POST", r"/tensions/(?P<tension_id>tns_[^/]+)/accept")
def post_tension_accept(store, tension_id, actor, body, **_):
    """The conflict is real and it stands. A finished piece of review work."""
    return tensions_mod.accept_tension(
        store, tension_id, _actor_id(actor), note=body.get("note"))


@route("POST", r"/tensions/(?P<tension_id>tns_[^/]+)/resolve")
def post_tension_resolve(store, tension_id, actor, body, **_):
    if not body.get("resolution"):
        raise ApiError(400, "Give `resolution` -- a resolved tension with no "
                            "account of the reasoning looks decided and cannot "
                            "be checked.")
    return tensions_mod.resolve_tension(
        store, tension_id, _actor_id(actor), body["resolution"])


@route("POST", r"/tensions/(?P<tension_id>tns_[^/]+)/withdraw")
def post_tension_withdraw(store, tension_id, actor, body, **_):
    if not body.get("reason"):
        raise ApiError(400, "Give `reason`.")
    return tensions_mod.withdraw_tension(
        store, tension_id, _actor_id(actor), body["reason"])


@route("GET", r"/documents/(?P<document_id>[^/]+)/tensions", permission="view")
def get_document_tensions(store, document_id, **_):
    """Every tension this document is a side of, however it is scoped."""
    return {"tensions": tensions_mod.tensions_for_document(store, document_id)}


# ---------------------------------------------------------------------------
# Reading with the machine, a passage at a time
# ---------------------------------------------------------------------------

@route("POST", r"/documents/(?P<document_id>[^/]+)/passages/(?P<page_no>\d+)/read",
       permission="view")
def post_read_passage(store, document_id, page_no, actor, body, **_):
    """Offer what this page seems to hold, and record that it was read.

    A POST guarded by `view` rather than `edit`, which looks odd and is right:
    it writes, but what it writes is a fact about the reader's own progress and
    a set of proposals, not a change to the document. Requiring `edit` would
    stop a viewer from reading with the companion at all.
    """
    return companion_mod.read_passage(
        store, document_id, int(page_no), actor_id=_actor_id(actor),
        engine=body.get("engine", companion_mod.DEFAULT_ENGINE),
        tier=body.get("tier", "local"),
        opt_in=body.get("cloud_opt_in") in ("1", "true", "True", True),
        context_chars=_int(body, "context_chars",
                           companion_mod.DEFAULT_CONTEXT_CHARS))


@route("GET", r"/documents/(?P<document_id>[^/]+)/passages/(?P<page_no>\d+)",
       permission="view")
def get_passage(store, document_id, page_no, body, **_):
    return companion_mod.passage(store, document_id, int(page_no),
                                 status=body.get("status", "offered"))


@route("GET", r"/documents/(?P<document_id>[^/]+)/reading", permission="view")
def get_reading_progress(store, document_id, actor, **_):
    return companion_mod.reading_progress(store, document_id,
                                          actor_id=_actor_id(actor))


def _suggestion_permission(store, actor, suggestion_id) -> str:
    """A suggestion is decided on the document it came from."""
    row = store.one("SELECT document_id FROM suggestions WHERE suggestion_id = ?",
                    (suggestion_id,))
    if row is None:
        raise NotFound(f"No suggestion {suggestion_id!r}.")
    if not auth.can(store, actor, row["document_id"], "edit"):
        raise PermissionDenied("Not permitted to edit that document.")
    return row["document_id"]


@route("POST", r"/suggestions/(?P<suggestion_id>sug_[^/]+)/accept")
def post_accept_suggestion(store, suggestion_id, actor, body, **_):
    """Record it. Properties given here correct it on the way in."""
    _suggestion_permission(store, actor, suggestion_id)
    return companion_mod.accept_suggestion(
        store, suggestion_id, _actor_id(actor),
        properties=body.get("properties") or None, note=body.get("note"))


@route("POST", r"/suggestions/(?P<suggestion_id>sug_[^/]+)/dismiss")
def post_dismiss_suggestion(store, suggestion_id, actor, body, **_):
    _suggestion_permission(store, actor, suggestion_id)
    return companion_mod.dismiss_suggestion(
        store, suggestion_id, _actor_id(actor), note=body.get("note"))


@route("GET", "/suggestions/quality")
def get_suggestion_quality(store, actor, body, **_):
    """How often the companion was right. Separate from extraction quality.

    That measures extraction against review; this measures offers against a
    person's attention. Mixing them would answer neither.
    """
    document_id = body.get("document_id")
    if document_id:
        if not auth.can(store, actor, document_id, "view"):
            raise PermissionDenied(f"Not permitted to view {document_id}.")
    elif not actor.get("is_admin"):
        raise PermissionDenied(
            "Corpus-wide suggestion quality spans documents you may not be "
            "able to read. Pass `document_id` for one you can.")
    return companion_mod.suggestion_quality(store, document_id)


# ---------------------------------------------------------------------------
# The corpus as a network
# ---------------------------------------------------------------------------
#
# All of these project instance-level rows up to entity pages, so what they can
# see is bounded by how much of the wiki has been built. Every one of them
# carries `coverage` for that reason.

@route("GET", "/graph/topology")
def get_topology(store, actor, body, **_):
    """The whole structural picture. Administrator only.

    Spans every document in the store, including ones this actor cannot read --
    the same reason `/quality` is administrator-only. A neighbourhood around one
    page is the scoped view and needs no such thing.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "The corpus topology spans documents you may not be able to read, "
            "so it is an administrator view. GET /graph/entities/<id> is scoped "
            "to one page and its neighbours.")
    return graph_mod.topology(
        store, seed=_int(body, "seed", graph_mod.DEFAULT_SEED),
        reviewed_only=body.get("reviewed_only") in ("1", "true", "True", True))


# ---------------------------------------------------------------------------
# Registers
# ---------------------------------------------------------------------------

@route("GET", "/registers")
def list_registers(store, actor, body, **_):
    """Every register and whether anybody has vouched for it."""
    return {"registers": registers_mod.list_registers(store),
            "reading": ("A `staged` register is readable and is not evidence. "
                        "Only an `active` one bears on a merge.")}


@route("GET", r"/registers/(?P<register_id>[^/]+)")
def get_register(store, register_id, actor, body, **_):
    return {"register": registers_mod.get_register(store, register_id),
            "rows": registers_mod.rows(store, register_id,
                                       status=body.get("status"),
                                       limit=_int(body, "limit", 100))}


@route("POST", r"/registers/(?P<register_id>[^/]+)/rows/(?P<row_no>\d+)/reject")
def reject_register_row(store, register_id, row_no, actor, body, **_):
    """Mark one row as not to be used. It stays readable."""
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Reference data everybody's answers rest on is an administrator's "
            "to vouch for.")
    return registers_mod.review_row(
        store, register_id, int(row_no), "rejected", note=body.get("note"),
        actor_id=_actor_id(actor))


@route("POST", r"/registers/(?P<register_id>[^/]+)/promote")
def promote_register(store, register_id, actor, body, **_):
    """Vouch for a register, and let it count as evidence.

    Administrator-only, and not because the rows are sensitive. A register is
    reference data every later answer rests on, so the person who says it is
    good is taking responsibility for what it decides.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Reference data everybody's answers rest on is an administrator's "
            "to vouch for.")
    return registers_mod.promote(store, register_id,
                                 actor_id=_actor_id(actor),
                                 note=body.get("note"))


@route("POST", r"/registers/(?P<register_id>[^/]+)/withdraw")
def withdraw_register(store, register_id, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Reference data everybody's answers rest on is an administrator's "
            "to vouch for.")
    return registers_mod.withdraw(store, register_id,
                                  actor_id=_actor_id(actor),
                                  note=body.get("note"))


# ---------------------------------------------------------------------------
# The ontology itself
# ---------------------------------------------------------------------------

@route("GET", "/ontology/candidates")
def get_ontology_candidates(store, actor, body, **_):
    """What a survey proposed, most-supported first."""
    return {"candidates": ontology.candidates(
        store, status=body.get("status", "proposed"),
        kind=body.get("kind") or None),
        "reading": ("`n_documents` of `n_sampled` is how many documents show "
                    "this, counted rather than claimed. It is not a "
                    "confidence: the model is never asked how sure it is.")}


@route("POST", "/ontology/survey")
def post_ontology_survey(store, actor, body, **_):
    """Read a sample of the corpus and propose what it seems to be about.

    Administrator-only, and for a different reason from the other admin routes.
    A survey reads across every document in the sample, including ones this
    actor may not be allowed to read -- but the decisive reason is what it is
    *for*: the queue it fills is the input to a decision that shapes every row
    the store will ever hold.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "A survey reads across the whole corpus and proposes the shape of "
            "everything that will be stored in it, so it is an administrator "
            "decision.")
    return ontology.survey(
        store, engine=body.get("engine") or ontology.DEFAULT_ENGINE,
        sample=_int(body, "sample", ontology.DEFAULT_SAMPLE),
        actor_id=_actor_id(actor), tier=body.get("tier", "local"),
        opt_in=bool(body.get("cloud_opt_in")),
        min_support=_int(body, "min_support", ontology.DEFAULT_MIN_SUPPORT),
        document_ids=body.get("document_ids") or None,
        primary_type=body.get("primary_type") or ontology.DEFAULT_PRIMARY_TYPE,
        chars_per_document=_int(body, "chars_per_document",
                                ontology.DEFAULT_CHARS_PER_DOCUMENT))


@route("POST", r"/ontology/candidates/(?P<candidate_id>[^/]+)/review")
def post_ontology_review(store, candidate_id, actor, body, **_):
    """Accept, rename or reject one proposal.

    `accepted_as` renames it. That is the ordinary accepting move, not an edge
    case: a survey notices that something recurs and has no way to know what it
    is called.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Accepting an object type decides the shape of every row that will "
            "ever be filed under it, so it is an administrator decision.")
    return ontology.review_candidate(
        store, candidate_id, body.get("decision", "rejected"),
        _actor_id(actor), accepted_as=body.get("accepted_as"),
        note=body.get("note"))


@route("POST", "/ontology/draft")
def post_ontology_draft(store, actor, body, **_):
    """Assemble a bundle from what was accepted, and return it.

    Returns the bundle; it does not register it. Registering an ontology is a
    deliberate act with a deployment behind it, and a drafting route that also
    installed it would be the one place in this API where an ontology arrived
    in a store without anybody choosing it.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Drafting a bundle is an administrator decision.")
    return ontology.draft_bundle(
        store, body.get("bundle_id") or "drafted-core",
        bundle_version=body.get("bundle_version") or "0.1.0",
        name=body.get("name"), description=body.get("description"),
        primary_type=body.get("primary_type") or None,
        document_types=body.get("document_types") or None,
        document_scoped=body.get("document_scoped") or None)


@route("GET", "/graph/map")
def get_graph_map(store, actor, body, **_):
    """Nodes and edges together, for drawing.

    The other graph routes answer questions in prose or in rows. This one is
    the shape a picture needs, and it is the same data: no separate projection,
    so a map cannot show a relation the text views would not.

    Centred on a page it is the scoped view and needs no special permission.
    Uncentred it spans every document in the store, including ones this actor
    cannot read, and is administrator-only for exactly the reason
    `/graph/topology` is.
    """
    entity_id = body.get("entity_id") or None
    reviewed_only = body.get("reviewed_only") in ("1", "true", "True", True)
    if not entity_id and not actor.get("is_admin"):
        raise PermissionDenied(
            "A map of the whole corpus spans documents you may not be able to "
            "read, so it is an administrator view. Pass entity_id to draw one "
            "page and its neighbours instead.")

    built = graph_mod.build(store, reviewed_only=reviewed_only)
    nodes, edges = built["nodes"], built["edges"]

    if entity_id:
        depth = max(1, min(_int(body, "depth", 2), 4))
        keep, frontier = {entity_id}, {entity_id}
        for _hop in range(depth):
            nxt = set()
            for node in frontier:
                nxt |= set(built["adjacency"].get(node, ()))
            frontier = nxt - keep
            keep |= nxt
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges
                 if e["from_entity_id"] in keep and e["to_entity_id"] in keep]

    return {
        "centre": entity_id,
        "nodes": list(nodes.values()),
        "edges": edges,
        "coverage": graph_mod.coverage(store),
        "caveat": ("The graph is a projection of the wiki, so its completeness "
                   "is the wiki's. Read `coverage` before reading the shape."),
    }


@route("GET", "/graph/edges")
def get_graph_edges(store, body, **_):
    """Canonical relations between pages, each with every source behind it."""
    return {"edges": graph_mod.canonical_edges(
        store, link_type_id=body.get("link_type_id"),
        reviewed_only=body.get("reviewed_only") in ("1", "true", "True", True)),
        "coverage": graph_mod.coverage(store)}


@route("GET", r"/graph/entities/(?P<entity_id>ent_[^/]+)")
def get_neighbourhood(store, entity_id, body, **_):
    """One page and what surrounds it. The view an agent or reviewer wants."""
    return graph_mod.neighbourhood(store, entity_id,
                                   depth=_int(body, "depth", 1))


@route("GET", "/graph/paths")
def get_paths(store, body, **_):
    """How two pages are connected, with the evidence for every hop.

    Each chain names its weakest link: one running through an unconfirmed
    machine guess is not the same finding as one a person has checked end to
    end, and reporting them alike invites the conclusion the store exists to
    prevent.
    """
    if not body.get("from") or not body.get("to"):
        raise ApiError(400, "Give `from` and `to` entity ids.")
    return graph_mod.paths_between(
        store, body["from"], body["to"],
        max_paths=_int(body, "max_paths", 5),
        max_length=_int(body, "max_length", 6))


@route("GET", "/graph/centrality")
def get_centrality(store, actor, body, **_):
    """Degree, and betweenness where networkx is installed."""
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Centrality is computed over the whole corpus, including documents "
            "you may not be able to read, so it is an administrator view.")
    sample = body.get("sample")
    return graph_mod.centrality(
        graph_mod.build(store), k=int(sample) if sample else None)


@route("GET", "/questions")
def get_questions(store, actor, body, **_):
    """Questions the shape of the corpus raises. Administrator only.

    Spans every document, like the topology it reads. Nothing here is a
    finding: each question names a chain, cites the documents behind each hop,
    and says how much of it anybody has confirmed.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "These questions are drawn from the whole corpus, including "
            "documents you may not be able to read, so it is an administrator "
            "view.")
    return questions_mod.raised(
        store, open_only=body.get("open_only") in ("1", "true", "True", True))


@route("POST", "/questions/review")
def post_review_question(store, actor, body, **_):
    """Record what somebody decided about a question, and why.

    The verb that makes this a feature rather than a display. `standing` is how
    a person says *this one is real and stays on the list*; `rationale` is
    required for every state, because the reason is the part worth anything to
    the next reviewer.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "These questions are drawn from the whole corpus, so ruling on one "
            "is an administrator action.")
    if not body.get("fingerprint") or not body.get("status"):
        raise ApiError(400, "Give `fingerprint` and `status`.")
    if not body.get("rationale"):
        raise ApiError(400, "Give `rationale` -- a judgement with no reason "
                            "means the next reviewer establishes it again.")
    return questions_mod.review_question(
        store, body["fingerprint"], body["status"], body["rationale"],
        actor_id=_actor_id(actor), kind=body.get("kind"),
        summary=body.get("summary"), subjects=body.get("subjects"),
        chain_digest=body.get("chain_digest"))


@route("GET", "/questions/reviews")
def get_question_reviews(store, actor, body, **_):
    if not actor.get("is_admin"):
        raise PermissionDenied("Administrator view.")
    return {"reviews": questions_mod.reviews(store, status=body.get("status"))}


@route("GET", "/corroboration")
def get_corroboration(store, actor, body, **_):
    """Where the corpus agrees with itself, and where it is quoting itself."""
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Corpus-wide corroboration spans documents you may not be able to "
            "read, so it is an administrator view. An entity page carries its "
            "own.")
    return corroboration_mod.summary(
        store, min_documents=_int(body, "min_documents", 2))


@route("GET", r"/entities/(?P<entity_id>ent_[^/]+)/corroboration")
def get_entity_corroboration(store, entity_id, **_):
    return corroboration_mod.for_entity(store, entity_id)


# ---------------------------------------------------------------------------
# Quality and administration
# ---------------------------------------------------------------------------

@route("GET", "/quality")
def get_quality(store, actor, body, **_):
    """Corpus-wide extraction quality. Administrator only.

    Not because the numbers are sensitive, but because they aggregate over every
    document in the store, including ones this actor has no permission to read.
    A per-document report needs only `view` on that document, and is the route
    a reviewer wants anyway.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "The corpus-wide quality report aggregates documents you may not "
            "be able to read, so it is an administrator view. "
            "GET /documents/<id>/quality is scoped to one document.")
    return quality.quality_report(store,
                                  min_reviewed=_int(body, "min_reviewed", 5))


@route("POST", "/export")
def post_export(store, actor, body, **_):
    """Write the markdown bundle to a directory on the server.

    Administrator only, and for a sharper reason than `/quality`: this writes
    every page in the store to a path the caller names. Scoping it per document
    would not help -- a bundle is a corpus-wide artefact by definition.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "An export writes every page in the store to disk, so it is an "
            "administrator action.")
    if not body.get("out"):
        raise ApiError(400, "Give `out` -- the directory to write into.")
    return export_md.export(
        store, body["out"], type_id=body.get("type_id"),
        confirmed_only=body.get("confirmed_only") in ("1", "true", "True", True))


@route("GET", "/lint")
def get_lint(store, actor, body, **_):
    """The adversarial pass. Administrator only, same reason as `/quality`.

    Not a health check: it reports located problems and, when it finds none,
    says why that proves less than it looks like it proves.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "The lint spans documents you may not be able to read, so it is an "
            "administrator view.")
    checks = body.get("checks")
    if isinstance(checks, str):
        checks = [c.strip() for c in checks.split(",") if c.strip()]
    return lint_mod.lint(
        store, deep=body.get("deep", "1") not in ("0", "false", "False", False),
        document_id=body.get("document_id"), checks=checks or None)


@route("GET", "/grounding")
def get_grounding(store, actor, body, **_):
    """How often each engine quoted something its document contains.

    Corpus-wide, so administrator-only for the same reason as `/quality`: it
    spans documents the caller may not be able to read.
    """
    if not actor.get("is_admin"):
        raise PermissionDenied(
            "Corpus-wide grounding spans documents you may not be able to "
            "read, so it is an administrator view.")
    return quality.grounding(store)


@route("GET", r"/documents/(?P<document_id>[^/]+)/quality", permission="view")
def get_document_quality(store, document_id, body, **_):
    return quality.quality_report(store, document_id,
                                  min_reviewed=_int(body, "min_reviewed", 1))


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
