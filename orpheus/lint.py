"""An adversarial pass over the store.

`quality.py` asks whether the confidence rubric ranks reliability — a
calibration question, answered with rates. This asks a different one: *where is
this store lying to a reader?* It is not a health check and it is not meant to
be reassuring. A run that reports nothing on a corpus of four documents has
found nothing because there was nothing to find, and says so rather than
implying the corpus is sound.

Every finding is **located**. "Some pages may be inconsistent" is not a finding;
"Ardmore Digital Ltd has two confirmed values for `address` and no recorded
conflict" is, because a person can open it and be looking at the problem. A
report of general observations is one nobody acts on.

The checks are ordered by what they cost a reader who trusts the output:

- A page asserting something with no source behind it is the worst, because the
  whole model rests on being unable to write one.
- A quotation the document does not contain is next: it looks like evidence.
- A conflict smoothed into two tidy rows is third, and is the one a summariser
  produces by default.

Then coverage and staleness, which mislead by omission rather than by assertion.
"""

from __future__ import annotations

from typing import Any

from . import entities as entities_mod
from .store import Store

SEVERITIES = ("high", "medium", "low")

# Anything past this and a "no findings" result says more about how little has
# been reviewed than about how sound the store is.
ENOUGH_TO_JUDGE = 20


def _finding(check: str, severity: str, where: dict, finding: str,
             suggestion: str) -> dict:
    return {"check": check, "severity": severity, "where": where,
            "finding": finding, "suggestion": suggestion}


# ---------------------------------------------------------------------------
# Assertions with nothing behind them
# ---------------------------------------------------------------------------

def uncited_pages(store: Store, limit: int = 50) -> list[dict]:
    """Pages that assert something no document says.

    The one failure the entity model is built to make impossible, so finding
    any is a sign something wrote to `entities` without going through
    `link_mention` — which is worth knowing immediately.
    """
    rows = store.query(
        "SELECT e.entity_id, e.canonical_name, e.type_id, e.status, e.description, "
        "       COUNT(m.instance_id) AS n_live "
        "FROM entities e "
        "LEFT JOIN entity_mentions m ON m.entity_id = e.entity_id "
        "  AND m.unlinked_at IS NULL "
        "WHERE e.merged_into IS NULL "
        "GROUP BY e.entity_id HAVING n_live = 0 LIMIT ?", (limit,))
    out = []
    for row in rows:
        asserts = bool(row["description"]) or row["status"] in ("confirmed", "amended")
        out.append(_finding(
            "uncited_page", "high" if asserts else "low",
            {"entity_id": row["entity_id"], "name": row["canonical_name"]},
            (f"{row['canonical_name']} ({row['type_id']}) is {row['status']} and "
             f"has no mention behind it"
             + (", but carries a written description" if row["description"] else "")),
            ("Link the mentions it was made from, or reject the page. A page "
             "with no source is the one thing this model is supposed to make "
             "unwritable.")))
    return out


def ungrounded_quotations(store: Store, document_id: str | None = None,
                          limit: int = 50) -> list[dict]:
    """Excerpts that do not appear in the document they cite.

    `alignment` is computed, never taken from the model, so this is the
    difference between a cautious extraction and an invented one — and both
    otherwise land at the same confidence.
    """
    clause, params = ("AND p.document_id = ?", [document_id]) if document_id \
        else ("", [])
    rows = store.query(
        "SELECT p.instance_id, p.document_id, p.excerpt, p.source, p.confidence, "
        "       d.filename, i.type_id "
        "FROM provenance p "
        "LEFT JOIN documents d ON d.document_id = p.document_id "
        "LEFT JOIN instance_index i ON i.instance_id = p.instance_id "
        f"WHERE p.alignment IS NULL AND p.excerpt IS NOT NULL AND p.excerpt != '' "
        f"{clause} LIMIT ?", (*params, limit))
    return [_finding(
        "ungrounded_quotation", "high",
        {"instance_id": r["instance_id"], "document_id": r["document_id"],
         "filename": r["filename"], "type_id": r["type_id"]},
        (f"{r['source']} quoted {_short(r['excerpt'])!r} as evidence, and "
         f"{r['filename'] or r['document_id']} does not contain it"),
        ("Reject it, or amend it to what the document says. It is currently "
         "rendered as a citation."))
        for r in rows]


# ---------------------------------------------------------------------------
# Consensus disguising conflict
# ---------------------------------------------------------------------------

def smoothed_conflicts(store: Store, limit: int = 50) -> list[dict]:
    """Reviewed mentions that disagree, with nothing recorded saying so.

    The finding a summariser produces by default: two confirmed values, listed
    one under the other in the same voice, reading as though they agree. Until
    a tension exists the page has no way to say otherwise.
    """
    from . import tensions as tensions_mod

    conflicts = tensions_mod.detect_conflicts(store)
    out = []
    for conflict in conflicts:
        if conflict["existing_tension_id"]:
            continue
        out.append(_finding(
            "smoothed_conflict", "high",
            {"entity_id": conflict["subject_id"],
             "name": conflict["subject_name"],
             "property_id": conflict["property_id"]},
            (f"{conflict['subject_name']} has {conflict['n_values']} confirmed "
             f"values for {conflict['property_id']} and no recorded conflict"),
            ("Raise a tension. Both are probably right about the moment each "
             "document was written, and that is worth saying rather than "
             "leaving two rows to be read as agreement.")))
        if len(out) >= limit:
            break
    return out


def unchecked_conflicts(store: Store, limit: int = 50) -> list[dict]:
    """Conflicts nobody has ruled on.

    Medium, not high: the page already shows the disagreement, so a reader is
    not being misled. What is missing is the judgement about whether it matters.
    """
    rows = store.query(
        "SELECT t.tension_id, t.subject_id, t.summary, t.property_id, t.raised_at, "
        "       e.canonical_name "
        "FROM tensions t LEFT JOIN entities e ON e.entity_id = t.subject_id "
        "WHERE t.status = 'open' ORDER BY t.raised_at LIMIT ?", (limit,))
    return [_finding(
        "unchecked_conflict", "medium",
        {"tension_id": r["tension_id"], "entity_id": r["subject_id"],
         "name": r["canonical_name"], "property_id": r["property_id"]},
        f"raised {r['raised_at']}, still unruled: {r['summary']}",
        ("Accept it if the conflict is real, resolve it if one side wins, "
         "withdraw it if it was formatting. All three are finished work."))
        for r in rows]


# ---------------------------------------------------------------------------
# Misleading by omission
# ---------------------------------------------------------------------------

def orphan_mentions(store: Store, limit: int = 50) -> list[dict]:
    """Confirmed mentions of a named thing that no page includes.

    Invisible in exactly the way that matters: the wiki looks complete, the
    entity page for that company is missing a document, and nothing on either
    surface says so.
    """
    out = []
    for type_id, table in entities_mod._named_tables(store):
        rows = store.query(
            f'SELECT x.instance_id, x.document_id, x.name, x.status, d.filename '
            f'FROM "{table}" x '
            f"LEFT JOIN documents d ON d.document_id = x.document_id "
            f"LEFT JOIN entity_mentions m ON m.instance_id = x.instance_id "
            f"  AND m.unlinked_at IS NULL "
            f"WHERE m.instance_id IS NULL AND x.status IN ('confirmed', 'amended') "
            f"LIMIT ?", (limit,))
        for row in rows:
            out.append(_finding(
                "orphan_mention", "medium",
                {"instance_id": row["instance_id"], "type_id": type_id,
                 "document_id": row["document_id"], "filename": row["filename"]},
                (f"{row['name']!r} in {row['filename'] or row['document_id']} is "
                 f"{row['status']} and belongs to no page"),
                "Link it to a page, or make one. The wiki cannot see it."))
    return out[:limit]


def split_pages(store: Store, limit: int = 25) -> list[dict]:
    """Two pages that are probably one thing.

    The queue cannot show these: every mention has a home, so the split is
    invisible precisely when the machine has finished and the queue reads empty.
    """
    return [_finding(
        "split_page", "medium",
        {"entity_id": pair["keep"]["entity_id"],
         "other_entity_id": pair["merge"]["entity_id"],
         "name": pair["keep"]["canonical_name"]},
        (f"{pair['keep']['canonical_name']!r} and "
         f"{pair['merge']['canonical_name']!r} look like one thing "
         f"({pair['evidence']})"),
        "Merge, or confirm both -- either way the queue will not raise it.")
        for pair in entities_mod.duplicate_pages(store, limit=limit)]


def unextracted_documents(store: Store, limit: int = 50) -> list[dict]:
    """Documents ingested and never extracted from.

    A corpus report over these is arithmetic on a smaller corpus than the one a
    reader thinks they are looking at.
    """
    rows = store.query(
        "SELECT d.document_id, d.filename, d.date_added, d.doc_type "
        "FROM documents d "
        "LEFT JOIN instance_index i ON i.document_id = d.document_id "
        "WHERE i.instance_id IS NULL "
        "GROUP BY d.document_id ORDER BY d.date_added LIMIT ?", (limit,))
    return [_finding(
        "unextracted_document", "medium",
        {"document_id": r["document_id"], "filename": r["filename"]},
        f"{r['filename']} was ingested {r['date_added']} and nothing was extracted",
        "Run extraction, or say why it was skipped. Every corpus count "
        "currently excludes it silently.")
        for r in rows]


def stale_evaluations(store: Store, limit: int = 50) -> list[dict]:
    """Analyses whose evidence has since been amended."""
    rows = store.query(
        "SELECT evaluation_id, concept_id, target_document_id, stale_reason, "
        "       generated_at FROM concept_evaluations "
        "WHERE stale = 1 ORDER BY generated_at LIMIT ?", (limit,))
    return [_finding(
        "stale_evaluation", "medium",
        {"evaluation_id": r["evaluation_id"], "concept_id": r["concept_id"],
         "document_id": r["target_document_id"]},
        (f"{r['concept_id']} on {r['target_document_id']} was generated "
         f"{r['generated_at']} and is stale: {r['stale_reason']}"),
        "Re-evaluate. It is still being served as a current reading.")
        for r in rows]


def unreviewed_groupings(store: Store, min_documents: int = 3,
                         limit: int = 50) -> list[dict]:
    """Pages joining several documents that nobody has checked.

    A page over one document is a proposal. A page over four is an assertion
    that four documents are about one thing, and it deserves review in
    proportion to how much it claims.
    """
    rows = store.query(
        "SELECT e.entity_id, e.canonical_name, "
        "       COUNT(DISTINCT m.document_id) AS n_documents, "
        "       SUM(CASE WHEN m.status IN ('confirmed','amended') THEN 1 ELSE 0 END) "
        "         AS n_confirmed "
        "FROM entities e JOIN entity_mentions m ON m.entity_id = e.entity_id "
        "  AND m.unlinked_at IS NULL "
        "WHERE e.merged_into IS NULL "
        "GROUP BY e.entity_id "
        "HAVING n_documents >= ? AND n_confirmed = 0 LIMIT ?",
        (min_documents, limit))
    return [_finding(
        "unreviewed_grouping", "low",
        {"entity_id": r["entity_id"], "name": r["canonical_name"]},
        (f"{r['canonical_name']} joins {r['n_documents']} documents on machine "
         f"evidence alone -- no link confirmed"),
        "Confirm the links or split the page. It is currently asserting a "
        "grouping nobody has checked.")
        for r in rows]


def _short(text: str | None, width: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

CHECKS = {
    "uncited_page": lambda s, d: uncited_pages(s),
    "ungrounded_quotation": lambda s, d: ungrounded_quotations(s, d),
    "smoothed_conflict": lambda s, d: smoothed_conflicts(s),
    "unchecked_conflict": lambda s, d: unchecked_conflicts(s),
    "orphan_mention": lambda s, d: orphan_mentions(s),
    "split_page": lambda s, d: split_pages(s),
    "unextracted_document": lambda s, d: unextracted_documents(s),
    "stale_evaluation": lambda s, d: stale_evaluations(s),
    "unreviewed_grouping": lambda s, d: unreviewed_groupings(s),
}

# The cheap ones. `smoothed_conflict` and `split_page` both compare every
# mention against every other, which is fine for a page and slow for a corpus.
SHALLOW = ("uncited_page", "ungrounded_quotation", "unchecked_conflict",
           "unextracted_document", "stale_evaluation")


def lint(store: Store, deep: bool = True, document_id: str | None = None,
         checks: list[str] | None = None) -> dict:
    """Run the adversarial pass and report located problems.

    `deep` is the default, which is the opposite of the usual arrangement, on
    purpose: the two checks it adds are the two that find conflict smoothed
    into consensus, and those are the ones worth the wait.
    """
    names = checks or (list(CHECKS) if deep else list(SHALLOW))
    findings: list[dict] = []
    ran = []
    for name in names:
        check = CHECKS.get(name)
        if check is None:
            continue
        ran.append(name)
        findings.extend(check(store, document_id))

    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["check"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}

    return {"headline": _headline(store, findings, counts, ran),
            "checks_run": ran, "counts": counts,
            "n_findings": len(findings), "findings": findings}


def _headline(store: Store, findings: list[dict], counts: dict,
              ran: list[str]) -> str:
    """Say what was found, or why finding nothing proves nothing.

    A clean bill of health is the most dangerous thing this could return, so it
    is the one answer it will not give without qualification.
    """
    reviewed = store.scalar(
        "SELECT COUNT(*) FROM entity_mentions WHERE status IN "
        "('confirmed','amended') AND unlinked_at IS NULL") or 0
    if findings:
        worst = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
        return (f"{len(findings)} located problem(s) across {len(ran)} check(s): "
                f"{worst}. Most-serious first.")
    if reviewed < ENOUGH_TO_JUDGE:
        return (f"Nothing found, but only {reviewed} link(s) have been reviewed. "
                f"Most of these checks compare things a person has confirmed, so "
                f"this says more about how little has been checked than about "
                f"whether the store is sound.")
    return (f"Nothing found across {len(ran)} check(s), over {reviewed} reviewed "
            f"link(s). That is evidence, not proof: these checks find "
            f"contradictions, uncited claims and gaps, and they cannot find a "
            f"claim that is wrong in a way every source agrees on.")
