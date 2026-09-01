← [Back to index](index.md)

# User guide

Two corpora, run end to end, photographed as they came out. Every screenshot
below is of a real Orpheus reading real documents with a real model — nothing
here is a mockup, and `tests/e2e/screenshots.py` regenerates the lot.

The two cases are chosen to be as unlike each other as possible:

| | Case 1 | Case 2 |
|---|---|---|
| Documents | 8 commercial contracts from [CUAD](https://www.atticusprojectai.org/cuad) — real SEC filings | 16 [Python Steering Council](https://github.com/python/steering-council) monthly updates |
| Shape | legal prose, clause-numbered, dated | governance minutes, no headers, no schema |
| Ontology | `contract-core` — shipped | none at the start; **the machine proposes one** |
| What came out | 189 findings, 55 pages, 205 relations | 322 findings, 193 pages, 264 relations |

The point of running both is the last row of this guide: the same engine
produces a sparse graph on one and a dense one on the other, and **says which
it is and why** rather than leaving you to guess.

---

## Before you start

Orpheus reads documents into a store where **every claim carries its source and
its review state**. It is not a summariser and it is not a chatbot over your
files. The thing it is careful about is the difference between *a machine read
this* and *a person checked it*, and almost every design decision below follows
from that.

```bash
pip install -e '.[all]'
```

## Set up a store

```bash
orpheus init --db contracts.sqlite \
  --admin "Cathal Byrne" --cloud-policy org_allow \
  --config datasette.yml --storage-root storage
```

`--cloud-policy org_allow` is the opt-in gate. Without it no cloud model is
reachable from this store however the environment is configured, which is the
default a public body should start from. `init` also writes the two Datasette
files, and prints the command that serves what it just built:

```bash
datasette serve contracts.sqlite --metadata metadata.yml --config datasette.yml \
  --plugins-dir plugins --template-dir templates --port 8001
```

---

# Case 1 — a folder of contracts

## Ingest and extract

```bash
export ANTHROPIC_API_KEY=...
orpheus ingest docs --db contracts.sqlite --actor-id act_… \
  --storage-root storage --extract --tier cloud --engine anthropic --cloud-opt-in
```

`--cloud-opt-in` is required per run even in a store whose policy allows cloud.
Sending a document to a third party is a decision, and it is taken each time
rather than once at setup.

![The document list](images/contracts-index.png)

Eight documents, classified into the bundle's own vocabulary — seven
`contract`, one `amendment`. The classifier is only ever offered a closed list,
so it cannot invent a thirteenth spelling of one answer.

## What the machine proposed

![A document page](images/contracts-document.png)

A strategic alliance agreement, read into **31 findings**: 13 clauses, 10
obligations, 2 people, 2 key dates, 2 companies, a flag and the contract itself.

Read the page in this order, because it is the order that matters:

1. **Every row says `unconfirmed`.** Nothing here is a fact yet.
2. **Every row carries an excerpt** — the passage the value came from — and a
   page number.
3. **The confidence is not the model's opinion.** No engine in Orpheus is ever
   asked how sure it is. `confidence` is computed by locating the excerpt in the
   document: quoted verbatim scores `explicit`, and an excerpt that cannot be
   found at all lands at `inferred` and is reported as a lint finding.

You confirm, amend or reject each row from this page. Amending keeps the
original in the history; rejecting excludes the row **without deleting it**,
because a rejected extraction is evidence about extraction quality and throwing
it away would discard the measurement along with the mistake.

## Reading it with the machine

![The reading companion](images/contracts-read.png)

A page at a time, with the passage on the left and what the machine would
propose on the right. Nothing on the right is in the store: each is an offer,
and it becomes a row only when you accept it. This is the surface to use on a
document you have never seen, where the question is not *is this value right*
but *is there anything here at all*.

## The wiki

Extractions are per-document. `Ardmore Digital Limited` in four contracts is
four rows, and nothing yet says they are one company.

```bash
orpheus wiki propose --db contracts.sqlite --actor-id act_…
```

![The wiki](images/contracts-wiki.png)

**55 pages** from 189 findings. Grouping is on a normalised name — legal-form
suffixes trimmed, case folded — and the page says so on every screen, because
that is a candidate and not resolution.

![An entity page](images/contracts-entity.png)

This is the page worth studying. Amy L. Domini, drawn from one filing, and
`job_title` holds **two different values**: `Trustee` and `Chief Executive
Officer`.

Orpheus does not pick one. Both are shown with their sources, under a line that
says *"Often both were true when each was written, and the disagreement is the
interesting part."* A summariser would have chosen, and you would never have
known there was a choice.

Below that, the merge control offers another page to fold into this one — and
the note explains that a merged page keeps its row and points here, so a link
made before the merge still resolves.

## The graph

![The network](images/contracts-network.png)

55 pages, 58 canonical relations, 8 components — and the number that matters
most on this page:

> **29% of extracted relations reached the graph.** Every relation between two
> named things is drawn; the other 146 link to a clause, a date or a document,
> which never gets a page. **This is the shape of the corpus, not an unbuilt
> wiki.**

A graph view without that sentence is a confident picture of whatever happened
to be linked. Contracts are mostly assertions *about clauses*, so most of their
relations have nowhere to land — which is a fact about contracts, and the page
says so rather than letting you read it as missing work.

## What falls due

```bash
orpheus calendar --db contracts.sqlite --within-days 3650
```

![The calendar](images/contracts-calendar.png)

```
6 past its date. 0 of 6 shown have been checked by a person; the other 6 are
machine readings nobody has confirmed. 4 of 8 document(s) have no date that
falls due at all. An empty stretch in this calendar may be that rather than a
quiet quarter.
```

Three things in one sentence, and each is a refusal:

- **The checked/unchecked split is in the headline**, not in a column you might
  not notice. A diary of unconfirmed machine readings that looks like a diary is
  the most damaging thing this page could be.
- **Overdue has its own section**, not a negative number in a sorted list.
- **Coverage travels with it.** Four of eight documents have no due date at all,
  so a quiet quarter and an unread corpus are told apart.

One row reads `obligation · Company` rather than a date role: the model proposed
an `Obligation` with a `due_date`, and its recurrence is printed as the document
put it — `recurs: one_off` — never expanded. Turning a `"quarterly"` into four
entries would put dates in your diary that no document contains.

The foot of the page closes the other half of the same question: *11 other
date(s) were set aside as start dates, signature dates or dates with no role
read from them — facts a contract has, not things that fall due.* A calendar
that silently discarded two thirds of the dates in the store is one nobody can
audit.

## The adversarial pass

```bash
orpheus lint --db contracts.sqlite
```

![The lint page](images/contracts-lint.png)

Not a health check. It asks *where is this store lying to a reader*, and every
finding names a row you can open. It will not give you an all-clear it has not
earned: with little reviewed it says so plainly instead of reporting nothing and
letting you read that as sound.

## Where to start reviewing

189 unreviewed findings and no order on them is a corpus nobody reviews. So the
queue is ranked off the thing the quality report is actually waiting for:

```bash
orpheus triage --db contracts.sqlite
```

```
189 unreviewed, and no amount of review will make the report speak: it needs
two confidence levels with 5 reviewed instances each, and this corpus has 1
level(s) that can ever get there. That is a corpus too uniform to calibrate
against — it needs more documents, or more variety in them, not more review.
```

Which is the honest answer here, and worth dwelling on. The model quoted so
well that 184 of 189 extractions landed `explicit`; the other two levels hold 1
and 4. No amount of reviewing makes a rubric rank when almost everything sits at
one level. **Telling you to review five things would have been a lie that cost
you an afternoon** — this guide's own corpus is where that message came from.

---

# Case 2 — minutes, with no ontology at all

The Steering Council publishes monthly updates: prose, no headers, no schema,
nothing that looks like a contract. Pointed at `contract-core` this corpus
produces nothing, because there is nothing in it to file under `Contract` or
`MonetaryAmount`.

So the first question is not *what does this document say* but **what kinds of
thing are these documents about**.

## The machine proposes an ontology

```bash
orpheus init --db council.sqlite --bundle orpheus/bundles/starter-0.1.0.json …
orpheus ingest docs --db council.sqlite --actor-id act_… --storage-root storage
orpheus ontology survey --db council.sqlite --actor-id act_… \
  --engine anthropic --tier cloud --cloud-opt-in --sample 10
```

`starter` declares **zero object types**. Nothing is being matched against a
template.

![The ontology queue](images/survey-ontology.png)

Eight candidates out of ten documents read, each with excerpts you can click
through to the document they came from:

| Candidate | Support |
|---|---|
| `Person` — object type, names normalise as personal | seen in 8 of 10 |
| `Person.name` | seen in 8 of 10 |
| `Person.role` | seen in 7 of 10 |
| `Person → Meeting` — link type | seen in 4 of 10 |
| `Event` — object type | seen in 4 of 10 |
| `GoverningBody` — object type | seen in 3 of 10 |
| `Meeting` — object type | seen in 2 of 10 |

Two things on this page carry the whole design:

**"seen in 8 of 10 document(s) read — counted, not claimed."** That is support,
not confidence. The model is never asked how sure it is; the number is a count
of documents the survey actually found the thing in.

**"The machine proposes; you decide."** And the reason, stated on the page: a
survey is good at noticing that the same kind of thing keeps appearing. It is
bad at what follows — whether two of these are one type with a role, whether a
property should have been a type of its own. A wrong extraction is one row. **A
wrong object type is every row ever filed under it.**

You accept, rename or reject each candidate, then:

```bash
orpheus ontology draft --db council.sqlite --out council-core.json \
  --bundle-id council-core --name "Steering Council"
```

`draft` warns about accepted types that have no `name` property — they get no
wiki page, and every relation through them is orphaned. That warning is how
`council-core` came to have the shape it has.

## The same pipeline, a different world

![The council document list](images/council-index.png)

![A council document](images/council-document.png)

One monthly update, read into **35 findings**: 14 PEPs, 8 people, 5
organisations, 4 positions, 3 events, 1 steering council. No contracts, no
money, no clauses — the same code, a different ontology.

![The council wiki](images/council-wiki.png)

![A person page](images/council-entity.png)

**193 pages.** This one is `Łukasz` — a Person joining **13 of the 16
documents**, which is what a recurring individual looks like when the corpus is
minutes rather than contracts.

## And now the contrast

![The council network](images/council-network.png)

> **63% of extracted relations reached the graph.** 10 more join a named thing
> to a named thing and wait on the wiki queue; 87 link to a clause, a date or a
> document, which never gets a page.

Set the two side by side:

| | Contracts | Minutes |
|---|---|---|
| Pages | 55 | 193 |
| Canonical relations | 58 | 157 |
| Largest component | 18 pages | 87 pages |
| Relations reaching the graph | **29%** | **63%** |

The council page has enough structure to say something about it: 12 clusters by
Louvain at modularity 0.67, and `Łukasz` sitting on more shortest paths between
other pages than anything else in the corpus (betweenness 0.10, 21 links). Both
are labelled **heuristic** and carry the method that produced them, because a
cluster boundary presented as a fact is a claim the data does not support — the
deterministic half of the page (components, articulation points, isolates) is
kept separate and is not.

Same engine, same code path, opposite results — and the software explains the
difference itself rather than presenting either as the truth about networks.
Contracts assert things *about clauses*; governance is people meeting people, so
its relations have somewhere to land. The council page also separates **10 edges
waiting on the wiki queue** (work you can do) from **87 that will never be drawn**
(the shape of the corpus). Only the first number is a to-do list.

---

## Things that apply to both

### The original, and whether it is still the original

```bash
orpheus verify --db contracts.sqlite
```

Every excerpt, page number and character offset was computed from the file that
was ingested, so `GET /documents/<id>/original` re-reads the file and checks it
against the SHA-256 recorded at ingest before serving it. A database and a
`storage/` from two different moments looks perfectly healthy from the inside;
this is the only thing that would notice. See
[Two different pasts](two-pasts.md) and [Redaction](redaction.md).

### Taking a document back out

```bash
orpheus redact doc_… --actor-id act_… --note "Erasure request." --dry-run
```

Destroys everything read from a document and the file itself, and **keeps the
row** so the count, the audit trail and the account of why survive. `--dry-run`
counts first, because offering an irreversible action without a way to look
first is not offering a choice.

### Registers

![The registers page](images/contracts-registers.png)

Reference data held apart from the corpus, whose rows never become facts. Their
one job is to give a reviewer an *identifier* — the decisive, rare value that
name matching never gets. `orpheus register --identifiers` proposes which pages
a register could identify; a person confirms each one.

### Asking about the past

```bash
orpheus as-of 2024-06-01 --db contracts.sqlite
```

Two answers, never merged: what this store believed on that date, and what the
documents say was running on it. A contract signed in March and ingested in
November appears in the second and not the first.

---

## What it will not do

- **It will not tell you a corpus is sound.** Every summary that could be read
  as an all-clear is qualified with what it did not check.
- **It will not turn a machine reading into a fact.** Nothing is confirmed
  without a person, and every surface says which state a row is in.
- **It will not pick a winner between disagreeing sources.** Both are shown.
- **It will not ask a model how confident it is.** Confidence is computed from
  whether the excerpt is really in the document.
- **It will not invent an ontology for you.** It proposes one with evidence and
  waits.

---

## Reproducing this guide

```bash
python tests/e2e/screenshots.py <store.sqlite> <storage-root> docs/images <case>
```

Starts a real Datasette with the real plugin, signs in, and photographs the
pages. A guide illustrated with mockups drifts from the software the first time
somebody renames a button, and the reader who notices is the one who was
following it step by step.
