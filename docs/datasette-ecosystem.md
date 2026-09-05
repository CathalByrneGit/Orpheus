← [Back to index](index.md)

# The Datasette ecosystem, and what is worth taking from it

Three passes, recorded in order: four plugins researched when the direction was
first set, a search for anything that already built the wiki UI, and then the
directory itself. Every verdict below came from installing the thing and making
a request; two of them contradicted what the metadata implied.

## First pass: four plugins, when the direction was set

Researched because the direction is Datasette-first and the Datasette team keeps
building things Orpheus would otherwise build badly.

### The headline question

Whether **`datasette-agent` removes the need for R and the Plumber API**. The
answer was no to the first half and yes to the second, but neither for the
reason it looked like.

The Plumber API is gone — not because a chat plugin replaced it, but because the
port made Datasette the writer, so the API became a dispatch table imported
in-process rather than a service to call. R is gone too, on
[evidence unrelated to any plugin](open-decisions.md#r-stack-vs-a-python-rebuild).
What the research below actually established is what an agent must *not* be
allowed to do, and that conclusion outlived both changes.

| Plugin | What it is | Verdict here |
|---|---|---|
| `datasette-agent` | Chat UI + tool-calling loop over `datasette-llm` | **Adopt as a client, never as a writer.** Its tool hook is the right seam; its `execute_write_sql` is the wrong one |
| `datasette-accounts` | Username/password accounts in the internal DB | **Adopt.** Settles an open decision, deletes ~half of `auth.py` |
| `datasette-paper` | Collaborative document editor | **Read it, don't adopt it.** The working reference for the hook Orpheus already emits SQL for |
| `datasette-apps` | Sandboxed HTML/JS apps over allow-listed queries | **Adopt for dashboards.** Wrong shape for anything that writes |

---

### `datasette-agent`

#### What it actually is

A chat interface at `/-/agent`, a background-task runner, and a tool-calling
loop built on the Claude Agent SDK and `datasette-llm`. Its built-in tools are
`list_databases_and_tables`, `describe_table`, `sql_query` (read-only),
`execute_write_sql`, `save_query`, and two for spawning and checking background
agents. 116 commits, actively developed, beta.

It is an **orchestration and conversation layer**. That is worth being precise
about, because it is the whole of the answer to the R question.

#### Why it cannot replace the core

The core is about 6,000 lines. The bulk of it, by what it does:

| Concern | Would `datasette-agent` cover it? |
|---|---|
| Concept evaluation, versioning, scores (`concepts.py`) | No |
| Store, migrations, WAL, writer lock (`store.py`) | No |
| Persistence, provenance, rubric snapping (`extract.py`) | No |
| Grounding: locating every excerpt in the source (`align.py`) | No |
| Bundle load, validate, DDL (`bundle.py`) | No |
| Quality measurement (`quality.py`) | No |
| Amendment model, edit history (`review.py`, `audit.py`) | No |
| Corpus analysis (`analysis.py`) | No |
| Auth, per-document permissions (`auth.py`) | Partly — but that is `datasette-accounts` |
| Model provider layer + cloud gate (`llm.py`) | **Yes** |
| Deterministic date/money pass (`deterministic.py`) | No |
| Ingest, OCR (`ingest.py`, `textract.py`) | No |

The agent does not extract entities against an ontology, snap confidences,
locate a quotation in the source it claims to come from, maintain a review
vocabulary, track evaluation staleness, or measure whether extraction is any
good. It calls models and runs SQL. Replacing the core with it would mean
writing all of the above again *and* handing the writes to a chat prompt.

**The port makes adopting it easier, not harder.** `register_agent_tools` wants
typed operations, and the API is now a dispatch table in the same process — so
each tool is one `api.handle()` call with the actor already resolved. The
obstacles that used to stand in the way (a second process, a token, a base URL)
are gone.

#### The part it would genuinely replace

`orpheus/llm.py` — the provider layer. `datasette-llm` does the same job with a real
plugin ecosystem behind it: keys via `datasette-secrets` or `llm`'s keystore,
per-model configuration, one interface for every provider `llm` supports.

This is a real saving, and it is also the one place adoption is dangerous, for
reasons below.

#### Why `execute_write_sql` must never touch the store

The agent can write. It analyses each statement, shows the user the SQL, and
asks for approval in chat. For a general Datasette that is a good design. For
Orpheus it dissolves the thing Phase 1 is:

- **Provenance** is written by `insert_instance()`, not by whoever writes the row.
  An agent-authored `INSERT` produces an instance with no record of where it came
  from — indistinguishable, afterwards, from one that had provenance and lost it.
- **The confidence rubric** is snapped downward at the persistence boundary. Raw
  SQL writes whatever number is in the string.
- **The four review states** are a vocabulary, not a convention. Nothing stops an
  agent writing `status = 'ok'`.
- **`edit_history`** is append-only *because every write path appends to it*.
  A write that skips it is a hole no storage guarantee closes.
- **The cloud gate** — org policy plus per-request opt-in plus the `llm_calls`
  audit — is enforced in the API. An agent calling a model directly means a
  document can reach a cloud provider with no record that it did.
- **The single-writer lock** refuses a second *Orpheus* writer. It cannot refuse
  a plugin opening its own connection, and the WAL reasoning stops holding the
  moment one does.

Chat approval does not fix this. It puts a human in front of the SQL, which is
the wrong altitude: the person is asked to approve a statement, not a fact, and
approving a hundred `INSERT`s is not review — review is what
[provenance and amendment](provenance-and-amendment.md) describes, one fact at a
time, with the machine's original kept beside the correction.

#### The shape that does work

`register_agent_tools` — the hook that lets a plugin add typed tools:

```python
@hookimpl
def register_agent_tools(datasette):
    return [
        AgentTool(
            name="orpheus_amend_instance",
            description="Correct one extracted fact, keeping the machine's original.",
            input_schema={...},
            fn=amend_handler,          # calls POST /instances/{id}/amend
            required_permission="orpheus-edit",
        ),
    ]
```

Registered this way, the model never sees `execute_write_sql` for the Orpheus
database (tools the actor lacks permission for are filtered out before the model
is asked), and every write it makes goes through the API — which applies
provenance, the rubric, the amendment history and permissions on the way in.

That is the same relationship the existing UI plugin already has: a thin client
over the API, verified not to open a SQLite connection. The agent becomes a
*second* client with a conversational interface, which is a strictly better
version of the reading companion than a bespoke pane.

#### What must be settled before adopting it

**The cloud gate is the blocker, and it is specific.** `datasette-llm` resolves
API keys from `datasette-secrets`, `llm`'s `keys.json`, or the environment, and
does not log calls — its README points at `llm_prompt_context` and
`llm_group_exit` as the hooks a plugin would use to add auditing. So adopting it
as-is bypasses `cloud_ai_policy`, the per-request `opt_in`, and the `llm_calls`
table in one step. Two routes:

1. Keep Orpheus's provider layer for anything touching document text, and let
   the agent use `datasette-llm` only for conversation about already-extracted
   rows. Simplest, and the gate stays where it is.
2. Implement the audit on `datasette-llm`'s hooks and move the policy check
   there. More work, and the gate then lives in Python while the policy lives in
   SQLite — worth it only if the core moves to Python anyway.

**Unverified:** whether a local tier is reachable. `llm`'s plugin ecosystem
includes `llm-ollama`, so it looks like it should be, but `datasette-llm`'s
documentation does not confirm it. Orpheus's design has the local tier always on
and the cloud tier opt-in; if `datasette-llm` cannot serve a local model, the
default posture inverts. **Test this before anything else** — it decides whether
the plugin is usable here at all.

---

### `datasette-accounts`

Username/password accounts stored in Datasette's internal database, with a web
admin UI, a CLI, and a JSON API. PBKDF2 hashing off the event loop, timing-safe
login, brute-force lockout, revocable server-side sessions, forced first-password
change, audit logging. 38 commits, marked experimental, needs Datasette 1.0a23+.

This settles the **identity provider** open decision, and the integration is
done. Orpheus's `actors` table already carried `idp` and `external_id` for
exactly this; `upsert_actor()` is the seam, and the plugin now calls it on every
request rather than reading a hand-written `actor_map`. `datasette-accounts`
emits stable actor `id` values and an `is_admin` flag, so an account created
upstream becomes an Orpheus actor on first sight, with the admin bit carried
across — no user list duplicated in YAML.

Verified against 1.0a38: it starts clean, registers `permission_resources_sql`
(the hook Orpheus generates SQL for), and a full upload-to-review loop attributes
correctly to the provisioned actor. Promoting and demoting upstream moves
`actors.is_admin` in step, which is what keeps `can()` and `permission_sql()`
from disagreeing.

**What it removes:** token minting, revocation and authentication —
`create_token()`, `revoke_token()`, `authenticate()`, and the `actor_tokens`
table. Roughly half of `auth.py`, and the more security-sensitive half.

The other thing it was going to fix has already gone. The R plugin held a
**shared token**, so every browser amendment was attributed to one identity —
its least honest feature. The Python plugin holds no token at all, because it
does not speak HTTP; it reads Datasette's actor directly.

**What it does not remove:** `can()`, the share grants, visibility, and
`permission_sql()`. Those are per-document rules over Orpheus's own tables;
`datasette-accounts` answers *who is this*, not *may they see this document*.

**Caveat:** it wants accounts in the **internal** database, which is a second
SQLite file. Datasette writes both now, so this no longer inverts anything —
which is one more thing the port simplified.

---

### `datasette-paper`

A collaborative document editor: ProseMirror front end, SQLite storage, SSE for
real-time collaboration, wiki-links, inline tags, task assignment. 218 commits,
Apache 2.0, and the most mature of the four.

Already named in [open decisions](open-decisions.md) as the model for
per-document sharing. Two concrete things came out of reading it properly.

**It is the working reference for `permission_resources_sql`.** Orpheus already
generates the SQL that hook needs, from `auth.permission_sql()`, and ships it as
a comment in the generated Datasette config because nothing consumes it yet.
`datasette-paper` consumes it. Its `_datasette_paper_share` table and three-level
visibility (`private`, `link-view`, `link-edit`) are the same design as Orpheus's
`document_shares` and `visibility` — so the gap between "Orpheus emits the rule"
and "Datasette enforces it row by row" is a small plugin, and there is a worked
example of it.

**Its schema is a quiet validation.** Four tables: document metadata, an
append-only step log, periodic snapshots, per-actor shares. That is structurally
what Orpheus does — `documents`, `edit_history`, instance rows as the materialised
current state, `document_shares` — arrived at independently for the same reason:
you cannot reconstruct who changed what unless the log is the source of truth and
the current row is a projection of it.

**Do not adopt it.** It edits prose collaboratively; Orpheus reviews extracted
facts. Different problem, and its ProseMirror step log has nothing to say about
typed amendments to typed properties. Read it for the hook and move on.

---

### `datasette-apps`

Stored HTML/JavaScript apps hosted inside Datasette, sandboxed in iframes, with
data access through injected `datasette.query()` and `datasette.storedQuery()`
helpers restricted by per-app allow-lists. Revisions tracked in an
`app_revisions` table. Strict CSP: no arbitrary external origins, no localhost,
no history API. 112 commits.

The natural home for **the quality report**. `quality_report()` answers the
question Phase 1 turns on — accuracy by confidence level, whether the rubric
ranks reliability, which rule concepts over-fire — and it currently answers it to
whoever runs `orpheus report` or calls `GET /quality`. The canned queries in the generated config are a
flat approximation. A stored app over allow-listed queries would give it a real
surface, and shipping a change to it is a form edit rather than a plugin release.

The `storedQuery()` allow-list matters: it means a dashboard can be handed a
fixed set of named queries and nothing else, which is the right blast radius for
something rendering review statistics.

**Not for anything that writes.** The documentation describes only query access
and says nothing about writes; treat write support as unconfirmed rather than
absent. Either way the sandbox is the wrong place for a write path that must
carry provenance — that belongs to the API, and the existing plugin already
reaches it.

---

## Second pass: when the wiki needed a UI

Before hand-rolling entity pages, the 319 `datasette-*` packages on PyPI were
checked for anything that already did the job. Four looked directly relevant.

### `datasette-rapidfuzz` — adopted

SQL functions for fuzzy string matching, via a `prepare_connection` hook.
Measured against the name pairs entity resolution actually gets wrong,
`token_sort_ratio` separates them cleanly:

| | |
|---|---|
| `Ernst & Young` / `Ernst and Young` | 85.7 — same |
| `Ardmore Digital Limited` / `Ardmore Digital Ltd` | 90.5 — same |
| **threshold 80** | |
| `Kestrel Medical Group` / `Kestrel Medical Ltd` | 75.0 — different |
| `CRH Group` / `CRH plc` | 62.5 — different |

The core uses `rapidfuzz` directly as a library rather than the plugin, since it
must work without Datasette; the plugin is a bonus for ad-hoc SQL. See
[Entities](entities.md).

### `datasette-jellyfish` — read, not adopted

The same idea, and its headline function is the wrong one. **Jaro-Winkler scores
`Kestrel Medical Group` against `Kestrel Medical Ltd` at 0.921 — higher than it
scores several true matches** — so using it would recreate exactly the false
merge that stripping `group` as a suffix caused. A reminder that "fuzzy
matching" is not one thing.

### `datasette-reconcile` — right idea, unmaintained

Exposes a table as an [OpenRefine reconciliation
service](https://reconciliation-api.github.io/specs/latest/), the W3C-track
protocol that data-cleaning tools speak. `entities` maps onto it with **no code
at all** — four field names in config:

```yaml
datasette-reconcile:
  id_field: entity_id
  name_field: canonical_name
  description_field: description
  type_field: type_id
```

That is exactly the "repurposable for other projects" story: anyone with a messy
list of company names could reconcile it against the wiki using standard tooling.

It does not run. `AttributeError: 'Datasette' object has no attribute
'permission_allowed'` — the method became `ensure_permission` in Datasette 1.0.
Unlike `datasette-comments`, this is not a stale release in front of a live
repo: the current `main` still calls the old method, and the last commit is
February 2024. The protocol is worth implementing directly.

### `datasette-comments` — maintained, but unreleased

Comment threads on tables, rows and values, which is the *debate* gap: the store
records what was decided, not why.

The PyPI release (0.1.0, November 2023) fails the same way as reconcile, and it
fails **loudly and everywhere**: it injects a body script into every HTML page,
so with it installed *every* page in the install returned 500 — including tables
it had nothing to do with. A broken plugin here is not contained to its own
routes.

**But that is the release, not the project.** `main` has no `permission_allowed`
anywhere and requires `datasette>=1.0a21`. Installing from git gets past that
and then fails on `ValueError: Entrypoint src/content_script/index.tsx not found
in manifest` — the TypeScript front end is not built in a source checkout.

So: alive, 1.0-ready in git, needs either a release or a frontend build. Worth
revisiting rather than replacing, which is the opposite of the conclusion the
PyPI version alone would support. 941 downloads a month.

## Third pass: the directory itself

`datasette.io/plugins` is unreachable from this environment, but the site is
built from a public repo, so the same data came from
`simonw/datasette.io/plugin_repos.yml` (the curated list) and
`simonw/package-stats/stats.json` (downloads). **158 plugins are curated, against
319 `datasette-*` packages on PyPI** — so roughly half of what is published is
listed, and the directory is the maintained subset.

The categories that bear on this project, with 30-day downloads:

| Category | Notable | /mo |
|---|---|---|
| Enrichment | `datasette-enrichments` | 992 |
| AI | `datasette-extract` | 1,171 |
| UI | `datasette-write-ui` | 1,476 |
| Search | `datasette-search-all` | 1,704 |
| Collaboration | `datasette-comments` | 941 |
| SQL Functions | `datasette-jellyfish` | 308 |

### 1.0-readiness, checked at source

The releases lag the repos, so both were checked. `permission_allowed` in the
current `main` is the test — it is the method 1.0 renamed to `ensure_permission`:

| Plugin | `main` | Declares |
|---|---|---|
| `datasette-extract` | clean | `>=1.0a26` |
| `datasette-enrichments` | clean | `>=1.0a21` |
| `datasette-comments` | clean | `>=1.0a21` |
| `datasette-checkbox` | clean | `>=1.0a19` |
| `datasette-edit-schema` | clean | `>=1.0a21` |
| `datasette-search-all` | clean | `>=1.0a20` |
| `datasette-create-view` | clean | `>=1.0a21` |
| `datasette-query-assistant` | clean | `>=1.0a21` |
| **`datasette-write-ui`** | **uses it** | `>=1.0a21` |
| **`datasette-embeddings`** | **uses it** | unpinned |
| **`datasette-reconcile`** | **uses it** | unpinned, last commit Feb 2024 |

Worth noting that `datasette-write-ui` is both the most-downloaded UI plugin and
still on the old API in its own `main` while declaring `>=1.0a21`. A declared
floor is not evidence of compatibility.

### The closest existing tool: `datasette-extract`

Worth being precise about, because it is what someone would reasonably ask why
this project is not. It imports unstructured text and images into structured
tables: you name the columns and types in a form, pick a model through
`datasette-llm`, and rows appear.

| | `datasette-extract` | Orpheus |
|---|---|---|
| Schema | Column names and types, per run | A versioned ontology bundle with interfaces, codelists and an amendment queue |
| Grounding | The model's output *is* the row | Every excerpt located in the source; one that cannot be found is scored down, not stored as fact |
| Provenance | — | `source`, `confidence`, `alignment`, page, excerpt, character span |
| Review | Rows land as data | Four states, append-only history, nothing overwritten |
| Measurement | — | Accuracy by confidence, calibration verdict, fabrication rate |

That is not a criticism of it. It does a different job well, and 1,171 downloads
a month says the shape — extract into a Datasette table — is one people want.
What it confirms is the gap: it gets structured data out fast, and Orpheus
exists to say **whether the structured data is any good**, which is the whole of
Phase 1.

`datasette-llm`, which it depends on for model management and API keys, is a
real alternative to `orpheus/llm.py`'s configuration half. It is not an
alternative to the gate: the two-condition opt-in and the `llm_calls` audit are
policy, not plumbing.

### What this says about the ecosystem

Datasette 1.0 is required here — browser upload needs `request.form(files=True)`,
which 0.x lacks. 1.0 renamed the permission API, and the ecosystem is mid-
migration: the plugins that failed all failed on that one method, and the
releases lag their repos by more than a year in places.

Three conclusions worth carrying:

- **Judge the repo, not the release.** The PyPI version said `datasette-comments`
  was dead; `main` says it is 1.0-ready and waiting on a build step. The
  opposite mistake is just as easy: `datasette-write-ui` declares `>=1.0a21` and
  does not work.
- **Implement the reconciliation protocol directly.** `datasette-reconcile` is
  genuinely unmaintained — last commit February 2024, still on the old API — but
  the spec it implements is stable and small, and it is the strongest available
  answer to "how does this get reused elsewhere".
- **Install and run before judging.** Every verdict here came from an install
  and a request, and two of them contradicted what the metadata implied.

### What Datasette does give free

Checked rather than assumed: with the broken plugins removed, `entities` renders
as an ordinary Datasette table — sortable, searchable, faceted by `type_id` and
`status`, exportable as JSON and CSV, with a row page per entity — for nothing
but a config block. So the wiki **index** does not need to be built.

What Datasette cannot give is the page itself, because it is a projection
computed in Python, and the review actions, because they must go through core
functions rather than raw SQL. Those are the parts worth writing.

---

## What to do about it

Rewritten after the third pass, since the first version argued from a codebase
that no longer exists — it recommended making the quality report visible "to
someone who does not run R".

1. **Implement the reconciliation protocol.** The clearest answer to how this
   gets reused elsewhere, and `datasette-reconcile` will not provide it. Blocked
   on calibrating the similarity threshold, which is blocked on the corpus run.
   See [open decisions](open-decisions.md#reconciliation-how-this-gets-reused-elsewhere).
2. **`datasette-accounts`**, when Datasette is the front door for real users. It
   deletes real code and closes a named open decision. Check it at `main` first:
   the released versions across this ecosystem lag badly.
3. **The `permission_resources_sql` plugin**, using `datasette-paper` as the
   reference. Orpheus already generates the SQL; this is the smallest step from
   "documented rule" to "enforced rule".
4. **Watch `datasette-comments` for a release.** It is the debate gap, it is
   1.0-ready in git, and it needs a frontend build that a release would carry.
5. **Watch `datasette-enrichments` for a release.** The hook is adopted and the
   enrichment is written; what is pinned is git, because the released version
   500s against the Datasette this runs on.

Deliberately not on this list: anything that writes to the store. The rule from
the first pass survived all three — the agent, the write UI, the enrichment
runner are all clients, and every write goes through core functions.

---

## Fourth pass: bringing in a register

Prompted by the gap this project has already named: conflict-of-interest work
needs ownership and directorships, and [those live in registers Orpheus does not
read](open-decisions.md#still-out-of-scope-for-phase-1). Four candidates, each
installed and requested.

### First, the part that is not about a plugin

**A register is not a document.** Everything in the store is a fact read out of
one, carrying a page and an excerpt that `align.py` located. A row in a
companies register has no excerpt to locate and no page to point at, and giving
it one would be an invention.

There are two honest ways to hold one, and they lead to different systems:

| | A register is a document | A register is reference data |
|---|---|---|
| Rows become | Instances, with provenance "row 42" | Nothing. It stays its own table |
| Extraction quality | Now averages reading a PDF with reading a CSV | Still measures reading documents |
| Feeds | The wiki, the graph, the queue | `resolution_evidence()`, as corroboration |
| A wrong row | Is a fact in the corpus | Is evidence a person weighed and can discount |

**The second.** The first dissolves the thing the quality number measures: a
register import is trivially "correct", so mixing it in inflates accuracy with
work no model did. The second is also what the question actually needs — a
register is useful here because it *settles a merge*, and
[the resolution loop](entities.md#the-loop-an-agent-gathers-evidence-a-person-decides)
already takes evidence and weighs it by how rare a value is. A registered number
shared by two pages is exactly the decisive, rare value the corpus does not
currently have: only 2 of 74 companies state one.

### `datasette-upload-csvs` and `datasette-upload-dbs`

**Neither does the reviewed upload.** `upload-csvs` writes the table straight
from the file; there is no staging step and no chance to correct anything first.
`upload-dbs` validates that the upload *is* a SQLite database and moves it into
place atomically — file validity, not data review.

So the "user and agent fix it before it becomes a table" step is Orpheus's to
build. What is worth taking is the pattern rather than the code: upload to a
staging table, let a person and a model work over it, and promote it only when
somebody says so — which is the shape the reading companion already has, where
[a suggestion is not an extraction](reading-companion.md).

**Built**, on those lines: `orpheus register` stages a delimited file, reports
which column it read names from rather than assuming, lets rows be rejected with
a reason, and counts for nothing until an administrator promotes it. Two agent
tools help a person work through a staged one; neither can promote it. See
[the register](entities.md#a-register-when-the-documents-cannot-settle-it).

### `datasette-enrichments`

**The right shape, and the strongest candidate here.** `register_enrichments()`
returns `Enrichment` subclasses with `get_config_form` (a form the user fills
in — the "user decides" part), `initialize`, `enrich_batch(rows, pks, config,
job_id, actor_id)` and `finalize`. It runs over a table or a filtered selection,
in batches, with a job table, per-row progress, an error table, cancel and
pause, and cost accounting.

That is the batch half of nearly everything Orpheus does by CLI: re-run
alignment over these rows, propose entities for these mentions, compare these
candidate pairs. Built, with the progress UI and the failure handling that
Orpheus would otherwise write badly.

**Adopt the hook, never the write.** `enrich_batch` writes with raw
`db.execute_write`, which is `execute_write_sql` again by another name and fails
for [all the same reasons](#why-execute_write_sql-must-never-touch-the-store).
The difference is that Orpheus writes the enrichment class, so what goes inside
`enrich_batch` is `api.handle()`. The plugin supplies the loop, not the write.

**Built**, on exactly those terms: `plugins/orpheus_enrichments.py` reads a
selection of `document_pages` through `companion.read_passage()`. See
[reading a batch](reading-companion.md#reading-a-batch-of-pages).

Two things the runner does not do, found by running it. `default_max_errors` is
declared on the class and never read, so a job that cannot work logs one error
per row and finishes reporting success — this enrichment stops itself when a
whole batch fails identically. And an exception from `enrich_batch` is logged
against *every* row in the batch, so a single bad page would be recorded as five
failures and four pages nobody read; failures are caught per row instead.

One tension worth naming: it accounts for cost in `cost_100ths_cent`, and
Orpheus denominates its budget in
[characters](network-and-corroboration.md#what-a-budget-is-denominated-in)
precisely so the number does not go stale with a price list. An Orpheus
enrichment should keep counting characters and leave that column alone.

**And a correction to the third pass.** That table reads `datasette-enrichments
| clean | >=1.0a21`, checked against `main`. The *released* version is not:

```
0.5.1 (PyPI)   GET /-/enrich/store/entities   500
               views.py: await datasette.permission_allowed(...)
0.6a0 (main)   GET /-/enrich/store/entities   200  "Enrich data in entities"
               views.py: await datasette.allowed(...)
```

Both declare `datasette>=1.0a21`; the store runs 1.0a38. So the earlier note
that a declared floor is not evidence of compatibility has a mirror image — a
clean `main` is not evidence that what `pip install` gives you works. Adopt from
git, or wait for a release.

### `datasette-comments`

The [third-pass verdict](prior-art.md#datasette-comments) stands: the discussion
*around* a correction, which Orpheus has no answer for. One thing to add now
that it has been read — **comments are stored in Datasette's internal database**,
not in the store. So they sit outside `edit_history`, outside the export, and
outside every guarantee this project makes about writes. That is the right place
for them and it is also the reason a comment can never become an amendment: an
amendment is a typed change with the previous value preserved, and it lives
where the audit can see it.

### `dogsheep-beta`

**No.** It builds a `search_index` table by running configured SQL across other
tables, and refreshing it means running `dogsheep-beta index` again. Orpheus's
[search](developer-guide.md#searching-the-corpus) is `sqlite-utils` FTS kept
current by triggers.

Trading a live index for a copy that is correct until the next write is a
regression on the one property this project is most careful about — a derived
view that has gone stale without saying so. The staleness machinery exists
because that failure is worth machinery. It is also dormant: 65 commits, and
the unified-search job it does is one Orpheus does not yet have across
documents, pages and suggestions.

---

## Fifth pass: somewhere for the jobs that write

| Plugin | What it is | Verdict here |
|---|---|---|
| `datasette-cron` | Database-backed scheduled tasks, run in-process | **Adopt as an optional extra.** The only place a scheduled *write* can run, because the single-writer lock refuses a second process. Cloud passes are refused outright rather than scheduled |

### `datasette-cron`

**The gap it fills is not "scheduled jobs would be nice".** It is that a
scheduled task which writes has no good home. The shipped container is one
process on `python:3.11-slim` with no cron daemon in it; and where a crontab
exists, `orpheus wiki propose` opens the store as an unsupervised second writing
process that applies migrations under a live server and contends with it for the
write lock.

One correction to the first assessment, which asserted that the advisory lock
refuses that outright. It does not: `Store(mode="write")` takes the lock, and
Datasette never does, because `Store.adopt()` borrows a connection it already
opened. Checked cross-process both ways — a CLI writer *is* refused while
another CLI writer holds the lock, and is *not* refused while Datasette is
serving. The argument survives the correction and is sharper for it: what stands
between a crontab and the corpus is not a guard, it is nothing.

The plugin runs in-process, so its handler reaches `execute_write_fn` — the same
seam `plugins/orpheus_datasette.py` already routes every write through. Its task
and run tables live in Datasette's **internal** database, so nothing pollutes
the Orpheus schema or appears as corpus data.

Four cautions were raised before any code was written, and each is answered in
the build rather than left as a note. [Work on a clock](scheduled-tasks.md) is
the full account; in short:

| Caution | What was done |
|---|---|
| **The cloud gate.** The per-request opt-in exists because sending a document to a third party is a decision taken each time. A clock cannot take it | `llm.no_cloud()` refuses any cloud call inside a scheduled run, ahead of both of the gate's own conditions. Enforcement rather than convention: a task added later that reaches for a model is refused, not reviewed |
| **Who is the actor?** Every write carries one, and a schedule has no person | A machine actor under `idp = 'orpheus-scheduled'`, never an administrator, and `auth.create_token` refuses to mint it a credential. So `orpheus report` can still tell human review from work a robot did |
| **The scheduler starts on the first HTTP request** — a Datasette with no traffic runs no tasks | Their design, and not something this side can work around. The container turns out to solve it already: `deploy/Dockerfile` has carried a 30-second `HEALTHCHECK` against `/-/orpheus/api/health` since before this existed. Said in [Deployment](deployment.md) for the deployments that do not |
| **It is `0.0.1a2`** | An optional `[cron]` extra alongside `[agent]`. Nothing registers when it is absent, and `orpheus scheduled run` runs every task by hand either way |

One claim from the first assessment did not survive the build, and is corrected
here rather than quietly dropped: **the full-text index does not go stale as
documents arrive.** The FTS triggers keep it in step, measured on a real store
rather than assumed. What the `search-index` task actually closes is a
deployment where the index was never built at all — a reconcile, not a rebuild.

Execution is **at-least-once**, which all four tasks tolerate: two are
idempotent by construction, one writes nothing, and `wiki-propose` attaches a
repeated group to the page that already exists rather than minting a second one.

---

[← Back to index](index.md)
