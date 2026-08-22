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
build on yet?** `orph_quality_report()` computes it — accuracy by confidence
level, whether the rubric ranks reliability at all, which rule concepts
over-fire, and which fields people keep fixing.

---

## Documentation

Start at **[docs/index.md](docs/index.md)**.

| Page | What it covers |
|---|---|
| [Data model](docs/data-model.md) | Tables, the ontology bundle, the confidence rubric |
| [Pipeline walkthrough](docs/pipeline-walkthrough.md) | The nine steps, with the function that runs each |
| [Provenance and amendment](docs/provenance-and-amendment.md) | How a machine guess becomes a checked fact |
| [API reference](docs/api-reference.md) | Endpoints, permissions, response shapes |
| [Deployment](docs/deployment.md) | Running it, and the WAL trap that catches people |
| [Developer guide](docs/developer-guide.md) | Setup, tests, troubleshooting |
| [Open decisions](docs/open-decisions.md) | What is still undecided, and what the build corrected |

---

## Quick start

```bash
R CMD INSTALL .
```

```r
library(orpheus)

con   <- orph_init_store("data/orpheus.sqlite")
admin <- orph_create_actor(con, "Ada", is_admin = TRUE)

doc <- orph_ingest(con, "contract.pdf", actor_id = admin)$document_id
orph_classify(con, doc, actor_id = admin)
orph_extract(con, doc, tier = "local", actor_id = admin)
orph_document_instances(con, doc)
```

Serving it:

```bash
Rscript inst/plumber/plumb.R                                    # writer, :8000

ORPHEUS_API_TOKEN=$TOKEN datasette serve data/orpheus.sqlite \
  --metadata inst/datasette/metadata.yml \
  --config   inst/datasette/datasette.yml \
  --plugins-dir plugins --template-dir templates --port 8001    # UI + reader, :8001
```

Or `cd deploy && docker compose up -d`, which runs both plus Ollama.

The Datasette plugin adds an upload page and per-row confirm/amend/reject. It is
a thin client over the API — it never opens a SQLite connection and never calls
a model, so the single-writer guarantee and the cloud opt-in gate both still
hold when a person is driving. See [deployment](docs/deployment.md#the-datasette-ui-plugin).

---

## Three things worth knowing before reading the code

**The Plumber API is the only writer.** SQLite permits one writer, and with
multiple concurrent users that is a real constraint rather than a theoretical
one. It is enforced by an advisory lock that refuses a second writer at startup,
and by read connections that reject writes before SQLite sees them.

**Do not serve the store with `--immutable`.** It is the obvious flag for a
database Datasette never writes to, and combined with WAL mode it silently shows
an empty site. [Why](docs/deployment.md#the-wal-and-immutable-mode-trap).

**Nothing is destructively overwritten.** Corrections insert into `edit_history`
and update the row's status; rejected rows are excluded, never deleted. That is
both the audit story and the only way to measure whether extraction is improving.

---

## Status

687 tests, no skips with `conceptR` installed:

```bash
Rscript -e 'testthat::test_dir("tests/testthat")'
```

The extraction engine (`ontologyDiscoverR`) sits behind a single adapter, and the
suite passes without it installed by driving a substitute engine through the same
interface. Whether to keep the R stack or rebuild its ideas in Python is
[still open](docs/open-decisions.md#r-stack-vs-a-python-rebuild) — that isolation
is what keeps the decision cheap.
