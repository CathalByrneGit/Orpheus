← [Back to index](index.md)

# Developer guide

---

## Prerequisites

| Requirement | Why | Hard requirement? |
|---|---|---|
| R ≥ 4.1 | The package | Yes |
| `DBI`, `RSQLite`, `jsonlite`, `digest`, `rlang`, `cli` | Imports | Yes |
| `plumber` | Serving the API | For the API only |
| `testthat`, `withr` | Tests | For development |
| `ontologyDiscoverR` | The extraction engine | For real extraction |
| `conceptR` | Rule concepts (step 7) | For step 7 |
| `objectSetsR` | Corpus queries (step 9) | No — SQL fallback exists |
| `ellmer` | Model providers | Unless you register your own |
| `pdftools` **or** `pdftotext` | PDF text | For PDFs |
| `tesseract` (R package or binary) | OCR | For scanned documents |

```r
install.packages(c("DBI", "RSQLite", "jsonlite", "digest", "rlang", "cli",
                   "plumber", "testthat", "withr", "pdftools", "ellmer"))
remotes::install_github(c(
  "CathalByrneGit/ontologyDiscoverR",
  "CathalByrneGit/conceptR",
  "CathalByrneGit/objectSetsR"
))
R CMD INSTALL .
```

The package **loads and its tests pass without any of the GitHub packages
installed** — they are `Suggests`, gated at their call sites with actionable
errors. That is deliberate: see [Open decisions](open-decisions.md#r-stack-vs-a-python-rebuild).

---

## Running the tests

```bash
Rscript -e 'testthat::test_dir("tests/testthat")'
```

442 tests, no skips on a machine with `conceptR` installed. Tests that need it
call `skip_if_no_conceptr()`.

### How the tests are built

Every test runs against a **real SQLite store** with the real bundle and real
migrations. Only two things are doubled, because they are the two that need a
network and a GPU:

| Doubled | Helper |
|---|---|
| The model | `fake_llm(json)` + `orph_set_llm_provider()` |
| The population engine | `fake_populator(...)` + `orph_set_populator()` |

Everything else — WAL, transactions, permission rules, `conceptR`'s SQL
evaluation, the audit trail — is exercised as it actually runs.

`new_test_store()` clears both providers on teardown. A fake left installed by
one test would silently serve the next.

### Adding a test

```r
test_that("a rejected counterparty drops out of the corpus match", {
  con  <- new_test_store()
  root <- test_storage_root()
  seed_actors(con)
  use_fakes()

  doc <- seed_document(con, root)
  # ...
})
```

`new_test_store()` and `test_storage_root()` register their own cleanup via
`withr::defer()`. Do not add `on.exit()` for them.

---

## Changing things

### Changing the ontology

Edit `data-raw/make_bundle.R`, then:

```bash
Rscript data-raw/make_bundle.R      # regenerates inst/bundles/contract-core-0.1.0.json
```

Bump `version` in the same edit. `orph_register_bundle()` applies the new schema;
existing tables gain new columns and keep their data.

Do not hand-edit the JSON. The generator exists so the seed bundle is
reproducible, and it is also where the reasoning about the duplicated field names
lives.

### Changing the permission model

Edit `orph_can()` **and** `orph_permission_sql()` together. The test
*"the Datasette permission SQL agrees with orph_can for every actor"* fails if
they diverge. Then regenerate the Datasette metadata:

```r
orph_write_datasette_metadata()
```

### Adding a migration

Append to the list in `orph_migrations()` with the next `version`. Migrations are
applied in order and recorded in `schema_migrations`; re-running is a no-op.

Never edit an applied migration — add a new one.

### Adding an endpoint

Use the `GET()` / `POST()` helpers inside `orph_api()`, not `plumber::pr_get()`
directly. They attach the unboxed JSON serializer. Setting a router default does
not work: `pr_get()` binds a serializer to each endpoint as it is registered, so
by the time a router default is set every route already has one.

Every handler follows the same shape:

```r
pr <- POST(pr, "/documents/<id>/thing", function(req, res, id) {
  actor <- require_actor(req, res)
  if (is.null(actor)) return(json_error(res, 401L, "Authentication required."))
  guard(res, {
    orph_require(con, actor, id, "edit")
    do_the_thing(con, id, actor$actor_id)
  })
})
```

`guard()` maps `orph_forbidden` conditions to 403 and everything else to 400.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `The single-writer lock … is held by pid N` | Another writer is running | Stop it, or use the API instead of a second connection |
| `A stale writer lock … remains` | A writer crashed | Confirm nothing is running, then `ORPHEUS_FORCE_LOCK=1` |
| `This connection is read-only` | A write attempted on a `mode = "read"` connection | Route it through the API |
| Datasette shows no rows | Served with `--immutable` against a live WAL store | Drop the flag — [Deployment](deployment.md#the-wal-and-immutable-mode-trap) |
| Datasette has no canned queries | Filename stem does not match the metadata database key | Name the file `orpheus.sqlite` |
| `no text to extract from` | Every page failed OCR | Check `document_pages.text_source`; configure an OCR provider |
| `No PDF text backend is available` | Neither `pdftools` nor `pdftotext` | Install one |
| `ontologyDiscoverR is not installed` | The engine is missing | Install it, or register an engine with `orph_set_populator()` |
| `Cloud processing is disabled` | `cloud_ai_policy` is `disabled` | An admin sets it via `POST /admin/settings` |
| `needs an explicit per-request opt-in` | Policy allows cloud, request did not ask | Send `cloud_opt_in: true` |
| `The local tier has already run` | Re-running an extraction | Pass `force: true` to supersede the earlier results |
| A first extraction fails, then works after installing the engine | Expected — the deterministic pass commits before the model pass, so its findings survive the failure | Just retry. Findings are matched on raw text and page, so the retry will not duplicate them |
| `… is not a declared property` | Amending a property the bundle lacks | Accept the schema amendment first |
| `Model did not return usable JSON` | A small local model wandered | Check the raw reply in the error; try a larger model |
| Euro amounts never extracted | Symbol matching broken by a locale mismatch | Should not happen — symbols are byte-matched. If it recurs, check `orph_find_amounts()` against `orpheus:::utf8_symbol(0xe2, 0x82, 0xac)` |
| `cannot start a transaction within a transaction` | Should not happen — `with_tx()` is re-entrant | Report it; a code path is bypassing `with_tx()` |

---

## Project structure

```
orpheus/
├── R/
│   ├── utils.R              # rubric, ids, JSON, naive keys, package state
│   ├── db.R                 # connect, WAL, single-writer lock, migrations, checkpoint
│   ├── bundle.R             # load/validate/register the bundle; generate its DDL
│   ├── audit.R              # edit_history and llm_calls
│   ├── ocr.R                # text extraction backends + the OCR provider registry
│   ├── ingest.R             # step 1
│   ├── llm.R                # provider registry, the cloud gate, excerpt selection
│   ├── classify.R           # step 3
│   ├── deterministic.R      # step 4, regex half
│   ├── ontology_stack.R     # the ontologyDiscoverR adapter — the only contact point
│   ├── extract.R            # steps 4-5, persistence, supersede-on-rerun
│   ├── amend.R              # steps 6/8, schema amendment queue, staleness
│   ├── concepts.R           # step 7, conceptR + narrative
│   ├── analysis.R           # step 9, corpus escalation
│   ├── auth.R               # actors, tokens, permissions
│   ├── api.R                # the Plumber router
│   └── datasette.R          # metadata generation, serve command
├── inst/
│   ├── bundles/             # contract-core-0.1.0.json
│   ├── plumber/plumb.R      # service entry point
│   └── datasette/metadata.yml
├── data-raw/make_bundle.R   # regenerates the bundle
├── tests/testthat/          # 442 tests
└── docs/
```

### Where to start reading

`R/extract.R` is the centre of the system: it is where a model's output becomes
rows with provenance, and where the rubric, the amendment queue and the audit
trail all meet. `R/ontology_stack.R` is the shortest file worth reading first —
it is the whole interface to the extraction engine, and the comment at the top
explains the two decisions that shaped it.

---

[← Back to index](index.md) | [Next: Open decisions →](open-decisions.md)
