← [Back to index](index.md)

# Prior art

Researched after Phase 1 was built, which is the wrong order. Recorded here so
the survey is not repeated, and so the parts of Orpheus that duplicate existing
work are known to be duplicating it.

The honest summary: **two of the four layers should probably be replaced with
existing tools, one is already built by other people, and one is the part worth
keeping.**

| Layer | Orpheus today | Prior art | Verdict |
|---|---|---|---|
| Parse | `ingest()` — pdftotext, with Docling when installed | Docling | **Adopted**, optional and untested in CI |
| Extract | `populate()` → LangExtract | LangExtract | **Adopted** on the Python branch |
| **Review, amend, audit, measure** | The amendment model | **nothing equivalent found** | **Keep** |
| Surface | Plumber API + read-only Datasette | `datasette-extract`, `datasette-comments`, `datasette-enrichments` | Mostly already built |

---

## The Datasette plugins

These matter because the intended direction is Datasette-first, and the Datasette
team has already built most of the interaction.

Four more — `datasette-accounts`, `datasette-agent`, `datasette-apps` and
`datasette-paper` — are assessed separately in
[Datasette ecosystem](datasette-ecosystem.md), because the question of whether
the agent plugin replaces the R core needed more room than a table row.

### `datasette-extract`

Upload a PDF, image or text into Datasette; an LLM extracts structured rows into
a SQLite table. Table-action menu, column names and types, per-column hints
("YYYY-MM-DD"). Model configuration via `datasette-llm`. Apache 2.0.

This is, almost exactly, "a user opens the UI, uploads a PDF, and the process
begins". It is worth reading before writing any upload page.

**What it does not do:** it writes plain rows. No provenance, no confidence, no
review state. And a PDF must have a text layer — scanned images are not OCR'd.
So the work is not building the page; it is making what the page writes carry
the fields review depends on.

### `datasette-comments`

Row-level comment threads with @-mentions, reactions and resolve-to-archive.
A ready-made conversation attached to any row.

Not a substitute for the amendment model — a comment is prose, an amendment is a
typed change with a preserved previous value — but a good fit for the discussion
*around* a correction, which Orpheus has no answer for at all.

### `datasette-enrichments` and `datasette-enrichments-llm`

> Read later, and installed: the shape is right and the released version is
> broken against Datasette 1.0. See
> [the fourth pass](datasette-ecosystem.md#datasette-enrichments).

Bulk-apply an operation, including an LLM prompt, across a filtered set of rows.
That is the batch-extract and re-evaluate paths, already generalised, with
multi-modal support via `media_url`.

`datasette-llm` underneath provides named *purposes*, so an administrator can map
"extraction" and "analysis" to different models — which is a cleaner version of
the local/cloud tier split in `orpheus/llm.py`.

---

## LangExtract

Google, Apache 2.0, ~38.5k stars, v1.6.0. Supports Gemini, OpenAI and **Ollama**,
so the local-first requirement survives.

The reason it matters is **source grounding**: every extraction carries a
`char_interval` locating it precisely in the source text. Orpheus stores an
excerpt string and a page number, which is weaker in a way that shows up
immediately in a reading UI — you cannot highlight an excerpt reliably, you can
only search for it and hope it appears once.

It also handles chunking, parallel workers and multi-pass extraction for recall,
all of which Orpheus would have to write.

**What it lacks:** confidence scores. That reads like a gap and turned out to be
closer to a virtue. It reports `alignment_status` instead — `match_exact`,
`match_greater`, `match_lesser`, `match_fuzzy`, or nothing when a span cannot be
located at all. That is a fact about the text rather than a model's opinion of
its own certainty, which is the thing the rubric exists to avoid storing, and it
maps onto the rubric without being bent: located verbatim is *explicit*,
boundary-shifted is *named*, fuzzy is *implied*, and unlocatable is *inferred* —
a model asserting something the document does not say.

**Adopted on the Python branch.** See `orpheus/population.py`. The gate is not
delegated with it: LangExtract would resolve its own API key and call its own
provider, which routes around the org policy, the per-request opt-in and the
`llm_calls` audit together — the same failure identified for `datasette-llm` in
[Datasette ecosystem](datasette-ecosystem.md).

### Docling

Converts PDFs into a structured document preserving **page numbers and bounding
boxes**. Paired with LangExtract, an extraction can be traced to a box on a page
rather than to a page. That is what a reading companion needs to highlight a
clause as someone scrolls past it.

**Adopted on the Python branch as an optional backend**, tried ahead of
`pdftotext` when installed and falling back to it when not — including when
Docling is present but cannot read a particular file, because a parser that
chokes on one document is not a reason to refuse the document.

**Not exercised.** Docling would not build in the environment this was written
in (`antlr4-python3-runtime` fails to compile), so the adapter is written
against the documented API and has never run. It is behind a capability flag
(`layout_aware`) and an extra (`pip install 'orpheus[layout]'`), and it should be
treated as untested until someone has run it. Only text is taken from it so far;
the bounding boxes are the next thing to plumb through, alongside LangExtract's
character intervals.

---

## OCDS — the Open Contracting Data Standard

The one that should have been found first.

A free, non-proprietary JSON Schema standard for public contracting, implemented
by **over 50 governments**, covering five stages: planning, tender, award,
contract, implementation. It ships validation tools and JSON↔CSV converters.

Phase 1 invented `Contract`, `Company` and `Person` from scratch. OCDS already
has this vocabulary, it is what Irish eTenders and EU TED data speak, and
adopting its names makes the extracted data interoperable rather than local.

See [OCDS alignment](ocds-alignment.md) for the mapping and what changes.

---

## CUAD

The Contract Understanding Atticus Dataset: **13,000 expert-labelled clauses
across 510 contracts, 41 clause categories, CC BY 4.0.**

Two uses, and the second is the valuable one:

1. A real `clause_type` vocabulary, instead of the list invented for the bundle.
2. **An evaluation set.** Extraction quality could be measured against expert
   labels immediately, rather than waiting for humans to review enough documents
   to make `quality_report()` meaningful. It does not replace real review —
   CUAD is US commercial contracts, not Irish public-sector ones — but it turns
   "we have no idea whether extraction works" into a number, today.

---

## Considered and rejected

**Label Studio, Argilla.** Annotation platforms for building training data. They
assume a labelling task queue, not a corpus with per-document permissions that
people browse and correct in place. Adopting either would mean replacing the
review layer with something less suited to the actual workflow.

**Rowfill, Hyper.** Whole products with their own extraction, verification and
audit stories. They would replace Orpheus rather than be tweaked into it. Worth
reading for how they present source-traceability.

---

## Markdown as the agent-maintained knowledge format

Four projects arrived independently at the same architecture: a directory of
markdown, one concept per file, maintained by an agent, with no database and no
proprietary platform.

| Project | What it applies the pattern to |
|---|---|
| Karpathy's **LLM-wiki** | The pattern proposed as a pattern |
| **MemPalace** | Personal memory, with retrieval benchmarks |
| Google's **Open Knowledge Format** v0.1 (2026-06-12) | A published spec: bundles, YAML frontmatter, `type` as the only required field, `index.md`, `log.md` |
| **DocIt** | Codebase documentation, with human notes flowing upward |

The shared surface is one concept per file, directory structure as taxonomy,
cross-linking via ordinary markdown links, an index for progressive disclosure,
and the agent as the runtime for reads and writes. The format is both
human-readable and machine-parseable with no tooling.

Two findings from that group carry directly:

**Store fully, structure the index.** MemPalace measured it: raw verbatim
storage scores 96.6% R@5 on LongMemEval against 84.2% for their lossy
compression dialect — a 12-point regression, because compression strips the
context retrieval depends on. Orpheus keeps `provenance.excerpt` rather than a
summary for the same reason, and this is the empirical case for a decision that
was otherwise argued from principle alone.

**Consensus docs are dangerous.** DocIt's `> **Tension**:` convention exists
because an agent summarising a body of knowledge produces accurate,
well-formatted, smoothed-over output, and the genuine conflicts disappear into
it. Orpheus had no equivalent until [conflicts](conflicts-and-lint.md); its
review vocabulary resolved only towards agreement.

Orpheus does not store markdown — it stores rows, because per-document
permissions, an append-only history and a confidence rubric enforced at the
persistence boundary are not things a directory of files does well. But
`orpheus export` projects the wiki out into this shape, which is what makes the
knowledge reusable by something that is not Orpheus.

Three things Orpheus has that none of the four do: provenance that locates every
quotation in its source, a review state on every claim, and per-document
permissions. Two things they have that Orpheus does not: no runtime at all, and
a place for a person to write knowledge that has no source behind it.

---

## sift-kg — the closest neighbour

[sift-kg][sift] is the same pipeline as Orpheus aimed at a different user: a CLI
with no server and no permissions, NetworkX and JSON in place of SQLite. Ingest,
schema discovery, LLM extraction, graph, human-approved entity resolution,
narrative, viewer. Its sibling [Civic Table][civic] adds "a 4-tier verification
system where analysts and JDs validate AI-extracted facts before they're treated
as evidence" — Orpheus's review model under another name, and independent
evidence that the model is the right one for evidentiary work.

[sift]: https://github.com/juanceresa/sift-kg
[civic]: https://github.com/juanceresa/forensic_analysis_platform

**Converged on independently:** human-in-the-loop resolution with the LLM
proposing merges; deterministic pre-dedup before the expensive pass (their
`prededup.py` strips title prefixes where `naive_key()` strips legal forms — the
same idea, a different domain's noise); provenance to document and passage; and
a discovered schema saved and reused so types stay consistent across chunks,
which is what having a bundle up front solves.

**Taken from it:** canonical edges with their sources kept whole, which became
[the network](network-and-corroboration.md) and, with it, corroboration; the
structural vocabulary of bridges, islands and disconnected clusters; a budget
cap; and the bundled agent skill.

**Not taken, and why:** it auto-approves merges above 0.85 and records them as
`CONFIRMED`, indistinguishable from a person's decision — Orpheus's `basis`
column exists so those never collapse into one another. Its `add_entity` merge
keeps the first context quote and discards later ones, so a node cites one
passage however many documents mention it. And it combines confidence across
sources by product-complement, which assumes an independence a document corpus
rarely has.

---

## LatticeDB — the storage question, asked properly

[LatticeDB][lattice] is an embedded single-file property-graph database in Zig:
HNSW vectors and BM25 full-text in the same engine and the same query layer as
graph traversal, one file, WAL, single writer. MIT, v0.12.0, one author. The
operational shape is Orpheus's exactly, which is what makes it worth taking
seriously rather than filing under "graph databases".

[lattice]: https://github.com/jeffhajewski/latticedb

Installed from the published wheel and run against the graph this corpus really
produced, rather than read about. It works, concurrent readers included. The
detailed findings — including the two limitations the benchmark tables do not
show — are in
[Open decisions](open-decisions.md#vector-and-similarity-search), because it is
a live candidate for the vector slot rather than settled prior art.

The short version: for *how are these two connected, through which hops, on
whose evidence*, it currently returns endpoints only, and that question is the
reason Orpheus would want a graph engine at all. And Datasette is the browsing
surface, the permission enforcement and the writer, so the storage engine is not
a component that can be swapped.

What it does confirm is that the shape Orpheus chose — one file, single writer,
WAL, graph and text over the same data — is a shape other people are building
deliberately rather than an accident of picking SQLite.

---

## What nothing else does

Searched for specifically, not found:

- Four-state review (`unconfirmed` / `confirmed` / `amended` / `rejected`) as a
  first-class property of every extracted fact.
- An append-only history that **preserves the machine's value beside the
  human's**, which is what makes extraction quality measurable after the fact.
- Staleness propagated through recorded evaluation dependencies.
- A confidence rubric enforced at the persistence boundary rather than accepted
  as whatever the model emitted.
- Quality measured *from the corrections themselves* — accuracy by rubric level,
  whether the rubric ranks reliability at all, which rules over-fire.

The nearest neighbours each cover one piece: `datasette-comments` has discussion
but not typed amendments; Rowfill has source-traceability but not amendment
history; LangExtract has grounding but no review state at all; sift-kg has the
graph and the resolution loop but reviews merges rather than facts, and has no
`amended`; LatticeDB has the storage and traversal primitives and no opinion
about evidence at all — a relation there is a fact, not a claim somebody made in
a document that a person may or may not have checked.

Two more found nowhere else, added since:

- **A verified conflict as a terminal state.** Every review vocabulary examined
  resolves towards one answer; none has a way to record that two sources
  disagree and both are right.
- **Agreement counted in distinct wordings.** Every system that counts
  corroboration counts rows, so copied boilerplate reads as independent
  confirmation.

That combination is the part of Orpheus worth keeping regardless of what happens
to the layers around it.

---

## What this does to the language question

Every tool above is Python — LangExtract, Docling, and the whole Datasette plugin
family. That is a stronger argument for a Python core than anything in
[Open decisions](open-decisions.md#r-stack-vs-a-python-rebuild), and it is a
different argument: not that R is worse, but that **the tweakable prior art is
all on one side**.

---

[← Back to index](index.md) | [Next: OCDS alignment →](ocds-alignment.md)
