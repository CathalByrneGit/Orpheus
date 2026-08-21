← [Back to index](index.md)

# API reference

The Plumber API is the only writer in the system. Everything that changes the
store enters here.

- **Base URL** — `http://127.0.0.1:8000` by default (`ORPHEUS_PORT`)
- **Auth** — `Authorization: Bearer <token>` on every route except `/health`
- **Bodies** — JSON
- **Responses** — JSON with scalars unboxed (`{"status":"ok"}`, not `{"status":["ok"]}`)

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
| `404` | No such edge or evaluation |

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

`orph_permission_sql("view" | "edit")` emits this same rule as SQL for
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
| `GET` | `/documents/<id>/edges` | view | Extracted relationships |
| `GET` | `/documents/<id>/history` | view | The document's audit trail |

Upload takes either a multipart file or a JSON body naming a server-local path —
which is how a watched drop-directory or a batch load feeds the same code path
as a browser upload.

```bash
curl -X POST localhost:8000/documents \
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
| `POST` | `/documents/<id>/extract` | edit | `tier`, `cloud_opt_in`, `deterministic`, `force` |
| `POST` | `/documents/<id>/concepts/evaluate` | edit | — |
| `POST` | `/documents/<id>/analyse` | edit | `tier`, `cloud_opt_in` |
| `POST` | `/documents/<id>/corpus-analysis` | view | `narrate`, `tier`, `cloud_opt_in` |
| `GET` | `/documents/<id>/evaluations` | view | `?kind=`, `?include_stale=` |

```bash
# Local extraction — always available
curl -X POST localhost:8000/documents/$ID/extract \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tier": "local"}'
```

```json
{ "n_entities": 7, "n_edges": 3, "n_amendments": 1, "dropped_edges": 0,
  "run_id": "run_77a5…", "tier": "local", "n_deterministic": 4,
  "excerpt_only": false }
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
| `GET` | `/instances/<id>/history` | view | — |
| `POST` | `/edges/<id>/review` | edit | `status`, `link_type_id`, `note` |
| `POST` | `/evaluations/<id>/review` | edit | `status`, `result`, `note` |
| `GET` | `/documents/<id>/review` | view | Counts by status |
| `POST` | `/documents/<id>/review` | edit | `reviewed` |

```bash
curl -X POST localhost:8000/instances/$IID/amend \
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

### Extraction quality

| Method | Path | Permission | Notes |
|---|---|---|---|
| `GET` | `/quality` | **administrator** | Corpus-wide; `?min_reviewed=` |
| `GET` | `/documents/<id>/quality` | view | Scoped to one document |

Corpus figures aggregate across documents the caller may not be able to read,
so they are administrator-only. A per-document report needs only `view` on that
document.

```json
{ "readiness": { "state": "measured",
                 "note": "67% of instances reviewed; 59% were accepted exactly as extracted." },
  "by_confidence": [ { "confidence_label": "named", "n_reviewed": 16, "accuracy": 0.812 } ],
  "calibration": { "verdict": "monotonic" },
  "concept_precision": [ { "concept_id": "open_ended_term", "precision": 0.125 } ] }
```

`state` is `unmeasured` when nothing has been reviewed, `insufficient_review`
below 20% coverage, and `measured` above it. Every rate covers reviewed rows
only — see [Provenance and amendment](provenance-and-amendment.md#measuring-extraction-quality).

### Administration

| Method | Path | Permission | Body |
|---|---|---|---|
| `GET` | `/audit/llm` | administrator | `?document_id=`, `?tier=` |
| `POST` | `/admin/settings` | administrator | `key`, `value` |
| `POST` | `/admin/concepts/setup` | administrator | — |

`cloud_ai_policy` is validated against `disabled` / `per_user` / `org_allow`; an
unrecognised value is rejected rather than stored, so the gate can never be left
in a state it cannot interpret.

---

## A full session

```bash
TOKEN=...; B=http://localhost:8000

DOC=$(curl -s -X POST $B/documents -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' -d '{"path":"/srv/in/contract.pdf"}' \
      | jq -r .document_id)

curl -s -X POST $B/documents/$DOC/classify -H "Authorization: Bearer $TOKEN"
curl -s -X POST $B/documents/$DOC/extract  -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"tier":"local"}'

curl -s -X POST $B/documents/$DOC/concepts/evaluate -H "Authorization: Bearer $TOKEN"
curl -s     $B/documents/$DOC/instances -H "Authorization: Bearer $TOKEN" | jq '.[0]'

curl -s -X POST $B/instances/$IID/confirm -H "Authorization: Bearer $TOKEN"
curl -s -X POST $B/documents/$DOC/review  -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"reviewed":true}'
```

---

[← Back to index](index.md) | [Next: Deployment →](deployment.md)
