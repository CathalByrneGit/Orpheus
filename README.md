# Orpheus

A document intelligence platform for public servants. **Phase 1: ingest and
extraction quality.**

Orpheus takes a document and turns it into structured, human-reviewable facts in
an ontology store, with enough provenance on every fact to know where it came
from and whether a person has checked it.

**The domain lives in the ontology bundle, not in the code.** The bundle shipped
here describes public-sector contracts — that is the worked example, and the one
the documentation uses throughout. A bundle describing planning applications,
inspection reports or grant awards runs the same pipeline with no code changes;
the test suite includes one, to keep that honest rather than aspirational.

Nothing downstream — entity resolution, the relationship graph, conflict-of-interest
views, the reading companion — is built here, because all of it depends on
extraction being trustworthy first.

---

## What it does

```
document.pdf
  → ingest        hash, page text, OCR fallback for scans
  → classify      local model: doc type, sector, jurisdiction
  → populate      dates and amounts by pattern; entities by model, against the bundle
  → review        confirm / amend / reject, nothing overwritten
  → analyse       versioned rule concepts + optional narrative reading
  → escalate      opt-in comparison against the rest of the corpus
```

Every fact carries `source` (`ai_local` / `ai_cloud` / `human`), a `confidence`
from a five-level rubric, a review `status`, and a row in an append-only audit
trail.

Because corrections preserve the machine's value beside the human's, the store
answers the question Phase 1 actually turns on: **is extraction good enough to
build on yet?** `orpheus report` computes it — accuracy by confidence level,
whether the rubric ranks reliability at all, which rule concepts over-fire, and
which fields people keep fixing.

---

## Quick start

```bash
pip install -e '.[server]'

orpheus --db data/orpheus.sqlite init --admin "Ada"
orpheus --db data/orpheus.sqlite ingest contract.pdf --actor-id act_... --extract
orpheus --db data/orpheus.sqlite report
```

`init` prints the command that serves what it just built:

```bash
datasette serve data/orpheus.sqlite \
  --metadata config/metadata.yml --config config/datasette.yml \
  --plugins-dir plugins --template-dir templates --port 8001
```

Or `cd deploy && docker compose up -d`, which runs that plus Ollama.

The browser page at `/-/orpheus` adds upload and per-row confirm/amend/reject.
The same routes are available as JSON under `/-/orpheus/api/`.

---

## Documentation

Start at **[docs/index.md](docs/index.md)**.

| Page | What it covers |
|---|---|
| [Data model](docs/data-model.md) | Tables, the ontology bundle, the confidence rubric |
| [Pipeline walkthrough](docs/pipeline-walkthrough.md) | The nine steps, with the function that runs each |
| [Entities: the wiki](docs/entities.md) | Mentions vs entities, and why a page is a projection |
| [Provenance and amendment](docs/provenance-and-amendment.md) | How a machine guess becomes a checked fact |
| [Extraction engines](docs/extraction-engines.md) | Four ways to run the model pass, and when each is right |
| [API reference](docs/api-reference.md) | Routes, permissions, response shapes |
| [Deployment](docs/deployment.md) | Running it, and the WAL trap that catches people |
| [Developer guide](docs/developer-guide.md) | Setup, tests, troubleshooting |
| [Open decisions](docs/open-decisions.md) | What is still undecided, and what the build corrected |

---

## Three things worth knowing before reading the code

**Datasette is the writer, and the core is a library it imports.** SQLite
permits one writer, and with multiple concurrent users that is a real constraint
rather than a theoretical one. There is one process: the plugin calls
`orpheus.api.handle()` on Datasette's own write thread, so Datasette's write
queue serialises every change. The invariant is *nothing writes except through
`orpheus` core functions* — no SQL is written in the plugin. An advisory lock
stops a second process (the CLI, a script) opening the same store for writing
while the server holds it.

**Do not serve the store with `--immutable`.** It is the obvious flag for a
database you think nothing writes to, and combined with WAL mode it silently
shows a site missing rows. [Why](docs/deployment.md#the-wal-and-immutable-mode-trap).

**Nothing is destructively overwritten.** Corrections insert into `edit_history`
and update the row's status; rejected rows are excluded, never deleted. That is
both the audit story and the only way to measure whether extraction is improving.

---

## Status

389 tests:

```bash
pip install -e '.[dev]'
python3 -m pytest
```

They call the core directly, which cannot catch what only goes wrong with a real
server in the middle. `tests/e2e/browser_loop.sh` drives the whole loop over
HTTP against a live Datasette — multipart limits, CSRF, upload, extraction,
grounding, confirm/amend/reject, rollback — and checks the store agrees with
what the browser was told.

The core has no third-party dependencies. Every extraction engine, the PDF
backends and OCR are optional installs, and the code says which one is missing
when you reach for it. What has **not** been done is the thing Phase 1 exists
for: no real model has run against a real corpus, so the question of whether
extraction is good enough remains open. See
[open decisions](docs/open-decisions.md).
