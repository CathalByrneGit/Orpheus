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
    {
        "version": 4,
        "name": "provenance_grounding",
        "statements": [
            # Where the excerpt was found, and how well it matched.
            #
            # The confidence column already carried the *consequence* of
            # grounding -- a quotation the document does not contain lands at
            # `inferred`. It did not carry the *cause*, so "0.5 because the
            # model could not be located" and "0.5 because the model said so"
            # were indistinguishable afterwards. Separating them is the whole
            # question a corpus run exists to answer: how often does this model
            # invent a quotation?
            #
            # NULL alignment means ungrounded, which is align.py's own
            # vocabulary rather than a new one.
            "ALTER TABLE provenance ADD COLUMN alignment TEXT",
            # The exact span, not a phrase to go looking for. A reading UI
            # highlights from these; searching for the excerpt string again
            # finds the wrong occurrence whenever a document repeats itself.
            "ALTER TABLE provenance ADD COLUMN char_start INTEGER",
            "ALTER TABLE provenance ADD COLUMN char_end INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_provenance_alignment "
            "ON provenance (alignment)",
        ],
    },
    {
        "version": 5,
        "name": "recompute_naive_keys",
        # `naive_key` stripped "group" and "holdings" anywhere in a name, and
        # they are not legal forms -- they are name components, and they denote
        # a *different legal entity* in a corporate structure. "Kestrel Medical
        # Group" and "Kestrel Medical Ltd" therefore shared a key, as did
        # "Ardmore Holdings plc" and "Ardmore Ltd".
        #
        # A false merge is worse than the false split this function is
        # documented as having. A split leaves two rows a person can join; a
        # merge combines two organisations and leaves nothing to notice. Stored
        # keys are recomputed here so an existing store stops matching on the
        # old basis rather than carrying it forward invisibly.
        "run": lambda store: _recompute_naive_keys(store),
    },
    {
        "version": 6,
        "name": "entities",
        "statements": [
            # An entity is the thing itself; an instance row is one *mention* of
            # it in one document. Everything before this was mentions only, and
            # two documents naming one company were two rows joined by a key
            # computed from the spelling.
            #
            # The split is what turns the store into something reusable. A wiki
            # page for a company is a projection of this table plus its
            # mentions, so every line on it points at a document, a page and an
            # excerpt, and says whether a person has checked it. A page of
            # uncited assertions is worth nothing downstream; this one cannot
            # be written.
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id      TEXT PRIMARY KEY,
                type_id        TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                naive_key      TEXT,
                description    TEXT,
                source         TEXT NOT NULL,
                confidence     REAL NOT NULL,
                status         TEXT NOT NULL DEFAULT 'unconfirmed',
                -- Set when this entity was merged into another. The row stays,
                -- so a link made before the merge still resolves and the merge
                -- itself can be read back.
                merged_into    TEXT REFERENCES entities(entity_id),
                created_at     TEXT NOT NULL,
                created_by     TEXT REFERENCES actors(actor_id),
                amended_by     TEXT REFERENCES actors(actor_id),
                amended_at     TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_entities_key ON entities (naive_key)",
            "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (type_id)",
            "CREATE INDEX IF NOT EXISTS idx_entities_merged ON entities (merged_into)",

            # Which mentions belong to which entity, and on what grounds.
            # `basis` is the evidence for the link, not a confidence score:
            # `identifier` is a stated registration number matching exactly,
            # `naive_key` is a normalised name, `human` is a person saying so.
            # They are not interchangeable and the difference has to survive.
            """
            CREATE TABLE IF NOT EXISTS entity_mentions (
                entity_id   TEXT NOT NULL REFERENCES entities(entity_id),
                instance_id TEXT NOT NULL,
                document_id TEXT REFERENCES documents(document_id),
                basis       TEXT NOT NULL,
                confidence  REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'unconfirmed',
                linked_by   TEXT REFERENCES actors(actor_id),
                linked_at   TEXT NOT NULL,
                -- Unlinking is not deletion. A link a person removed is
                -- evidence about how well matching works, which is the same
                -- reason a rejected instance is kept.
                unlinked_at TEXT,
                unlinked_by TEXT REFERENCES actors(actor_id),
                note        TEXT,
                PRIMARY KEY (entity_id, instance_id)
            )
            """,
            # One mention belongs to at most one entity at a time. Enforced by
            # the database rather than by convention, because a mention on two
            # wiki pages is two pages claiming the same evidence and nothing
            # would notice. Partial, so an unlinked row does not block a relink.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_mentions_one_home "
            "ON entity_mentions (instance_id) WHERE unlinked_at IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity "
            "ON entity_mentions (entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_entity_mentions_doc "
            "ON entity_mentions (document_id)",
        ],
    },
    {
        "version": 7,
        "name": "tensions",
        "statements": [
            # Every review verb in the store so far resolves *towards*
            # agreement: confirm, amend, reject. That is the right shape for
            # "was the machine right", and the wrong shape for "these two
            # documents disagree and both are correct". Without somewhere to
            # put the second, an entity page renders two confirmed mentions
            # that contradict each other side by side, in the same voice, and
            # reads as though they agree. The disagreement is usually the part
            # worth knowing.
            #
            # A tension is not uncertainty -- `confidence` is already the
            # uncertainty axis, five levels of it. A tension is a conflict
            # somebody *verified*. So `accepted` is a perfectly good place for
            # one to stay: it means a person looked, and the conflict is real.
            """
            CREATE TABLE IF NOT EXISTS tensions (
                tension_id   TEXT PRIMARY KEY,
                scope        TEXT NOT NULL,
                subject_id   TEXT,
                kind         TEXT NOT NULL,
                property_id  TEXT,
                summary      TEXT NOT NULL,
                detail       TEXT,
                status       TEXT NOT NULL DEFAULT 'open',
                resolution   TEXT,
                source       TEXT NOT NULL,
                confidence   REAL NOT NULL,
                raised_by    TEXT REFERENCES actors(actor_id),
                raised_at    TEXT NOT NULL,
                settled_by   TEXT REFERENCES actors(actor_id),
                settled_at   TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tensions_subject "
            "ON tensions (scope, subject_id)",
            "CREATE INDEX IF NOT EXISTS idx_tensions_status ON tensions (status)",

            # The sides of the argument, each an instance that carries
            # provenance. Two of them at minimum, enforced in `tensions.py`.
            #
            # This is the same rule as the entity page: a claim with no mention
            # behind it cannot be written. Without it a tension is an opinion,
            # and a store full of unfalsifiable opinions is exactly what the
            # provenance model exists to prevent.
            """
            CREATE TABLE IF NOT EXISTS tension_sides (
                tension_id  TEXT NOT NULL REFERENCES tensions(tension_id),
                instance_id TEXT NOT NULL,
                document_id TEXT REFERENCES documents(document_id),
                position    TEXT,
                PRIMARY KEY (tension_id, instance_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tension_sides_instance "
            "ON tension_sides (instance_id)",
        ],
    },
]


def _recompute_naive_keys(store) -> int:
    """Rewrite every stored `naive_key` with the current function.

    Which tables carry the column is a bundle question, so it is read from the
    schema rather than assumed. On a store with no bundle applied yet there are
    none, and this is correctly a no-op.
    """
    from .utils import naive_key, new_id, now, to_json

    tables = [row[0] for row in store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'instances_%'")]

    changed = 0
    for table in tables:
        columns = {row[1] for row in store.execute(f'PRAGMA table_info("{table}")')}
        if not {"naive_key", "name"} <= columns:
            continue
        for row in store.query(
                f'SELECT instance_id, name, naive_key FROM "{table}" '
                "WHERE name IS NOT NULL AND name != ''"):
            fresh = naive_key(row["name"])
            if fresh == row["naive_key"]:
                continue
            store.execute(f'UPDATE "{table}" SET naive_key = ? WHERE instance_id = ?',
                          (fresh, row["instance_id"]))
            changed += 1

    if changed:
        # Corpus matching ran on the old keys, so any comparison built on it is
        # now out of date. Silently stale is worse than visibly stale, which is
        # the whole reason the staleness machinery exists.
        store.execute(
            "UPDATE concept_evaluations SET stale = 1, stale_reason = ? "
            "WHERE kind = 'corpus' AND COALESCE(stale, 0) = 0",
            ("Name matching keys were recomputed; corpus matches need re-running.",))
        store.execute(
            "INSERT INTO edit_history (id, table_name, row_id, document_id, action, "
            "previous_value, new_value, edited_by, edited_at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id("edit"), "instances_*", "naive_key", None, "migrate", None,
             to_json({"rows_changed": changed, "migration": 5}), None, now(),
             "Legal-form suffixes are now stripped only where they trail, so a "
             "holding company no longer shares a key with its subsidiary."))
    return changed
