# The network, corroboration, and what a budget is denominated in

Four changes prompted by reading [sift-kg][sift], the closest prior art to
Orpheus yet: the same pipeline — ingest, extract, resolve entities with a human
in the loop, project a graph — aimed at a different user, as a CLI with no
server and NetworkX in place of SQLite. Its sibling [Civic Table][civic] adds
analyst verification before extracted facts carry evidentiary weight, which is
Orpheus's review model under another name.

Most of what it does, Orpheus already did. Four things it does that Orpheus did
not, and one defect the comparison exposed.

[sift]: https://github.com/juanceresa/sift-kg
[civic]: https://github.com/juanceresa/forensic_analysis_platform

---

## The defect: `edges` was unreachable

`population.normalise_population()` has always accepted `relationships`.
`extract.persist_population()` has always written them to `edges`, mapping each
engine's local instance handle through `id_map` to a freshly minted store id.
The table, the normaliser and the writer were all correct.

And not one of the five shipped engines ever returned a relationship. Every one
of them returned `{"extractions": [...]}` and nothing else, so the whole path
was reachable only by a caller constructing the population dict by hand. **No
corpus Orpheus had ever processed could contain a single relation.**

That would have made everything below permanently empty, so it is fixed here:
the two JSON-returning engines now ask for relations in the link types the
bundle declares, and parse them. Constrained to declared types deliberately — an
undeclared type is dropped by `extract()` and recorded as a schema amendment, so
inviting free-form relation names produces a pile of amendments instead of a
graph.

## Corroboration: the mirror of a tension

[Tensions](conflicts-and-lint.md) gave a verified disagreement somewhere to
live. That left the opposite case with nothing: four contracts independently
naming the same director were four rows, and the store knew nothing about the
fact that they agreed.

sift-kg keeps a `mentions` list on each canonical edge and combines their
confidences by product-complement — three sources at 0.7 becoming 0.973.
Orpheus counts and cites, and **changes no confidence value**, for two reasons.

The first is the rubric. Confidence here is one of five levels, each meaning
something a reviewer can state out loud: *explicit* is stated verbatim,
*implied* is mentioned with structure implied. A combined score is none of
those, and once one row carries 0.973 the levels have stopped meaning anything.

The second is worse. Combining assumes independence, and documents in a real
corpus frequently are not independent — an amendment quoting its parent, a
supplier declaration pasted into six tenders, a framework's boilerplate
inherited by every call-off under it. Six copies of one sentence is one source
wearing six hats, and counting it as six manufactures certainty out of
duplication, in a corpus assembled precisely because somebody suspects
something, in exactly the direction that suspicion would prefer.

So corroboration is counted in **distinct wordings across distinct documents**.
Excerpts are grouped by similarity, and copied boilerplate reports as one
wording across six documents rather than as six agreeing sources:

```
[     *] Ardmore Digital Ltd   role      supplier    3 doc(s) / 3 wording(s)
[copied] Ardmore Digital Ltd   address   Ushers Quay 6 doc(s) / 1 wording(s)
```

The threshold (`COPY_THRESHOLD`, 0.92) is not calibrated against a real corpus
and is a module setting and an argument for that reason. It is set high
deliberately: the failure that matters is counting copies as independent
sources, so the bias is toward calling near-identical text a copy.

**A known limit:** agreement on a property value is exact-string. "12 Ushers
Quay, Dublin 8" and "12 Ushers Quay" are one address to a person and two values
here. Normalising them silently would be worse — it would merge values nobody
asked it to merge — so this is left visible rather than papered over.

## The network

`edges` records a relation between two *instances* — one clause in one document.
Joining each endpoint through `entity_mentions` turns four contracts asserting
one relation into one canonical edge with four sources.

Two kinds of structure, kept apart because they are not equally trustworthy:

| Deterministic | Heuristic |
|---|---|
| `components` — islands | `communities` — label propagation, seeded |
| `articulation_points` — remove one, the graph splits | `bridges` — pages joining 2+ communities |
| `isolates`, degree | `community_connections` |

The heuristic half carries `basis: "heuristic"` on every row. Label propagation
is unstable — ties break at random, a different seed gives a different map, and
on a dense graph it can collapse everything into one cluster. Seeded so two
readers of a store see the same partition, and labelled so nobody mistakes a
cluster boundary for a fact. **Where a claim has to hold up, use a component: an
island is a fact, a community is a reading.**

Communities are label propagation rather than Louvain because the core has no
third-party dependencies, which rules out networkx. Tarjan's algorithm for
articulation points is iterative for the same practical reason recursion would
be wrong — depth would follow the longest path in the corpus, which is not a
number this can bound.

### Coverage is the first thing the topology reports

The graph is a projection, and **its completeness is the wiki's**. An edge
exists only where both endpoints resolve to entity pages, so a mention still in
the queue contributes nothing.

A sparse-looking network over 30% coverage means a half-built wiki, not a thin
corpus. Those are opposite findings and no structural number distinguishes them,
so `coverage` is the first key in the topology and the first line of the network
page, and it says which:

> Only 30% of extracted relations reached the graph: the rest have at least one
> endpoint with no entity page. The structure below describes the linked part of
> the corpus, not the corpus.

### What the structure is for

The highest-value finding is a **disconnected cluster pair** — two clusters the
corpus knows about separately and has never connected. For contracts that is not
a curiosity: "these two suppliers appear in unconnected parts of your corpus and
share a director" is the conflict-of-interest question, and it is visible in the
shape before anybody thinks to ask it.

The lint gained one check the graph makes possible: **`fragile_join`**, an
articulation point whose links nobody has confirmed. The graph's shape depends
on that page, and a link's review status does not change the shape — so the
topology gives no hint that a structural reading rests on an unchecked guess.

## What a budget is denominated in

sift-kg has `--max-cost`, which works because LiteLLM carries a pricing table.
Orpheus talks to OpenRouter and the rest over plain HTTP and knows nothing about
their price lists. A cap in euro would need a hardcoded rate table that goes
stale the week a provider changes one — and a budget that silently stops
matching the invoice is worse than no budget, because it is a control somebody
is relying on.

So the cap is in **characters sent**: exact, always available, and the thing a
public body has to answer for anyway. It is the third condition on the cloud
gate, checked before any text is prepared, so a refused call sends nothing.
Failed calls count — a call that errored sent its payload just the same. A
deployment that knows its own rate can set `cloud_price_per_million_chars` and
get an estimate labelled as an estimate.

## The agent skill

`.claude/skills/orpheus/SKILL.md`. sift-kg ships one teaching an agent to use
its graph as persistent memory; Orpheus had an export and an API and nothing
telling an agent how to read either, which is the difference between reusable as
a property and reusable as something that happens.

Its load-bearing rule: *never assert something the store does not hold, and
never assert it more firmly than the store does.* Then the four review states
and how to report each, the instruction to lead with a tension rather than
smoothing it, and the one that matters most for a corpus like this — **propose,
never decide.** `basis` and `source` record whether a human or a machine made
each call, and confirming something to tidy a queue destroys the measurement.

## What was deliberately not copied

**Auto-approving merges above a threshold.** sift-kg's reviewer confirms merges
with no human when all members clear 0.85, recorded as `CONFIRMED` and
indistinguishable from a person's decision. Orpheus's `basis` column exists
precisely so that *a person said so* and *the machine matched* never collapse
into one another.

**First-wins evidence.** `add_entity` there keeps the first context quote and
discards later ones, so a node cites one passage however many documents mention
it. Under that rule the Ardmore case — one company, two registered addresses,
two contracts — loses the second excerpt entirely at the node.

**Combined confidence.** Above.
