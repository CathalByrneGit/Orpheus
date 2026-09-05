← [Back to index](index.md)

# What falls due

`KeyDate` has carried a `date_role` since the deterministic pass was written —
`end`, `milestone`, `start`, `signature` — and `Obligation` has carried a
`due_date` and a `recurrence` since the bundle was authored. **Nothing had ever
queried either.** Every date in the store was extracted, located on a page,
graded by the rubric, and left there.

That is the gap between a document intelligence system and a system somebody
opens on a Monday. A public servant holding four hundred contracts does not
begin with *what does the corpus say about Ardmore*; they begin with *what
expires this quarter, and have I looked at it*.

```bash
orpheus calendar --within-days 90
```

It exits non-zero when something is past its date, so it can run on a schedule
and stay quiet when there is nothing to say —
`orpheus scheduled calendar-digest`, weekly, is that same behaviour inside the
server. See [Work on a clock](scheduled-tasks.md).

```
2 past its date and 2 in the next 90 days. 0 of 4 shown have been checked by
a person; the other 4 are machine readings nobody has confirmed. 1 came from a
slash date that can be read two ways, so may fall in a different month
entirely. 1 of 4 document(s) have no date that falls due at all. An empty
stretch in this calendar may be that rather than a quiet quarter. No
`Obligation` has been extracted from this corpus, so everything here is a date
rather than a duty somebody owes.

  past its date:
    2026-08-31    -1d ? end        02-kestrel.txt
    2026-07-04   -59d ? end        03-halloran.txt  (04/07/2026 -- or 2026-04-07)

  coming up:
    2026-09-30   +29d ? milestone  02-kestrel.txt
    2026-11-15   +75d ? end        01-ardmore.txt

  ? marks a machine reading nobody has confirmed.
```

It exits non-zero when anything is past its date, so it can run from cron and
only speak up when there is something to say. `/-/orpheus/calendar` is the same
thing in the browser; `GET /calendar` is the same over HTTP, administrator-only
across the corpus or scoped with `?document_id=`.

## The four things it will not do

Turning extracted dates into a list is easy. The work is in what it refuses.

**It will not present a machine reading as a commitment.** Every row carries its
review status and the split is in the *headline*, not in a column somebody has
to notice. An unconfirmed expiry date is worth showing — that is the whole point
of showing it, so somebody checks — but showing it as though a person had agreed
to it is the single most damaging thing this page could do. Rejected extractions
never appear at all.

**It will not bury an overdue contract in a sorted list.** Anything past its
date gets its own section rather than a negative number among things that have
not happened yet. A contract that expired last month and that nobody has looked
at is the most alarming row in the store.

**It will not let an empty calendar mean two things.** Nothing due may mean
nothing is due, or it may mean no end date was ever extracted from a single
document. Those are opposite findings, so `coverage` travels beside the entries
— the same reason `graph.py` reports coverage beside its topology. It also says
plainly when no `Obligation` has ever been extracted, because nothing in this
codebase writes one: only a model proposing `type: "Obligation"` ever has, and
"no obligations" would otherwise read as "no obligations exist".

**It will not invent an entry no document contains.** `Obligation.recurrence` is
free text a model wrote — `"quarterly"`, `"annually on the anniversary"` — and
is reported verbatim. The next occurrence is what the document says; the ones
after it would be what a parser guessed.

## Dates that read two ways

`find_dates` reads slash dates day-first, which is right for Irish, UK and EU
documents and wrong for American ones, and records an either-way date at
`inferred` with its raw text kept rather than picking a side silently.

In a diary that matters more than anywhere else, because the entry may be three
months from where it is shown. So the calendar does not say *ambiguous* — a
warning nobody can act on — it says what else the date could be:

```
2026-07-04   -59d ? end   03-halloran.txt  (04/07/2026 -- or 2026-04-07)
```

The reader knows which their document meant. One of those two is overdue by two
months and the other by five.

## What counts as falling due

| Role | In the calendar | Why |
|---|---|---|
| `end`, `milestone`, `renewal`, `review`, `break` | yes | something happens on this date |
| `start`, `signature` | no | facts a contract *has*, not things that fall due |
| `unknown` | no | no cue was near enough to say what the date was for |
| `Obligation.due_date` | yes | an obligation is a duty; it needs no role to say so |

Set-aside dates are counted, not dropped quietly: `n_context_dates_set_aside`
is on every response and on the page, because a calendar that silently discards
two thirds of the dates in the store is one nobody can audit.

## Reproducibility

`--as-of` is a plain ISO date and defaults to today. It is a parameter rather
than a constant because *"seventeen things were due last quarter"* is a claim
about a date, and reading it off the clock makes the same command answer
differently every morning with nothing recording why.
