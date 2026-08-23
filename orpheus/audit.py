"""The append-only record of what changed.

Nothing here deletes or updates. A correction inserts; a rejection inserts; the
current state of a row is a projection of the log, and the log is the truth.
That is both the audit story and the only way to measure whether extraction is
getting better, because "what did the machine say before a person fixed it" is
a question only the history can answer.

Ordered by `seq`, never by time. Rows written in one transaction share a
timestamp to the second, so ordering by `edited_at` puts them in an arbitrary
order and ordering by `id` puts them in a random one -- both were tried.
"""

from __future__ import annotations

from typing import Any

from .store import Store
from .utils import new_id, now, to_json


def record_edit(store: Store, table_name: str, row_id: str,
                document_id: str | None, action: str,
                previous: Any = None, new: Any = None,
                actor_id: str | None = None, note: str | None = None) -> str:
    store.assert_writable()
    edit_id = new_id("edit")
    store.insert("edit_history", {
        "id": edit_id,
        "table_name": table_name,
        "row_id": row_id,
        "document_id": document_id,
        "action": action,
        "previous_value": to_json(previous),
        "new_value": to_json(new),
        "edited_by": actor_id,
        "edited_at": now(),
        "note": note,
    })
    return edit_id


def row_history(store: Store, table_name: str, row_id: str) -> list[dict]:
    """Everything that has happened to one row, oldest first."""
    return store.query(
        "SELECT seq, id, action, previous_value, new_value, edited_by, edited_at, note "
        "FROM edit_history WHERE table_name = ? AND row_id = ? ORDER BY seq",
        (table_name, row_id),
    )


def document_history(store: Store, document_id: str) -> list[dict]:
    return store.query(
        "SELECT seq, id, table_name, row_id, action, previous_value, new_value, "
        "       edited_by, edited_at, note "
        "FROM edit_history WHERE document_id = ? ORDER BY seq",
        (document_id,),
    )
