"""The schema, as an ordered list of migrations.

Ported from the R implementation statement for statement, so a store created
by either reads correctly under the other. The migration table records what
has run, so an existing store upgrades in place rather than being rebuilt.

Every table here is deliberate; the ones worth knowing before reading the rest
of the package are `provenance` (the immutable record of what the machine
said), `edit_history` (append-only, ordered by a monotonic seq and never by
time), and `instance_index` (the cross-type lookup the per-type instance
tables hang off).
"""

from __future__ import annotations

MIGRATIONS: list[dict] = [
    {
        "version": 1,
        "name": 'core',
        "statements": [
            """CREATE TABLE IF NOT EXISTS actors (
    actor_id         TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    email            TEXT,
    idp              TEXT,
    external_id      TEXT,
    departments_json TEXT,
    is_admin         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    disabled_at      TEXT
    )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_actors_external
    ON actors (idp, external_id) WHERE external_id IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS actor_tokens (
    token_id   TEXT PRIMARY KEY,
    actor_id   TEXT NOT NULL REFERENCES actors(actor_id),
    token_hash TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
    )""",
            """CREATE INDEX IF NOT EXISTS idx_tokens_actor ON actor_tokens (actor_id)""",
            """CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    mime_type       TEXT,
    byte_size       INTEGER,
    storage_path    TEXT,
    n_pages         INTEGER,
    text_source     TEXT,
    doc_type        TEXT,
    sector          TEXT,
    jurisdiction    TEXT,
    classification_source     TEXT,
    classification_confidence REAL,
    classification_status     TEXT,
    date_added      TEXT NOT NULL,
    created_by      TEXT REFERENCES actors(actor_id),
    visibility      TEXT NOT NULL DEFAULT 'private',
    review_status   TEXT NOT NULL DEFAULT 'unreviewed',
    reviewed_by     TEXT REFERENCES actors(actor_id),
    reviewed_at     TEXT
    )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents (file_hash)""",
            """CREATE TABLE IF NOT EXISTS document_pages (
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    page_no     INTEGER NOT NULL,
    text        TEXT,
    text_source TEXT,
    image_path  TEXT,
    char_count  INTEGER,
    PRIMARY KEY (document_id, page_no)
    )""",
            """CREATE TABLE IF NOT EXISTS document_shares (
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    actor_id    TEXT NOT NULL REFERENCES actors(actor_id),
    role        TEXT NOT NULL,
    granted_by  TEXT REFERENCES actors(actor_id),
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (document_id, actor_id)
    )""",
            """CREATE TABLE IF NOT EXISTS bundles (
    bundle_id      TEXT NOT NULL,
    bundle_version TEXT NOT NULL,
    bundle_json    TEXT NOT NULL,
    stage          TEXT NOT NULL DEFAULT 'production',
    created_at     TEXT NOT NULL,
    created_by     TEXT REFERENCES actors(actor_id),
    is_active      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bundle_id, bundle_version)
    )""",
            """CREATE TABLE IF NOT EXISTS instance_index (
    instance_id TEXT PRIMARY KEY,
    type_id     TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    document_id TEXT REFERENCES documents(document_id),
    created_at  TEXT NOT NULL
    )""",
            """CREATE INDEX IF NOT EXISTS idx_instance_index_doc ON instance_index (document_id)""",
            """CREATE INDEX IF NOT EXISTS idx_instance_index_type ON instance_index (type_id)""",
            """CREATE TABLE IF NOT EXISTS edges (
    edge_id          TEXT PRIMARY KEY,
    from_instance_id TEXT NOT NULL,
    to_instance_id   TEXT NOT NULL,
    link_type_id     TEXT NOT NULL,
    document_id      TEXT REFERENCES documents(document_id),
    evidence         TEXT,
    source           TEXT NOT NULL,
    confidence       REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'unconfirmed',
    amended_by       TEXT REFERENCES actors(actor_id),
    amended_at       TEXT,
    created_at       TEXT NOT NULL
    )""",
            """CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_instance_id)""",
            """CREATE INDEX IF NOT EXISTS idx_edges_to ON edges (to_instance_id)""",
            """CREATE INDEX IF NOT EXISTS idx_edges_doc ON edges (document_id)""",
            """CREATE TABLE IF NOT EXISTS provenance (
    provenance_id TEXT PRIMARY KEY,
    instance_id   TEXT NOT NULL,
    document_id   TEXT REFERENCES documents(document_id),
    source_label  TEXT,
    page_no       INTEGER,
    excerpt       TEXT,
    confidence    REAL,
    created_at    TEXT NOT NULL
    )""",
            """CREATE INDEX IF NOT EXISTS idx_provenance_instance ON provenance (instance_id)""",
            """CREATE TABLE IF NOT EXISTS concept_evaluations (
    evaluation_id       TEXT PRIMARY KEY,
    concept_id          TEXT NOT NULL,
    concept_version     INTEGER,
    concept_scope       TEXT,
    kind                TEXT NOT NULL,
    scope               TEXT NOT NULL,
    target_document_id  TEXT REFERENCES documents(document_id),
    result              TEXT,
    corpus_context_used TEXT,
    resolution_quality  TEXT,
    source              TEXT NOT NULL,
    confidence          REAL,
    status              TEXT NOT NULL DEFAULT 'unconfirmed',
    amended_by          TEXT REFERENCES actors(actor_id),
    amended_at          TEXT,
    generated_at        TEXT NOT NULL,
    generated_by        TEXT REFERENCES actors(actor_id),
    stale               INTEGER NOT NULL DEFAULT 0,
    stale_reason        TEXT
    )""",
            """CREATE INDEX IF NOT EXISTS idx_evals_doc ON concept_evaluations (target_document_id)""",
            """CREATE TABLE IF NOT EXISTS concept_evaluation_dependencies (
    evaluation_id TEXT NOT NULL REFERENCES concept_evaluations(evaluation_id),
    instance_id   TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, instance_id)
    )""",
            """CREATE INDEX IF NOT EXISTS idx_eval_deps_instance
    ON concept_evaluation_dependencies (instance_id)""",
            """CREATE TABLE IF NOT EXISTS edit_history (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    table_name     TEXT NOT NULL,
    row_id         TEXT NOT NULL,
    document_id    TEXT,
    action         TEXT NOT NULL,
    previous_value TEXT,
    new_value      TEXT,
    edited_by      TEXT REFERENCES actors(actor_id),
    edited_at      TEXT NOT NULL,
    note           TEXT
    )""",
            """CREATE INDEX IF NOT EXISTS idx_edit_history_row ON edit_history (table_name, row_id)""",
            """CREATE INDEX IF NOT EXISTS idx_edit_history_doc ON edit_history (document_id)""",
            """CREATE TABLE IF NOT EXISTS schema_amendments (
    amendment_id   TEXT PRIMARY KEY,
    document_id    TEXT REFERENCES documents(document_id),
    amendment_type TEXT NOT NULL,
    type_id        TEXT,
    property_id    TEXT,
    observed_value TEXT,
    inferred_type  TEXT,
    rationale      TEXT,
    occurrences    INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'pending',
    proposed_at    TEXT NOT NULL,
    reviewed_by    TEXT REFERENCES actors(actor_id),
    reviewed_at    TEXT,
    review_note    TEXT
    )""",
            """CREATE INDEX IF NOT EXISTS idx_schema_amendments_status
    ON schema_amendments (status)""",
            """CREATE TABLE IF NOT EXISTS extraction_runs (
    run_id       TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(document_id),
    tier         TEXT NOT NULL,
    actor_id     TEXT REFERENCES actors(actor_id),
    bundle_id    TEXT,
    bundle_version TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    n_entities   INTEGER DEFAULT 0,
    n_edges      INTEGER DEFAULT 0,
    n_amendments INTEGER DEFAULT 0,
    error        TEXT
    )""",
            """CREATE TABLE IF NOT EXISTS llm_calls (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL UNIQUE,
    document_id  TEXT REFERENCES documents(document_id),
    actor_id     TEXT REFERENCES actors(actor_id),
    tier         TEXT NOT NULL,
    provider     TEXT,
    model        TEXT,
    purpose      TEXT,
    prompt_chars INTEGER,
    excerpt_only INTEGER,
    payload_digest TEXT,
    created_at   TEXT NOT NULL,
    error        TEXT
    )""",
            """CREATE INDEX IF NOT EXISTS idx_llm_calls_doc ON llm_calls (document_id)""",
            """CREATE TABLE IF NOT EXISTS org_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_by TEXT REFERENCES actors(actor_id),
    updated_at TEXT
    )""",
        ],
    },
    {
        "version": 2,
        "name": 'provenance_source',
        "statements": [
            """ALTER TABLE provenance ADD COLUMN source TEXT""",
        ],
    },
    {
        "version": 3,
        "name": "concepts_native",
        # These five tables were created by conceptR, an external R package, and
        # so were absent from the R migrations even though the store depended on
        # them. In Python there is no conceptR: Orpheus owns the versioned-concept
        # machinery, which means it also owns the schema. IF NOT EXISTS keeps a
        # store that conceptR already built upgradeable in place.
        #
        # One thing changed in the move. conceptR's
        # cpt_add_score_component(version = NULL) documented its default as "use
        # whichever version is active at evaluation time", but version is NOT NULL
        # and part of the primary key, so that default could never insert. Here a
        # component always pins a concrete version, which is the better behaviour
        # anyway: a score records which concept version produced it.
        "statements": [
            """CREATE TABLE IF NOT EXISTS concept_definitions (
    concept_id      TEXT    NOT NULL,
    object_type_id  TEXT    NOT NULL,
    description     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (concept_id)
    )""",
            """CREATE TABLE IF NOT EXISTS concept_versions (
    concept_id            TEXT    NOT NULL,
    scope                 TEXT    NOT NULL,
    version               INTEGER NOT NULL,
    sql_expr              TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'draft',
    stage                 TEXT    NOT NULL DEFAULT 'development',
    rationale             TEXT,
    source_standard       TEXT,
    template_id           TEXT,
    parameter_values_json TEXT,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at          TIMESTAMP,
    deprecated_at         TIMESTAMP,
    PRIMARY KEY (concept_id, scope, version)
    )""",
            """CREATE TABLE IF NOT EXISTS concept_templates (
    template_id     TEXT    NOT NULL,
    object_type_id  TEXT    NOT NULL,
    base_sql_expr   TEXT    NOT NULL,
    parameters_json TEXT    NOT NULL,
    source_standard TEXT,
    description     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (template_id)
    )""",
            """CREATE TABLE IF NOT EXISTS composite_scores (
    score_id        TEXT    NOT NULL,
    object_type_id  TEXT    NOT NULL,
    description     TEXT,
    aggregation     TEXT    NOT NULL DEFAULT 'weighted_sum',
    thresholds_json TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (score_id)
    )""",
            """CREATE TABLE IF NOT EXISTS composite_score_components (
    score_id    TEXT    NOT NULL,
    concept_id  TEXT    NOT NULL,
    scope       TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    weight      REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (score_id, concept_id, scope, version)
    )""",
            """CREATE INDEX IF NOT EXISTS idx_concept_versions_active
    ON concept_versions (concept_id, scope, status)""",
        ],
    },
]
