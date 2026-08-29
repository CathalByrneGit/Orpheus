# Where an ontology comes from

Everything else in this store is written against a bundle. Ingest fills tables
the bundle declared, extraction fills columns it named, the wiki is built out of
types it listed, the graph projects links it defined. The claim the project
rests on is that none of that knows about contracts — **the domain is the
bundle**, and swapping the bundle swaps the domain.

That claim had a hole in it. Swapping the bundle assumes somebody has a bundle
to swap in. For a domain nobody has modelled yet the first question is not
"what does this document say" but "what kinds of thing are these documents
about", and until it is answered there is no type to file an answer under. So
the first bundle was always written by hand, by somebody who had already read
enough of the corpus to know what was in it — which is exactly the work the
rest of this project exists to help with.

`orpheus/ontology.py` is that missing step.

---

## Should the machine help author an ontology?

**Yes, and it should never author one.** That is not a hedge; it is where the
line actually falls, and the two halves come from different observations.

A model reading forty documents is genuinely good at one thing here. It notices
that the same kind of thing keeps appearing and that it keeps carrying the same
handful of attributes. That is a real reading of a corpus, it is tedious to do
by hand across forty files, and it is the part that scales.

It is bad at the decision that follows. Whether `Author` and `Sponsor` are one
type with a role or two types. Whether `status` is a property of the proposal or
a thing in its own right with a history. Where the line between a person and the
office they hold runs. Those are not readings, they are commitments — and they
are the commitments that make an ontology somebody's rather than nobody's.

They are also expensive to get wrong in a way an extraction is not. A wrong
extraction is one row to amend and the
[machinery for that](provenance-and-amendment.md) is already built. A wrong
object type is every row that will ever be filed under it, a table, a wiki page
kind, a set of edges, and a migration for anybody who has already loaded data.
`orpheus/schema_ops.py` exists because that turned out to be permanent once.

So the split is the one this project keeps arriving at: **the machine proposes
with evidence, a person decides.** A survey produces a queue of candidates, each
with a quotation `align.py` located and a count of how many sampled documents
show it. A bundle comes into being only when somebody has been through that
queue. Nothing in this module writes a bundle as a side effect, and
`draft_bundle()` deliberately does not register what it drafts.

---

## The loop

```
orpheus init --bundle orpheus/bundles/starter-0.1.0.json   # no object types
orpheus ingest ./corpus --actor-id act_...

orpheus ontology survey    --actor-id act_...              # propose
orpheus ontology candidates                                # read the queue
orpheus ontology review cnd_... --decision accepted --to Proposal --actor-id act_...
orpheus ontology draft --bundle-id mydomain-core --out mydomain-0.1.0.json \
                       --type-id Proposal --document-type ... --register
```

`starter-0.1.0.json` is a bundle with no object types at all. That used to be an
invalid state — the schema required a `primaryObjectType` of every bundle — and
making it legal is what lets a corpus be ingested, read and surveyed before
anybody has decided what it is about. A bundle that *has* object types still
must name a primary one; the check moved from the JSON schema into
`_domain_problems()`.

### Support is not confidence

The model is never asked how sure it is, here as anywhere else. A candidate's
`confidence` is what the alignment says, by the same rubric everything else
uses. What a reviewer wants instead is `n_documents` of `n_sampled`: how many
documents showed this, out of how many were read. A type in one document of
forty is a different proposition from one in thirty-eight, and neither of those
is a probability.

It is counted, not claimed. A model saying a type recurs is not evidence that it
does; the count comes from locating its quotations.

### Renaming is the ordinary move

`review_candidate(..., accepted_as=...)` records "yes, and it is called this",
and the candidate lands at `amended` rather than `accepted`. A survey notices
that something recurs and has no way to know what it is called, so a vocabulary
with only accept and reject would record every rename as a rejection followed by
a hand-written type — throwing away the evidence that argued for the thing.

### What a drafted bundle gets that nobody was asked about

Three things, because they are not domain knowledge:

- the provenance columns every instance table carries, and the `Reviewable`
  interface that says a row can be confirmed;
- `naive_key` alongside any accepted `name`, and the `Named` interface — without
  which entity resolution has nothing to match on and the wiki is a list of
  pages that can never be the same page as anything;
- a foreign key on the `to` side of every accepted link. No survey proposes one:
  the documents say a condition belongs to an application, not that the
  condition row carries the application's key.

`DocumentScoped` is *not* added automatically. Whether two documents sharing a
title are one thing or two is a fact about the domain, and `--document-scoped`
is how somebody says which.

---

## Two passes, and what each is for

### The header-block pass (`deterministic`, the default)

Reads runs of `Key: Value` lines — the RFC 822 convention that mail, PEPs,
Debian control files, front matter and most memo templates all inherit — and
proposes a property per recurring key. It sends nothing anywhere, needs no
opt-in, and cannot propose a field the documents do not literally contain.

Three rules do the work:

- **A block, not a line.** Two adjacent lines with colons happen in prose all
  the time. `MIN_BLOCK = 3` is what keeps this from proposing `Note` and
  `Warning` as properties of everything.
- **An empty value is still a field.** Ten of forty documents in the
  calibration corpus carry a bare `Post-History:`. Requiring a value counted
  that field in twenty-four of them — a claim about how often it is *filled in*,
  not about whether the corpus has it.
- **A block must carry at least one value.** Otherwise three bare headings
  (`Introduction:`, `Motivation:`, `Specification:`) are an ontology.

### The model pass (`chat`, `anthropic`, `llm`)

One call over the openings of every sampled document, not one call per document.
"What does this document say" is answerable a document at a time; "what kinds of
thing recur across these documents" is not answerable at all from one, and
asking it twenty times produces twenty ontologies to reconcile — which is the
hard part of the job, done twenty times worse.

It goes through `engines.ask()`: the transport of the extraction engines
without their prompt, with the same cloud gate before anything is sent and the
same `record_llm_call` in a `finally`. `gliner2` and `langextract` are refused,
with a message naming the engines that can answer — they are handed a field list
and return spans for it, and there is no shape of call that asks either of them
an open question.

Two things are computed rather than trusted:

- **A quotation found nowhere in the corpus is dropped**, not stored at low
  confidence. Elsewhere an unlocatable excerpt still has a property value
  attached and is worth keeping at `inferred`; here the quotation *is* the whole
  of the evidence, and a candidate whose support is a sentence that exists in no
  document is one a reviewer would be accepting on trust.
- **A fuzzy match is not evidence that a document contains something.** `align`
  falls back to matching three consecutive words. On the real corpus that found
  a proposed `abstract` property, quoted from one PEP, in twenty-one of forty
  documents that share nothing but an opening phrase. Support counts
  `match_exact`, `match_greater` and `match_lesser` only.

### The cheap pass measures what the expensive one proposed

The model's support count is a count of *its quotations*. On the first run it
proposed `title`, cited two documents, and the survey reported 2 of 40 for a
field every document in the corpus carries. The number was not wrong — it was
checked, and the checking is what makes it worth anything — but read as "how
much of the corpus has this" it is a floor, and a reviewer sorting the queue by
support would find the real types at the bottom of it.

So where the corpus states its own fields, the header pass counts them:

| property | quoted from | after corroboration | true |
|---|---|---|---|
| `title` | 2 | 40 | 40 |
| `created` | 3 | 40 | 40 |
| `python_version` | 7 | 28 | 28 |
| `post_history` | — | 34 | 34 |
| `number` | 2 | 2 | 40 |

`number` is the interesting row. The model named it `number`; the corpus calls
the field `PEP`, so nothing corroborates it and it stays at its quotation count
— which is itself the signal that a reviewer should rename it.

One further rule: **a type is supported at least as well as its best-supported
property.** You cannot have the title of a proposal in forty documents and the
proposal itself in three. Without it, a model that quoted its types sparsely and
its properties widely left a queue with properties above the threshold and no
type for them to be properties of — not a modelling question a reviewer can
answer, just an artefact of how many quotations the model happened to give.

---

## The second domain, measured

The claim is tested rather than asserted. `tests/test_domain_neutrality.py`
runs an unrelated bundle through the pipeline; this is the step before it — a
corpus arriving with no bundle at all.

**The corpus.** Forty Python Enhancement Proposals, sampled with a fixed seed
from the 738 in `python/peps`. They share nothing with contracts: no parties, no
governing law, no value, no term. They also have a *documented* ontology — PEP 1
defines the headers — so the survey can be scored rather than admired.

**The header pass found the corpus's own schema exactly.**

| | |
|---|---|
| distinct header fields present in the 40 documents | 16 |
| present in ≥ 2 documents (the support threshold) | 14 |
| of those, `Status` — the column the store owns | 1, skipped |
| proposed | **13 of 13 available** |
| proposed but not present anywhere | **0** |
| document counts matching ground truth | **13 of 13, exactly** |

`Superseded-By` and `Requires` appear in one document each and were held back by
the support threshold, reported as `n_below_support: 2`. Not one line of PEP
prose was proposed as a field.

**The pipeline then ran on the drafted bundle with no code changes.** Forty
documents, forty `Proposal` instances, zero schema amendments — the ontology
fitted. Scored against the headers:

| field | correct / present |
|---|---|
| `pep` | 40/40 |
| `name` | 40/40 |
| `proposal_type` | 40/40 |
| `created` | 40/40 |
| `python_version` | 28/28 |
| `topic` | 12/12 |
| `resolution` | 12/13 |

The one miss is a leading RST backtick the model dropped from a URL.

**And the graph had no edges at all.** One object type is a table, not a
network: `Author: Guido van Rossum <guido@python.org>` is a *field* to a header
block, and a header block has no way to say it names a person.

The model pass, on the same corpus, proposed `Person` with `name` and `email`,
and three links — `authored_by`, `sponsored_by`, `delegated_to`. Reviewed the
same way, drafted into a second bundle and run over the same forty documents,
that is the difference it makes:

| | header-block bundle | model bundle |
|---|---|---|
| object types | 1 | 2 |
| link types | 0 | 3 |
| entity pages | 40 | 101 |
| edges | **0** | **82** |
| isolated pages | 40 | 0 |
| graph coverage | — (no relation material) | 1.0 |
| components | 0 | 21 |

The pages are not just more numerous, they are a different kind of thing. Sixty-
one `Person` pages, merged across documents on `naive_key` because the drafted
bundle declared `Named` — Barry Warsaw on six PEPs, Guido van Rossum on four —
and PEP 8001 sitting at degree 13 as the hub of the governance cluster. None of
that is reachable from the header block that produced the *better* field
extraction.

That is exactly the decision the module docstring says a pattern cannot make,
and it is the clearest statement of what each pass is for:

> **The header pass gets the fields right and the shape wrong. The model gets
> the shape and needs the header pass to tell it how much of the corpus it is
> talking about.**

Neither is the reviewer. What neither proposed, and what a person looking at
this queue would have to decide, is whether `Sponsor`, `PEP-Delegate` and
`BDFL-Delegate` are three link types or one link type with a role — the same
question `Author`/`Sponsor` raises, and the same one that made
`RESOLUTION_STATUSES` a separate vocabulary from `STATUSES` two features ago.

---

## A third domain: prose with no structure at all

PEPs have a header block, so the pattern pass had something to read. The
harder case is a corpus that states nothing about itself, and the one this was
run on next is **forty-eight Python Steering Council monthly updates**
(`python/steering-council`, `updates/*.md`) — governance minutes, 181,000
characters of narrative bullets with no `Key: Value` anywhere.

**The pattern pass found nothing.** Not a low-confidence something: zero
candidates, and zero held back by the support threshold. That is the correct
answer for prose, and it is the property worth having — a survey that
manufactures a schema out of a corpus that has none is worse than one that
says so.

```
$ orpheus ontology survey --sample 48
{"engine": "deterministic", "n_documents_read": 48,
 "n_candidates": 0, "n_below_support": 0}
```

**The model did all the work, and did it differently each time.** Three
readings of the same forty-eight documents:

| | run A | run B (`--min-support 1`) | run C (fresh store) |
|---|---|---|---|
| object types | Meeting, PEP, Person, SteeringCouncil, WorkingGroup | + Position, Event, Organisation | Organisation, Pep, Person, PythonRelease, WorkingGroup, SteeringCouncilMeeting |
| `Person.name` | **not proposed** | 36/48 | 12/48 |
| candidates | 14 | 35 | 17 |

That instability is the single strongest argument in this whole feature for
where the line is drawn. Run A proposed a `Person` type with `role` as its only
property and no `name` at all — a bundle drafted from it would have produced a
wiki whose people could never be the same person as anybody. Run C found
`PythonRelease`, which neither of the others saw, and called the meeting type
`SteeringCouncilMeeting` rather than `Meeting`. None of those readings is
wrong. They are readings, and choosing between them is not something a second
model call settles.

**Support is quotation support here, and it sits low.** With no header block to
corroborate against, the numbers mean "the model quoted this from N documents,
and those quotations were found there". Run C's best-supported type was
`Organisation` at 44 of 48; several real link types sat at 2 of 48. One
candidate — `Pep → Meeting`, "discussed in" — came back at **0 of 48**: its
quotation was located in no document at all, so it counted nothing and the
threshold dropped it. Grounding computed rather than trusted, doing exactly
what it is for.

### The three decisions a person made that the machine could not

Driven through `/-/orpheus/ontology`, one form submit at a time:

- **A role is a thing, not a string.** The survey proposed `Person.role` *and*
  a `Position` type with a title, from the same documents. Only one of them can
  say when the holder changed. `Person.role` rejected.
- **A property that is really a relation.** `PEP.bdfl_delegate` alongside a
  `person_appointed_bdfl_delegate` link. Keeping both would file the same fact
  in two places and let them disagree. Property rejected, link kept.
- **The same relation under two names.** `council_discusses_pep` and
  `meeting_covers_pep`. Rejected the one that duplicates the other a level up.

Thirty-nine candidates decided, four of them renamed — `PEP.title` and
`Position.title` both to `name`, so the wiki can merge a proposal or a post
across the documents that mention it, and `Meeting.meeting_date` to `date`.
The drafted bundle: 8 object types, 85 properties, 8 link types, no problems.

### And then the graph came back at 8%

Forty-eight documents extracted, 794 relations found — and 62 of them reached
the graph.

`Meeting` and `SteeringCouncil` had been accepted with no property called
`name`. The wiki is built from types implementing `Named`; the graph is a
projection over wiki pages. So those two types held rows, got no pages, and
**625 of the 794 edges had nowhere to land** — every `meeting_covers_pep`,
every `person_attends_meeting`, every `council_holds_meeting`.

Nothing was wrong. `coverage` said exactly what had happened, in the sentence
it leads every structural view with. But it said it after forty-eight model
calls, and the decision that caused it looked obviously right at the time: a
meeting is identified by its date, so `Meeting.meeting_date` → `date`.

Three things came out of that:

1. **`draft_bundle` now warns before the extraction.** A type with no `name`
   gets a warning naming it and counting the link types that touch it. It is a
   warning and not a problem — not everything is an entity — but it is said
   while it is still cheap.
2. **`reopen_candidate` exists**, because a warning is only worth having if the
   decision it warns about can be changed. Reopening restores the question, not
   the state before it was asked: the evidence stays attached and
   `edit_history` shows `accepted → reopened → amended`.
3. **The correction cost no model calls at all.**
   `orpheus property rename Meeting date --to name` moved the column and the
   bundle property together, keeping all 174 values; re-drafting at 0.2.0 added
   `naive_key` and `Named`; `wiki propose` and the graph did the rest.

| | before | after |
|---|---|---|
| relations reaching the graph | 62 (7.8%) | **513 (64.6%)** |
| canonical edges | 44 | 481 |
| connected pages | 64 | 330 |
| components | 20 | 13 |
| documents re-read | — | **none** |

### One more thing the run found

The first `wiki propose` after that correction produced a single `Meeting` page
called *"April through June 19, 2024"* carrying **174 mentions** — every meeting
in the corpus, merged into one.

`propose_entities` grouped on the `naive_key` **column as read**, and a bundle
that has just gained that column has it null on every row written before. Every
such mention therefore shared the key `None`. The document-scoped branch
already derived the key rather than reading it, for exactly this reason, in a
comment that says so; the named branch did not.

A false merge at the scale of a whole type is the worst outcome this store has
a rule about, so the fix is the one the rule implies: derive the key when the
column is empty, and give a name that still reduces to nothing a page of its
own. A false split is what to fail towards. 1 page became 169, and the same
change removed a latent `NameError` in the scoped branch that had never fired
only because its `or` short-circuits whenever the column is populated.

## What this does not do

- **It does not propose concepts, scores, actions or queries.** Those are
  judgements about what matters in a domain, not readings of what is in it.
- **It does not merge two candidates that are the same thing under two names.**
  The evidence machinery for exactly that decision exists one level down, for
  instances (`entities.resolution_evidence`); lifting it to types is the obvious
  next thing and is not built.
- **It does not amend an ontology already in force.** That is
  [`schema_amendments`](provenance-and-amendment.md), which is a different
  question with different stakes: a patch bump against rows already filed.
  Sharing a queue would mean a reviewer could not tell which question they were
  being asked.
