← [Back to index](index.md)

# The audit trail, and the proof that it was not edited

`edit_history` is the thing this project rests on hardest. Nothing is
destructively overwritten: a correction inserts, a rejection inserts, and the
current state of any row is a projection of the log. That is both the audit
story and the only way extraction quality can be measured at all, because
*"what did the machine say before a person fixed it"* is a question only the
history can answer.

Until recently it was append-only **by convention**.

Nothing writes except through `record_edit()` — enforced in the core, kept by
the plugins, covered by tests. All true, and still a convention. The store is a
SQLite file, and anybody holding it can open it with `sqlite3` and rewrite a
row. Nothing would notice. The claim the whole design leans on was the one
claim it could not demonstrate.

## The chain

Every row now carries two more columns:

| | |
|---|---|
| `prev_hash` | the hash of the row before it, by `seq` |
| `row_hash` | `sha256` of that link plus this row's own content |

Edit any row and its hash changes, so its own content check fails. Delete or
reorder one and the next row's `prev_hash` no longer matches, so the link
fails. Either way `verify_history()` names the first row where it happened.

```
$ orpheus history
The audit trail is broken at row 2. Row 2 does not hash to what it claims.
Its content was changed after it was written.

  row 2  confirm on instances_Company/i1
  recorded 2026-10-12T02:11:24Z
  expected fa3198300540d8bfdbd27ecab2575d4373938298c38cae22bd3dafab6718a382
  found    c5b3867b7fa65f114c18243b62c292159470a62a70a109a3963bcab2ada22e05

  Every row after this one fails too, so only the first is shown: it is
  where the trail stops being evidence.
```

It exits non-zero when the chain is broken, so it can gate a restore the way
`orpheus verify` does for the documents. The two are the same question asked of
different things: *are these still the documents*, and *is this still the record
of who changed them*.

Fields are **length-prefixed** rather than joined by a separator before
hashing. A separator can appear inside a JSON payload or a free-text note, and
then two different rows hash identically — the classic `"ab" + "c"` versus
`"a" + "bc"` collision. Length prefixing is injective, so they cannot.

## What the chain does not prove

A chain catches edits. It does not catch a **rewrite**: somebody who recomputes
every hash from the start produces a chain that verifies perfectly. That is not
a hypothetical — it is six lines of Python, and there is a test that does
exactly it.

The answer is an anchor:

```
$ orpheus history anchor --note "quarter end"
row 2  c5b3867b7fa65f114c18243b62c292159470a62a70a109a3963bcab2ada22e05

  Copy this out of the store. An anchor kept only inside the database it
  attests is one a rewrite would rewrite too.
```

That last line is the whole mechanism. An anchor is recorded in
`history_anchors` for convenience and **means nothing until it leaves** —
pasted into a ticket, mailed to yourself, committed to a repository, read out
at a meeting. A head hash you wrote down last Tuesday is evidence the store
cannot forge. One kept only in the store is not evidence at all.

With an anchor, `orpheus history` reports both facts separately:

```
4 of 4 rows verify against the row before them. It still matches the anchor
taken at row 5 on 2026-10-12T02:05:36Z.
```

and, when the chain is internally perfect but disagrees with what you wrote
down:

```
… But it does NOT match the anchor taken at row 5. A consistent chain that
disagrees with an anchor is what a wholesale rewrite looks like.
```

Without one it says so, rather than letting an intact chain be read as more
than it is:

```
… No anchor has been recorded, so this says the chain is internally consistent
and not that nobody rewrote it. `orpheus history anchor` writes one down.
```

## Three honest edges

### A sealed row attests its content at sealing, not at writing

A store upgraded into migration 16 has rows that predate the chain. They are
enrolled in `seq` order and the enrolment is recorded **as its own event**,
because hashing a row that had already been tampered with would bless the
tampering:

> *N existing row(s) were enrolled in the hash chain. Their hashes attest their
> content as it stood at this moment, not as it was written — nothing before
> this point can be proved unedited. Every row after it can.*

A store created at version 16 or later gets no such event: there is nothing to
enrol and no caveat to record, and writing *"0 rows were sealed"* as the first
line of every new store's history would be a bookkeeping entry claiming
something happened.

### A redacted payload can never be recomputed

Redaction nulls `previous_value`, `new_value` and `note` on rows already
written — deliberately, so the history keeps *what happened* and loses *what it
said*. Those rows can therefore never be content-verified again.

They are **marked** (`payload_redacted_at`) rather than left to fail, because
without the mark every subject access request would read as an attack. The
`row_hash` is **not** recomputed, which is what keeps the linkage intact: every
row after a redaction still points exactly where it pointed, and the chain runs
straight through.

```
4 of 6 rows verify against the row before them. 2 had their payloads redacted,
so their content can no longer be recomputed — the chain runs through them and
what they said is gone on purpose.
```

A redacted row cannot be used as cover: tamper with a row *next to* one and the
chain still breaks.

### A row written behind the core's back is unprotected, not broken

A row inserted by something that bypassed `record_edit` has no hash at all.
That is a different finding from a broken link and is counted separately —
*"N carry no hash at all and are not protected."*

## Using it

```bash
orpheus history            # replay and report; non-zero if broken
orpheus history --since 400
orpheus history anchor --note "quarter end" --actor-id act_…
orpheus history anchors    # what has been pinned, newest first
orpheus history --json
```

Over HTTP, administrator-only, because a broken chain is a statement about the
store rather than about any one document:

| | |
|---|---|
| `GET /audit/history` | replay and report |
| `GET /audit/history/anchors` | what has been pinned |
| `POST /audit/history/anchor` | pin the current head |

## What it costs

One `SELECT ... ORDER BY seq DESC LIMIT 1` and one `UPDATE` per recorded edit,
inside the transaction that writes the row — so a row never exists without its
link. Reading the head and writing the next row needs no extra locking for the
same reason the rest of the design does not: Datasette serialises writes on one
thread, and the advisory lock refuses a second writing process.

## Where it came from

Read honestly: from [docbank](prior-art.md#docbank--the-storage-question-asked-again),
whose opt-in, irreversible, replay-verified audited scopes were better than
what this project had. The evaluation that rejected it as a storage layer is in
[prior art](prior-art.md); this is the part worth taking.

---

[← Back to index](index.md)
