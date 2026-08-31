← [Back to index](index.md)

# Redaction

Everything else in Orpheus is built to be immutable. `provenance` is the record
of what the machine said. A rejected extraction is kept, because deleting it
would throw away the measurement along with the mistake. Nothing anywhere
deletes.

That is the right default for an audit trail and the wrong one for a corpus of
contracts. Contracts carry names, signatures, addresses, and third parties who
never agreed to be in anybody's database. Somebody uploads the wrong file.
Somebody uploads a file they were not cleared to hold. Somebody asks to be
erased. `delete` had sat in `rubric.ACTIONS` since the beginning, `auth.can`
computed it for every actor, and **nothing consumed it** — the only way to take
a document out of a store was to destroy the store.

## A redaction is not a delete

The `documents` row survives as a tombstone. The corpus count stays true, the
audit trail keeps its order, and *a document was here, this person removed it,
on this date, for this reason* stays answerable — which is the one fact a
deletion would also erase.

Everything read **from** the document is destroyed: its text and page images,
its extracted values, every excerpt quoting it, the relations drawn from it,
the tensions it was a side of, and the stored original itself.

```bash
orpheus redact doc_a3cf… --actor-id act_… --note "Erasure request." --dry-run
# Redacting doc_a3cf… would destroy 1 pages, 4 instances, 4 provenance,
# 2 entity pages, 1 files. The row would stay, with the account of why.

orpheus redact doc_a3cf… --actor-id act_… --note "Erasure request."
```

`--dry-run` opens the store read-only and changes nothing. Offering an
irreversible action without a way to look first is not offering a choice, and
the count is the part that makes it informed — *this will also remove two wiki
pages* is not something a person can work out from the page they are on.

A `--note` is required. A redaction nobody can account for later is
indistinguishable from data loss, and the person who has to account for it is
rarely the person who did it.

## Three decisions worth stating

Each could reasonably have gone the other way.

**The audit trail keeps its rows and loses its payloads.** `edit_history` holds
`previous_value` and `new_value`, so an amendment records the text on both
sides of it. Keeping those would mean the history quietly retained what the
redaction was for; dropping the rows would break the `seq` chain that makes the
history a history. So the rows stay, in order, with their payloads nulled: what
happened and who did it survives, what it said does not. The `redact` entry is
appended afterwards and keeps its own note — it is the only row left that says
anything, and what it says is why.

**A page whose every source is redacted is deleted; a page two documents share
is not.** `entity_mentions` for the document go, and any wiki page left with no
live mention goes with them. A page that outlived all of its sources would
assert something no document says, which `lint.uncited_pages` calls the worst
failure this model can have — and it would be right.

**The file hash is kept.** It is the one identifying thing that stays, and the
alternative is worse: without it the same file re-ingested would sail through
deduplication and be extracted all over again, resurrecting exactly what was
erased.

## Afterwards

| Route | Answer |
|---|---|
| `GET /documents/<id>` | The tombstone: when, by whom, and why |
| `GET /documents/<id>/history` | Still readable — that is the point of keeping the row |
| `GET /documents/<id>/text`, `/instances`, `/original` | `410 Gone`, with the reason |
| `POST …/classify`, `/extract`, `/review`, `/share` | `410 Gone` |

`410` rather than an empty list, because an empty list reads as *this document
said nothing* rather than *this document was removed*, and the difference is
the whole point of keeping the row.

`orpheus verify` counts redactions separately and does not call them failures:

```
All 6 remaining originals are present and hash to the digests recorded at
ingest. 1 were redacted, and are absent on purpose.
```

Otherwise every store that ever removed a document would fail its restore gate
for good, and a gate that is permanently red is a gate nobody reads. The
`unavailable_original` lint check skips them for the same reason. They are
named every time and never folded into the total: a count that quietly shrank
would be the one way a redaction could look like a loss.

## Who may

`delete` permission, which resolves to the document's owner and to
administrators — the same two who could have shared it. A `viewer` or an
`editor` cannot, and neither can a link. A share is permission to work on a
document, never to remove one.

## How this is tested

`tests/test_redact.py` is built around one assertion:
`test_nothing_the_document_said_survives_anywhere` walks **every text column of
every table** in the store looking for phrases only the redacted document
contained.

Enumerating the tables to clear by hand and asserting on each would test the
list rather than the property, and the list is the part most likely to be wrong.
It was: the scan found the canonical name of a deleted wiki page still sitting
in `edit_history.new_value`, because a page is created against the `entities`
table with no document on the row, so clearing by `document_id` alone missed it.
Nothing short of scanning would have found that.

A companion test asserts the scan finds those phrases *before* the redaction,
across at least four tables — a leak detector that cannot see a leak proves
nothing.

The whole flow is also driven through a real browser session in
`tests/e2e/browser_loop.sh`: the confirmation page, a redaction with no reason
refused, then the file gone from disk, the row kept with its history, and `410`
on every route that would have read it.
