← [Back to index](index.md)

# API reference

One dispatch table, reachable three ways. Everything that changes the store
enters here — the browser pages call it, `/-/orpheus/api/` exposes it over HTTP,
and a script can call `api.handle()` directly with no server at all.

```python
status, payload = api.handle(store, "POST", f"/documents/{doc}/extract",
                             {"tier": "local"}, actor={"actor_id": "act_…"})
```

There is no separate API service. The R implementation ran one — a Plumber
process that owned the only write connection — and the Datasette plugin was an
HTTP client over it. Datasette is the writer now, so the same handlers run
in-process on its write thread.

- **Base URL** — `/-/orpheus/api/` on the Datasette server (`:8001` by default)
- **Auth** — the Datasette actor (any auth plugin), or `Authorization: Bearer <token>`
  when calling with no session. Every route except `/health` needs one
- **Bodies** — JSON for `POST`; query string for `GET`
- **Responses** — JSON

---

## Errors

```json
{ "error": { "status": 403, "message": "Not permitted to edit document …", "detail": null } }
```

| Status | Meaning |
|---|---|
| `400` | Bad request, or a refused operation (cloud gate, re-run without `force`, undeclared property) |
| `401` | No token, or a token that is unknown, revoked, expired, or belongs to a disabled actor |
| `403` | Authenticated but not permitted, including administrator-only routes |
| `404` | No such route, document, edge or evaluation |

---

## Permissions

Resolution order, highest first: **administrator → document owner → explicit
share → visibility level**. Anonymous requests are refused outright; contract
documents have no public audience.

| Actor | view | edit | share | delete |
|---|---|---|---|---|
| Administrator (`is_admin`) | ✓ | ✓ | ✓ | ✓ |
| Document owner (`created_by`) | ✓ | ✓ | ✓ | ✓ |
| Share role `editor` | ✓ | ✓ | ✗ | ✗ |
| Share role `viewer` | ✓ | ✗ | ✗ | ✗ |
| Any actor, `visibility = link-edit` | ✓ | ✓ | ✗ | ✗ |
| Any actor, `visibility = link-view` | ✓ | ✗ | ✗ | ✗ |
| Any actor, `visibility = private` | ✗ | ✗ | ✗ | ✗ |
| Anonymous | ✗ | ✗ | ✗ | ✗ |

A share cannot be used to widen a share: sharing and deleting stay with the
owner and administrators.

`auth.permission_sql("view" | "edit")` emits this same rule as SQL for
Datasette's `permission_resources_sql` plugin hook, generated from one place so
the two cannot drift. The test suite asserts the two agree for every actor.

---

## Endpoints

### Service

| Method | Path | Auth | Returns |
|---|---|---|---|
| `GET` | `/health` | none | Service status and time |
| `GET` | `/capabilities` | actor | Text-extraction backends, model config, cloud policy, active bundle, rubric |
| `GET` | `/bundle` | actor | The active ontology bundle |

`/capabilities` is worth calling first: it reports whether this deployment can
read PDFs, whether OCR is available, and whether cloud is enabled — before a user
uploads something the server cannot read.

### Documents

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/documents` | actor | Only what this actor may see |
| `POST` | `/documents` | actor | `201` new, `200` duplicate |
| `GET` | `/documents/<id>` | view | Metadata plus review progress |
| `GET` | `/documents/<id>/text` | view | Full text and per-page `text_source` |
| `GET` | `/documents/<id>/original` | view | The file as uploaded; `?metadata=1` for its size and digest, `?download=1` to force a save |
| `GET` | `/documents/<id>/instances` | view | `?type_id=`, `?include_rejected=` |
| `GET` | `/documents/<id>/history` | view | The document's audit trail |
| `POST` | `/documents/<id>/redact` | **delete** | Destroy everything read from it, keep the row; `?dry_run=1` counts first |

#### Getting the original back

Everything else on this surface is Orpheus's *reading* of a document. `/original`
is the document: the bytes that were uploaded, which every excerpt, page number
and character offset in the store was computed from.

It is the one route that does not return JSON. The payload is a `FileBody`, and
a transport that can send bytes sends them — a distinct type rather than a dict
with an agreed key, so a transport that cannot fails visibly instead of
serialising a path into a body and calling that a download.

Before anything is sent, the file is checked against the SHA-256 recorded at
ingest. That check is the point of the route, and it decides the status:

| Condition | Status | `reason` |
|---|---|---|
| The bytes hash to the recorded digest | `200` | — |
| The row records no path | `404` | `not_stored` |
| The path is not where a document of that hash belongs | `404` | `misfiled` |
| The path is right and nothing is there | `404` | `missing` |
| Something is there and it is not what was ingested | `409` | `altered` |

`409` rather than `404` for the last one is deliberate: the document exists and
the request was fine — the store disagrees with its own disk, which an operator
has to act on and a client must not retry its way past.

Verifying the hash is also what makes reading a path out of a database column
safe. `storage_path` is a path in a table, and a table is a thing that gets
written to; serving whatever is at the end of it would turn one write into an
arbitrary file read. Nothing an attacker can point that column at will hash to
a digest recorded before they got there — and the layout check refuses a path
that is not where content-addressed storage puts a document, before anything is
read at all.

The response is served `inline` for the handful of types a browser renders
without running anything the uploader wrote (PDF, plain text, raster images) and
as an `attachment` for everything else — `image/svg+xml` deliberately included,
since an SVG can carry script and would run it on this origin with the
reviewer's session. `X-Content-Type-Options: nosniff` covers the mislabelled
rest. The ETag is the document's own digest, so a client that already has the
file gets a `304`.

```bash
# What is there, without fetching fifty megabytes to find out
curl -H "Authorization: Bearer $TOKEN" \
  'localhost:8001/-/orpheus/api/documents/doc_abc/original?metadata=1'

# The file itself
curl -H "Authorization: Bearer $TOKEN" -OJ \
  localhost:8001/-/orpheus/api/documents/doc_abc/original
```

From the CLI, `orpheus original <document_id> --to <path>` does the same and
refuses on the same terms.

Upload takes either a multipart file or a JSON body naming a server-local path —
which is how a watched drop-directory or a batch load feeds the same code path
as a browser upload.

```bash
curl -X POST localhost:8001/-/orpheus/api/documents \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"path": "/srv/incoming/contract.pdf", "visibility": "private"}'
```

```json
{ "document_id": "doc_a3cf…", "duplicate": false, "filename": "contract.pdf",
  "n_pages": 12, "needs_ocr": 0, "text_source": "native" }
```

### Processing

| Method | Path | Permission | Body |
|---|---|---|---|
| `POST` | `/documents/<id>/classify` | edit | `tier`, `cloud_opt_in`, `engine` |
| `POST` | `/documents/<id>/extract` | edit | `tier`, `engine`, `cloud_opt_in`, `deterministic`, `force` |
| `POST` | `/documents/<id>/concepts/evaluate` | edit | — |
| `POST` | `/documents/<id>/corpus-analysis` | edit | `narrate`, `tier`, `cloud_opt_in` |
| `GET` | `/documents/<id>/evaluations` | view | `?kind=`, `?include_stale=` |

```bash
# Local extraction — always available
curl -X POST localhost:8001/-/orpheus/api/documents/$ID/extract \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tier": "local"}'
```

```json
{ "n_entities": 7, "n_edges": 3, "n_amendments": 1, "dropped_edges": 0,
  "run_id": "run_77a5…", "tier": "local", "n_deterministic": 4,
  "engine": "langextract", "model_error": null, "superseded": 0 }
```

Cloud needs both conditions. With the policy still `disabled`:

```json
{ "error": { "status": 400,
  "message": "Cloud processing is disabled for this deployment. …" } }
```

With the policy set but no `cloud_opt_in`:

```json
{ "error": { "status": 400,
  "message": "Cloud processing needs an explicit per-request opt-in. …" } }
```

### Review

| Method | Path | Permission | Body |
|---|---|---|---|
| `POST` | `/instances/<id>/confirm` | edit | — |
| `POST` | `/instances/<id>/amend` | edit | `changes` (required, non-empty), `note` |
| `POST` | `/instances/<id>/reject` | edit | `note` |
| `POST` | `/evaluations/<id>/review` | edit | `status`, `result`, `note` |
| `POST` | `/documents/<id>/review` | edit | `reviewed` |

The three instance verbs carry no `document_id`, so the permission is resolved
through the instance: `locate_instance()` finds its document, and `edit` on that
document is required. Review counts come back on `GET /documents/<id>` rather
than a route of their own — a reviewer reading a document wants both together.

An amendment that changes nothing is a `400`, not a no-op write. A browser form
posts every field it renders, so accepting the unchanged ones would flip
`source` to `human` on a value the machine got right — and the quality report
counts amendments as machine errors a human had to fix.

```bash
curl -X POST localhost:8001/-/orpheus/api/instances/$IID/amend \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"changes": {"name": "Meridian Systems Ltd.", "role": "prime_supplier"},
       "note": "Name per the companies register"}'
```

Amending a property the bundle does not declare is a `400` pointing at the
schema-amendment route — adding a property changes the bundle, which is a
separate review from correcting one row.

### Sharing

| Method | Path | Permission | Body |
|---|---|---|---|
| `POST` | `/documents/<id>/share` | share | `actor_id`, `role`, `revoke` |
| `POST` | `/documents/<id>/visibility` | share | `visibility` |

### Schema amendments

| Method | Path | Permission |
|---|---|---|
| `GET` | `/schema-amendments` | actor (`?status=`) |
| `POST` | `/schema-amendments/<id>/review` | **administrator** |

### Scores and thresholds

| Method | Path | Permission |
|---|---|---|
| `POST` | `/documents/<id>/score` | edit |
| `GET` | `/concept-parameters` | actor — effective thresholds and where each came from |
| `POST` | `/admin/concept-parameters` | **administrator** |

Changing a threshold changes what every document is measured against, so it is
an administrator action even though it looks like a setting. The response says a
new concept version was created and affected documents need re-evaluating.

### Extraction quality

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/quality` | **administrator** | Corpus-wide; `?min_reviewed=` |
| `GET` | `/documents/<id>/quality` | view | Scoped to one document |

Corpus figures aggregate across documents the caller may not be able to read,
so they are administrator-only. A per-document report needs only `view` on that
document.

```json
{ "headline": "81% of reviewed instances were confirmed as extracted, 12% needed correcting and 6% were rejected, over 67% of the population.",
  "extraction": { "overall": { "n_reviewed": 16, "n_confirmed": 13,
                               "accuracy": 0.812, "coverage": 0.67 },
                  "by_type": [], "by_confidence": [], "by_tier": [] },
  "calibration": { "verdict": "monotonic", "levels": [], "inversions": [] },
  "concept_precision": [ { "concept_id": "open_ended_term", "precision": 0.125 } ],
  "property_corrections": [], "codelist_violations": [] }
```

The headline says so in words when nothing has been reviewed, because a rate
over zero rows is not a low score — it is no measurement, and reporting it as a
number invites reading it as one. `calibration.verdict` is the finding that
matters: `monotonic` means the rubric ranks reliability, and any `inversions`
name a level the machine was more sure about that turned out less often right
than one below it. Every rate covers reviewed rows only — see [Provenance and amendment](provenance-and-amendment.md#measuring-extraction-quality).

### Conflicts

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/tensions` | actor | `?scope=`, `?subject_id=`, `?status=`, `?kind=`, `?standing=1` |
| `POST` | `/tensions` | actor | `kind`, `summary`, `sides` (2+), `subject_id` |
| `GET` | `/tensions/conflicts` | actor | Properties whose reviewed mentions disagree. Reads only |
| `POST` | `/tensions/propose` | actor | Raise the ones not already recorded, at `open` |
| `GET` | `/tensions/<id>` | actor | The tension and its cited sides |
| `POST` | `/tensions/<id>/accept` | actor | The conflict is real and it stands |
| `POST` | `/tensions/<id>/resolve` | actor | `resolution` **required** |
| `POST` | `/tensions/<id>/withdraw` | actor | `reason` **required** |
| `GET` | `/documents/<id>/tensions` | view | Every tension this document is a side of |

`sides` is two or more instance ids, or objects of `{"instance_id", "position"}`.
Fewer than two distinct existing instances is a 400: one claim on its own is an
assertion, and if it is wrong the verb is `reject`.

`accept` is the one worth knowing about. Every other review verb in the API
makes a disagreement go away, and a reviewer with no way to say *this conflict
is real* has only two exits — pick a side, or leave it looking unreviewed. An
accepted tension is finished review work, and the wiki renders it as an
assertion. `resolve` requires a reason for one: a settled conflict with no
account of the reasoning looks decided and cannot be checked.

A settled tension is never reopened. Raise a new one instead, so what was
decided then stays readable. See [Conflicts and lint](conflicts-and-lint.md).

### Lint and export

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/lint` | **administrator** | `?deep=0` for the cheap checks, `?checks=a,b` |
| `GET` | `/storage/audit` | **administrator** | Are the originals still there; `?verify=1` with a `document_id` re-reads one |
| `POST` | `/export` | **administrator** | `out` (server path), `?confirmed_only=1` |

Both span the whole corpus, including documents the caller may not be able to
read. `/export` writes every page in the store to a directory the caller names,
which is a sharper reason again.

```json
{ "headline": "1 located problem(s) across 9 check(s): 1 high. Most-serious first.",
  "checks_run": ["uncited_page", "ungrounded_quotation", "smoothed_conflict"],
  "counts": { "high": 1, "medium": 0, "low": 0 },
  "findings": [ { "check": "smoothed_conflict", "severity": "high",
                  "where": { "entity_id": "ent_…", "name": "Ardmore Digital Ltd",
                             "property_id": "address" },
                  "finding": "… has 2 confirmed values for address and no recorded conflict",
                  "suggestion": "Raise a tension. …" } ] }
```

Every finding carries a `where` naming a row, because a report of general
observations is one nobody acts on. When nothing is found the headline says how
much has been reviewed rather than reporting an all-clear — over a handful of
reviews, "nothing found" says more about how little was checked.

### Reading with the machine

| Method | Path | Permission | Notes |
|---|---|---|---|
| `POST` | `/documents/<id>/passages/<n>/read` | view | `engine`, `tier`, `cloud_opt_in`, `context_chars` |
| `GET` | `/documents/<id>/passages/<n>` | view | `?status=offered\|accepted\|dismissed\|all` |
| `GET` | `/documents/<id>/reading` | view | Progress, per actor |
| `POST` | `/suggestions/<id>/accept` | edit on its document | `properties` corrects on the way in |
| `POST` | `/suggestions/<id>/dismiss` | edit on its document | `note` |
| `GET` | `/suggestions/quality` | **administrator**, or `?document_id=` | Acceptance rate per engine |

The read route is a `POST` guarded by `view`, which looks odd and is right: it
writes, but what it writes is the reader's own progress and a set of proposals,
not a change to the document. Requiring `edit` would stop a viewer using the
companion at all.

**A suggestion is not an extraction.** Reading a passage writes no instance and
no provenance:

```json
{ "page_no": 1, "engine": "deterministic", "n_offered": 4,
  "suggestions": [ { "suggestion_id": "sug_…", "type_id": "KeyDate",
                     "status": "offered", "confidence": 1.0,
                     "properties": { "value": "2024-03-03", "date_role": "start" },
                     "excerpt": "This Agreement is made on 3 March 2024…" } ] }
```

Accepting writes the instance through the same path a batch pass uses —
`source: "human"`, `status: "confirmed"`, with provenance carrying the page, the
excerpt and the span under `source_label: "companion:<engine>"`. Dismissals are
kept: they are the only evidence there is about whether the suggestions are
worth reading, which is what `/suggestions/quality` reports. That measure is
deliberately separate from `/quality` — one measures extraction against review,
the other measures offers against a person's attention.

See [Reading with the machine](reading-companion.md).

### Registers

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/registers` | actor | Every register and whether anybody vouched for it |
| `GET` | `/registers/<id>` | actor | The register and its rows; `?column=`&`?value=` filter on an exposed column |
| `GET` | `/registers/columns` | actor | Which register keys are queryable |
| `POST` | `/registers/columns/expose` | **administrator** | `key`, `as_column`, `note` |
| `POST` | `/registers/columns/hide` | **administrator** | `column`, `note` |
| `POST` | `/registers/<id>/rows/<n>/reject` | **administrator** | `note` |
| `POST` | `/registers/<id>/promote` | **administrator** | Makes it evidence |
| `POST` | `/registers/<id>/withdraw` | **administrator** | Stops it counting; keeps it readable |

`/registers/columns` is declared before `/registers/<register_id>`: both match
the same path and first match wins, so the specific one has to come first.

Exposing a column is administrator-only because it alters `register_rows` for
every register in the store — not because the values are sensitive. It copies
nothing: the column is computed from `values_json` on read, so hiding one loses
nothing.

Reading a staged register is deliberately not restricted — being looked at is
what staging is for. Promoting is administrator-only because reference data
every later answer rests on means somebody takes responsibility for what it
decides. See [the register](entities.md#a-register-when-the-documents-cannot-settle-it).

### The ontology itself

For a corpus that has no bundle yet. See
[Where an ontology comes from](ontology.md).

| Method | Path | Permission | Notes |
|---|---|---|---|
| `POST` | `/ontology/survey` | **administrator** | `engine`, `sample`, `min_support`, `document_ids`, `primary_type`, `chars_per_document`, `tier`, `cloud_opt_in` |
| `GET` | `/ontology/candidates` | actor | `?status=proposed`, `?kind=object_type\|property\|link_type` |
| `POST` | `/ontology/candidates/<id>/review` | **administrator** | `decision`, `accepted_as`, `note` |
| `POST` | `/ontology/draft` | **administrator** | `bundle_id`, `bundle_version`, `name`, `primary_type`, `document_types`, `document_scoped`, `sectors`, `jurisdictions` |

Surveying and reviewing are administrator-only, and not because a survey is
dangerous. Accepting an object type fixes the shape of every row that will ever
be filed under it — a decision `schema_ops.py` exists because somebody once
could not undo.

`accepted_as` renames a candidate and lands it at `amended`. That is the
ordinary accepting move: a survey notices that something recurs and has no way
to know what it is called.

`/ontology/draft` returns the bundle; it does not register it. Registering an
ontology is a deliberate act with a deployment behind it, and a drafting route
that also installed it would be the one place in this API where an ontology
arrived without anybody choosing it. Register it with the CLI
(`orpheus ontology draft --register`) or by loading the returned JSON.

The Datasette page `/-/orpheus/ontology` is the same four calls with the
evidence rendered: every candidate's quotations, its support, and accept /
rename / reject in one form. The rename box sits beside Accept rather than
behind a second screen, because renaming is the ordinary accepting move.

Each candidate carries `n_documents` of `n_sampled` — how many documents show
it, **counted rather than claimed**. It is not a confidence, and the model is
never asked for one.

### The corpus as a network

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/graph/topology` | **administrator** | `?seed=`, `?reviewed_only=1` |
| `GET` | `/graph/edges` | actor | `?link_type_id=`, `?reviewed_only=1` |
| `GET` | `/graph/map` | **administrator**, or actor with `?entity_id=` | `?depth=2` (1–4), `?reviewed_only=1` |
| `GET` | `/graph/entities/<entity_id>` | actor | `?depth=1` — one page and its neighbours |
| `GET` | `/graph/paths` | actor | `?from=`, `?to=`, `?max_paths=5`, `?max_length=6` |
| `GET` | `/graph/centrality` | **administrator** | `?sample=` to approximate betweenness |
| `GET` | `/corroboration` | **administrator** | `?min_documents=2` |
| `GET` | `/entities/<entity_id>/corroboration` | actor | Scoped to one page |

`/graph/map` is the same projection in the shape a drawing needs: `nodes` and
`edges` together, with `coverage` and a caveat alongside. It reads
`graph.build`, so a picture cannot show a relation the text views would not.
Uncentred it spans every document in the store and is administrator-only for
the reason `/graph/topology` is; passing `entity_id` scopes it to one page and
its neighbours out to `depth`, which any actor may ask for. Edges are returned
only between pages that are also in `nodes`, so nothing is drawn to nowhere.

`/graph/edges` returns one row per `(from_page, link_type, to_page)`, with every
contributing edge kept whole underneath it — its document, evidence, confidence
and review status. Four contracts asserting one subcontracting relation is one
edge with four sources, not four unrelated rows.

Every response carries **`coverage`**, and it is the first key in the topology
on purpose:

```json
{ "coverage": { "n_edges_total": 40, "n_edges_projected": 12,
                "projected_rate": 0.3, "n_unlinked_mentions": 61,
                "note": "Only 30% of extracted relations reached the graph…" },
  "counts": { "entities": 18, "canonical_edges": 12, "components": 2 },
  "components": [ … ], "articulation_points": [ … ],
  "communities": [ … ], "disconnected_pairs": [ … ] }
```

The graph is a projection: an edge exists only where **both** endpoints resolve
to entity pages, so a mention still in the wiki queue contributes nothing. A
sparse-looking network over 30% coverage means a half-built wiki, not a thin
corpus — opposite findings that the structural numbers alone cannot distinguish.

`components`, `articulation_points`, `isolates`, degree and `/graph/paths` are
deterministic and need nothing installed. `communities` and the `bridges`
defined from them are a **seeded heuristic** and say so in `basis` on every row,
alongside the `method` that produced them: reproducible, but one defensible
partition among several. Where a claim has to hold up, use a component — an
island is a fact, a community is a reading.

With `orpheus[graph]` installed, communities come from **Louvain** and carry
`modularity` — the number saying whether the partition means anything at all —
and `/graph/centrality` adds **betweenness**. Without it, clustering falls back
to label propagation and centrality returns degree alone, each saying so rather
than substituting a number that would not mean the same thing.

`/graph/paths` answers *how are these two connected*, and every chain names its
weakest hop:

```json
{ "paths": [ { "n_hops": 3, "confirmed_throughout": false,
               "entities": [ … ],
               "hops": [ { "link_type_id": "subcontracts_to",
                           "n_documents": 2, "n_confirmed": 0 } ],
               "weakest": { "n_confirmed": 0, "n_documents": 1 } } ],
  "note": "1 chain(s), shortest 3 hop(s). 0 vouched for at every hop…" }
```

A chain running through a relation nobody has checked is not the same finding as
one vouched for end to end, and reporting them alike invites the conclusion the
store exists to prevent.

### Corroboration

Counted in **distinct wordings across distinct documents**, never in rows:

```json
{ "property_id": "address", "value": "12 Ushers Quay, Dublin 8",
  "n_documents": 6, "n_wordings": 1, "independent": false,
  "note": "6 documents, one wording. The same sentence appears in each, so this
           is one source quoted several times rather than several sources agreeing." }
```

Six call-off contracts carrying one framework's boilerplate is one source
wearing six hats. **No confidence value is changed by any of this** — confidence
says how sure one extraction is, corroboration says how many independent sources
there are, and combining them would put a number on the rubric that no reviewer
could state.

### Administration

| Method | Path | Permission | Body |
|---|---|---|---|
| `GET` | `/audit/llm` | administrator | `?document_id=`, `?tier=` |
| `POST` | `/admin/settings` | administrator | `key`, `value` |
| `POST` | `/admin/concepts/setup` | administrator | — |
| `GET` | `/concept-parameters` | actor | Effective thresholds and their source |

`cloud_ai_policy` is validated against `disabled` / `per_user` / `org_allow`; an
unrecognised value is rejected rather than stored, so the gate can never be left
in a state it cannot interpret.

**The cloud budget** is the third condition on that gate, alongside the org
policy and the per-request opt-in, and is checked before any text is prepared so
a refused call sends nothing. It is denominated in **characters sent, not
currency**: Orpheus talks to its providers over plain HTTP and knows nothing
about their price lists, and a cap in euro would need a hardcoded rate table
that goes stale the week a provider changes one — a budget that silently stops
matching the invoice is a control somebody is relying on. Characters are exact,
always available, and measure the thing a public body has to answer for anyway:
how much of its material left the building.

| Setting | Meaning |
|---|---|
| `cloud_budget_chars` | Characters that may be sent per window. Unset means no cap. |
| `cloud_budget_window` | `total`, `day` or `month` (default `month`) |
| `cloud_price_per_million_chars` | Optional. A deployment's own rate, for an estimate labelled as one. |

Failed calls count: a call that errored sent its payload just the same, which is
the same reason `llm_calls` records it. The current state is on
`GET /capabilities` under `cloud.budget`, so a deployment sees the cap before a
run hits it, and `orpheus budget` exits non-zero when it is spent.

---

## A full session

```bash
TOKEN=...; B=http://localhost:8001/-/orpheus/api

DOC=$(curl -s -X POST $B/documents -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' -d '{"path":"/srv/in/contract.pdf"}' \
      | jq -r .document_id)

curl -s -X POST $B/documents/$DOC/classify -H "Authorization: Bearer $TOKEN"
curl -s -X POST $B/documents/$DOC/extract  -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"tier":"local"}'

curl -s -X POST $B/documents/$DOC/concepts/evaluate -H "Authorization: Bearer $TOKEN"
curl -s     $B/documents/$DOC/instances -H "Authorization: Bearer $TOKEN" | jq '.instances[0]'

curl -s -X POST $B/instances/$IID/confirm -H "Authorization: Bearer $TOKEN"
curl -s -X POST $B/documents/$DOC/review  -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"reviewed":true}'
```

---

[← Back to index](index.md) | [Next: Deployment →](deployment.md)
