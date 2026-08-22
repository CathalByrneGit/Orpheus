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
| Parse | `orph_ingest()` — pdftools/pdftotext, page text | Docling | Replaceable, and Docling is better |
| Extract | `orph_populate()` → ontologyDiscoverR | LangExtract | Replaceable, and LangExtract is better |
| **Review, amend, audit, measure** | The amendment model | **nothing equivalent found** | **Keep** |
| Surface | Plumber API + read-only Datasette | `datasette-extract`, `datasette-comments`, `datasette-enrichments` | Mostly already built |

---

## The Datasette plugins

These matter because the intended direction is Datasette-first, and the Datasette
team has already built most of the interaction.

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

Bulk-apply an operation, including an LLM prompt, across a filtered set of rows.
That is the batch-extract and re-evaluate paths, already generalised, with
multi-modal support via `media_url`.

`datasette-llm` underneath provides named *purposes*, so an administrator can map
"extraction" and "analysis" to different models — which is a cleaner version of
the local/cloud tier split in `R/llm.R`.

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

**What it lacks:** confidence scores. The rubric would have to be applied on top
— which is what `orph_snap_confidence()` already does at the persistence
boundary, so the seam exists.

### Docling

Converts PDFs into a structured document preserving **page numbers and bounding
boxes**. Paired with LangExtract, an extraction can be traced to a box on a page
rather than to a page. That is what a reading companion needs to highlight a
clause as someone scrolls past it.

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
   to make `orph_quality_report()` meaningful. It does not replace real review —
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
history; LangExtract has grounding but no review state at all.

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
