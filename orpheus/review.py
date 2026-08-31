"""Steps 6 and 8: a machine reading becomes a checked fact.

Four states, and they are a vocabulary rather than a convention:
`unconfirmed` → `confirmed` / `amended` / `rejected`. Everything downstream
reads them, so nothing else may be written into that column.

**Nothing is destructively overwritten.** A correction updates the instance row
and appends to `edit_history`; the provenance row still says what the machine
claimed. A rejection excludes a row without deleting it, because a rejected
extraction is evidence about extraction quality and deleting it would throw away
the measurement along with the mistake.

An amendment is ground truth, so it takes `source = 'human'` and the top rubric
level. Leaving the model's confidence in place would mean a corrected row still
read as a machine guess.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from . import quality
from .audit import record_edit
from .rubric import (CONFIDENCE, RESERVED_PROPS, REVIEWED_STATUSES,
                     STATUSES, confidence_label)
from .store import Store
from .utils import (NotFound, OrpheusError, naive_key, now, 
                    require_choice, require_string)


def locate_instance(store: Store, instance_id: str) -> dict:
    row = store.one(
        "SELECT instance_id, type_id, table_name, document_id FROM instance_index "
        "WHERE instance_id = ?", (instance_id,))
    if row is None:
        raise NotFound(f"No instance {instance_id!r}.")
    return row


def read_instance(store: Store, table: str, instance_id: str) -> dict:
    row = store.one(f'SELECT * FROM "{table}" WHERE instance_id = ?', (instance_id,))
    if row is None:
        raise NotFound(f"No row for instance {instance_id!r} in {table}.")
    return row


# ---------------------------------------------------------------------------
# The three verbs
# ---------------------------------------------------------------------------

def confirm_instance(store: Store, instance_id: str, actor_id: str,
                     note: str | None = None) -> str:
    """Keep the machine's value, and record that a person checked it."""
    store.assert_writable()
    require_string(actor_id, "actor_id")
    location = locate_instance(store, instance_id)
    before = read_instance(store, location["table_name"], instance_id)

    with store.transaction():
        store.execute(
            f'UPDATE "{location["table_name"]}" SET status = \'confirmed\', '
            "amended_by = ?, amended_at = ? WHERE instance_id = ?",
            (actor_id, now(), instance_id))
        record_edit(store, location["table_name"], instance_id,
                    location["document_id"], "confirm",
                    previous={"status": before["status"]},
                    new={"status": "confirmed"}, actor_id=actor_id, note=note)
    return instance_id


def amend_instance(store: Store, instance_id: str, changes: dict, actor_id: str,
                   note: str | None = None) -> str:
    """Replace one or more property values, keeping the original in the history."""
    store.assert_writable()
    require_string(actor_id, "actor_id")
    if not isinstance(changes, dict) or not changes:
        raise OrpheusError("changes must be a non-empty mapping.")

    location = locate_instance(store, instance_id)
    bundle = bundle_mod.active(store)
    obj = bundle_mod.object_type(bundle, location["type_id"]) if bundle else None
    if obj is None:
        raise OrpheusError(
            f"The active bundle has no object type {location['type_id']!r}.")

    amendable = set(bundle_mod.property_ids(obj)) - set(RESERVED_PROPS)
    unknown = sorted(set(changes) - amendable)
    if unknown:
        raise OrpheusError(
            f"{', '.join(repr(u) for u in unknown)} "
            f"{'is not a declared property' if len(unknown) == 1 else 'are not declared properties'} "
            f"of {location['type_id']!r}. Propose a schema amendment instead — "
            "adding a property changes the bundle, which is a separate review "
            "from correcting one row."
        )

    before = read_instance(store, location["table_name"], instance_id)

    # An "amendment" that changes nothing is not an amendment. Recording it as
    # one would flip source to `human` and status to `amended` on a value the
    # machine got right, so the row would claim a person supplied what a person
    # only agreed with -- and the quality report counts amendments as machine
    # errors a human had to fix. A browser form posts every field it renders,
    # so most of what arrives here is unchanged; the comparison is on the
    # stored type, since a form sends everything back as a string.
    changes = {key: value for key, value in changes.items()
               if not _same(before.get(key), value)}
    if not changes:
        raise OrpheusError(
            "Nothing was changed. Confirming records that a person checked the "
            "value and agreed with it; amending is for correcting it."
        )
    previous = {key: before.get(key) for key in changes}

    updates = dict(changes)
    updates.update({
        "status": "amended",
        "source": "human",
        "confidence": CONFIDENCE["explicit"],
        "amended_by": actor_id,
        "amended_at": now(),
    })
    # Derived from `name`, so it has to be recomputed rather than left stale --
    # otherwise a corrected name still matches under the old key.
    if "name" in changes and "naive_key" in bundle_mod.property_ids(obj):
        updates["naive_key"] = naive_key(changes["name"],
                                         bundle_mod.name_style(obj))

    assignments = ", ".join(f'"{c}" = ?' for c in updates)
    with store.transaction():
        store.execute(
            f'UPDATE "{location["table_name"]}" SET {assignments} WHERE instance_id = ?',
            tuple(updates.values()) + (instance_id,))
        record_edit(store, location["table_name"], instance_id,
                    location["document_id"], "amend", previous=previous,
                    new=changes, actor_id=actor_id, note=note)
        mark_dependent_evaluations_stale(
            store, instance_id, f"Instance {instance_id} was amended.")
    return instance_id


def _same(stored, submitted) -> bool:
    """Whether a submitted value means the same as what is already stored.

    A form posts strings, so `1480000.0` comes back as `"1480000.0"` and a
    naive `!=` would read every rendered number as an edit.
    """
    if stored is None:
        return submitted is None or submitted == ""
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        try:
            return float(stored) == float(submitted)
        except (TypeError, ValueError):
            return False
    return str(stored) == str(submitted)


def reject_instance(store: Store, instance_id: str, actor_id: str,
                    note: str | None = None) -> str:
    """Exclude a finding without deleting it."""
    store.assert_writable()
    require_string(actor_id, "actor_id")
    location = locate_instance(store, instance_id)
    before = read_instance(store, location["table_name"], instance_id)

    with store.transaction():
        store.execute(
            f'UPDATE "{location["table_name"]}" SET status = \'rejected\', '
            "amended_by = ?, amended_at = ? WHERE instance_id = ?",
            (actor_id, now(), instance_id))
        record_edit(store, location["table_name"], instance_id,
                    location["document_id"], "reject",
                    previous={"status": before["status"]},
                    new={"status": "rejected"}, actor_id=actor_id, note=note)
        mark_dependent_evaluations_stale(
            store, instance_id, f"Instance {instance_id} was rejected.")
    return instance_id


def review_edge(store: Store, edge_id: str, status: str, actor_id: str,
                link_type_id: str | None = None, note: str | None = None) -> str:
    store.assert_writable()
    require_string(actor_id, "actor_id")
    require_choice(status, ("confirmed", "amended", "rejected"), "status")
    edge = store.one("SELECT * FROM edges WHERE edge_id = ?", (edge_id,))
    if edge is None:
        raise NotFound(f"No edge {edge_id!r}.")

    updates = {"status": status, "amended_by": actor_id, "amended_at": now()}
    if link_type_id:
        updates["link_type_id"] = link_type_id
        updates["status"] = "amended"
        updates["source"] = "human"
    assignments = ", ".join(f'"{c}" = ?' for c in updates)
    with store.transaction():
        store.execute(f"UPDATE edges SET {assignments} WHERE edge_id = ?",
                      tuple(updates.values()) + (edge_id,))
        record_edit(store, "edges", edge_id, edge["document_id"], f"edge_{status}",
                    previous={"status": edge["status"],
                              "link_type_id": edge["link_type_id"]},
                    new=updates, actor_id=actor_id, note=note)
    return edge_id


# ---------------------------------------------------------------------------
# Document-level review state
# ---------------------------------------------------------------------------

def review_progress(store: Store, document_id: str) -> dict:
    counts = {status: 0 for status in STATUSES}
    tables = {r["table_name"] for r in store.query(
        "SELECT DISTINCT table_name FROM instance_index WHERE document_id = ?",
        (document_id,))}
    for table in tables:
        for row in store.query(
                f'SELECT status, COUNT(*) AS n FROM "{table}" WHERE document_id = ? '
                "GROUP BY status", (document_id,)):
            if row["status"] in counts:
                counts[row["status"]] += row["n"]
    edges = store.scalar("SELECT COUNT(*) FROM edges WHERE document_id = ?",
                         (document_id,)) or 0
    return {**counts, "total": sum(counts.values()), "edges_total": edges}


def mark_document_reviewed(store: Store, document_id: str, actor_id: str,
                           reviewed: bool = True) -> dict:
    """Set the document-level flag.

    Deliberately not derived from the instance counts. "I have looked at this
    document" and "every row in it has been confirmed" are different claims, and
    a reviewer may finish with a document while leaving rows they cannot judge.
    Marking it reviewed with rows outstanding is allowed, and says so.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    document = store.one("SELECT review_status FROM documents WHERE document_id = ?",
                         (document_id,))
    if document is None:
        raise NotFound(f"No document {document_id!r}.")

    status = "reviewed" if reviewed else "unreviewed"
    with store.transaction():
        store.execute(
            "UPDATE documents SET review_status = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE document_id = ?",
            (status, actor_id if reviewed else None,
             now() if reviewed else None, document_id))
        record_edit(store, "documents", document_id, document_id, "review_status",
                    previous={"review_status": document["review_status"]},
                    new={"review_status": status}, actor_id=actor_id)

    outstanding = review_progress(store, document_id)["unconfirmed"]
    return {
        "document_id": document_id,
        "review_status": status,
        "unconfirmed_instances": outstanding,
        "note": (f"Marked reviewed with {outstanding} instance(s) still unconfirmed."
                 if reviewed and outstanding else None),
    }


# ---------------------------------------------------------------------------
# Schema amendments
# ---------------------------------------------------------------------------

def triage(store: Store, *, limit: int = 50, min_reviewed: int = 5,
           document_id: str | None = None) -> dict:
    """Which unreviewed extractions to look at first, and what it would settle.

    A store with nine hundred unconfirmed instances and no order on them is a
    store nobody reviews. That matters more than it sounds: `orpheus report`
    answers `insufficient_evidence` until review has happened, so every quality
    claim this system makes is downstream of somebody starting -- and "start
    anywhere" is the instruction people do not act on.

    **The ranking is not a heuristic about importance.** It is read off the
    thing the report is actually waiting for. `confidence_calibration` needs
    two confidence levels each holding `min_reviewed` reviewed instances before
    it will say anything at all; below that it returns `insufficient_evidence`
    no matter how much work has gone in. So a reviewer can confirm three
    hundred `explicit` extractions and move the verdict not one inch, while
    five at a level nobody has touched would move it from silence to an answer.
    This puts those five at the top and says so.

    Within a level, the queue rotates through documents. A calibration measured
    on one document is a measurement of that document -- it says how well the
    extractor read *that* contract, which is not the question. Spreading the
    same amount of review across the corpus costs the reviewer nothing and is
    the difference between a number and a number that generalises.

    Once two levels are over the line the ranking keeps going, weakest level
    first. It cannot promise a verdict then: the next thing in the way is
    whether the levels *separate*, and that depends on the corpus containing
    enough wrong extractions to tell them apart. Saying so is better than
    ranking on a rule invented to look purposeful.
    """
    outcomes = quality.collect_review_outcomes(store, document_id)
    if not outcomes:
        return {"n_unreviewed": 0, "queue": [], "levels": [],
                "headline": "Nothing has been extracted yet."}

    reviewed_at: dict[float, int] = {}
    waiting: dict[float, list[dict]] = {}
    for row in outcomes:
        level = row["confidence"]
        if row["status"] in REVIEWED_STATUSES:
            reviewed_at[level] = reviewed_at.get(level, 0) + 1
        else:
            waiting.setdefault(level, []).append(row)
        reviewed_at.setdefault(level, 0)

    over_the_line = [lvl for lvl, n in reviewed_at.items() if n >= min_reviewed]
    # A level with nothing left to review cannot be brought over the line, so
    # counting it among the ways out would be offering work that does not exist.
    reachable = [lvl for lvl in waiting
                 if reviewed_at[lvl] < min_reviewed
                 and reviewed_at[lvl] + len(waiting[lvl]) >= min_reviewed]

    levels = [{
        "confidence": lvl,
        "confidence_label": confidence_label(lvl),
        "n_reviewed": reviewed_at[lvl],
        "n_waiting": len(waiting.get(lvl, [])),
        "short_by": max(0, min_reviewed - reviewed_at[lvl]),
        "counts_toward_a_verdict": lvl in over_the_line,
    } for lvl in sorted(reviewed_at, reverse=True)]

    # The levels that would unblock a verdict, cheapest first: the one closest
    # to the threshold is the shortest way to a second qualifying level. On a
    # tie -- which is the ordinary case, since a corpus nobody has reviewed has
    # every level at zero -- prefer the level with the fewest waiting. That is
    # the scarcest evidence in the store and the one a reviewer working through
    # volume will never reach on their own, so it is the review that buys
    # something nothing else would.
    unblocking = sorted(reachable, key=lambda lvl: (min_reviewed - reviewed_at[lvl],
                                                    len(waiting[lvl])))
    if len(over_the_line) >= 2:
        # Already answerable. Deepen the thinnest evidence instead, which is
        # where a wrong verdict would come from.
        ordered = sorted(waiting, key=lambda lvl: reviewed_at[lvl])
        why = "deepening"
    else:
        needed = 2 - len(over_the_line)
        ordered = unblocking[:needed] + [
            lvl for lvl in sorted(waiting, key=lambda l: reviewed_at[l])
            if lvl not in unblocking[:needed]]
        why = "unblocking"

    queue: list[dict] = []
    for lvl in ordered:
        rows = waiting.get(lvl, [])
        if not rows:
            continue
        short_by = max(0, min_reviewed - reviewed_at[lvl])
        unblocks = why == "unblocking" and lvl in unblocking[:2 - len(over_the_line)]
        # A level being brought over the line contributes exactly what it is
        # short by and no more. Letting the first one fill the whole queue
        # would leave the second one short and the report still silent, which
        # is the failure this exists to prevent -- and it is why the count
        # comes out at `min_reviewed` times two rather than approximately that.
        candidates = _round_robin_by_document(rows)
        if unblocks:
            candidates = candidates[:short_by]
        for row in candidates:
            queue.append({
                "instance_id": row["instance_id"],
                "document_id": row["document_id"],
                "type_id": row["type_id"],
                "confidence": lvl,
                "confidence_label": confidence_label(lvl),
                "source": row["source"],
                "reason": (
                    f"{confidence_label(lvl)} is {short_by} reviewed instance(s) "
                    "short of counting toward a verdict, and nothing else is "
                    "closer." if unblocks else
                    f"{confidence_label(lvl)} has the least review behind it "
                    f"({reviewed_at[lvl]} instance(s))."),
            })
            if len(queue) >= limit:
                break
        if len(queue) >= limit:
            break

    n_unreviewed = sum(len(rows) for rows in waiting.values())
    return {"n_unreviewed": n_unreviewed, "min_reviewed": min_reviewed,
            "levels": levels, "queue": queue,
            "headline": _triage_headline(n_unreviewed, over_the_line, unblocking,
                                         reviewed_at, min_reviewed)}


def _round_robin_by_document(rows: list[dict]) -> list[dict]:
    """One from each document, then round again.

    A calibration measured on one document is a measurement of that document.
    Spreading the same amount of review across the corpus costs the reviewer
    nothing and is the difference between a number and a number that
    generalises.
    """
    by_document: dict[str, list[dict]] = {}
    for row in rows:
        by_document.setdefault(row["document_id"], []).append(row)
    spread = []
    while by_document:
        for document_id in list(by_document):
            spread.append(by_document[document_id].pop(0))
            if not by_document[document_id]:
                del by_document[document_id]
    return spread


def _triage_headline(n_unreviewed: int, over_the_line: list, unblocking: list,
                     reviewed_at: dict, min_reviewed: int) -> str:
    if not n_unreviewed:
        return "Everything extracted has been reviewed."
    if len(over_the_line) >= 2:
        return (f"{n_unreviewed} unreviewed. The rubric already has two levels "
                "with enough behind them, so the report will answer; what is "
                "left is depth, weakest level first.")
    if not unblocking:
        return (f"{n_unreviewed} unreviewed, and no confidence level can reach "
                f"{min_reviewed} reviewed instances even if you review every "
                "one of them. The report cannot rank this rubric on this "
                "corpus -- it needs more documents, not more review.")
    first = unblocking[0]
    needed = min_reviewed - reviewed_at[first]
    return (f"{n_unreviewed} unreviewed, and the report is silent because "
            f"fewer than two confidence levels have {min_reviewed} reviewed "
            f"instances behind them. Reviewing {needed} at "
            f"`{confidence_label(first)}` is the shortest way to an answer.")


def schema_amendments(store: Store, status: str = "pending") -> list[dict]:
    return store.query(
        "SELECT * FROM schema_amendments WHERE status = ? ORDER BY occurrences DESC, proposed_at",
        (status,))


def bump_patch(version: str) -> str:
    parts = str(version).split(".")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return f"{version}.1"
    if len(numbers) < 3:
        return f"{version}.1"
    return f"{numbers[0]}.{numbers[1]}.{numbers[2] + 1}"


def review_schema_amendment(store: Store, amendment_id: str, decision: str,
                            actor_id: str, note: str | None = None) -> dict:
    """Accept or reject a proposed change to the bundle.

    Accepting a `new_property` applies it and bumps the bundle's patch version,
    because the bundle that produced yesterday's extractions and the bundle in
    force today are different objects and the store records which was which.
    Other amendment types are recorded but not applied: adding a type or a link
    type is a modelling decision with a shape to design, not a column to append.
    """
    store.assert_writable()
    require_string(actor_id, "actor_id")
    require_choice(decision, ("accepted", "rejected"), "decision")

    amendment = store.one("SELECT * FROM schema_amendments WHERE amendment_id = ?",
                          (amendment_id,))
    if amendment is None:
        raise NotFound(f"No schema amendment {amendment_id!r}.")
    if amendment["status"] != "pending":
        raise OrpheusError(
            f"Amendment {amendment_id} was already {amendment['status']}.")

    applied = False
    new_version = None
    with store.transaction():
        store.execute(
            "UPDATE schema_amendments SET status = ?, reviewed_by = ?, "
            "reviewed_at = ?, review_note = ? WHERE amendment_id = ?",
            (decision, actor_id, now(), note, amendment_id))

        if decision == "accepted" and amendment["amendment_type"] == "new_property":
            bundle = bundle_mod.active(store)
            obj = bundle_mod.object_type(bundle, amendment["type_id"]) if bundle else None
            if obj is not None and amendment["property_id"] not in bundle_mod.property_ids(obj):
                obj["properties"].append({
                    "id": amendment["property_id"],
                    "type": amendment["inferred_type"] or "string",
                    "nullable": True,
                    "display": {"description":
                                f"Accepted schema amendment: {amendment['rationale'] or ''}"},
                    "source": {"column": amendment["property_id"]},
                })
                new_version = bump_patch(bundle["bundleVersion"])
                bundle["bundleVersion"] = new_version
                bundle_mod.register(store, bundle, actor_id=actor_id, activate=True)
                applied = True

        record_edit(store, "schema_amendments", amendment_id,
                    amendment["document_id"], f"schema_amendment_{decision}",
                    previous={"status": "pending"},
                    new={"status": decision, "applied": applied,
                         "bundle_version": new_version},
                    actor_id=actor_id, note=note)

    return {
        "amendment_id": amendment_id,
        "decision": decision,
        "applied_to_bundle": applied,
        "bundle_version": new_version,
        "note": ("Recorded. This amendment type is not applied automatically — "
                 "register an updated bundle."
                 if decision == "accepted" and not applied else None),
    }


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def mark_dependent_evaluations_stale(store: Store, instance_id: str,
                                     reason: str) -> int:
    """Every interpretation built on an amended instance becomes visibly stale.

    This is the point of recording which instances an evaluation read: staleness
    is automatic rather than something a person has to notice. A concept
    evaluated over a value that has since been corrected is not wrong-and-quiet;
    it is marked.
    """
    store.assert_writable()
    cursor = store.execute(
        "UPDATE concept_evaluations SET stale = 1, stale_reason = ? "
        "WHERE stale = 0 AND evaluation_id IN ("
        "  SELECT evaluation_id FROM concept_evaluation_dependencies WHERE instance_id = ?)",
        (reason, instance_id))
    return cursor.rowcount
