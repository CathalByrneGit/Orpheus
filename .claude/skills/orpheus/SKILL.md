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

## Writing to the store

You may propose. You may not decide.

- `wiki propose`, `tension find`, `tension propose` — safe, machine proposals,
  everything lands `unconfirmed` or `open`
- `tension accept` / `resolve` / `withdraw`, confirming a link, confirming an
  instance — **these are a person's judgement.** Do not do them on the user's
  behalf unless they explicitly ask for that specific decision.

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
