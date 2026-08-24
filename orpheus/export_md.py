"""The store, projected out as a markdown knowledge bundle.

The point of the entity layer was that the knowledge be reusable by the next
project. Reusable through this API means reusable by anything that speaks this
API, which is one process and one schema — so the layer is projected out into
the format four independent projects have converged on: a directory of markdown
files, one concept per file, cross-linked, with an index for progressive
disclosure and no database in the middle.

The shape follows Google's Open Knowledge Format (v0.1) where it says anything —
YAML frontmatter, `type` as the only required field, `index.md`, `log.md`, file
path as concept identity — and DocIt's conventions where OKF is silent, because
those are what carry the parts of Orpheus that a plain wiki has no word for:

- `> **Tension**:` for a conflict somebody verified. Orpheus has a table of
  these; markdown has a convention for them, and they mean the same thing.
- `> **Inferred**:` for a claim below the `named` rubric level.
- `> **Context**:` for the one part of a page a person wrote rather than a
  document.

What does not change is the invariant. Every claim exported carries the document,
page and excerpt it came from, and a claim with no mention behind it is not
written — here as in the store. An export that quietly dropped the citations
would be the failure mode this whole model exists to avoid, and it would be
invisible, because the result would read beautifully.

Excerpts are the ground truth of the bundle and must not be edited downstream:
they are what the store can be checked against. That is stated in the index
rather than left to be inferred.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import bundle as bundle_mod
from . import entities as entities_mod
from . import tensions as tensions_mod
from .rubric import CONFIDENCE
from .store import Store
from .utils import now

FORMAT = "okf/0.1"
GENERATOR = "orpheus"

# Below this, a claim is offered rather than asserted, and is marked as such
# wherever it appears. The same threshold the review queue works to.
ASSERTED_AT = CONFIDENCE["named"]


# ---------------------------------------------------------------------------
# Files and names
# ---------------------------------------------------------------------------

def slug(text: str, fallback: str = "untitled") -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return out[:60] or fallback


def _unique(taken: set[str], candidate: str, identifier: str) -> str:
    """A stable file name, disambiguated by id rather than by a counter.

    A counter would renumber every page after the one that was added, so a
    re-export would rewrite files whose content had not changed and the diff
    would be useless for seeing what actually moved.
    """
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    suffixed = f"{candidate}-{identifier.split('_')[-1][:8]}"
    taken.add(suffixed)
    return suffixed


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def frontmatter(fields: dict) -> str:
    """OKF frontmatter. `type` is the only field the spec requires.

    Everything Orpheus adds is prefixed, so a consumer that knows only OKF sees
    a valid document and one that knows Orpheus can find its way back to the row.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            lines.append(f"{key}: [{', '.join(_yaml_scalar(v) for v in value)}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _quote(text: str | None) -> str:
    """A markdown blockquote that survives a multi-line excerpt."""
    lines = " ".join((text or "").split()).strip()
    return "> " + lines if lines else ""


# ---------------------------------------------------------------------------
# One entity
# ---------------------------------------------------------------------------

def entity_markdown(page: dict, links: dict, confirmed_only: bool = False) -> str:
    """One entity page, as markdown, with a source on every line."""
    entity = page["entity"]
    tensions = page.get("tensions") or []
    contested = set(page.get("contested_properties") or [])

    out = [frontmatter({
        "type": entity["type_id"],
        "title": entity["canonical_name"],
        "timestamp": now(),
        "aliases": page.get("aliases") or None,
        "orpheus_id": entity["entity_id"],
        "orpheus_status": entity["status"],
        "orpheus_resolution": page.get("resolution_quality"),
        "orpheus_sources": page["counts"]["mentions"],
        "orpheus_confirmed_sources": page["counts"]["confirmed_links"],
        "orpheus_tensions": len(tensions),
        "orpheus_corroborated": page.get("corroborated_properties") or None,
    }), "", f"# {entity['canonical_name']}", ""]

    if page.get("description"):
        # DocIt's marker for the one part of a page a person wrote. Kept
        # visually apart here for the same reason it is in the UI: everything
        # else on the page is answerable to a document and this is not.
        out += [f"> **Context**: {page['description']}", ""]

    if page.get("caveat"):
        out += [f"> **Inferred**: {page['caveat']}", ""]

    # Conflicts first, before the facts they are about. A reader who scrolls
    # past two contradictory rows has already been misled.
    if tensions:
        out += ["## Where the sources disagree", ""]
        for tension in tensions:
            state = ("checked, and it stands" if tension["status"] == "accepted"
                     else "not yet checked")
            out.append(f"> **Tension**: {tension['summary']} "
                       f"({tension['kind'].replace('_', ' ')}, {state})")
            for side in tension["sides"]:
                where = links["documents"].get(side["document_id"])
                cite = (f"[{side['filename']}]({where})" if where
                        else side.get("filename") or side["document_id"])
                page_no = f", p. {side['page_no']}" if side.get("page_no") else ""
                position = f"**{side['position']}** — " if side.get("position") else ""
                out.append(f">   - {position}{cite}{page_no}: "
                           f"{' '.join((side.get('excerpt') or '').split())}")
            if tension.get("resolution"):
                # Its own quoted paragraph, not a bare indented line: a lazy
                # continuation renders as part of the last bullet, which reads
                # as though the reviewer's reasoning came from that source.
                out += [">", f"> {tension['resolution']}"]
            out.append("")

    if page["properties"]:
        out += ["## What the sources say", "",
                "| Property | Value | Sources | Confirmed |",
                "|---|---|---|---|"]
        corroborated = set(page.get("corroborated_properties") or [])
        copied = set(page.get("copied_properties") or [])
        for prop, values in page["properties"].items():
            mark = (" ⚠︎" if prop in contested else
                    " ✓" if prop in corroborated else
                    " ⧉" if prop in copied else "")
            for i, value in enumerate(values):
                if confirmed_only and not value["n_confirmed"]:
                    continue
                label = f"{prop}{mark}" if i == 0 else ""
                out.append(f"| {label} | {value['value']} | "
                           f"{len(value['mentions'])} | {value['n_confirmed']} |")
        out += ["",
                "Where two sources disagree both values are listed; neither was "
                "chosen. ⚠︎ marks a disagreement somebody verified, ✓ marks "
                "agreement between documents that word it differently, and ⧉ "
                "marks the same sentence appearing in several documents — a "
                "citation chain rather than corroboration.", ""]

    relations = (page.get("corroboration") or {}).get("relations") or []
    if relations:
        out += ["## Related pages", "",
                "| Relation | Documents | Distinct wordings |",
                "|---|---|---|"]
        for relation in relations:
            other = (relation["to_name"]
                     if relation["from_entity_id"] == entity["entity_id"]
                     else relation["from_name"])
            other_id = (relation["to_entity_id"]
                        if relation["from_entity_id"] == entity["entity_id"]
                        else relation["from_entity_id"])
            target = links["entities"].get(other_id)
            cite = f"[{other}]({target})" if target else other
            note = "" if relation["independent"] else " (one wording, copied)"
            out.append(f"| {relation['link_type_id'].replace('_', ' ')} {cite} | "
                       f"{relation['n_documents']} | "
                       f"{relation['n_wordings']}{note} |")
        out.append("")

    shown = [m for m in page["mentions"]
             if not confirmed_only
             or m["link"]["status"] in ("confirmed", "amended")]
    asserted = [m for m in shown if m["link"]["status"] in ("confirmed", "amended")]
    proposed = [m for m in shown if m not in asserted]

    out += ["## Sources", ""]
    if not asserted:
        out += ["*Nothing here has been confirmed by a person yet.*", ""]
    for record in asserted:
        out += _source_lines(record, links)
    if proposed:
        out += ["### Proposed, not yet checked", "",
                "Suggested by machine matching. Listed apart so nothing below "
                "is mistaken for something a person vouched for.", ""]
        for record in proposed:
            out += _source_lines(record, links)

    out += ["---", "",
            f"Projected from Orpheus on {now()}. The quoted excerpts are the "
            "ground truth of this bundle: they are what the store can be "
            "checked against, and editing them here breaks that.", ""]
    return "\n".join(out)


def _source_lines(record: dict, links: dict) -> list[str]:
    document = record.get("document") or {}
    evidence = record.get("evidence") or {}
    target = links["documents"].get(record["document_id"])
    name = document.get("filename") or record["document_id"]
    cite = f"[{name}]({target})" if target else name
    page_no = f", p. {evidence['page_no']}" if evidence.get("page_no") else ""

    lines = [f"**{cite}**{page_no} — linked on {record['link']['basis']}, "
             f"{record['link']['status']}"]
    if evidence.get("excerpt"):
        lines += ["", _quote(evidence["excerpt"])]
    # Two different failures, told apart on purpose. A low-confidence quotation
    # is a cautious model; a quotation the document does not contain is an
    # invented one, and they must not read the same.
    if evidence and not evidence.get("alignment"):
        lines += ["", "> **Tension**: this quotation was not found in the "
                      "document it cites. It is evidence of the extraction, "
                      "not of the fact."]
    elif evidence.get("confidence") is not None and \
            evidence["confidence"] < ASSERTED_AT:
        lines += ["", f"> **Inferred**: recorded at confidence "
                      f"{evidence['confidence']}, below the level at which this "
                      f"bundle asserts anything."]
    for tension in record.get("tensions") or []:
        lines += ["", f"> **Tension**: disagrees with another source"
                      + (f" about {tension['property_id']}"
                         if tension.get("property_id") else "") + "."]
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------

def document_markdown(store: Store, document: dict, links: dict) -> str:
    """A source document, as a concept file.

    OKF has no notion of "the thing the knowledge was read from", and DocIt
    does: a source doc, whose quoted requirements are immutable and whose
    coverage is tracked. A contract is exactly that, so it is exported as one.
    """
    found = entities_mod.entities_in_document(store, document["document_id"])
    document_tensions = tensions_mod.tensions_for_document(
        store, document["document_id"])

    out = [frontmatter({
        "type": "source",
        "title": document["filename"],
        "timestamp": now(),
        "orpheus_id": document["document_id"],
        "orpheus_doc_type": document.get("doc_type"),
        "orpheus_review_status": document.get("review_status"),
        "orpheus_pages": document.get("n_pages"),
        "orpheus_ingested": document.get("date_added"),
    }), "", f"# {document['filename']}", "",
        f"{document.get('doc_type') or 'unclassified'} · "
        f"{document.get('n_pages') or '?'} page(s) · ingested "
        f"{document.get('date_added')} · review: "
        f"{document.get('review_status')}", ""]

    if document_tensions:
        out += ["## Conflicts this document is part of", ""]
        for tension in document_tensions:
            out.append(f"> **Tension**: {tension['summary']} "
                       f"({tension['status']})")
        out.append("")

    out += ["## What was read from it", ""]
    if found:
        out += ["| Page | Type | Link basis | Reviewed |", "|---|---|---|---|"]
        for row in found:
            target = links["entities"].get(row["entity_id"])
            name = row["canonical_name"]
            cite = f"[{name}]({target})" if target else name
            out.append(f"| {cite} | {row['type_id']} | {row['basis']} | "
                       f"{row['link_status']} |")
        out.append("")
    else:
        # A gap, said out loud. DocIt's rule: a section with nothing
        # implementing it is a documented gap, not an omission.
        out += ["*Nothing from this document has been linked to a page.* "
                "It is in the corpus and contributes nothing to the knowledge "
                "below — a gap, not an omission.", ""]

    counts = store.query(
        "SELECT type_id, COUNT(*) AS n FROM instance_index "
        "WHERE document_id = ? GROUP BY type_id ORDER BY n DESC",
        (document["document_id"],))
    if counts:
        out += ["## Extracted", "",
                ", ".join(f"{c['n']} {c['type_id']}" for c in counts), ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------

def export(store: Store, out_dir: str | Path, type_id: str | None = None,
           confirmed_only: bool = False, limit: int = 1000) -> dict:
    """Write the bundle. Returns what was written and what was left out."""
    root = Path(out_dir)
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "documents").mkdir(parents=True, exist_ok=True)

    listed = entities_mod.list_entities(store, type_id=type_id, limit=limit)
    documents = store.query(
        "SELECT document_id, filename, doc_type, n_pages, date_added, "
        "       review_status FROM documents ORDER BY date_added")

    # Both link maps are built before a single file is written, because an
    # entity page cites documents and a document page cites entities. Writing
    # as we go would leave every forward reference broken.
    taken_e: set[str] = set()
    taken_d: set[str] = set()
    links = {"entities": {}, "documents": {}}
    names = {"entities": {}, "documents": {}}
    for entity in listed:
        name = _unique(taken_e, slug(entity["canonical_name"]),
                       entity["entity_id"])
        names["entities"][entity["entity_id"]] = name
        links["entities"][entity["entity_id"]] = f"../entities/{name}.md"
    for document in documents:
        name = _unique(taken_d, slug(document["filename"]),
                       document["document_id"])
        names["documents"][document["document_id"]] = name
        links["documents"][document["document_id"]] = f"../documents/{name}.md"

    written, skipped = [], []
    for entity in listed:
        page = entities_mod.entity_page(store, entity["entity_id"],
                                        include_unconfirmed=not confirmed_only)
        if not page["mentions"]:
            # The invariant, enforced at the boundary too: a page with no
            # source behind it is not written out, however it got into the
            # table. The lint reports these; the export refuses them.
            skipped.append({"entity_id": entity["entity_id"],
                            "name": entity["canonical_name"],
                            "reason": "no source behind it"})
            continue
        path = root / "entities" / f"{names['entities'][entity['entity_id']]}.md"
        path.write_text(entity_markdown(page, links, confirmed_only),
                        encoding="utf-8")
        written.append(str(path))

    for document in documents:
        path = root / "documents" / f"{names['documents'][document['document_id']]}.md"
        path.write_text(document_markdown(store, document, links),
                        encoding="utf-8")
        written.append(str(path))

    index = root / "index.md"
    index.write_text(_index_markdown(store, listed, documents, names, skipped,
                                     type_id, confirmed_only), encoding="utf-8")
    log = root / "log.md"
    log.write_text(_log_markdown(store), encoding="utf-8")
    written += [str(index), str(log)]

    return {"root": str(root), "n_files": len(written),
            "n_entities": len(listed) - len(skipped),
            "n_documents": len(documents),
            "skipped": skipped, "files": written, "format": FORMAT}


def _index_markdown(store: Store, listed: list[dict], documents: list[dict],
                    names: dict, skipped: list[dict], type_id: str | None,
                    confirmed_only: bool) -> str:
    active = bundle_mod.active(store) or {}
    standing = tensions_mod.list_tensions(store, open_only=True, limit=500)

    # ontologySpecR keys, which are camelCase, and a name that lives under
    # `metadata`. Reading `id`/`name` off the top level returns None for every
    # bundle -- so the index would be titled "Orpheus knowledge bundle" whatever
    # domain it described, and the one artefact meant to travel would be the one
    # that hid whose knowledge it was.
    metadata = active.get("metadata") or {}
    title = metadata.get("name") or "Orpheus knowledge bundle"
    description = metadata.get("description")

    out = [frontmatter({
        "type": "index",
        "title": title,
        "timestamp": now(),
        "format": FORMAT,
        "generator": GENERATOR,
        "orpheus_bundle": active.get("bundleId"),
        "orpheus_bundle_version": active.get("bundleVersion"),
        "orpheus_entities": len(listed) - len(skipped),
        "orpheus_documents": len(documents),
        "orpheus_tensions": len(standing),
    }), "", f"# {title}", ""]
    if description:
        out += [description, ""]
    out += [
        "Every claim in this bundle carries the document, page and excerpt it "
        "was read from. A claim with no source behind it is not written — that "
        "is the property that makes this reusable rather than merely readable.",
        "",
        "**The quoted excerpts are immutable.** They are the ground truth this "
        "bundle can be checked against; editing one severs it from the record "
        "it was taken from.", "",
        "Markers follow the conventions a reader is likely to already know:",
        "",
        "- `> **Tension**:` — sources that disagree, verified. Not a gap in the "
        "reading: a conflict that is really there, and usually the part worth "
        "knowing.",
        "- `> **Inferred**:` — a claim below the level at which this bundle "
        "asserts anything.",
        "- `> **Context**:` — written by a person rather than read from a "
        "document.", ""]

    if confirmed_only:
        out += ["Exported with unconfirmed material excluded: everything here "
                "was checked by a person.", ""]
    else:
        out += ["Includes proposals a person has not checked. They are listed "
                "under their own heading on each page and never mixed in.", ""]

    if standing:
        out += ["## Where the sources disagree", ""]
        for tension in standing[:50]:
            target = f"entities/{names['entities'].get(tension['subject_id'], '')}.md"
            link = (f"[{tension['summary']}]({target})"
                    if tension["subject_id"] in names["entities"]
                    else tension["summary"])
            out.append(f"- {link} — {tension['status']}")
        out.append("")

    out += ["## Pages", ""]
    for entity in listed:
        if entity["entity_id"] not in names["entities"]:
            continue
        if any(s["entity_id"] == entity["entity_id"] for s in skipped):
            continue
        out.append(f"- [{entity['canonical_name']}]"
                   f"(entities/{names['entities'][entity['entity_id']]}.md) — "
                   f"{entity['type_id']}, {entity['n_documents']} source(s)"
                   + (f", {entity['n_tensions']} conflict(s)"
                      if entity.get("n_tensions") else ""))
    out += ["", "## Sources", ""]
    for document in documents:
        out.append(f"- [{document['filename']}]"
                   f"(documents/{names['documents'][document['document_id']]}.md)"
                   f" — {document.get('doc_type') or 'unclassified'}")

    if skipped:
        out += ["", "## Not exported", "",
                "Pages with no source behind them. They exist in the store and "
                "are reported by `orpheus lint`; they are not written here, "
                "because a bundle of uncited assertions is the thing this "
                "format is meant to prevent.", ""]
        for entry in skipped:
            out.append(f"- {entry['name']} — {entry['reason']}")
    out.append("")
    return "\n".join(out)


def _log_markdown(store: Store, limit: int = 500) -> str:
    """OKF's `log.md`: what happened, newest first.

    Read from `edit_history`, which is append-only and ordered by `seq` rather
    than by time — rows written in one transaction share a timestamp to the
    second, and this is the one file where getting that order wrong would be
    read as the sequence of events.
    """
    rows = store.query(
        "SELECT seq, edited_at, edited_by, table_name, action, note "
        "FROM edit_history ORDER BY seq DESC LIMIT ?", (limit,))
    out = [frontmatter({"type": "log", "title": "History",
                        "timestamp": now()}), "", "# History", "",
           "Append-only, newest first. Ordered by sequence rather than by "
           "timestamp, because changes made in one transaction share a "
           "timestamp to the second.", "",
           "| # | When | Who | What | Where | Note |", "|---|---|---|---|---|---|"]
    for row in rows:
        note = " ".join((row["note"] or "").split())[:80]
        out.append(f"| {row['seq']} | {row['edited_at']} | "
                   f"{row['edited_by'] or '—'} | {row['action']} | "
                   f"{row['table_name']} | {note} |")
    out.append("")
    return "\n".join(out)
