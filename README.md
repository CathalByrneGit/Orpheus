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
  → read          a passage at a time, offering what seems worth recording
  → graph         relations between pages, with every source behind each one
  → export        the wiki as a portable markdown bundle
```

Every fact carries `source` (`ai_local` / `ai_cloud` / `human`), a `confidence`
from a five-level rubric, a review `status`, and a row in an append-only audit
trail.

Because corrections preserve the machine's value beside the human's, the store
answers the question Phase 1 actually turns on: **is extraction good enough to
build on yet?** `orpheus report` computes it — accuracy by confidence level,
whether the rubric ranks reliability at all, which rule concepts over-fire, and
which fields people keep fixing.

`confirm / amend / reject` all resolve towards one answer, which is right for
grading an extraction and wrong for a corpus, because a corpus is full of
documents that disagree and are both correct. So there is a fourth verb.
`orpheus tension` records a conflict two sources really are in — cited on both
sides, and **accepted** is a place a reviewer can stop. `orpheus lint` hunts for
the ones nobody recorded, along with the other ways the store can mislead a
reader; it reports located rows, never general observations, and it will not
give a clean bill of health it has not earned.

`orpheus read <id> --page N` — or the reading page in the browser — goes through
a document a passage at a time, offering what it seems to hold. **Nothing it
offers is in the store until you say so.** That is not politeness: proposals
nobody asked for, landed as unconfirmed instances, would pour into the number
extraction quality is measured by. Accepting writes through the same path a
batch pass uses, carrying the page, the excerpt and the span; dismissing is kept,
because it is the only evidence there is about whether the suggestions are worth
reading.

The mirror case is agreement, and it is counted in **distinct wordings across
distinct documents** rather than in rows. Six call-off contracts carrying one
framework's boilerplate is one source wearing six hats;
`orpheus corroboration` says so instead of reporting six agreeing sources, and
changes no confidence value doing it.

`orpheus questions` asks what the shape is worth looking at — two suppliers
connected only through one shared subcontractor, a party recorded on two sides
of one agreement, a chain of subcontracts that comes back round. **None of it is
a finding.** A shared subcontractor is usually a small market; what the corpus
can honestly say is that two parties are closer than they look, here is the
chain, and here is how much of it anybody has checked. Chains somebody has
confirmed sort first, because one built from unreviewed guesses is a reason to
check the extraction rather than to act.

`orpheus graph topology` reads the corpus as a network — islands, the pages
holding it together, clusters that never touch. It leads with how much of the
corpus reached the graph, because a sparse-looking network over 30% coverage
means a half-built wiki rather than a thin corpus, and no structural number
tells those apart. `orpheus graph path A --to B` answers *how are these two
connected*, and names the weakest hop in the chain: one running through a
relation nobody has checked is not the same finding as one vouched for end to
end.

Structure that must hold up is deterministic and needs nothing installed.
`pip install 'orpheus[graph]'` swaps label propagation for Louvain and adds
betweenness centrality; without it both degrade and say which ran.

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
| [Reading with the machine](docs/reading-companion.md) | A passage at a time, and why a suggestion is not an extraction |
| [Conflicts and lint](docs/conflicts-and-lint.md) | The fourth review verb, the adversarial pass, and the markdown export |
| [Network and corroboration](docs/network-and-corroboration.md) | The relation graph, counting agreement honestly, and what a budget is denominated in |
| [Questions the corpus raises](docs/questions.md) | Where the shape is worth asking about, and why none of it is a finding |
| [Provenance and amendment](docs/provenance-and-amendment.md) | How a machine guess becomes a checked fact |
| [Extraction engines](docs/extraction-engines.md) | Four ways to run the model pass, and when each is right |
| [API reference](docs/api-reference.md) | Routes, permissions, response shapes |
| [Deployment](docs/deployment.md) | Running it, and the WAL trap that catches people |
| [Developer guide](docs/developer-guide.md) | Setup, tests, troubleshooting |
| [Open decisions](docs/open-decisions.md) | What is still undecided, and what the build corrected |

An agent working with a store should read `.claude/skills/orpheus/SKILL.md`
first. Its load-bearing rule is *never assert something the store does not hold,
and never assert it more firmly than the store does* — the four review states
are not interchangeable, and a summary that flattens them is the failure mode
this whole design exists to prevent.

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

**Disagreement is a finding, not a defect.** Two confirmed extractions that
contradict each other are usually both right about the moment each document was
written. Rendered in the same voice one under the other, they read as though
they agree — so a verified conflict gets its own record, its own state, and the
top of the page. [Why](docs/conflicts-and-lint.md).

---

## Status

563 tests:

```bash
pip install -e '.[dev]'
python3 -m pytest
```

They call the core directly, which cannot catch what only goes wrong with a real
server in the middle. `tests/e2e/browser_loop.sh` drives the whole loop over
HTTP against a live Datasette — multipart limits, CSRF, upload, extraction,
grounding, confirm/amend/reject, rollback, reading a passage and recording from
it, the lint page, the network page and the markdown export — and checks the store agrees with what the browser was told.

The core has no third-party dependencies. Every extraction engine, the PDF
backends and OCR are optional installs, and the code says which one is missing
when you reach for it. What has **not** been done is the thing Phase 1 exists
for: no real model has run against a real corpus, so the question of whether
extraction is good enough remains open. See
[open decisions](docs/open-decisions.md).
