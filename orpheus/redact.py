"""Removing a document without removing the record that it was here.

Everything else in this package is built to be immutable. `provenance` is the
record of what the machine said. A rejected extraction is kept, because
deleting it would throw away the measurement along with the mistake. Nothing
anywhere deletes: `delete` has sat in `rubric.ACTIONS` since the beginning,
`auth.can` computes it for every actor, and until this module nothing consumed
it.

That is the right default for an audit trail and the wrong one for a corpus of
contracts. Contracts carry names, signatures, addresses, and third parties who
never agreed to be in anybody's database. Somebody uploads the wrong file.
Somebody uploads a file they were not cleared to hold. Somebody asks to be
erased. A deployment with no answer to any of those cannot responsibly take a
document in at all -- and the answer cannot be "destroy the store", which is
what it was.

**A redaction is not a delete.** The `documents` row survives as a tombstone:
the count stays true, the audit trail keeps its order, and "a document was
here, this person removed it, on this date, for this reason" stays answerable.
Everything read *from* the document is destroyed -- its text, its page images,
its extracted values, every excerpt quoting it, the relations drawn from it,
and the stored original itself.

Three decisions worth stating, because each could reasonably have gone the
other way:

**The audit trail keeps its rows and loses its payloads.** `edit_history` holds
`previous_value` and `new_value`, so an amendment records the text on both
sides of it. Keeping those would mean the history quietly retained what the
redaction was for. Dropping the rows would break the `seq` chain that makes the
history a history. So the rows stay, in order, with their payloads nulled: what
happened and who did it survives, what it said does not.

**A page whose every source is redacted is deleted, not left standing.**
`entity_mentions` for the document go, and any page left with no live mention
goes with them. An entity page that outlived all of its sources would assert
something no document says, which `lint.uncited_pages` calls the worst failure
this model can have -- and it would be right.

**The file hash is kept.** It is the one identifying thing that stays, and the
alternative is worse: without it the same file re-ingested would sail through
deduplication and be extracted all over again, resurrecting exactly what was
erased. A redacted document is a fact about the corpus that a re-upload must
run into, so it does.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from . import bundle as bundle_mod
from .audit import record_edit
from .store import Store
from .utils import NotFound, OrpheusError, now

#: What a tombstone keeps, and nothing else. Everything absent from this list
#: is cleared on the `documents` row itself.
#:
#: The classification is kept because a doc_type is a category, not content:
#: it is what makes "the corpus held 12 contracts, one since redacted" a
#: sentence anybody can still write. `filename` is *not* kept -- a filename is
#: content, and often the most direct kind ("Jane Doe medical report.pdf").
TOMBSTONE_KEEPS = (
    "document_id", "file_hash", "n_pages", "doc_type", "sector", "jurisdiction",
    "classification_source", "classification_confidence", "classification_status",
    "date_added", "created_by", "visibility", "review_status", "reviewed_by",
    "reviewed_at", "redacted_at", "redacted_by", "redaction_note",
)

#: Cleared on the row. `byte_size` goes with the file: reporting the size of
#: something nobody can read is a fingerprint and not a fact anyone needs.
TOMBSTONE_CLEARS = ("storage_path", "mime_type", "byte_size", "text_source")

#: `filename` is `NOT NULL`, and rightly so -- every other row in this table
#: has one, and a rebuild to make one column nullable for one case would be a
#: worse trade than a name that says what happened. A filename is content, and
#: often the most direct kind ("Jane Doe medical report.pdf"), so it does not
#: survive; this stands in its place and reads correctly in every listing that
#: was already showing filenames.
REDACTED_FILENAME = "(redacted)"


def is_redacted(store: Store, document_id: str) -> bool:
    row = store.one("SELECT redacted_at FROM documents WHERE document_id = ?",
                    (document_id,))
    return bool(row and row["redacted_at"])


def redacted_documents(store: Store) -> list[dict]:
    return store.query(
        "SELECT document_id, redacted_at, redacted_by, redaction_note, date_added "
        "FROM documents WHERE redacted_at IS NOT NULL ORDER BY redacted_at DESC")


def _instance_tables(store: Store) -> list[str]:
    """Every table a bundle files instances in.

    Read from the bundle rather than from `sqlite_master LIKE 'instances_%'`,
    because a redaction that missed a table would be a silent partial erasure
    -- and the bundle is the only authority on which tables the domain has.
    """
    bundle = bundle_mod.active(store)
    if not bundle:
        return []
    return [bundle_mod.table_name(o) for o in bundle.get("objects") or []]


def _remove_files(store: Store, document_id: str, storage_path: str | None,
                  dry_run: bool) -> list[str]:
    """The original, and the page images rendered from it for OCR.

    The images are as much the document as the file is -- a rendered page of a
    scanned contract is the contract -- and they live outside the row that
    points at them, so a redaction that only unlinked `storage_path` would
    leave the whole document sitting in `storage/pages/`.
    """
    targets: list[Path] = []
    if storage_path:
        targets.append(Path(storage_path))
    for row in store.query(
            "SELECT DISTINCT image_path FROM document_pages "
            "WHERE document_id = ? AND image_path IS NOT NULL", (document_id,)):
        targets.append(Path(row["image_path"]))

    removed = []
    for target in targets:
        if not target.exists():
            continue
        removed.append(str(target))
        if not dry_run:
            target.unlink()
    # The per-document image directory `_apply_ocr` creates, which is empty
    # once its pages are gone and is itself named after the document.
    for target in targets:
        parent = target.parent
        if parent.name == document_id and parent.parent.name == "pages":
            if not dry_run and parent.exists() and not any(parent.iterdir()):
                shutil.rmtree(parent, ignore_errors=True)
            break
    return removed


def redact_document(store: Store, document_id: str, *, actor_id: str | None = None,
                    note: str | None = None, dry_run: bool = False) -> dict:
    """Destroy everything read from a document; keep the record that it was here.

    Irreversible, and deliberately so -- a redaction you can undo is not one.
    `dry_run` counts what would go without touching anything, because the only
    honest way to offer an irreversible action is to let somebody look first.

    A `note` is required. A redaction nobody can account for later is
    indistinguishable from data loss, and the person who has to account for it
    is rarely the person who did it.
    """
    document = store.one("SELECT * FROM documents WHERE document_id = ?",
                         (document_id,))
    if document is None:
        raise NotFound(f"No document {document_id!r}.")
    if document["redacted_at"]:
        raise OrpheusError(
            f"{document_id!r} was already redacted on {document['redacted_at']}. "
            "There is nothing left to remove.")
    if not (note or "").strip():
        raise OrpheusError(
            "Give a note saying why. A redaction is irreversible and the only "
            "thing that survives it is the account of why it happened.")

    counts: dict[str, int] = {}

    def wipe(sql: str, params: tuple, label: str) -> None:
        counted = store.query(f"SELECT COUNT(*) AS n FROM {sql}", params)
        counts[label] = counted[0]["n"]
        if not dry_run and counts[label]:
            store.execute(f"DELETE FROM {sql}", params)

    instance_ids = [r["instance_id"] for r in store.query(
        "SELECT instance_id FROM instance_index WHERE document_id = ?",
        (document_id,))]
    # Collected before the sides are wiped, because after that there is nothing
    # left joining a tension to this document.
    tension_ids = [r["tension_id"] for r in store.query(
        "SELECT DISTINCT tension_id FROM tension_sides WHERE document_id = ?",
        (document_id,))]
    entity_ids = [r["entity_id"] for r in store.query(
        "SELECT DISTINCT entity_id FROM entity_mentions WHERE document_id = ?",
        (document_id,))]

    files = _remove_files(store, document_id, document["storage_path"], dry_run)

    # A dry run opens no transaction, so it can be asked of a store opened
    # read-only -- which is the state anybody looking before they leap should
    # be in.
    with (contextlib.nullcontext() if dry_run else store.transaction()):
        # Order matters only where a row points at another. Instances go last
        # of the instance-shaped things, so nothing is left pointing at one.
        wipe("entity_mentions WHERE document_id = ?", (document_id,), "mentions")
        wipe("provenance WHERE document_id = ?", (document_id,), "provenance")
        wipe("edges WHERE document_id = ?", (document_id,), "edges")
        wipe("suggestions WHERE document_id = ?", (document_id,), "suggestions")
        wipe("ontology_evidence WHERE document_id = ?", (document_id,),
             "ontology_evidence")
        wipe("tension_sides WHERE document_id = ?", (document_id,), "tension_sides")
        # `observed_value` is a value read out of the document.
        wipe("schema_amendments WHERE document_id = ?", (document_id,),
             "schema_amendments")
        # `result` is generated prose *about* the document, which quotes it.
        wipe("concept_evaluations WHERE target_document_id = ?", (document_id,),
             "evaluations")
        wipe("document_pages WHERE document_id = ?", (document_id,), "pages")
        wipe("document_shares WHERE document_id = ?", (document_id,), "shares")
        wipe("reading_passages WHERE document_id = ?", (document_id,), "passages")

        if instance_ids:
            marks = ",".join("?" * len(instance_ids))
            wipe(f"concept_evaluation_dependencies WHERE instance_id IN ({marks})",
                 tuple(instance_ids), "evaluation_dependencies")
        n_instances = 0
        for table in _instance_tables(store):
            rows = store.query(
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE document_id = ?',
                (document_id,))
            n_instances += rows[0]["n"]
            if not dry_run and rows[0]["n"]:
                store.execute(f'DELETE FROM "{table}" WHERE document_id = ?',
                              (document_id,))
        counts["instances"] = n_instances
        wipe("instance_index WHERE document_id = ?", (document_id,),
             "instance_index")

        # Any tension this document was a side of goes entirely, not just its
        # side. `summary` and `detail` are prose written about a disagreement
        # between two named passages, so a tension that keeps its other side
        # keeps a description of the one that was redacted. And a tension
        # holding a single position is not a disagreement about anything: it
        # would show a reader a conflict with nothing on the other side of it.
        counts["tensions"] = len(tension_ids)
        if not dry_run:
            for tension_id in tension_ids:
                store.execute("DELETE FROM tension_sides WHERE tension_id = ?",
                              (tension_id,))
                store.execute("DELETE FROM tensions WHERE tension_id = ?",
                              (tension_id,))

        # A page that outlived every one of its sources asserts something no
        # document says.
        # On a dry run the mentions are still there, so "how many are left"
        # has to be asked as "how many are left that are not this document's".
        # A dry run that under-reports what it would destroy is worse than no
        # dry run at all: it is a reassurance.
        surviving = ("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ? "
                     "AND unlinked_at IS NULL"
                     + (" AND document_id != ?" if dry_run else ""))
        orphaned = []
        for entity_id in entity_ids:
            params = (entity_id, document_id) if dry_run else (entity_id,)
            if not store.scalar(surviving, params):
                orphaned.append(entity_id)
        counts["entity_pages"] = len(orphaned)
        if not dry_run:
            for entity_id in orphaned:
                store.execute("DELETE FROM entity_mentions WHERE entity_id = ?",
                              (entity_id,))
                store.execute("DELETE FROM entities WHERE entity_id = ?",
                              (entity_id,))
                # A recorded decision about whether two pages were the same
                # thing, where one of them no longer exists. The rationale is
                # a person's own words about a page that is gone, so it goes
                # with it rather than dangling.
                store.execute(
                    "DELETE FROM resolution_reviews WHERE entity_a = ? OR "
                    "entity_b = ?", (entity_id, entity_id))

        counts["files"] = len(files)

        if dry_run:
            return {"document_id": document_id, "dry_run": True,
                    "would_remove": counts, "files": files,
                    "headline": _headline(document_id, counts, dry_run=True)}

        # The history keeps its rows and loses its payloads. Dropping the rows
        # would break the `seq` chain that makes it a history; keeping the
        # payloads would mean the audit trail quietly retained what the
        # redaction was for.
        # By `row_id` as well as by `document_id`. An entity page is created
        # against the `entities` table with no document on the row -- a page
        # is drawn from several -- so clearing by document alone left the
        # page's canonical name sitting in `new_value` after the page itself
        # was gone. The leak scan in `tests/test_redact.py` is what found
        # that, which is the argument for scanning rather than enumerating.
        touched = list(instance_ids) + orphaned
        marks = ",".join("?" * len(touched))
        where = ("document_id = ?" + (f" OR row_id IN ({marks})" if touched else ""))
        params = (document_id, *touched)
        counts["history_payloads_cleared"] = store.scalar(
            f"SELECT COUNT(*) FROM edit_history WHERE ({where}) AND "
            "(previous_value IS NOT NULL OR new_value IS NOT NULL)", params)
        store.execute(
            "UPDATE edit_history SET previous_value = NULL, new_value = NULL, "
            f"note = NULL WHERE {where}", params)

        cleared = {column: None for column in TOMBSTONE_CLEARS}
        cleared["filename"] = REDACTED_FILENAME
        store.execute(
            "UPDATE documents SET "
            + ", ".join(f"{c} = NULL" for c in TOMBSTONE_CLEARS)
            + ", filename = ?, redacted_at = ?, redacted_by = ?, "
            "redaction_note = ? WHERE document_id = ?",
            (REDACTED_FILENAME, now(), actor_id, note.strip(), document_id))

        # Appended after the payloads are cleared, so this row survives with
        # its own. It is the only thing in the history that still says
        # anything, and what it says is why.
        record_edit(store, "documents", document_id, document_id, "redact",
                    previous={"filename": document["filename"]},
                    new={**cleared, "redacted": True},
                    actor_id=actor_id, note=note.strip())

    return {"document_id": document_id, "dry_run": False, "removed": counts,
            "files": files, "headline": _headline(document_id, counts)}


def _headline(document_id: str, counts: dict, dry_run: bool = False) -> str:
    parts = [f"{n} {name.replace('_', ' ')}" for name, n in counts.items() if n]
    what = ", ".join(parts) if parts else "nothing -- it had no extractions"
    if dry_run:
        return (f"Redacting {document_id} would destroy {what}. The row would "
                "stay, with the account of why.")
    return (f"Redacted {document_id}: destroyed {what}. The row remains, with "
            "who removed it, when, and why.")
