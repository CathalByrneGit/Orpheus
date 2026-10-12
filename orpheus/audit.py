"""The append-only record of what changed, and the proof that it is.

Nothing here deletes or updates. A correction inserts; a rejection inserts; the
current state of a row is a projection of the log, and the log is the truth.
That is both the audit story and the only way to measure whether extraction is
getting better, because "what did the machine say before a person fixed it" is
a question only the history can answer.

Ordered by `seq`, never by time. Rows written in one transaction share a
timestamp to the second, so ordering by `edited_at` puts them in an arbitrary
order and ordering by `id` puts them in a random one -- both were tried.

**Append-only was a convention until it was a chain.** Nothing writes except
through these functions, which is true, enforced and tested -- and it is still
a convention, in a SQLite file anybody holding can open. A row rewritten with
`sqlite3` left no trace, so the claim this project rests on hardest was the one
claim it could not demonstrate.

Each row now carries `prev_hash`, the hash of the row before it, and
`row_hash`, a hash of itself including that link. Editing any row changes its
hash and breaks every link after it, and `verify_history()` names the first
row where the break is. Three things are worth being exact about:

- **The chain detects edits, not rewrites.** Somebody who recomputes the whole
  chain produces a consistent one. That is what `anchor()` is for, and it only
  works because an anchor is meant to be copied *out* of the store: a head hash
  written down last Tuesday is evidence the store cannot forge.
- **A sealed row attests its content at sealing, not at writing.** Rows that
  predate the chain are enrolled by migration 16, and hashing a row that had
  already been tampered with would bless the tampering. The enrolment is
  recorded as its own event so a reader is told which rows those are.
- **A redacted payload can never be recomputed.** Redaction nulls
  `previous_value` and `new_value` on rows already written, deliberately. Those
  rows keep their original `row_hash`, so the *linkage* is undisturbed and
  everything after them still verifies; what is gone is the ability to confirm
  that one row's content. It is marked, counted and reported rather than
  quietly failing.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .store import Store
from .utils import OrpheusError, new_id, now, to_json


#: The columns the chain covers, in this order. `seq` is included and is why
#: the hash is written in a second statement: SQLite assigns it on insert, so
#: it does not exist to be hashed until the row does.
CHAINED_COLUMNS = ("seq", "id", "table_name", "row_id", "document_id", "action",
                   "previous_value", "new_value", "edited_by", "edited_at",
                   "note")


def chain_digest(prev_hash: str | None, row) -> str:
    """The hash of one row, given the one before it.

    Each field is length-prefixed rather than joined by a separator. A
    separator can appear inside a JSON payload or a free-text note, and then
    two different rows hash the same -- the classic concatenation collision,
    where `"ab" + "c"` and `"a" + "bc"` are indistinguishable. Length prefixing
    is injective, so they cannot be.
    """
    fields = [prev_hash or ""]
    for column in CHAINED_COLUMNS:
        value = row[column] if column in row.keys() else None
        fields.append("" if value is None else str(value))
    payload = "".join(f"{len(f)}:{f}" for f in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def head(store: Store) -> dict | None:
    """The last row of the chain: what an anchor pins and the next row links to."""
    return store.one(
        "SELECT seq, row_hash FROM edit_history ORDER BY seq DESC LIMIT 1")


def record_edit(store: Store, table_name: str, row_id: str,
                document_id: str | None, action: str,
                previous: Any = None, new: Any = None,
                actor_id: str | None = None, note: str | None = None) -> str:
    """Append one event, linked to the one before it.

    The insert and the hash are one transaction, so a row never exists without
    its link. Reading the head and writing the next row is safe without further
    locking for the reason the whole design is: Datasette serialises writes on
    one thread, and the advisory lock refuses a second writing process.
    """
    store.assert_writable()
    edit_id = new_id("edit")
    with store.transaction():
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
        previous_row = store.one(
            "SELECT row_hash FROM edit_history WHERE seq < "
            "(SELECT seq FROM edit_history WHERE id = ?) "
            "ORDER BY seq DESC LIMIT 1", (edit_id,))
        written = store.one("SELECT * FROM edit_history WHERE id = ?", (edit_id,))
        prev_hash = previous_row["row_hash"] if previous_row else None
        store.execute(
            "UPDATE edit_history SET prev_hash = ?, row_hash = ? WHERE id = ?",
            (prev_hash, chain_digest(prev_hash, written), edit_id))
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


# ---------------------------------------------------------------------------
# Proving it
# ---------------------------------------------------------------------------

def verify_history(store: Store, since: int = 0) -> dict:
    """Replay the chain and say whether it holds.

    Walks `edit_history` in `seq` order, recomputing each row's hash from the
    one before it. The first mismatch is the answer: every row after a tampered
    one fails too, so reporting all of them would bury the one that matters.

    Reports rather than raises. A broken chain is a finding about the store,
    and a function that threw would make the finding harder to look at than the
    fault.
    """
    rows = store.query(
        "SELECT * FROM edit_history WHERE seq > ? ORDER BY seq", (since,))
    if not rows:
        return _history_report(0, 0, 0, 0, None, store)

    expected = None
    if since:
        earlier = store.one("SELECT row_hash FROM edit_history WHERE seq <= ? "
                            "ORDER BY seq DESC LIMIT 1", (since,))
        expected = earlier["row_hash"] if earlier else None

    checked = unlinked = redacted = 0
    broken = None
    for row in rows:
        if row["row_hash"] is None:
            # Written before the chain existed and never sealed, or written by
            # something that bypassed `record_edit`. Either way it is not a
            # broken link -- it is an unprotected row, which is a different
            # thing and is counted separately.
            unlinked += 1
            expected = None
            continue

        if expected is not None and row["prev_hash"] != expected:
            broken = _break(row, "link", expected)
            break

        if row["payload_redacted_at"]:
            # The content is gone on purpose, so its hash cannot be recomputed.
            # Its stored hash is taken on trust and the chain continues through
            # it -- which is what keeps every row after a redaction verifiable.
            redacted += 1
        else:
            recomputed = chain_digest(row["prev_hash"], row)
            if recomputed != row["row_hash"]:
                broken = _break(row, "content", recomputed)
                break
            checked += 1
        expected = row["row_hash"]

    return _history_report(len(rows), checked, unlinked, redacted, broken, store)


def _break(row, kind: str, expected: str) -> dict:
    return {
        "seq": row["seq"],
        "id": row["id"],
        "kind": kind,
        "table_name": row["table_name"],
        "row_id": row["row_id"],
        "document_id": row["document_id"],
        "action": row["action"],
        "edited_at": row["edited_at"],
        "expected": expected,
        "found": row["prev_hash"] if kind == "link" else row["row_hash"],
        "message": (
            f"Row {row['seq']} does not follow the row before it: its "
            f"`prev_hash` is not the previous row's hash. Something was "
            f"inserted, removed or reordered at or before this point."
            if kind == "link" else
            f"Row {row['seq']} does not hash to what it claims. Its content "
            f"was changed after it was written."),
    }


def _history_report(total: int, checked: int, unlinked: int, redacted: int,
                    broken: dict | None, store: Store) -> dict:
    anchor_row = store.one(
        "SELECT anchor_id, seq, head_hash, anchored_at FROM history_anchors "
        "ORDER BY seq DESC, anchored_at DESC LIMIT 1")
    current = head(store)
    anchor_holds = None
    if anchor_row:
        at_anchor = store.one(
            "SELECT row_hash FROM edit_history WHERE seq = ?", (anchor_row["seq"],))
        anchor_holds = bool(at_anchor
                            and at_anchor["row_hash"] == anchor_row["head_hash"])

    return {
        "rows": total,
        "verified": checked,
        "unlinked": unlinked,
        "redacted_payloads": redacted,
        "intact": broken is None,
        "broken": broken,
        "head": current,
        "anchor": dict(anchor_row) if anchor_row else None,
        "anchor_holds": anchor_holds,
        "headline": _history_headline(total, checked, unlinked, redacted,
                                      broken, anchor_row, anchor_holds),
    }


def _history_headline(total, checked, unlinked, redacted, broken, anchor,
                      anchor_holds) -> str:
    """What was proved, and -- just as important -- what was not.

    An intact chain is a narrow claim: these rows follow each other and none of
    their content has changed since it was hashed. It is not "this history is
    complete" and it is not "nobody rewrote the whole thing", and a summary
    that let either be inferred would be the kind of unearned all-clear the
    rest of this project refuses to print.
    """
    if not total:
        return "No history to check."
    if broken:
        return (f"The audit trail is broken at row {broken['seq']}. "
                f"{broken['message']}")

    parts = [f"{checked} of {total} rows verify against the row before them."]
    if redacted:
        parts.append(f"{redacted} had their payloads redacted, so their content "
                     "can no longer be recomputed -- the chain runs through "
                     "them and what they said is gone on purpose.")
    if unlinked:
        parts.append(f"{unlinked} carry no hash at all and are not protected.")
    if anchor is None:
        parts.append("No anchor has been recorded, so this says the chain is "
                     "internally consistent and not that nobody rewrote it. "
                     "`orpheus history anchor` writes one down.")
    elif anchor_holds:
        parts.append(f"It still matches the anchor taken at row {anchor['seq']} "
                     f"on {anchor['anchored_at']}.")
    else:
        parts.append(f"But it does NOT match the anchor taken at row "
                     f"{anchor['seq']} on {anchor['anchored_at']}. A consistent "
                     "chain that disagrees with an anchor is what a wholesale "
                     "rewrite looks like.")
    return " ".join(parts)


def anchor(store: Store, actor_id: str | None = None,
           note: str | None = None) -> dict:
    """Pin the chain's head, so a later rewrite of the whole thing shows up.

    Recorded in the store for convenience and **meant to be copied out of it**.
    An anchor only proves anything where the store cannot reach: pasted into a
    ticket, mailed to yourself, committed to a repository. One kept only here
    is one an attacker rewriting the chain would rewrite too.
    """
    store.assert_writable()
    current = head(store)
    if current is None or current["row_hash"] is None:
        raise OrpheusError(
            "There is no chained history to anchor yet. Nothing has been "
            "recorded, or the store predates migration 16.")
    anchor_id = new_id("anch")
    store.insert("history_anchors", {
        "anchor_id": anchor_id,
        "seq": current["seq"],
        "head_hash": current["row_hash"],
        "anchored_at": now(),
        "anchored_by": actor_id,
        "note": note,
    })
    return {"anchor_id": anchor_id, "seq": current["seq"],
            "head_hash": current["row_hash"],
            "note": ("Copy this out of the store. An anchor kept only inside "
                     "the database it attests is one a rewrite would rewrite "
                     "too.")}


def anchors(store: Store, limit: int = 20) -> list[dict]:
    return store.query(
        "SELECT anchor_id, seq, head_hash, anchored_at, anchored_by, note "
        "FROM history_anchors ORDER BY seq DESC, anchored_at DESC LIMIT ?",
        (limit,))
