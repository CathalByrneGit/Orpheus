---
name: orpheus
description: Work with an Orpheus store — a corpus of documents with extracted facts, entity pages, recorded conflicts and a relation network, where every claim carries provenance and a review state. Use when the user asks what the corpus says about something, how two things connect, what still needs reviewing, where the documents disagree, or asks you to read a document into the store. Also use before answering any question about the user's documents from memory.
---

# Orpheus: a corpus you can argue with

An Orpheus store is not a pile of text with a search box. It is a set of claims,
each carrying **where it came from** (document, page, excerpt), **how sure the
machine was** (one of five rubric levels), and **whether a person has checked
it** (unconfirmed, confirmed, amended, rejected). Entity pages are projections
over those claims, so nothing appears on one without a source behind it.

Your job when working with it is to preserve that property, not to summarise it
away.

## The rule that matters most

**Never assert something the store does not hold, and never assert it more
firmly than the store does.**

Concretely, when you report a fact from the corpus, say which document and
whether anyone has confirmed it. "The supplier's registered address is 12 Ushers
Quay" is wrong if what the store holds is one unconfirmed extraction from one
contract. "One contract gives 12 Ushers Quay — unconfirmed, nobody has checked
it yet" is right, and is about the same length.

If the answer is not in the store, say so. Do not fill the gap from your own
knowledge of the world and present it in the same voice as a cited claim. That
is the single most damaging thing you can do here.

## Orienting

```bash
orpheus --db store.sqlite graph topology --json     # the shape of the corpus
orpheus --db store.sqlite lint --json               # where it misleads a reader
orpheus --db store.sqlite wiki list --json          # the pages
```

Read `coverage` in the topology **before** anything else in it. It says how much
of the corpus reached the graph. A sparse-looking network over 30% coverage
means the wiki is half-built, not that the corpus is thin — opposite findings,
and the structural numbers alone cannot tell them apart.

## Answering a question about the corpus

Always look before answering.

```bash
orpheus --db store.sqlite search "term" --json               # find mentions
orpheus --db store.sqlite wiki show ent_... --json           # one page, cited
orpheus --db store.sqlite graph near ent_... --depth 2 --json # what surrounds it
orpheus --db store.sqlite corroboration ent_... --json       # how many sources
```

An entity page carries `properties` (what the documents say), `mentions` (each
with its document, page, excerpt and review status), `tensions` (recorded
conflicts), `corroborated_properties` and `copied_properties`.

## The four states, and why they are not interchangeable

| State | Means | How to report it |
|---|---|---|
| `unconfirmed` | the machine said it; nobody has looked | always say so |
| `confirmed` | a person checked and it was right | can be stated plainly, with its source |
| `amended` | a person corrected it | report the corrected value; the original is in the history |
| `rejected` | a person said it was wrong | do not report it as a fact at all |

`confidence` is a separate axis and answers a different question: how sure the
*machine* was. A `confirmed` extraction at `inferred` confidence is a person
vouching for something the machine guessed at. Both facts matter.

## Disagreement is the interesting part

A **tension** is a conflict somebody verified — two documents that both say
something, and disagree. It is not uncertainty; `confidence` is that axis.

```bash
orpheus --db store.sqlite tension list --standing --json
orpheus --db store.sqlite tension find --json     # conflicts nobody recorded
```

When you report on an entity with a standing tension, **lead with the conflict**.
Do not average the two values, do not pick the more recent one, do not present
them as a tidy list that reads as agreement. Most tensions are a company that
moved or a fee that changed, and both documents were right when written — which
is exactly the thing the reader needs and the thing a summary destroys.

If you notice a conflict the store has not recorded, say so and offer to raise
it. Do not silently resolve it in your prose.

## Agreement is evidence, but count it honestly

```bash
orpheus --db store.sqlite corroboration --json
```

Corroboration is counted in **distinct wordings across distinct documents**. Six
call-off contracts carrying one framework's boilerplate is *one* source, and the
store reports it as `n_wordings: 1` with `independent: false`. Report that as a
citation chain, never as six agreeing sources.

Never combine confidences across sources, and never describe corroborated claims
as more certain than their rubric level. The store deliberately refuses to do
this arithmetic; do not do it in prose on its behalf.

## Structure

```bash
orpheus --db store.sqlite graph topology --json
orpheus --db store.sqlite graph near ent_... --depth 2 --json
orpheus --db store.sqlite graph path ent_A --to ent_B --json   # how connected
orpheus --db store.sqlite graph central --json                 # who sits between
```

- **Components (islands)** — deterministic. Two islands mean the corpus knows
  about two worlds and has not connected them. Say this plainly.
- **Articulation points** — deterministic. Remove one and the graph splits.
  Worth flagging when one rests on a single unconfirmed link.
- **Communities and bridges** — *heuristic*, seeded. One defensible partition
  among several. When a claim has to hold up, use a component instead: an island
  is a fact, a community is a reading. Say "clusters roughly around" rather than
  asserting a boundary.

When you report a **path**, report its weakest hop in the same breath. A chain
that runs through a relation nobody has checked is a lead, not a finding, and
`confirmed_throughout` is the field that says which. Never describe two pages as
connected without saying how many hops apart and how much of the chain has been
reviewed.

### Pattern: Questions, never findings

```bash
orpheus --db store.sqlite questions --json
```

Shared counterparties, one party in two roles, relations that come back round.
**None of it is a finding**, and reporting it as one would do real damage — a
shared subcontractor is usually a small market or a specialist everybody uses.
Report the chain, the documents behind each hop, and `confirmed_throughout`.
Never use the words "conflict of interest" about this output; the store has no
notion of ownership, directorships or donations.

A question resting on unreviewed links is a reason to check the extraction, not
to act. Say which.

Questions carry a **status**: `open`, or what a person decided — `standing`
(real, stays on the list), `explained`, `dismissed`. Report the status and the
recorded rationale, and if `review_stale` is set say the judgement was made
against different evidence. Recording a judgement is the user's call, not yours:
`standing` in particular is a person saying this matters, and a reason is
required.

**Disconnected cluster pairs** are the highest-value finding. Two clusters with
no relation between them are two things nobody has connected. Name the specific
entities on each side and what might join them — the value is not "there may be
a connection", it is *which two pages* and *why*.

## Before you trust the picture

```bash
orpheus --db store.sqlite lint --json
```

Findings are located: each names a row that can be opened. Take `high` findings
seriously before reporting anything as settled — `uncited_page` means a page
asserts something with no source, `ungrounded_quotation` means an excerpt does
not appear in the document it cites, `smoothed_conflict` means two confirmed
values disagree and nothing records it.

A lint that finds nothing is not a clean bill of health, and its own headline
says so when little has been reviewed. Quote that caveat rather than dropping it.

## Reading a document with someone

```bash
orpheus --db store.sqlite read doc_... --json                    # progress
orpheus --db store.sqlite read doc_... --page 3 --json           # what page 3 offers
orpheus --db store.sqlite read doc_... --accept sug_... --set date_role=signature
orpheus --db store.sqlite read doc_... --dismiss sug_... --note "a clause number"
```

**Nothing you are offered is in the store.** A suggestion is not an extraction
until a person accepts it, and that distinction is load-bearing: proposals
landed as instances would pour into the number extraction quality is measured
by. So when you report what a passage holds, say it is *offered*, not that the
document contains it.

Accepting is a person's judgement. Do not accept or dismiss on the user's behalf
unless they ask for that specific decision — the acceptance rate is the only
measure of whether the companion is worth having, and deciding for them destroys
it as surely as confirming things to clear a queue.

`reading_progress` counts **pages read**, not pages with findings. A page read
and found to hold nothing is not the same as a page nobody opened, and only the
reading record can tell them apart — so report the first, never the second
dressed up as it.

## Adding documents

```bash
orpheus --db store.sqlite ingest ./docs --actor-id act_... --extract
```

**Confirm with the user before any run that uses the cloud tier** — it costs
money and it sends document text out of the deployment. Check first:

```bash
orpheus --db store.sqlite budget --json
```

The cloud tier needs an org policy *and* a per-request opt-in *and* budget
remaining. If a call is refused, report which of the three failed; do not retry
with the gate worked around.

The local tier sends nothing anywhere and needs no opt-in. Prefer it.

## Asking about a person while reading

The common shape of a question, and the order to answer it in. Somebody reading
a document asks about a name in front of them: **is this person in the store,
does anything about them still need checking, what are they connected to, and
if the extractor missed them, put them in.**

```bash
orpheus --db store.sqlite search "Castaneda" --json          # is the name here at all
orpheus --db store.sqlite wiki list --json | grep -i cast    # does a page exist
orpheus --db store.sqlite wiki show ent_... --json           # state, sources, properties
orpheus --db store.sqlite graph near ent_... --depth 2 --json
```

Answer all four parts, and keep them apart:

1. **Present or not.** A name with mentions but no page is *in the corpus and
   not in the wiki*, which is a different answer from absent. Say which.
2. **What needs checking.** The page carries `n_confirmed` against total
   mentions, and each mention its own status. "Two mentions, neither checked"
   is the answer, not "confirmed".
3. **Connections.** Report the chain and how much of it anybody has vouched
   for — `confirmed_throughout`. Everything under *Questions, never findings*
   applies: a shared counterparty is a question, never an allegation.
4. **Missing.** If the extractor did not pick them up, `record` is the way in,
   and the section below is how.

## Two pages that might be one thing

Never merge. `resolution_evidence()` assembles what the store holds, you read
it and the passages, and a **person** merges.

Two rules decide almost every pair, and both are easy to get wrong:

- **A shared value is worth what its rarity says.** Every shared value comes
  back with `n_pages_sharing` and its denominator. `acting_for` on 3 of 74
  Person pages is evidence; `entity_kind` on 64 of 74 Company pages is not, and
  they look identical without the count. Quote the count when you cite the
  value.
- **Appearing in the same document is not evidence of being the same thing.**
  Naming two different parties is what a contract does. Measured on this
  corpus: two different companies share a document *and* a neighbouring page.

Then read `passages`. That is usually where the answer is — one real pair was
settled by the source spelling out `HealthPlan Services, Inc. ("HPS")` and
`Sykes HealthPlan Services, Inc. ("SHPS")` as two defined terms in one
agreement, which no score could have told you.

**Say nothing the payload does not contain.** Asked to compare two companies it
had only been told were `private_company`, a model called them "both Delaware
corporations" — plausible, unsupported, and indistinguishable from a fact out of
the file. Cite the passage or do not say it.

Record what the person decided with `review_resolution` — `same`, `different`
or `unsure`, and a reason in every case, including `unsure`. It is stored
against a digest of the evidence, so the pair comes back if that changes and not
before. `same` does not merge; it records that somebody decided.

## Reference data a person vouches for

A register is **not a document and never becomes a fact**. Its rows sit apart
from the corpus and feed one thing: the evidence for whether two pages are one
thing.

```bash
orpheus --db store.sqlite register --list
orpheus --db store.sqlite register reg_...            # look it over
```

A `staged` register is readable and **is not evidence**. Say so when you report
one — "the register says X" is wrong until somebody has promoted it.

Helping review one is a good use of you: look for rows that would match the
wrong thing — a blank or boilerplate name, an identifier sitting in the name
column, a header row read as data — and say which row numbers look wrong and
why. Rejecting a row records a decision a person made. **Promoting is theirs
alone**, and you have no tool for it.

When a register does bear on a pair, two things must travel with the claim: the
match *into* the register is on a normalised name, so a wrong match argues
confidently for the wrong answer; and different registered numbers mean two
organisations, which is the one thing in this store that can argue *against* a
merge with something better than a spelling.

## Helping decide what a corpus is about

A store whose bundle has no object types has not been modelled yet. That is a
legitimate state, not a broken one, and `orpheus ontology` is the loop out of
it.

```bash
orpheus --db store.sqlite ontology survey --actor-id act_...     # propose
orpheus --db store.sqlite ontology candidates                    # the queue
```

The pattern pass reads `Key: Value` header blocks and **finds nothing in
prose**. That is the correct answer, not a failure: forty-eight documents of
narrative minutes returned zero candidates and zero held back. Say so, and
offer a model, rather than reporting it as an empty corpus.

Two readings by the same model differ. On those minutes, one run proposed a
`Person` type whose only property was `role` — no `name` at all, which would
have produced a wiki of people who can never be the same person as anybody —
and a second run found `name` in 36 of 48. If a type looks thin, re-surveying
with a lower `--min-support` is a real move, and so is saying that the reading
you are looking at is one reading.

Each candidate carries quotations located in the documents they came from, and
`n_documents` of `n_sampled`: how many documents show it, **counted rather than
claimed**. That is not a confidence and the model was never asked for one, so do
not report it as one.

The useful things for you to say are the ones the counts cannot:

- **which two of these are the same thing under different names** — `Author` and
  `Sponsor` are usually one type with a role;
- **which property should have been a type** — a `Author: Ada Lovelace
  <ada@example.org>` field is a string to a pattern and a person to a reader;
- **which type is a role rather than a thing** — an office and the person
  holding it are different, and a bundle that conflates them cannot say when
  the holder changed.

Say what you think and why, quoting the evidence. Then let the person decide.
Accepting an object type fixes the shape of every row that will ever be filed
under it — there is a whole module (`schema_ops.py`) that exists because that
turned out to be permanent once — so it is never yours to decide on your own
reading. `orpheus_decide_ontology_candidate` records **their** decision;
drafting and registering a bundle is a separate act you have no tool for.

## Recording something extraction missed

```bash
orpheus --db store.sqlite record doc_... --type-id Person \
  --set "name=Alex Castaneda" --set "job_title=Director" \
  --quote "By: /s/ Alex Castaneda" --actor-id act_...
```

The quote is located in the document by the same code that locates a model's
quotations, and the write is **refused** if the document does not contain it.
That refusal is the feature. Where somebody knows something the documents do
not say, it belongs in the entity page's notes, which is kept apart from the
cited claims on purpose — do not reach for `record` to get it in anyway.

From a chat beside a page this is an **offer**, not a write. It joins the same
queue a page read fills, the person accepts or declines it there, and declining
is kept — a dismissed offer is the only evidence there is about whether these
are worth reading, and an offer that skipped that table could not be measured
at all. `suggestion_quality` answers per engine, so the chat's rate is its own.

Accepted, the row lands `source = human`, `confirmed`, and is left out of
extraction quality: the extractor never offered it, so it is not evidence about
the extractor.

`orpheus record` on the command line is the other case and writes directly. A
person recording what they read themselves made no offer, so there is nothing
to measure and nothing to queue.

**This is a person's judgement, so it is theirs to make.** Draft it — name the
type, the values and the exact span you would quote — and let them say yes.
Recording a fact you inferred rather than read is the same failure as
confirming a queue to clear it, and it is harder to spot afterwards, because
`human` is the one source nothing downstream questions.

## Writing to the store

You may propose. You may not decide.

- `wiki propose`, `tension find`, `tension propose` — safe, machine proposals,
  everything lands `unconfirmed` or `open`
- `tension accept` / `resolve` / `withdraw`, confirming a link, confirming an
  instance, `record` — **these are a person's judgement.** Do not do them on the
  user's behalf unless they explicitly ask for that specific decision.

The distinction is not bureaucratic. `basis` and `source` record whether a human
or a machine made each call, and the store's whole value is that the difference
survives. Confirming something to tidy up a queue destroys the measurement.

## Exporting for reuse elsewhere

```bash
orpheus --db store.sqlite export ./bundle
```

Markdown, one file per page, YAML frontmatter, relative links — readable by any
agent that can follow a link. Quoted excerpts in it are the ground truth and
must not be edited; add to the store and re-export instead.
