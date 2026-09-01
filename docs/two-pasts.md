← [Back to index](index.md)

# Two different pasts

An amendment dated 3 March and recorded in the store on 14 November is true
from March and *known* from November. Ask what a contract said on 1 June and
there are two right answers:

| | On 1 June |
|---|---|
| **What was true** | The amendment was in force. Anyone reading the contract correctly read EUR 310,000. |
| **What we believed** | Nobody here had read the amendment yet. Every report this store produced said EUR 250,000, and said it honestly. |

Both matter, and they answer different questions. *Was the department paying the
right rate in June* is valid time. *Why did the June report say what it said* is
transaction time — and it is the one that exonerates or indicts the people who
acted on it. A system that answers one without saying which is lying by
omission.

The second question is the one almost no system can answer at all, because
almost none keeps the record. This one does.

```bash
orpheus as-of 2024-06-01
```

```
  what this store believed on 2024-06-01
    As of 2024-06-01 this store held 0 document(s) and 0 extraction(s),
    0 of them reviewed. This is what it believed then, not what it holds now.
    calibration then: insufficient_evidence

  what the documents say was running on 2024-06-01
    On 2024-06-01 the corpus says 2 of 2 document(s) were in force: 0 had not
    begun, 0 had ended. 2 of those in force rest on a date nobody has confirmed.
      ? 2023-01-01 to 2025-12-31  02-kestrel.txt
      ? 2024-03-01 to 2026-11-15  01-ardmore.txt
```

Two contracts running, and a store that had never heard of either. Both true.

## Transaction time — `believed_at`

Reconstructed, not stored. An instance existed if its `created_at` is not after
the moment asked about; its status is whatever the last review action at or
before that moment left it at, and `unconfirmed` if there was none. Ordered by
`seq` rather than by timestamp, because three reviews inside one second are
unorderable by clock and the sequence is the record of what happened when.

The report it returns is the report that *would have been produced then* — not
today's report filtered to old rows. The calibration verdict is computed on the
same `min_reviewed` threshold as today's, so that "the report was silent then
and speaks now" is itself a visible change rather than the reason two runs look
incomparable.

**This survives redaction.** Every review action names the status it produced —
`confirm` → `confirmed`, `reject` → `rejected` — rather than carrying it in a
payload, and [redaction](redaction.md) nulls the payloads while leaving the
actions in place. What a redacted document *said* is gone; that somebody
confirmed something about it on a given date is still answerable. That is
exactly the split redaction was built to make, and it is load-bearing here.

## Valid time — `in_force_on`

Read off the documents' own dates: a `start` role at or before, and either no
`end` role or one at or after. Nothing to do with when any of it was extracted
or reviewed.

Two honesty rules, both about what cannot be placed on a timeline at all:

- **A document with no start date is `unplaceable`** — neither in force nor out
  of force. It has no beginning to compare against, and calling it either would
  be inventing a fact about it. The count is in the headline.
- **An unconfirmed date is still a machine reading**, so the headline says how
  many of the contracts it calls "in force" rest on one.

A redacted document appears on neither timeline.

## One value over time — `value_history`

The third question the two axes make askable, and the one somebody asks out
loud: *the contract is for EUR 310,000 — what was it before, and who changed
it?*

```bash
curl "$BASE/-/orpheus/api/instances/$ID/values"
```

`row_history` has held the raw answer since the audit trail was written; this
reads it as a timeline per property rather than as a list of edits, which is
the shape the question has. Transaction time throughout: these are the moments
the *store* changed its mind. When the underlying document changed is a
different question, and only an amending document can answer it.

A redacted document leaves no instance to ask about — redaction deletes the
instances themselves, so this answers `404`. Its document history survives, with
the values gone from it.

## Never merged

`orpheus as-of` and `GET /as-of` return both axes and the sentence that keeps
them apart. `?axis=believed` or `?axis=in_force` asks for one. There is no
combined number anywhere, because there is no number that is both.
