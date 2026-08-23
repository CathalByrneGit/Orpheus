← [Back to index](index.md)

# Developer guide

---

## Prerequisites

The core is standard library only. Everything below is a capability you opt
into, and the code says which install is missing when you reach for one you did
not do.

| Requirement | Why | Hard requirement? |
|---|---|---|
| Python ≥ 3.10 | The package | Yes |
| — | The core imports nothing outside the standard library | — |
| `datasette` ≥ 1.0a32 | The browser UI, and the writer in a deployment | For serving |
| `pytest` | Tests | For development |
| `jsonschema` | Validating a bundle against its two schemas | For bundle work |
| `pdftotext` (poppler) **or** `pdfminer.six` | PDF text | For PDFs |
| `docling` | Layout-aware parsing: reading order, tables | Optional, heavy |
| `tesseract` + `pytesseract` | OCR | For scanned documents |
| One of `langextract` / `gliner2` / `llm` | The model pass | Unless you register your own populator |

```bash
pip install -e '.[dev]'          # everything the test suite needs
pip install -e '.[server,pdf]'   # a working deployment, no model
```

Datasette 1.0 or newer specifically: browser file upload needs
`request.form(files=True)`, which 0.x does not have.

**The package imports and its tests pass with no engine installed.** Tests that
need one drive a substitute populator through the same interface. That is
deliberate — the engine choice is a setting, not an architecture. See
[Extraction engines](extraction-engines.md).

---

## Running the tests

```bash
python3 -m pytest
```

307 tests, no skips with the `dev` extra installed.

### How the tests are built

Every test runs against a **real SQLite store** with the real bundle and real
migrations. Only one thing is routinely doubled — the model — because it is the
only part that needs a network or a GPU:

| Doubled | Helper |
|---|---|
| The population engine | `set_populator(fn)` in `orpheus.population` |

`tests/test_domain_neutrality.py` is the one to keep working. It builds a
planning-application bundle — a domain sharing nothing with contracts — and runs
the pipeline against it. If it breaks, the claim that the domain lives in the
bundle has stopped being true, and the break will be somewhere the code assumed
a contract-bundle name.

Everything else — WAL, transactions, the writer lock, permission rules, concept
SQL evaluation, the audit trail — is exercised as it actually runs. Where a real
service can stand in for a fake it does: `test_engines.py` runs the `chat`
engine against an HTTP server it starts itself, and the `llm` engine against
`llm-echo`.

An autouse fixture clears the populator on teardown. One left installed by a
test would silently serve the next.

### The loop no unit test can check

```bash
tests/e2e/browser_loop.sh [port]
```

It starts a real Datasette, signs in, uploads the PDF fixture through the
multipart form, confirms/amends/rejects through the review form, and checks the
store agrees with what the browser was told. Four things only break with
Datasette in the middle, and all four have broken this plugin at least once: its
multipart parser and limits, its CSRF token, its write queue, and the
transaction that queue opens around every task.

### Adding a test

```python
def test_a_rejected_counterparty_drops_out_of_the_corpus_match(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    document_id = ingest(store, PDF, actor_id="act_test",
                         storage_root=tmp_path / "storage")["document_id"]
    set_populator(populator([COMPANY]))
    extract(store, document_id, tier="local", actor_id="act_test")
    ...
```

The `store` fixture in `conftest.py` builds a fresh store per test and closes it
after, releasing the writer lock.

---

## Changing things

### Changing the ontology

Edit `orpheus/bundles/contract-core-<version>.json` and bump `bundleVersion` in
the same edit. `bundle.register()` applies the new schema; existing tables gain
new columns and keep their data.

Validate before committing — the schema half is skipped silently if `jsonschema`
is not installed, so ask for it explicitly:

```bash
orpheus bundle --strict orpheus/bundles/contract-core-0.2.0.json
```

The bundle is an
[ontologySpecR](https://github.com/CathalByrneGit/ontologySpecR) bundle, and it
is validated against that project's schema unmodified as well as against
Orpheus's own. Orpheus-specific concerns live under `extensions.orpheus`, which
is where the spec puts vendor extensions — so the same file is readable by tools
that know nothing about Orpheus.

### Changing the permission model

Edit `auth.can()` **and** `auth.permission_sql()` together. The test
*"the Datasette permission SQL agrees with can() for every actor"* fails if they
diverge. Then regenerate the Datasette files:

```bash
orpheus --db data/orpheus.sqlite config
```

### Adding a migration

Append to `MIGRATIONS` in `orpheus/schema.py` with the next `version`.
Migrations are applied in order and recorded in `schema_migrations`; re-running
is a no-op.

Never edit an applied migration — add a new one.

### Adding a route

Decorate a function in `orpheus/api.py`:

```python
@route("POST", r"/documents/(?P<document_id>[^/]+)/thing", permission="edit")
def post_thing(store, document_id, actor, body, **_):
    return do_the_thing(store, document_id, actor_id=_actor_id(actor))
```

The `permission=` argument does the check before the handler runs, using the
`document_id` group. Raise `OrpheusError` (or `NotFound` / `PermissionDenied`)
with a message written for a person; `_run()` maps it to a status code and
passes the message through unchanged.

The route is then reachable three ways with no further work: from the plugin's
pages, over HTTP at `/-/orpheus/api/...`, and from a script calling
`api.handle()` directly.

### Adding an extraction engine

Write a function with the shared signature and register it in
`orpheus/engines.py`:

```python
@engine("my_engine")
def my_engine_extract(*, store, document, bundle, text, tier, opt_in, actor_id):
    ...
    return {"extractions": [...]}
```

It must call `llm.assert_cloud_allowed()` before any cloud call and
`llm.record_llm_call()` on both success and failure. Returned excerpts are
located in the source text by `align.py` and scored on match quality — an engine
cannot assert its own grounding.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `The single-writer lock … is held by pid N` | The server (or another command) is running | Stop it; the CLI and a live Datasette cannot both write |
| `A stale writer lock … remains` | A writer crashed | Confirm nothing is running, then `--force-lock` |
| `This connection is read-only` | A write on a `mode="read"` store | Open it for writing, or route through the API |
| `cannot start a transaction within a transaction` | A caller drove a connection Datasette already had a transaction open on | Use `Store.adopt(conn, owns_transaction=False)` |
| Datasette shows no rows | Served with `--immutable` against a live WAL store | Drop the flag — [Deployment](deployment.md#the-wal-and-immutable-mode-trap) |
| Datasette has no canned queries | Filename stem does not match the config's database key | Name the file `orpheus.sqlite` |
| Datasette dies at startup with `KeyError: 'sql'` | A canned query ended up in `--metadata` | Queries belong in `--config`; regenerate with `orpheus config` |
| `no text to extract from` | Every page failed OCR | Check `document_pages.text_source`; configure an OCR backend |
| `No PDF text backend is available` | Neither `pdftotext` nor `pdfminer.six` | Install one |
| `Unknown extraction engine` | A typo, or the engine is not installed | The message lists the ones that are |
| `Cloud processing is disabled` | `cloud_ai_policy` is `disabled` | An admin sets it via `POST /admin/settings` |
| `needs an explicit per-request opt-in` | Policy allows cloud, request did not ask | Send `cloud_opt_in: true` |
| `The local tier has already run` | Re-running a **succeeded** extraction | Pass `force: true` to supersede the earlier results. A `partial` run needs no force |
| Run recorded `partial`, findings present | The model pass failed; the deterministic pass did not | Read `extraction_runs.error`. Re-run when the model is available; findings are matched on raw text and page, so nothing duplicates |
| `Nothing was changed` on an amendment | Every submitted value already matched | Confirm instead — that is what agreeing with the machine is called |
| `… is not a declared property` | Amending a property the bundle lacks | Accept the schema amendment first |
| `Model did not return usable JSON` | A small local model wandered | Check the raw reply in the error; try a larger model |

---

## Project structure

```
orpheus/
├── orpheus/
│   ├── rubric.py            # the two vocabularies: confidence and review status
│   ├── utils.py             # ids, JSON, naive keys, the error types
│   ├── store.py             # connect, WAL, the writer lock, transactions, adopt()
│   ├── schema.py            # MIGRATIONS
│   ├── bundle.py            # load/validate/register the bundle; generate its DDL
│   ├── audit.py             # edit_history and llm_calls
│   ├── textract.py          # PDF/docx/OCR backends
│   ├── ingest.py            # step 1
│   ├── llm.py               # model config, the cloud gate, the call audit
│   ├── classify.py          # step 3
│   ├── deterministic.py     # step 4, pattern half
│   ├── align.py             # locating an excerpt in the source; grounding
│   ├── engines.py           # the engine registry: gliner2 / langextract / llm / chat
│   ├── population.py        # step 5, engine-neutral
│   ├── extract.py           # steps 4-5, persistence, supersede-on-rerun
│   ├── review.py            # steps 6/8, schema amendment queue, staleness
│   ├── concepts.py          # step 7, versioned rule concepts and scores
│   ├── analysis.py          # step 9, corpus escalation
│   ├── quality.py           # the report Phase 1 exists to produce
│   ├── benchmark.py         # CUAD scoring
│   ├── auth.py              # actors, tokens, permissions
│   ├── api.py               # the dispatch table
│   ├── datasette_config.py  # generates metadata.yml and datasette.yml
│   ├── cli.py               # the command line
│   ├── bundles/             # contract-core-0.2.0.json
│   └── schemas/             # the two JSON Schemas a bundle is checked against
├── plugins/orpheus_datasette.py   # the UI, and the API mounted in-process
├── templates/               # the two pages
├── config/                  # generated: metadata.yml + datasette.yml
├── tests/
│   ├── e2e/browser_loop.sh  # the loop, over HTTP, against a real Datasette
│   └── fixtures/            # a real two-page PDF and the generator that built it
└── docs/
```

### Where to start reading

`orpheus/extract.py` is the centre of the system: it is where a model's output
becomes rows with provenance, and where the rubric, the amendment queue and the
audit trail all meet. `orpheus/align.py` is the shortest file worth reading
first — it is why grounding is computed rather than trusted, which is the single
decision the quality report depends on.

---

[← Back to index](index.md) | [Next: Open decisions →](open-decisions.md)
