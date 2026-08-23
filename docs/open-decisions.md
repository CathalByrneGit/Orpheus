← [Back to index](index.md)

# Open decisions

What Phase 1 deliberately did not decide, and what the build corrected. Each
entry says what is open, what was built so the decision stays cheap, and what
would settle it.

---

## R stack vs. a Python rebuild

**Status: decided. The platform is Python; the R implementation is deleted.**

Phase 1 was specified as building on the existing R ontology stack rather than
re-implementing its ideas. That was the right default, and the stack's thinking
— discover → populate, the confidence rubric, schema amendment candidates,
versioned concepts — is genuinely reusable and was reused. What was decided
against is the *stack*, not the ideas.

The R implementation was built first and ran end to end: 709 tests, a live
Plumber API, a Datasette plugin, a real PDF through the whole pipeline. It was
then ported rather than abandoned, and the port is what settled the question.

### What the R build actually found

These came out of building against the packages' source, and they are the
substantive input to the decision:

1. **Two incompatible bundle shapes shared one class.**
   `ontologySpecR::bundle()` emits `bundleId` / `objects` / `links`;
   `ontologyDiscoverR::dis_to_bundle()` emits `bundle_id` / `object_types` /
   `link_types`. Both are classed `ontology_bundle`, so a consumer cannot tell
   them apart, and each of three consumers read a different subset of the field
   names. The R bundle carried **every spelling** and validation asserted the
   duplicated pairs agreed — a workaround for an inconsistency inside the stack,
   and the reason the file was 114 KB.

2. **`pop_add_file()` would silently discard OCR text.** It re-parses the file
   from disk with `pdftools`, so a scanned document would have thrown away the
   OCR pass and extracted from nothing — on exactly the documents where quality
   matters most. Orpheus bypassed it by injecting a `DiscoverySource` built from
   stored page text into `pop_sess$sources`, relying on an internal shape rather
   than the public API.

3. **Vocabulary mismatches at the boundary.** The stack used
   `pending` / `approved` against this platform's four states, and documented a
   confidence rubric while `pop_extract()` defaulted to `0.8` and passed
   arbitrary floats through.

4. **`cpt_add_score_component(version = NULL)` could never work.** Its own
   default is documented as "use whichever version is active at evaluation
   time", but `composite_score_components.version` is `NOT NULL` *and* part of
   the primary key, so the default always failed on insert.

5. Three of the four packages **could not be installed** in a clean environment
   at all: `ontologySpecR` needs V8 via `jsonvalidate`, `objectSetsR` imports it,
   `ontologyDiscoverR` needs `ellmer`. Only `conceptR` installed cleanly and it
   worked exactly as documented — 37 tests exercised it for real.

### What the port changed

| | R | Python |
|---|---|---|
| Bundle | Every list twice, 114 KB, validated for self-agreement | One spelling, 58 KB, [ontologySpecR](https://github.com/CathalByrneGit/ontologySpecR)'s format validated against that project's own schema |
| Extraction | One engine behind an adapter | A registry: `gliner2`, `langextract`, `llm`, `chat` — see [Extraction engines](extraction-engines.md) |
| Grounding | Whatever the engine claimed | Computed: every excerpt located in the source, the rubric level following from match quality |
| Writer | A Plumber API; Datasette read-only beside it | Datasette itself, with the core as a library it imports |
| Processes | Two, plus a token between them | One |
| Dependencies | `DBI`, `RSQLite`, `jsonlite`, `digest`, `rlang`, `cli`, `plumber`, plus four GitHub packages | None. The core is standard library only |
| Tests | 709 | 300, plus a browser loop over real HTTP |

The test count fell because the R suite tested things the Python core does not
have to: the bundle's self-agreement, the adapter's vocabulary translation, and
the interop workarounds above. Removing the need for a test is better than
passing it.

### What the port kept

Everything that was a decision rather than a mechanism. The four-state review
vocabulary, the confidence rubric and its downward-biased snapping, provenance
as the immutable record beside a mutable instance row, `seq`-ordered
append-only history, the two-condition cloud gate, dependency-tracked staleness,
and the schema-amendment queue. Those were the stack's good ideas, and they are
all here — several of them, like the rubric and the amendment queue, were
already re-implemented in Orpheus rather than borrowed.

The R implementation is preserved in git history, and
`tests/fixtures/contract-core-0.1.0.json` keeps its bundle so a store built by
it still opens. A test proves Python opens and migrates an R-built store in
place.

**What is still not settled** is the question this was all supposed to answer,
and no language choice touches it: see *Extraction quality is unmeasured* below.

---

## Scope: a document platform, with contracts as the worked example

**Settled: the engine is domain-neutral and the bundle carries the domain.**

Phase 1 was specified around contracts and the code drifted into assuming them —
`compare_contract_values()` selected from `instances_Contract` by name,
deterministic findings were linked by a hardcoded `contract_instance_id`, the
classifier's vocabulary was a constant, and a Datasette canned query listed the
instance tables of one domain.

None of that was necessary. The pipeline needs to know *which type plays which
role*, not what the types mean, so a `x_orpheus` block on the bundle now says
so and the four sites read it. See
[the domain block](data-model.md#the-domain-block).

Contracts remain the shipped bundle and the example throughout the docs: it is
the driving use case, and a general platform documented only in the abstract is
harder to understand than one with a worked example. But
`orpheus/bundles/contract-core-0.2.0.json` is data, and replacing it replaces the
domain.

**Kept honest by a test.** `tests/test_domain_neutrality.py` defines a planning-
application bundle — Application, Applicant, Condition, its own decision
codelist, its own rule concept, its own comparable value (floor area in square
metres) — and runs ingest, extraction, deterministic linking, review, concept
evaluation and codelist reporting against it with no code changes. A claim of
generality that nothing exercises tends to stop being true quietly, and this one
had: porting the test found `raise_flag()` hardcoding `instances_Flag`, so any
bundle without a `Flag` type crashed concept evaluation.

**What is still contract-flavoured, deliberately:** the seed concepts
(`high_value`, `direct_award`, `uncapped_liability`), the OCDS codelists, and
the CUAD benchmark mapping. Those are bundle content and benchmark
configuration, which is exactly where domain knowledge belongs.

---

## Datasette as the primary surface

**Status: taken, and now further than "primary surface" — Datasette *is* the
writer.** The decision was to lean into the Datasette ecosystem rather than
build a bespoke UI, and to make the central interaction a person and a model
working through a document together.

**Built:** `plugins/orpheus_datasette.py` — upload a file in the browser, watch
it ingest, classify and extract, then confirm, amend or reject each fact. The
API is mounted in the same process at `/-/orpheus/api/`.

The R arrangement had Datasette as a read-only client of a Plumber API that
owned the only write connection. The Python one inverts that: Datasette holds
the database, the core is a library it imports, and every write is queued
through Datasette's own write thread. The invariant that replaces "never opens a
connection" is stricter and easier to check — *nothing writes except through
core functions* — and the constraints, plus what running it for real exposed,
are in [Deployment](deployment.md#the-datasette-ui-plugin).

**Not built: the reading companion.** The plugin reviews a document after
extraction has run over the whole of it. Annotation *as you read* is a different
thing, and three problems stand between here and there:

- **Incremental classification.** `classify()` is one-shot over the whole
  document. Per-page or per-passage proposals are a different call shape and a
  different unit of provenance.
- **Latency.** A batch pass can take seconds; a companion reacting to scrolling
  cannot. That is what the local tier is for, with the cloud tier reserved for
  on-demand questions.
- **Identity.** Partly addressed. The plugin now maps a Datasette actor onto an
  Orpheus one through `actor_map`, so any Datasette auth plugin supplies the
  person. Which provider a deployment should use is still open.

This overlaps with what agents.md scopes as Phase 3 and with `ontologyMCP`. The
difference is the surface: agents.md imagines a bespoke split-view reading pane,
and this puts the same interaction inside Datasette. That choice is now made;
what remains is the three problems above.

**`datasette-agent` is the most likely vehicle for it** — a chat surface with a
`register_agent_tools` hook, which is the right seam for handing a model typed
Orpheus operations rather than raw SQL. It does not remove the need for the
core; it needs it more than the current UI does, because its alternative write
path is arbitrary SQL approved in a chat window. Now that the API is mounted
in-process, registering those tools is a small piece of work: each one is an
`api.handle()` call. The conditions for adopting it are in
[Datasette ecosystem](datasette-ecosystem.md#datasette-agent).

---

## The rest of the ontology stack: what was taken, what was left

The account holds fifteen ontology-family packages. They were surveyed once so
that this does not have to happen again. `ontologyR` is out of scope by
instruction.

**Taken: one idea, from `auditR`.** It samples a concept's output, records human
judgments and computes disagreement rates. Orpheus does not need its sampling —
review here is exhaustive, so every reviewed row is already a labelled example —
but the underlying point was a real gap: Phase 1 turns on extraction being good
enough, and nothing measured it. That gap is now closed by
[`quality_report()`](provenance-and-amendment.md#measuring-extraction-quality),
built on data the store already held. The package itself is not a dependency.

**Left, as later phases:**

| Package | Belongs to |
|---|---|
| `ontologyMCP` — MCP server, 10 tools | Phase 3, the reading companion. agents.md already names it |
| `vertexR` — graph, alert rules, risk propagation | Phase 5. Substantially more than the graph work Phase 5 anticipates |
| `ontologyConnectR` — REST/FHIR/JDBC/GraphQL, plus `live_icij` | Phase 5. An ICIJ Offshore Leaks connector is squarely conflict-of-interest territory |
| `actionTypesR`, `machineryR` — action lifecycle, state machines | Could back the review workflow later; Phase 1's four statuses do not need them |
| `lineageR` — source/transform/column lineage | Overlaps staleness tracking, but at dataset level rather than fact level |
| `datapond` — DuckLake | The storage migration above |

**A caveat on all of it.** Commit counts suggest how much has actually been
exercised: `datapond` 172, `explicaR` 38, `actionTypesR` 13, most others 4–6 —
and `auditR`, `sqlglotR`, `ontologyAPI`, `ontologyMCP` and `objectExploreR` have
**one or two commits each**. Those read as generated once and never run. Treat
the list as available ideas rather than as working machinery until something has
been exercised.

**Two findings that fed the R-versus-Python decision above:**

- `ontologyAPI` already exposes ontology queries over plumber, but its auth is a
  **single shared API key with no actor concept**. It could not have backed
  Orpheus's per-document permissions, which need `created_by`, share grants and
  `amended_by` tied to a real person.
- `sqlglotR` wraps Python SQLGlot through reticulate. The stack was **already
  not pure R**, so "R or Python" was never the clean choice it looked like.

`ontologySpecR` is the one that survived the port, and not as a dependency: its
**bundle format** is what Orpheus now writes, validated against its schema
unmodified.

---

## Decisions the build made, and why

These were open in the architecture and are now settled in code. They can still
be changed; they are recorded so the reasoning is not lost.

### Document-level review state

**Settled: both, and they are kept separate.**

Per-instance status answers "has this fact been checked"; the document flag
answers "has anyone been through the whole thing". Marking a document reviewed
deliberately does **not** confirm its instances — conflating them would let one
click promote every unchecked machine guess to `confirmed`. The result reports
what is still outstanding instead. See
[Provenance and amendment](provenance-and-amendment.md#two-levels-of-review).

### Cloud AI opt-in policy

**Partly settled: the mechanism is built, the policy is a runtime setting that
defaults to off.**

Both candidate policies exist (`per_user`, `org_allow`) and neither is assumed.
The default is `disabled`, so a deployment handling sensitive contract data has
to enable cloud processing deliberately rather than discover it was on. Policy
alone never authorises a call — an explicit per-request `opt_in` is always
required as well.

**What would settle it:** whether an individual user may opt a sensitive document
into cloud processing on a shared server is a question for the organisation's
information-governance people, not a technical one.

### OCR approach

**Deliberately not settled. A provider registry was built instead.**

`set_ocr_backend()` takes any `f(image_path) -> text`. Built-in fallbacks are
`pytesseract` then a `tesseract` binary. With no backend, pages are marked
`needs_ocr` rather than passed off as empty.

A third candidate arrived with the port: **Docling** does layout-aware parsing
and OCR in one pass, giving reading order and table structure rather than a flat
string. It is wired as an optional text backend and is **untested** — it would
not install in the build environment (`antlr4-python3-runtime` conflicts) — so
it is a path, not a recommendation.

**What would settle it:** evaluating candidates against real scanned
contracts — the accuracy difference on poor-quality government scans is the whole
question, and it cannot be answered from documentation.

### Identity provider

**Deliberately not settled. The bridge was built instead.**

`auth.upsert_actor(store, idp, external_id, ...)` is where any provider lands.
Tokens work today, and the plugin's `actor_map` connects whatever a Datasette
auth plugin produces to an Orpheus actor. `auth.permission_sql()` emits the
row-level rule for whichever plugin is chosen.

**What would settle it:** confirming what the deployment target actually runs —
Entra ID, Okta, a government SSO, or nothing yet.

**A candidate has since appeared.** `datasette-accounts` stores accounts in
Datasette's internal database with PBKDF2 hashing, brute-force lockout,
revocable sessions and audit logging, and emits stable actor ids — which is
exactly what `upsert_actor()` was left open for. It would delete token minting,
revocation and authentication from `auth.py`. The shared-token problem it was
also going to solve has since gone away on its own: the plugin no longer holds a
token at all, because it no longer speaks HTTP. It is marked experimental and it
puts a second writer on a second database. See
[Datasette ecosystem](datasette-ecosystem.md#datasette-accounts).

### Permission boundaries

**Mechanism settled, rules deliberately not.**

Owner + visibility + share-table, following `datasette-paper`. Actors carry
`departments_json` so a `datasette-acl` dynamic group can key off it the moment
department rules exist — but no department rule is invented, because guessing
would bake a wrong rule into the schema.

**What would settle it:** scoping with real stakeholders whether the boundary is
department, sensitivity tag, document owner, or a combination.

### `bundle_diff()` and staging concept versions

**Partly addressed.** `bundle.register(stage="staging")` stores a bundle without
activating it, and a staging bundle cannot be activated by accident. Concepts
are versioned and deprecated rather than edited, so an evaluation always points
at a version that still exists.

A true `bundle_diff()` — showing what registering a bundle would change before
it changes it — is not built. It is now ordinary work in this repository rather
than upstream work in another project, which is one thing the port bought.

---

## Extraction quality is unmeasured

**Status: open, and it is the only entry here that Phase 1 cannot be called done
without.**

Phase 1's definition of done is extraction *good enough to trust as a
foundation*. Everything above is machinery for producing that number and for
making it honest — provenance preserved beside corrections, grounding computed
rather than believed, unreviewed rows never counted as correct, rule flags kept
out of the extraction figures. The machinery works. The number does not exist.

What has not happened, plainly:

| | Status |
|---|---|
| A real model has run against a real document | **No.** No API key and no local model were available in the build environment. Every model result in this repository comes from a substitute populator, an echo model, or a test HTTP server |
| A real corpus has been ingested | **No.** One hand-built two-page PDF fixture |
| A person has reviewed extractions they did not write | **No** |
| `gliner2` has run | **No.** Its weights could not be downloaded |
| `docling` has run | **No.** It would not install |

The deterministic pass *is* exercised for real — it finds four dates and an
amount in the fixture, grounded to the right pages, through a real browser
upload. That is the part that needs no model. It is also the smaller part.

**What would settle it:** ingest a few hundred real contracts with a model
configured, have someone who knows the domain review a sample, and read
`orpheus report`. The three things to look at, in order:

1. **`calibration.verdict`.** If it is not `monotonic`, the confidence rubric is
   not ranking reliability and everything built on it — which triage queue a
   reviewer sees first, which findings are trusted without checking — rests on
   nothing. That is a finding about the design, not about the corpus.
2. **`accuracy` at `explicit` and `named`.** These are the levels a person would
   plausibly skim rather than check. If they are not high, the review burden is
   the whole cost of the system and the automation is not paying for itself.
3. **`property_corrections`.** The fields people keep fixing are the prompt's
   defects, and they are cheap to act on.

Nothing else on this list is worth deciding before that number exists. A
language, a storage engine and an identity provider are all answerable later; an
extraction pipeline nobody has measured is answerable only by measuring it.

---

## Revisit when triggered

Deferred with an explicit trigger rather than a date.

### Storage migration to DuckLake

**Trigger: the application-level audit trail proves insufficient, or write load
outgrows the single-writer pattern.**

SQLite was chosen for Phase 1 because it is Datasette's first-class format and
needs no glue code. Two gaps are being accepted knowingly:

| Gap | What carries it instead | Weaker how |
|---|---|---|
| No storage-level time travel | The `edit_history` table | A code path that forgot to log would be a hole a storage guarantee would not have |
| No bundle versioning tied to data state | `concept_id` / `concept_version` columns | Application-level rather than a snapshot |

The mitigation is that every mutation routes through `record_edit()` inside the
same transaction as the change, and nothing writes to an instance table
directly. That is a convention enforced by code review, not by the storage
engine.

Migrating a working SQLite schema into DuckLake later is a much smaller job than
building the plumbing speculatively now. Note that a Datasette↔DuckLake access
plugin would also be needed, and was previously scoped as custom work.

### Vector and similarity search

**Trigger: a concrete, requested need — "find clauses like this one across
contracts".**

Not built, and not a reason to introduce LanceDB now. If it happens, DuckDB's
vector extension is the more consistent path, and it is entangled with the
storage decision above.

---

## Corrections this build made to the architecture

Two places where the specification, followed literally, would not have worked.

### `--immutable` is wrong for a live WAL store

The architecture asks for WAL mode *and* for Datasette in `--immutable` mode.
**These are incompatible.** `immutable=1` makes SQLite skip the WAL, so an
immutable reader sees the database as of the last checkpoint — nothing at all on
a freshly written store, with no error raised.

Measured: 0 documents visible immutable-without-checkpoint, 1 with a read-only
connection, 1 immutable after `wal_checkpoint(TRUNCATE)`.

Orpheus serves without the flag, and checkpoints after writes. See
[Deployment](deployment.md#the-wal-and-immutable-mode-trap).

### Concept evaluation does not carry lineage

The architecture refers to `ont_evaluate()` carrying lineage automatically.
Nothing in the stack did: `conceptR::cpt_evaluate()` evaluates one SQL
expression against one table and returns a data frame.

Lineage is therefore built here: `concept_evaluation_dependencies` records which
instances each evaluation read, which is what makes `stale` automatic rather
than something a person has to notice. Worth knowing when reading the
architecture alongside the code.

### A model failure must not cost the deterministic findings

Not in the specification at all, and it only shows up once one transaction
spans a whole request. The deterministic pass needs no model; raising through a
failed model call rolled its findings back, so the half of the pipeline that
works offline only ever survived when the half that doesn't also worked. A model
failure now leaves the run `partial`, keeps what was found, and says so.

---

## Still out of scope for Phase 1

Unchanged from the architecture: entity resolution and alias merging, the
cross-document relationship graph, conflict-of-interest and procurement views,
and the live reading-pane companion.

The one deliberate exception is the step 9 corpus escalation, which is
best-effort naive name matching, labelled `naive_unresolved` on every result. It
is a stepping stone to Phase 4 resolution, and it should be **replaced** by real
resolution rather than patched indefinitely. `naive_key()` has a test asserting
its known failure — `"Ernst & Young"` and `"Ernst and Young"` produce different
keys — so the limitation cannot quietly disappear.

It had a second, worse one that was not documented because it was not known:
`group` and `holdings` were treated as legal-form suffixes and stripped anywhere
in a name, so `"Kestrel Medical Group"` and `"Kestrel Medical Ltd"` shared a
key, as did `"Ardmore Holdings plc"` and `"Ardmore Ltd"`. A false *merge* is
strictly worse than a false split: a split leaves two rows a person can join,
while a merge combines two organisations and leaves nothing to notice — and a
holding company is not its subsidiary, which is the distinction that matters
most in exactly the conflict-of-interest work this is a stepping stone to. Only
trailing legal forms are stripped now, and migration 5 recomputes stored keys.

**A better tool for the same job now exists.** `search.unlinked_mentions()`
asks the question key matching cannot: which documents *name* something with
nothing extracted from them. Key matching can only join instances that were
already extracted, so the misses are invisible to it by construction. That is
the screen real resolution should be built on.

---

[← Back to index](index.md)
