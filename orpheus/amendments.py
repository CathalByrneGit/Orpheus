"""Schema amendment candidates: what the bundle did not have a place for.

A property the model returned that the bundle does not declare is **not**
dropped. Silently discarding it would throw away exactly the signal that says
the schema is wrong — the ontology is a hypothesis about the documents, and this
is the evidence against it.

Accepting one changes the bundle for every document, so it is an administrator
decision rather than an ordinary review.
"""

from __future__ import annotations

from .store import Store
from .utils import new_id, now

AMENDMENT_TYPES = ("new_property", "new_type", "new_link_type")


def record_schema_amendment(store: Store, document_id: str | None,
                            amendment_type: str, type_id: str | None,
                            property_id: str | None, observed_value: str = "",
                            inferred_type: str = "string",
                            rationale: str = "", actor_id: str | None = None) -> str | None:
    """Record a candidate, or say nothing if this one is already queued.

    Deduplicated on what it is rather than when it was seen: the same property
    on the same type is one candidate however many documents show it, or the
    queue fills with the same row over and over and a reviewer stops reading it.
    """
    store.assert_writable()
    existing = store.one(
        "SELECT amendment_id FROM schema_amendments "
        "WHERE amendment_type = ? AND IFNULL(type_id, '') = IFNULL(?, '') "
        "  AND IFNULL(property_id, '') = IFNULL(?, '') AND status = 'pending'",
        (amendment_type, type_id, property_id),
    )
    if existing:
        # Seen again rather than seen anew. The count is the useful part: a
        # property appearing in one document out of two hundred is a different
        # proposition from one appearing in all of them, and a reviewer
        # deciding whether to change the schema needs to know which.
        store.execute(
            "UPDATE schema_amendments SET occurrences = occurrences + 1 "
            "WHERE amendment_id = ?", (existing["amendment_id"],))
        return existing["amendment_id"]

    amendment_id = new_id("amd")
    store.insert("schema_amendments", {
        "amendment_id": amendment_id,
        "document_id": document_id,
        "amendment_type": amendment_type,
        "type_id": type_id,
        "property_id": property_id,
        "observed_value": observed_value,
        "inferred_type": inferred_type,
        "rationale": rationale,
        "occurrences": 1,
        "status": "pending",
        "proposed_at": now(),
    })
    return amendment_id


def pending_amendments(store: Store) -> list[dict]:
    return store.query(
        "SELECT * FROM schema_amendments WHERE status = 'pending' ORDER BY created_at"
    )
