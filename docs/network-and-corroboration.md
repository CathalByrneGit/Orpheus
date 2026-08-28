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

### networkx is an optional extra, not a refusal

An earlier draft of this page said the zero-dependency rule "rules out
networkx". That was wrong, and worth correcting rather than quietly fixing: the
rule is that the **core** has no third-party dependencies, and there are eleven
optional extras already. The direct precedent is `match = ["rapidfuzz>=3"]` —
without it, entity candidates come from exact keys only.

So `graph = ["networkx>=3"]` works the same way:

| Without it | With it |
|---|---|
| label propagation | **Louvain**, and `modularity` saying whether the partition means anything |
| degree | **betweenness centrality** |

Everything else — components, articulation points, isolates, degree, and the
paths below — is stdlib and always available, because a store has to be
readable structurally by a script with nothing installed. Every function that
degrades says which method ran, so two reports are never silently comparing
different things.

Tarjan's algorithm for articulation points stays hand-rolled and is held to
networkx's implementation by a test: it is load-bearing, deterministic, and was
otherwise validated by nothing but itself.

### Paths: how two pages are connected

The question a corpus is actually asked, and the one a list of entities cannot
answer. It needs no dependency at all — a breadth-first walk over the adjacency
already built — and blaming networkx for its earlier absence would have been
wrong.

The Orpheus-specific part is that a chain is a chain of *claims*, so it is only
as good as its weakest hop:

```
1 chain(s), shortest 3 hop(s). 0 vouched for at every hop; the rest pass
through at least one link nobody has checked.

  [UNCHECKED] Ardmore Digital Ltd -> Kestrel Medical Group ->
              Meridian Systems Ltd -> Halloran Instruments Inc
      Ardmore  subcontracts_to  Kestrel   2 doc(s), nobody has checked this
      Kestrel  subcontracts_to  Meridian  1 doc(s), nobody has checked this
      Meridian subcontracts_to  Halloran  1 doc(s), nobody has checked this
```

That is a real finding from the test corpus — two otherwise unconnected
suppliers joined through a shared subcontractor — and reporting it without the
`UNCHECKED` would invite exactly the conclusion the store exists to prevent.
Every path names its weakest hop rather than leaving a reader to scan for it.

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
articulation point whose links nobody has confirmed.

### Drawing it

`/-/orpheus/map` draws the same projection: force-directed, pan and zoom, drag
a node, click one for what the store holds about it. The legend doubles as a
filter, size is the number of relations, and a dashed line is a relation nobody
has confirmed.

Two things are deliberate. **The coverage banner leads**, as it does on the
network page and more so — a diagram is more persuasive than a table and says
exactly as much, and a map read without knowing that 30% of extracted relations
reached the graph is a confident picture of a fraction of the evidence. And it
is **not a second source of truth**: the page reads `/graph/map`, which is
`graph.build` with no separate projection, so the picture cannot show a relation
the text views would not, or hide one they do.

The whole-corpus map spans every document in the store, so it is an
administrator view. An entity page links to its own neighbourhood instead —
scoped to that page out to an adjustable depth, which is the view an ordinary
actor may ask for.

### Two renderings, one payload

The map has two front ends, and which one a deployment gets changes how the
picture behaves and never what it claims — both read the same server-rendered
JSON from the same route.

**Built** (`frontend/`): Svelte 5, Vite and `d3-force`, the toolchain
`datasette-paper` uses for its link graph. d3's simulation is a better layout
than a hand-written relaxation, and the component gets what that buys: a
selected page lights itself and its neighbours and dims the rest, a dragged
node stays where it is put, and the zoom keeps the point under the cursor
under the cursor.

```
cd frontend && npm install && npm run build
```

That writes `plugins/static/`, which the plugin serves itself — Datasette's
`/-/static-plugins/` mount is not available to a plugin loaded with
`--plugins-dir`, because it resolves the directory by importing the plugin as
a package. `npm test` runs the unit tests over the pure rules (what counts as
unchecked, when a node is dimmed, how the view is fitted); `npm run check`
type-checks.

**Not built**: a template with a few dozen lines of relaxation, no toolchain,
works offline. `plugins/static/` is a build artefact and is not committed, so
this is what a fresh checkout draws, and the page chooses between them by
whether a Vite manifest is there to read.

Neither is a second source of truth. Both take `nodes`, `edges` and `coverage`
from `/graph/map`, rendered into the page by the server — which has already
decided what this actor may see, so there is no second round trip and no
second answer to disagree with the first.

A deployment that wants the built map as the only one should make the plugin an
installed package with an entry point rather than a `--plugins-dir` module;
`datasette-vite` then serves the bundle and this route can go.

### A defect the run found: proposing twice split every page

Running a real corpus, ingesting one more document and proposing again produced
a *second* "Kestrel Medical Group" beside the one already there — two pages with
an identical `naive_key`, which is the strongest evidence of sameness the store
has. `propose_entities()` always created; it never looked for a page the group
already belonged to.

Proposing after ingesting more documents is the normal thing to do, so the wiki
fragmented a little more every round. A group is now attached to an existing
page when a stated identifier or an exact key matches — the same two bases, in
the same order, that the grouping itself uses. Anything weaker stays with
`duplicate_pages()` and a person: attaching on a *similar* name here would be
resolution by machine, which the design refuses.

Attaching does not rename. The existing title was somebody's decision, or an
earlier proposal's, and a later batch carrying a longer spelling is not grounds
to overwrite it. The graph's shape depends
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
