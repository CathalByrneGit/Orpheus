"""A person recording something the extractor missed.

Every other way a fact reaches the store starts with a machine: an engine
extracts it, or the reading companion offers it and somebody accepts. Neither
helps when the model simply did not see a party named in a signature block. Up
to now there was no way to say "this person is in this document" at all, so the
only options were to leave the corpus wrong or to edit SQLite by hand.

Two rules make this safe to have.

**It is grounded like anything else.** The person quotes the document and the
quote is located in it, by exactly the code that locates a model's quotations.
A human claim that cites nothing is more dangerous than a machine one, not
less, because nothing downstream doubts it. Where a person knows something the
documents do not say, the entity page's notes field is the place for it, and it
is kept apart from the cited claims on purpose.

**It is not evidence about the extractor.** `source` on the provenance row is
`human`, and extraction quality skips those. A fact the extractor never
produced says nothing about how well the extractor works, and counting it as a
confirmed extraction would move a number that exists to answer a different
question. An accepted suggestion is not the same thing and is still counted:
there the machine did offer something and a person agreed.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from .align import align
from .audit import record_edit
from .extract import insert_instance, write_provenance
from .ingest import document_text
from .population import page_for_offset, page_offsets
from .store import Store
from .utils import OrpheusError, new_id

# A person who read the line and typed it in is the top of the rubric: this is
# what `explicit` means. The rubric describes how sure the *source* is, and a
# person quoting a document they are looking at is as sure as it gets.
HUMAN_CONFIDENCE = 1.0

SOURCE = "human"


def record_fact(store: Store, document_id: str, type_id: str,
                properties: dict, quote: str, actor_id: str,
                note: str | None = None, bundle: dict | None = None) -> dict:
    """Record a fact a person read in a document, with the line they read it on.

    Raises rather than storing anything the document does not contain.
    """
    store.assert_writable()
    if not actor_id:
        raise OrpheusError("A recorded fact needs the person who recorded it.")
    quote = (quote or "").strip()
    if not quote:
        raise OrpheusError(
            "Quote the line you read this on. A fact with no excerpt cannot be "
            "checked by the next person, and this is the one kind of row nobody "
            "downstream doubts.")
    if not properties:
        raise OrpheusError("Nothing to record: no values were given.")

    bundle = bundle or bundle_mod.active(store)
    if bundle is None:
        raise OrpheusError("No bundle is registered, so there is nothing to "
                           "record this against.")

    text = document_text(store, document_id)
    if not text:
        raise OrpheusError(f"{document_id} has no text to quote from.")

    start, end, alignment = align(text, quote)
    if alignment is None or start is None:
        raise OrpheusError(
            f"{document_id} does not contain that quote, so it cannot be "
            "recorded against it. Check the wording, or put it in the entity "
            "page's notes instead — that field is for what a person knows and "
            "the documents do not say.")

    spans = page_offsets(store, document_id)
    page_no = page_for_offset(spans, start)

    instance_id = new_id("inst")
    with store.transaction():
        written = insert_instance(
            store, bundle, type_id, instance_id, document_id, dict(properties),
            SOURCE, HUMAN_CONFIDENCE, status="confirmed", actor_id=actor_id)
        if written is None:
            raise OrpheusError(
                f"{type_id} is not a type this bundle declares, so there is "
                "nowhere to put this. A schema amendment comes first.")
        write_provenance(
            store, instance_id, document_id, "recorded by hand", SOURCE,
            page_no, text[start:end], HUMAN_CONFIDENCE,
            alignment=alignment, char_start=start, char_end=end)
        record_edit(store, bundle_mod.table_name(
            bundle_mod.object_type(bundle, type_id)), instance_id,
            document_id, "record",
            new={"type_id": type_id, "properties": dict(properties),
                 "source": SOURCE, "confidence": HUMAN_CONFIDENCE,
                 "char_start": start, "char_end": end},
            actor_id=actor_id, note=note)

    return {"instance_id": instance_id, "type_id": type_id,
            "document_id": document_id, "page_no": page_no,
            "excerpt": text[start:end], "alignment": alignment,
            "char_start": start, "char_end": end,
            "source": SOURCE, "status": "confirmed",
            "note": ("Recorded, and confirmed because a person read it. It is "
                     "kept out of extraction quality: the extractor never "
                     "offered it, so it says nothing about the extractor.")}
