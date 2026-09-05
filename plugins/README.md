# The Datasette plugins

Four files. One is the surface Orpheus is used through; the other three are
optional, and each registers nothing when the package it hooks into is absent.

| File | What it is | Needs |
|---|---|---|
| `orpheus_datasette.py` | The UI and the API: upload, review, the wiki, the map, the calendar, the ontology queue | — |
| `orpheus_agent.py` | Orpheus tools for the chat, so it answers from the store rather than from raw SQL | `orpheus[agent]` |
| `orpheus_enrichments.py` | The reading pass as a batch job over a selection | `orpheus[enrichments]` |
| `orpheus_cron.py` | Scheduled tasks — verify, search-index, wiki-propose, calendar-digest | `orpheus[cron]` |

**Datasette is the writer, and the core is a library it imports.** There is one
process. The plugin calls `orpheus.api.handle()` on Datasette's own write
thread, and the invariant every file here keeps is that **no SQL is written in
`plugins/`** — a direct `INSERT` would skip alignment, provenance, the audit
trail and the human/machine source split, and leave rows nothing downstream
could tell apart from reviewed ones.

None of them calls a model directly either. Doing so would bypass the cloud
opt-in gate, the organisation's policy and the `llm_calls` audit in one step.

## Running them

```bash
datasette serve data/orpheus.sqlite \
  --metadata config/metadata.yml \
  --config   config/datasette.yml \
  --plugins-dir plugins --template-dir templates --port 8001
```

`orpheus init` generates both config files and `orpheus serve` prints or runs
this command. Settings live in the `plugins` block of the generated
`datasette.yml`: which database holds the store, where ingest puts originals, an
upload size ceiling, and — for `orpheus-cron` — which tasks to schedule.

There is no API token and no base URL. Both belonged to the retired arrangement
where a separate service owned the only write connection; the core is now
imported in-process, so there is nothing to authenticate to.

Templates live in `../templates/`. Browser file upload needs **Datasette 1.0a32
or newer**; on an older server the page degrades to the server-side path field.

See [../docs/deployment.md](../docs/deployment.md#the-datasette-ui-plugin) for
how the single-writer property is verified, and
[../docs/scheduled-tasks.md](../docs/scheduled-tasks.md) for what a scheduled
run may and may not do.
