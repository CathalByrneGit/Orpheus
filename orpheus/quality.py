"""The question Phase 1 exists to answer: is extraction good enough to build on?

Everything else here is machinery for producing this report. It is computable
only because corrections never destroy what they correct — the instance row
carries the current value, the provenance row carries what the machine said, and
the difference between them, summed over a corpus, is the measurement.

Four things it reports, each answering a different question:

- **Accuracy by type and tier** — how often an extraction survived review
  untouched. Split three ways, because "confirmed", "amended" and "rejected"
  mean different things: amended is *wrong in detail but worth keeping*, and a
  high amend rate with a low reject rate is a working extractor with a sloppy
  field, not a broken one.
- **Calibration** — whether the rubric ranks reliability at all. A rubric that
  does not is worse than none, because people trust it.
- **Concept precision** — which rules over-fire, measured from the flags people
  dismissed.
- **Property corrections** — which fields people keep fixing, read from the
  history rather than from current values, because the question is what had to
  be changed.

Every rate is over reviewed rows only, and `coverage` says how much of the
population that is. Rates from fewer than `min_reviewed` rows are suppressed
rather than shown, because a rate computed from three rows is noise wearing a
number's clothes.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from .rubric import REVIEWED_STATUSES, confidence_label
from .store import Store
from .utils import from_json


def _scope(document_id: str | None) -> tuple[str, tuple]:
    return (" AND i.document_id = ?", (document_id,)) if document_id else ("", ())


def collect_review_outcomes(store: Store, document_id: str | None = None,
                            bundle: dict | None = None) -> list[dict]:
    """One row per extracted instance, with its original confidence and tier.

    Joined through `provenance`, which is what keeps rule-raised flags out of
    the extraction numbers: a concept flag has no provenance row, because it is
    not an extraction. Give concept flags provenance and this quietly starts
    reporting rule precision as extraction accuracy.

    Facts a person recorded by hand are excluded for the same reason. The
    extractor never offered them, so they are not evidence about the extractor,
    and counting each one as a confirmed extraction would walk the accuracy
    number upward every time somebody filled a gap the model left. An accepted
    suggestion is a different case and stays counted: there the machine did
    offer something and a person agreed, which is exactly what this measures.
    Provenance carries the offering engine, so the two separate cleanly.
    """
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    clause, params = _scope(document_id)
    rows: list[dict] = []

    for obj in bundle_mod.managed_object_types(bundle):
        table = bundle_mod.table_name(obj)
        if not table or not store.table_exists(table):
            continue
        # Concept-raised flags are excluded here for the same reason.
        rule_filter = (" AND IFNULL(i.raised_by_pass, '') != 'concept'"
                       if "raised_by_pass" in store.columns(table) else "")
        for row in store.query(
                f'SELECT i.instance_id, i.document_id, i.status, '
                f"       p.confidence AS confidence, p.source AS source "
                f'FROM "{table}" i '
                "LEFT JOIN provenance p ON p.instance_id = i.instance_id "
                "WHERE IFNULL(p.source, '') != 'human'"
                f"{rule_filter}{clause}", params):
            # An instance with no provenance cannot be attributed, so it is
            # left out rather than counted at an invented confidence.
            if row["confidence"] is None:
                continue
            row["source"] = row["source"] or "unknown"
            row["type_id"] = obj["id"]
            rows.append(row)
    return rows


def _summarise(rows: list[dict], key) -> list[dict]:
    groups: dict = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)

    out = []
    for name, part in groups.items():
        reviewed = [r for r in part if r["status"] in REVIEWED_STATUSES]
        n_reviewed = len(reviewed)
        confirmed = sum(1 for r in reviewed if r["status"] == "confirmed")
        amended = sum(1 for r in reviewed if r["status"] == "amended")
        rejected = sum(1 for r in reviewed if r["status"] == "rejected")
        out.append({
            "group": name,
            "n_total": len(part),
            "n_reviewed": n_reviewed,
            "n_confirmed": confirmed,
            "n_amended": amended,
            "n_rejected": rejected,
            "coverage": round(n_reviewed / len(part), 3) if part else None,
            # Accepted as extracted: the machine needed no correction at all.
            "accuracy": round(confirmed / n_reviewed, 3) if n_reviewed else None,
            # Wrong in detail, but the row itself was worth keeping.
            "amend_rate": round(amended / n_reviewed, 3) if n_reviewed else None,
            "reject_rate": round(rejected / n_reviewed, 3) if n_reviewed else None,
        })
    return out


def _suppress_small(rows: list[dict], min_reviewed: int) -> list[dict]:
    for row in rows:
        if row["n_reviewed"] < min_reviewed:
            row["accuracy"] = row["amend_rate"] = row["reject_rate"] = None
    return rows


def extraction_quality(store: Store, document_id: str | None = None,
                       min_reviewed: int = 5) -> dict:
    rows = collect_review_outcomes(store, document_id)
    if not rows:
        return {"overall": {"n_total": 0, "n_reviewed": 0, "coverage": None,
                            "accuracy": None},
                "by_type": [], "by_confidence": [], "by_tier": [],
                "min_reviewed": min_reviewed,
                "note": "Nothing has been extracted yet."}

    overall = _summarise(rows, lambda r: "all")[0]
    overall.pop("group", None)

    by_confidence = _summarise(rows, lambda r: r["confidence"])
    for row in by_confidence:
        row["confidence"] = row.pop("group")
        row["confidence_label"] = confidence_label(row["confidence"])
    by_confidence.sort(key=lambda r: -r["confidence"])

    by_type = _summarise(rows, lambda r: r["type_id"])
    for row in by_type:
        row["type_id"] = row.pop("group")

    by_tier = _summarise(rows, lambda r: r["source"])
    for row in by_tier:
        row["source"] = row.pop("group")

    return {
        "overall": overall,
        "by_type": _suppress_small(by_type, min_reviewed),
        "by_confidence": _suppress_small(by_confidence, min_reviewed),
        "by_tier": _suppress_small(by_tier, min_reviewed),
        "min_reviewed": min_reviewed,
    }


def confidence_calibration(store: Store, document_id: str | None = None,
                           min_reviewed: int = 5) -> dict:
    """Does a higher rubric level really mean a more reliable fact?

    The rubric is only worth carrying if it ranks. This says plainly whether the
    ordering survives contact with real review, because a rubric that does not
    rank is worse than no rubric at all — people trust it.

    Read what it ranks carefully. No engine here is asked for a confidence, on
    purpose: a model's opinion of its own certainty is the thing the rubric was
    invented to avoid storing, so `ALIGNMENT_CONFIDENCE` supplies the level from
    how exactly the excerpt was located instead. The ordering under test is
    therefore *grounding* against review — whether a verbatim quotation is
    confirmed more often than a fuzzy or an unlocatable one — and not a model's
    self-assessment against review. An engine that does report a confidence
    keeps its own, and then this compares the two kinds of claim in one column,
    which is worth knowing before reading a verdict off it.
    """
    levels = extraction_quality(store, document_id, min_reviewed)["by_confidence"]
    usable = [row for row in levels if row["accuracy"] is not None]

    if len(usable) < 2:
        return {"levels": levels, "verdict": "insufficient_evidence",
                "inversions": [],
                "note": (f"Fewer than two confidence levels have {min_reviewed} or "
                         "more reviewed instances. Review more before trusting "
                         "the rubric.")}

    usable.sort(key=lambda r: -r["confidence"])
    inversions = [
        {"higher_level": usable[i]["confidence_label"],
         "higher_accuracy": usable[i]["accuracy"],
         "lower_level": usable[i + 1]["confidence_label"],
         "lower_accuracy": usable[i + 1]["accuracy"]}
        for i in range(len(usable) - 1)
        if usable[i]["accuracy"] < usable[i + 1]["accuracy"]
    ]
    # No inversions is not the same as ranking. Every usable level scoring the
    # same is a flat line, and calling that `monotonic` passes an exit check it
    # has not earned -- the note even said accuracy rises, on data where it did
    # not move at all. A rubric that does not separate is exactly as useless as
    # one that inverts, and harder to notice.
    accuracies = {row["accuracy"] for row in usable}
    if not inversions and len(accuracies) == 1:
        only = next(iter(accuracies))
        return {
            "levels": usable, "verdict": "flat", "inversions": [],
            "note": (f"Every level with enough reviewed instances scored the "
                     f"same ({only:.0%}), so nothing here shows the rubric "
                     f"ranking anything. Not a failure and not a pass: it needs "
                     f"either more review at the levels that were skipped, or a "
                     f"corpus with enough wrong extractions to separate them."),
        }
    return {
        "levels": usable,
        "verdict": "monotonic" if not inversions else "inverted",
        "inversions": inversions,
        "note": ("Accuracy rises with the rubric level, as it should."
                 if not inversions else
                 "A higher rubric level scored worse than a lower one. The "
                 "rubric is not ranking reliability here — treat the levels as "
                 "labels, not as a ranking, until this resolves."),
    }


def concept_precision(store: Store, document_id: str | None = None,
                      min_reviewed: int = 5) -> list[dict]:
    """How often each rule points at something a person agreed with.

    A concept that keeps getting dismissed is a rule that needs changing, and
    this is the only place that shows up as a number.
    """
    if not store.table_exists("instances_Flag"):
        return []
    clause, params = _scope(document_id)
    flags = store.query(
        "SELECT flag_type, status FROM instances_Flag i "
        f"WHERE raised_by_pass = 'concept'{clause}", params)
    if not flags:
        return []

    groups: dict = {}
    for flag in flags:
        groups.setdefault(flag["flag_type"], []).append(flag)

    out = []
    for concept_id, part in groups.items():
        reviewed = [f for f in part if f["status"] in REVIEWED_STATUSES]
        upheld = sum(1 for f in reviewed if f["status"] in ("confirmed", "amended"))
        out.append({
            "concept_id": concept_id,
            "n_raised": len(part),
            "n_reviewed": len(reviewed),
            "n_upheld": upheld,
            "n_dismissed": sum(1 for f in reviewed if f["status"] == "rejected"),
            "precision": (round(upheld / len(reviewed), 3)
                          if len(reviewed) >= min_reviewed else None),
        })
    # Worst first: the rules worth fixing are the ones people keep dismissing.
    out.sort(key=lambda r: (r["precision"] is None, r["precision"]))
    return out


def property_corrections(store: Store, document_id: str | None = None,
                         limit: int = 25) -> list[dict]:
    """Which fields people keep having to fix.

    Read from `edit_history` rather than from the instance tables, because the
    question is not what a value is now but which fields had to be changed. The
    properties at the top are where extraction is weakest, and are the ones
    worth changing a prompt or a pattern for.
    """
    clause = " AND document_id = ?" if document_id else ""
    params = (document_id,) if document_id else ()
    edits = store.query(
        "SELECT table_name, previous_value, new_value FROM edit_history "
        f"WHERE action = 'amend'{clause} ORDER BY seq DESC", params)

    counts: dict = {}
    for edit in edits:
        new = from_json(edit["new_value"]) or {}
        old = from_json(edit["previous_value"]) or {}
        if not isinstance(new, dict):
            continue
        for prop, value in new.items():
            key = (edit["table_name"], prop)
            entry = counts.setdefault(key, {"table_name": edit["table_name"],
                                            "property_id": prop, "n_corrections": 0,
                                            "example_was": None, "example_now": None})
            entry["n_corrections"] += 1
            if entry["example_was"] is None:
                # A worked example is worth more than a count: it shows what
                # kind of mistake this is.
                entry["example_was"] = old.get(prop) if isinstance(old, dict) else None
                entry["example_now"] = value
    out = sorted(counts.values(), key=lambda r: -r["n_corrections"])
    return out[:limit]


def codelist_violations(store: Store, bundle: dict | None = None,
                        document_id: str | None = None) -> list[dict]:
    """Values outside a closed codelist.

    A closed codelist is a promise about the data. Without this the promise is
    just a comment in the bundle, and values drift outside it silently.
    """
    bundle = bundle or bundle_mod.active(store) or bundle_mod.load()
    clause, params = _scope(document_id)
    out = []
    for obj in bundle_mod.managed_object_types(bundle):
        table = bundle_mod.table_name(obj)
        if not table or not store.table_exists(table):
            continue
        columns = set(store.columns(table))
        for prop in obj.get("properties", []):
            extensions = prop.get("extensions") or {}
            allowed = extensions.get("values")
            if not allowed or extensions.get("open") or prop["id"] not in columns:
                continue
            placeholders = ", ".join("?" for _ in allowed)
            for row in store.query(
                    f'SELECT i.instance_id, i.document_id, i."{prop["id"]}" AS value '
                    f'FROM "{table}" i WHERE i."{prop["id"]}" IS NOT NULL '
                    f'AND i."{prop["id"]}" != \'\' '
                    f'AND i."{prop["id"]}" NOT IN ({placeholders})'
                    f"{clause} AND i.status != 'rejected'",
                    tuple(allowed) + params):
                out.append({"type_id": obj["id"], "property_id": prop["id"],
                            "instance_id": row["instance_id"],
                            "document_id": row["document_id"],
                            "value": row["value"], "allowed": list(allowed)})
    return out


def grounding(store: Store, document_id: str | None = None) -> dict:
    """How often each engine quoted something the document actually contains.

    The question a corpus run exists to answer, and the one `confidence` alone
    cannot: a row at `inferred` may be there because the model reported low
    confidence in a real quotation, or because it fabricated one. Those are
    opposite findings — the first is a calibrated model, the second is a model
    that cannot be trusted with a citation — and until `alignment` was recorded
    they were the same number.

    Only rows with an excerpt are counted. A finding with nothing quoted has
    nothing to locate, so scoring it as ungrounded would blame the engine for
    the ontology's shape.
    """
    clause, params = ("WHERE p.document_id = ?", (document_id,)) if document_id \
        else ("", ())
    rows = store.query(
        "SELECT p.source, p.alignment, COUNT(*) AS n "
        "FROM provenance p "
        f"{clause}{' AND' if clause else 'WHERE'} p.excerpt IS NOT NULL "
        "AND p.excerpt != '' "
        "GROUP BY p.source, p.alignment", params)

    by_source: dict[str, dict] = {}
    for row in rows:
        entry = by_source.setdefault(row["source"], {
            "source": row["source"], "n_quoted": 0, "n_grounded": 0,
            "n_fabricated": 0, "by_alignment": {}})
        entry["n_quoted"] += row["n"]
        entry["by_alignment"][row["alignment"] or "ungrounded"] = row["n"]
        if row["alignment"]:
            entry["n_grounded"] += row["n"]
        else:
            entry["n_fabricated"] += row["n"]

    out = []
    for entry in by_source.values():
        quoted = entry["n_quoted"]
        entry["grounded_rate"] = round(entry["n_grounded"] / quoted, 3) if quoted else None
        entry["fabrication_rate"] = round(entry["n_fabricated"] / quoted, 3) if quoted else None
        out.append(entry)
    out.sort(key=lambda e: e["source"])

    worst = max((e for e in out if e["n_quoted"]),
                key=lambda e: e["fabrication_rate"], default=None)
    if worst is None:
        note = "Nothing has been extracted with a quotation yet."
    elif worst["fabrication_rate"] == 0:
        note = ("Every quotation was located in its document. No engine has "
                "invented one.")
    else:
        note = (f"{worst['source']} quoted text its document does not contain "
                f"in {worst['fabrication_rate']:.0%} of findings "
                f"({worst['n_fabricated']} of {worst['n_quoted']}). Those are "
                "recorded at `inferred` rather than as fact.")
    return {"by_source": out, "note": note}


def quality_report(store: Store, document_id: str | None = None,
                   min_reviewed: int = 5) -> dict:
    """Everything above, in one call, with the verdict said out loud."""
    quality = extraction_quality(store, document_id, min_reviewed)
    calibration = confidence_calibration(store, document_id, min_reviewed)
    overall = quality["overall"]

    if overall.get("n_reviewed", 0) < min_reviewed:
        headline = (f"Only {overall.get('n_reviewed', 0)} instance(s) reviewed. "
                    "Not enough to say anything about extraction quality yet.")
    else:
        headline = (f"{overall['accuracy']:.0%} of reviewed instances were "
                    f"confirmed as extracted, {overall['amend_rate']:.0%} needed "
                    f"correcting and {overall['reject_rate']:.0%} were rejected, "
                    f"across {overall['coverage']:.0%} of the population.")

    return {
        "headline": headline,
        "extraction": quality,
        "calibration": calibration,
        "grounding": grounding(store, document_id),
        "concept_precision": concept_precision(store, document_id, min_reviewed),
        "property_corrections": property_corrections(store, document_id),
        "codelist_violations": codelist_violations(store, document_id=document_id),
    }
