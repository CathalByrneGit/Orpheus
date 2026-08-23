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
| `GET` | `/documents/<id>/instances` | view | `?type_id=`, `?include_rejected=` |
| `GET` | `/documents/<id>/history` | view | The document's audit trail |

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
| `POST` | `/documents/<id>/classify` | edit | — |
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
