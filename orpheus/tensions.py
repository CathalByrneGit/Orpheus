"""Conflicts that survive review.

Every review verb in the store resolves *towards* agreement. `confirm` says the
machine was right; `amend` says it was nearly right; `reject` says it was wrong.
All three end with one answer standing. That is the correct shape for grading an
extraction, and the wrong shape for the thing a corpus is actually full of: two
documents that disagree, where both are right about the moment they were
written.

Without somewhere to put that, an entity page renders both confirmed mentions in
the same voice, one under the other, and reads as though they agree. The
disagreement is usually the part worth knowing — a change of registered address,
a fee that moved, two clauses that both claim to govern termination. Smoothed
into a tidy list, it becomes invisible, and the person who needed to see it
never learns it was there.

A tension is not uncertainty. `confidence` is already the uncertainty axis and
has five levels of it; a low-confidence extraction means *the machine is not
sure*. A tension means *somebody checked, and the conflict is real*. That is why
`accepted` is a terminal state here: a reviewer who cannot say "yes, this
conflict stands" has only two exits, picking a side or leaving it looking
unreviewed, and both bury the finding.

The rule that keeps this from becoming a notes field: **a tension cites at least
two sides, and every side is an instance carrying provenance.** The same rule as
the entity page, for the same reason. An unfalsifiable opinion in a store built
on citations would be the one row nobody could check.
"""

from __future__ import annotations

from typing import Any

from .audit import record_edit
from .rubric import (CONFIDENCE, SETTLED_TENSIONS, TENSION_KINDS,
                     TENSION_SOURCES, TENSION_STATUSES, snap_confidence)
from .store import Store
from .utils import (NotFound, OrpheusError, new_id, now, require_choice,
                    require_string)

SCOPES = ("entity", "document", "corpus")

# Two is the floor, not a convention. One side is an assertion; a tension is the
# relation between at least two of them.
MIN_SIDES = 2


# ---------------------------------------------------------------------------
# Raising
# ---------------------------------------------------------------------------

def raise_tension(store: Store, kind: str, summary: str,
                  sides: list[dict | str], actor_id: str | None = None,
                  scope: str = "entity", subject_id: str | None = None,
                  property_id: str | None = None, detail: str | None = None,
                  source: str = "human", confidence: float | None = None,
                  status: str = "open") -> str:
    """Record a conflict between two or more cited claims.

    `sides` is instance ids, or dicts of `{"instance_id": ..., "position": ...}`
    where `position` is what that side asserts in a few words. Every one is
    checked against `instance_index` before anything is written, so a tension
    can never point at a mention that does not exist.
    """
    store.assert_writable()
    require_choice(kind, TENSION_KINDS, "kind")
    require_choice(scope, SCOPES, "scope")
    require_choice(source, TENSION_SOURCES, "source")
    require_choice(status, TENSION_STATUSES, "status")
    require_string(summary, "summary")

    prepared = _prepare_sides(store, sides)
    if len(prepared) < MIN_SIDES:
        raise OrpheusError(
            f"A tension needs at least {MIN_SIDES} distinct sides; got "
            f"{len(prepared)}. One claim on its own is an assertion -- if it is "
            "wrong, reject it; if it is unclear, that is what confidence is for.")
    if scope == "entity" and not subject_id:
        raise OrpheusError("An entity-scoped tension needs a subject_id.")

    tension_id = new_id("tns")
    store.insert("tensions", {
        "tension_id": tension_id,
        "scope": scope,
        "subject_id": subject_id,
        "kind": kind,
        "property_id": property_id,
        "summary": summary,
        "detail": detail,
        "status": status,
        "resolution": None,
        "source": source,
        "confidence": snap_confidence(confidence),
        "raised_by": actor_id,
        "raised_at": now(),
        "settled_by": None,
        "settled_at": None,
    })
    for side in prepared:
        store.insert("tension_sides", {"tension_id": tension_id, **side})

    record_edit(store, "tensions", tension_id,
                subject_id if scope == "document" else None, "raise_tension",
                new={"kind": kind, "summary": summary, "scope": scope,
                     "subject_id": subject_id,
                     "sides": [s["instance_id"] for s in prepared]},
                actor_id=actor_id)
    return tension_id


def _prepare_sides(store: Store, sides: list[dict | str]) -> list[dict]:
    """Normalise and verify the sides, dropping duplicates but keeping order."""
    out: list[dict] = []
    seen: set[str] = set()
    for side in sides or []:
        if isinstance(side, str):
            side = {"instance_id": side}
        instance_id = side.get("instance_id")
        if not instance_id or instance_id in seen:
            continue
        row = store.one(
            "SELECT instance_id, document_id FROM instance_index "
            "WHERE instance_id = ?", (instance_id,))
        if row is None:
            raise NotFound(f"No instance {instance_id!r} to hang a side on.")
        seen.add(instance_id)
        out.append({"instance_id": instance_id,
                    "document_id": row["document_id"],
                    "position": side.get("position")})
    return out


# ---------------------------------------------------------------------------
# Settling -- or deliberately not
# ---------------------------------------------------------------------------

def accept_tension(store: Store, tension_id: str, actor_id: str,
                   note: str | None = None) -> dict:
    """A person looked, and the conflict is real.

    The important verb. Everything else in the review vocabulary makes a
    disagreement go away; this one signs it. An accepted tension is a finished
    piece of review work, not an outstanding task, and the wiki renders it as
    an assertion rather than as a question.
    """
    return _settle(store, tension_id, "accepted", actor_id, note)


def resolve_tension(store: Store, tension_id: str, actor_id: str,
                    resolution: str) -> dict:
    """The conflict was genuine and has been settled.

    `resolution` is required and says *how*, because a resolved tension with no
    account of the reasoning is worse than an open one: it looks decided and
    nobody can check the decision.
    """
    require_string(resolution, "resolution")
    return _settle(store, tension_id, "resolved", actor_id, resolution)


def withdraw_tension(store: Store, tension_id: str, actor_id: str,
                     reason: str) -> dict:
    """It was not a real conflict.

    Kept rather than deleted, like a rejected instance: a withdrawn tension is
    evidence about how well conflict detection works, and deleting it throws
    away the measurement along with the mistake.
    """
    require_string(reason, "reason")
    return _settle(store, tension_id, "withdrawn", actor_id, reason)


def _settle(store: Store, tension_id: str, status: str, actor_id: str,
            note: str | None) -> dict:
    store.assert_writable()
    existing = get_tension(store, tension_id)
    if existing["status"] == status:
        raise OrpheusError(f"Tension {tension_id} is already {status}.")
    if existing["status"] in SETTLED_TENSIONS:
        raise OrpheusError(
            f"Tension {tension_id} was {existing['status']} on "
            f"{existing['settled_at']}. Raise a new one rather than reopening: "
            "what was decided then stays readable.")
    store.execute(
        "UPDATE tensions SET status = ?, resolution = ?, settled_by = ?, "
        "settled_at = ? WHERE tension_id = ?",
        (status, note, actor_id, now(), tension_id))
    record_edit(store, "tensions", tension_id, None, f"{status}_tension",
                previous={"status": existing["status"]},
                new={"status": status}, actor_id=actor_id, note=note)
    return get_tension(store, tension_id)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get_tension(store: Store, tension_id: str) -> dict:
    row = store.one("SELECT * FROM tensions WHERE tension_id = ?", (tension_id,))
    if row is None:
        raise NotFound(f"No tension {tension_id!r}.")
    return {**row, "sides": _sides(store, tension_id)}


def _sides(store: Store, tension_id: str) -> list[dict]:
    """The cited sides, each with the excerpt it rests on.

    The excerpt is joined in here rather than left to the caller because a
    tension displayed without its evidence is the thing this module exists to
    prevent.
    """
    rows = store.query(
        "SELECT s.instance_id, s.document_id, s.position, "
        "       p.excerpt, p.page_no, p.confidence AS provenance_confidence, "
        "       p.alignment, d.filename, i.type_id "
        "FROM tension_sides s "
        "LEFT JOIN provenance p ON p.instance_id = s.instance_id "
        "LEFT JOIN documents d ON d.document_id = s.document_id "
        "LEFT JOIN instance_index i ON i.instance_id = s.instance_id "
        "WHERE s.tension_id = ? ORDER BY s.rowid", (tension_id,))
    return [dict(r) for r in rows]


def list_tensions(store: Store, scope: str | None = None,
                  subject_id: str | None = None, status: str | None = None,
                  kind: str | None = None, open_only: bool = False,
                  limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if scope:
        clauses.append("scope = ?"); params.append(scope)
    if subject_id:
        clauses.append("subject_id = ?"); params.append(subject_id)
    if status:
        clauses.append("status = ?"); params.append(status)
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    if open_only:
        # "Standing" rather than "unreviewed": an accepted tension is live on
        # the page, and leaving it out of this list would hide the finding
        # again the moment somebody signed it.
        clauses.append("status IN ('open', 'accepted')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.query(
        f"SELECT * FROM tensions {where} ORDER BY raised_at DESC LIMIT ?",
        (*params, limit))
    return [{**r, "sides": _sides(store, r["tension_id"])} for r in rows]


def tensions_for_entity(store: Store, entity_id: str,
                        standing_only: bool = False) -> list[dict]:
    return list_tensions(store, scope="entity", subject_id=entity_id,
                         open_only=standing_only)


def tensions_for_document(store: Store, document_id: str) -> list[dict]:
    """Every tension this document is a side of, however it is scoped.

    Scoped by the *sides*, not by `subject_id`: a document's most important
    conflicts are usually with another document, and those hang off an entity.
    """
    rows = store.query(
        "SELECT DISTINCT t.* FROM tensions t "
        "JOIN tension_sides s ON s.tension_id = t.tension_id "
        "WHERE s.document_id = ? ORDER BY t.raised_at DESC", (document_id,))
    return [{**r, "sides": _sides(store, r["tension_id"])} for r in rows]


def tensions_for_instances(store: Store, instance_ids: list[str]) -> dict[str, list[dict]]:
    """Which of these instances are a side of a standing tension.

    Returned as a map so a page rendering a list of mentions can mark them
    without a query each.
    """
    if not instance_ids:
        return {}
    marks = ",".join("?" * len(instance_ids))
    rows = store.query(
        f"SELECT s.instance_id, t.tension_id, t.kind, t.status, t.summary, "
        f"       t.property_id "
        f"FROM tension_sides s JOIN tensions t ON t.tension_id = s.tension_id "
        f"WHERE s.instance_id IN ({marks}) AND t.status IN ('open', 'accepted')",
        tuple(instance_ids))
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["instance_id"], []).append(dict(row))
    return out


# ---------------------------------------------------------------------------
# Finding them
# ---------------------------------------------------------------------------

def detect_conflicts(store: Store, entity_id: str | None = None,
                     type_id: str | None = None,
                     reviewed_only: bool = True) -> list[dict]:
    """Properties where the mentions of one entity disagree.

    The machine proposing, in the same posture as everywhere else: this returns
    candidates and writes nothing. `reviewed_only` is the default because two
    *unconfirmed* extractions disagreeing is much more likely to be one bad
    extraction than a real conflict — that is what the review queue is for, and
    raising a tension over it would drown the real ones.
    """
    from .entities import mention  # entity_page reads tensions; break the cycle

    if entity_id:
        entities = [store.one("SELECT * FROM entities WHERE entity_id = ?",
                              (entity_id,))]
        entities = [e for e in entities if e]
    else:
        clauses = ["merged_into IS NULL"]
        params: list[Any] = []
        if type_id:
            clauses.append("type_id = ?"); params.append(type_id)
        entities = store.query(
            f"SELECT * FROM entities WHERE {' AND '.join(clauses)}", tuple(params))

    reserved = {"instance_id", "document_id", "source", "confidence", "status",
                "amended_by", "amended_at", "created_at", "naive_key", "name"}
    found = []
    for entity in entities:
        links = store.query(
            "SELECT instance_id, document_id, status FROM entity_mentions "
            "WHERE entity_id = ? AND unlinked_at IS NULL", (entity["entity_id"],))
        if reviewed_only:
            links = [l for l in links if l["status"] in ("confirmed", "amended")]
        if len(links) < MIN_SIDES:
            continue

        values: dict[str, dict[str, list[dict]]] = {}
        for link in links:
            try:
                found_mention = mention(store, link["instance_id"])
            except NotFound:
                continue
            instance_status = found_mention["properties"].get("status")
            if instance_status == "rejected":
                # A rejected extraction is not a side. It is a known error, and
                # arguing with it would manufacture conflicts out of mistakes
                # the review already caught.
                continue
            for key, value in found_mention["properties"].items():
                if key in reserved or value is None or value == "":
                    continue
                values.setdefault(key, {}).setdefault(str(value), []).append(
                    {"instance_id": link["instance_id"],
                     "document_id": link["document_id"], "value": value})

        for key, by_value in values.items():
            if len(by_value) < 2:
                continue
            existing = store.one(
                "SELECT tension_id, status FROM tensions WHERE scope = 'entity' "
                "AND subject_id = ? AND property_id = ? "
                "AND status IN ('open', 'accepted', 'resolved')",
                (entity["entity_id"], key))
            sides = [group[0] for group in by_value.values()]
            found.append({
                "scope": "entity",
                "subject_id": entity["entity_id"],
                "subject_name": entity["canonical_name"],
                "type_id": entity["type_id"],
                "kind": "conflicting_value",
                "property_id": key,
                "summary": (f"{entity['canonical_name']} has "
                            f"{len(by_value)} different values for {key}: "
                            + "; ".join(sorted(by_value)[:4])),
                "sides": [{"instance_id": s["instance_id"],
                           "position": str(s["value"])} for s in sides],
                "n_values": len(by_value),
                # Already recorded, so the caller can list without re-raising.
                "existing_tension_id": existing["tension_id"] if existing else None,
                "existing_status": existing["status"] if existing else None,
            })
    return found


def propose_tensions(store: Store, actor_id: str | None = None,
                     entity_id: str | None = None, type_id: str | None = None,
                     reviewed_only: bool = True) -> dict:
    """Raise a tension for each conflict not already recorded.

    Raised at `open` and sourced `lint`, never `accepted`: the machine can see
    that two values differ, and cannot see whether the difference matters. That
    judgement is the whole point of the state, and pre-making it would empty it.
    """
    store.assert_writable()
    conflicts = detect_conflicts(store, entity_id=entity_id, type_id=type_id,
                                 reviewed_only=reviewed_only)
    raised, skipped = [], 0
    for conflict in conflicts:
        if conflict["existing_tension_id"]:
            skipped += 1
            continue
        raised.append(raise_tension(
            store, kind=conflict["kind"], summary=conflict["summary"],
            sides=conflict["sides"], actor_id=actor_id, scope="entity",
            subject_id=conflict["subject_id"], property_id=conflict["property_id"],
            source="lint", confidence=CONFIDENCE["explicit"],
            detail=("Detected by comparing the reviewed mentions linked to this "
                    "entity. That the values differ is a fact; whether the "
                    "difference matters is not, and is why this is open.")))
    return {"raised": raised, "n_raised": len(raised),
            "already_recorded": skipped, "n_conflicts": len(conflicts)}
