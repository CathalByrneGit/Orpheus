← [Back to index](index.md)

# Four Datasette plugins, and what each is worth here

`datasette-accounts`, `datasette-agent`, `datasette-apps`, `datasette-paper`.
Researched because the direction is Datasette-first and the Datasette team keeps
building things Orpheus would otherwise build badly.

The headline question was whether **`datasette-agent` removes the need for R and
the Plumber API**. The answer was no to the first half and yes to the second, but
neither for the reason it looked like.

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

## `datasette-agent`

### What it actually is

A chat interface at `/-/agent`, a background-task runner, and a tool-calling
loop built on the Claude Agent SDK and `datasette-llm`. Its built-in tools are
`list_databases_and_tables`, `describe_table`, `sql_query` (read-only),
`execute_write_sql`, `save_query`, and two for spawning and checking background
agents. 116 commits, actively developed, beta.

It is an **orchestration and conversation layer**. That is worth being precise
about, because it is the whole of the answer to the R question.

### Why it cannot replace the core

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

### The part it would genuinely replace

`orpheus/llm.py` — the provider layer. `datasette-llm` does the same job with a real
plugin ecosystem behind it: keys via `datasette-secrets` or `llm`'s keystore,
per-model configuration, one interface for every provider `llm` supports.

This is a real saving, and it is also the one place adoption is dangerous, for
reasons below.

### Why `execute_write_sql` must never touch the store

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

### The shape that does work

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

### What must be settled before adopting it

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

## `datasette-accounts`

Username/password accounts stored in Datasette's internal database, with a web
admin UI, a CLI, and a JSON API. PBKDF2 hashing off the event loop, timing-safe
login, brute-force lockout, revocable server-side sessions, forced first-password
change, audit logging. 38 commits, marked experimental, needs Datasette 1.0a23+.

This settles the **identity provider** open decision. Orpheus's `actors` table
already carries `idp` and `external_id` for exactly this, and `upsert_actor()`
is the seam. `datasette-accounts` emits stable actor `id` values; the plugin's
`actor_map` already maps a Datasette actor id onto an Orpheus one, so the
integration is a config entry rather than code.

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

## `datasette-paper`

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

## `datasette-apps`

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

## Suggested order

1. **Test whether `datasette-llm` can drive a local model.** Everything else
   about `datasette-agent` depends on it, and it is an afternoon.
2. **`datasette-accounts`**, if the local-model answer is yes and Datasette is
   going to be the front door. It deletes real code and closes a named open
   decision.
3. **The `permission_resources_sql` plugin**, using `datasette-paper` as the
   reference. Orpheus already generates the SQL; this is the smallest step from
   "documented rule" to "enforced rule".
4. **`datasette-apps` for the quality report** — independent of all of the above,
   and the fastest way to make the Phase 1 question visible to someone who does
   not run R.
5. **`datasette-agent` as a tool client**, last, and only through
   `register_agent_tools`. Never with `execute_write_sql` pointed at the store.

---

[← Back to index](index.md) | [Prior art →](prior-art.md)
