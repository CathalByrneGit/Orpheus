# ---------------------------------------------------------------------------
# Storage: SQLite, used natively.
#
# Two deliberate constraints from the architecture, enforced here rather than
# left to convention:
#
#   1. Single writer. Every write -- extraction, human amendment, concept
#      evaluation -- funnels through one process. SQLite permits exactly one
#      writer, and with multiple concurrent users that stops being a
#      theoretical limit, so the constraint is made explicit and loud instead
#      of being discovered under load.
#   2. WAL mode from the start, so readers (Datasette, the API's own read
#      connections) are not blocked by the writer.
# ---------------------------------------------------------------------------

#' Path of the advisory writer lock for a database
#' @keywords internal
writer_lock_path <- function(path) paste0(path, ".writer.lock")

#' Acquire the advisory single-writer lock
#'
#' Creates a lock file next to the database recording the owning process. A
#' second writer fails here with a clear message rather than silently becoming
#' a second writer.
#'
#' @param path Database path.
#' @param force Take the lock even if a lock file exists but its process is
#'   gone (a crashed writer).
#' @keywords internal
acquire_writer_lock <- function(path, force = FALSE) {
  lock <- writer_lock_path(path)
  if (file.exists(lock)) {
    holder <- tryCatch(from_json(paste(readLines(lock, warn = FALSE), collapse = "")),
                       error = function(e) NULL)
    stale <- is.null(holder) || !process_alive(holder$pid)
    if (!stale && !identical(holder$pid, Sys.getpid())) {
      cli::cli_abort(c(
        "The single-writer lock on {.path {path}} is held by pid {holder$pid}.",
        i = "Only one process may write to the store. Point this process at the
             Plumber API instead of opening a second writer.",
        i = "If that process is gone, re-open with {.code force = TRUE}."
      ))
    }
    if (stale && !force) {
      cli::cli_abort(c(
        "A stale writer lock from pid {holder$pid %||% NA} remains on {.path {path}}.",
        i = "The owning process is not running. Re-open with {.code force = TRUE} to take over."
      ))
    }
  }
  writeLines(as.character(to_json(list(pid = Sys.getpid(), acquired_at = orph_now()))), lock)
  invisible(lock)
}

#' @keywords internal
process_alive <- function(pid) {
  if (is.null(pid) || is.na(pid)) return(FALSE)
  dir.exists(file.path("/proc", as.character(pid)))
}

#' @keywords internal
release_writer_lock <- function(path) {
  lock <- writer_lock_path(path)
  if (file.exists(lock)) {
    holder <- tryCatch(from_json(paste(readLines(lock, warn = FALSE), collapse = "")),
                       error = function(e) NULL)
    if (is.null(holder) || identical(holder$pid, Sys.getpid())) unlink(lock)
  }
  invisible(TRUE)
}

#' Open a connection to the Orpheus store
#'
#' @param path Path to the SQLite database file.
#' @param mode `"write"` acquires the single-writer lock and applies
#'   migrations; `"read"` opens a read-only connection and never writes.
#' @param force_lock Take over a stale writer lock left by a crashed process.
#' @param migrate Apply pending migrations (write mode only).
#' @return A `DBIConnection` carrying the mode in its attributes.
#' @export
orph_connect <- function(path, mode = c("write", "read"),
                         force_lock = FALSE, migrate = TRUE) {
  mode <- match.arg(mode)
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)

  if (mode == "write") {
    acquire_writer_lock(path, force = force_lock)
    con <- DBI::dbConnect(RSQLite::SQLite(), path)
    # From here to the end of migration, a failure must not leave the
    # connection open and the writer lock held -- that would lock every later
    # attempt out of a database nobody is actually using.
    ok <- FALSE
    on.exit(if (!ok) {
      try(DBI::dbDisconnect(con), silent = TRUE)
      release_writer_lock(path)
    }, add = TRUE)
    DBI::dbExecute(con, "PRAGMA journal_mode = WAL")
    DBI::dbExecute(con, "PRAGMA foreign_keys = ON")
    DBI::dbExecute(con, "PRAGMA busy_timeout = 5000")
    DBI::dbExecute(con, "PRAGMA synchronous = NORMAL")
  } else {
    if (!file.exists(path)) cli::cli_abort("No database at {.path {path}}.")
    con <- DBI::dbConnect(RSQLite::SQLite(), path, flags = RSQLite::SQLITE_RO)
    DBI::dbExecute(con, "PRAGMA busy_timeout = 5000")
  }

  # Set before migrating: assert_writable() reads these attributes, so a
  # connection is not usable until it is labelled.
  attr(con, "orph_mode") <- mode
  attr(con, "orph_path") <- path

  if (mode == "write" && migrate) orph_migrate(con)
  if (mode == "write") ok <- TRUE
  con
}

#' Close a connection, releasing the writer lock if held
#' @param con A connection from [orph_connect()].
#' @export
orph_disconnect <- function(con) {
  path <- attr(con, "orph_path")
  mode <- attr(con, "orph_mode")
  DBI::dbDisconnect(con)
  if (identical(mode, "write") && !is.null(path)) release_writer_lock(path)
  invisible(TRUE)
}

#' Assert that a connection may write
#'
#' Called at the top of every mutating function. Without this, a read
#' connection would fail deep inside a statement with an opaque SQLite error.
#'
#' @param con A connection.
#' @keywords internal
assert_writable <- function(con) {
  if (!identical(attr(con, "orph_mode"), "write")) {
    cli::cli_abort(c(
      "This connection is read-only.",
      i = "All writes go through the single writer -- use the Plumber API."
    ))
  }
  invisible(con)
}

#' Run a function inside a transaction
#' @param con A writable connection.
#' @param expr Expression to evaluate.
#' @keywords internal
with_tx <- function(con, expr) {
  assert_writable(con)
  key <- paste0("tx_", attr(con, "orph_path") %||% "default")

  # Re-entrant. Higher-level operations compose lower-level ones -- accepting a
  # schema amendment registers a bundle, which is itself transactional -- and a
  # nested dbBegin() is an error in SQLite. The outermost call owns the
  # transaction; inner calls join it, so the whole operation still commits or
  # rolls back as one unit.
  depth <- (orph_env[[key]] %||% 0L)
  if (depth > 0L) {
    orph_env[[key]] <- depth + 1L
    on.exit(orph_env[[key]] <- orph_env[[key]] - 1L, add = TRUE)
    return(force(expr))
  }

  DBI::dbBegin(con)
  orph_env[[key]] <- 1L
  ok <- FALSE
  on.exit({
    orph_env[[key]] <- 0L
    if (!ok) try(DBI::dbRollback(con), silent = TRUE)
  }, add = TRUE)
  result <- force(expr)
  DBI::dbCommit(con)
  ok <- TRUE
  result
}

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

#' The migration set
#'
#' A list of `list(version=, name=, statements=)`. Migrations are applied in
#' version order and recorded, so an existing store upgrades in place.
#'
#' @keywords internal
orph_migrations <- function() {
  list(
    list(
      version = 1L,
      name    = "core",
      statements = c(
        # --- identity -------------------------------------------------------
        "CREATE TABLE IF NOT EXISTS actors (
           actor_id         TEXT PRIMARY KEY,
           display_name     TEXT NOT NULL,
           email            TEXT,
           idp              TEXT,
           external_id      TEXT,
           departments_json TEXT,
           is_admin         INTEGER NOT NULL DEFAULT 0,
           created_at       TEXT NOT NULL,
           disabled_at      TEXT
         )",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_actors_external
           ON actors (idp, external_id) WHERE external_id IS NOT NULL",

        "CREATE TABLE IF NOT EXISTS actor_tokens (
           token_id   TEXT PRIMARY KEY,
           actor_id   TEXT NOT NULL REFERENCES actors(actor_id),
           token_hash TEXT NOT NULL UNIQUE,
           label      TEXT,
           created_at TEXT NOT NULL,
           expires_at TEXT,
           revoked_at TEXT
         )",
        "CREATE INDEX IF NOT EXISTS idx_tokens_actor ON actor_tokens (actor_id)",

        # --- documents ------------------------------------------------------
        "CREATE TABLE IF NOT EXISTS documents (
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
         )",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents (file_hash)",

        "CREATE TABLE IF NOT EXISTS document_pages (
           document_id TEXT NOT NULL REFERENCES documents(document_id),
           page_no     INTEGER NOT NULL,
           text        TEXT,
           text_source TEXT,
           image_path  TEXT,
           char_count  INTEGER,
           PRIMARY KEY (document_id, page_no)
         )",

        "CREATE TABLE IF NOT EXISTS document_shares (
           document_id TEXT NOT NULL REFERENCES documents(document_id),
           actor_id    TEXT NOT NULL REFERENCES actors(actor_id),
           role        TEXT NOT NULL,
           granted_by  TEXT REFERENCES actors(actor_id),
           granted_at  TEXT NOT NULL,
           PRIMARY KEY (document_id, actor_id)
         )",

        # --- ontology bundles -----------------------------------------------
        "CREATE TABLE IF NOT EXISTS bundles (
           bundle_id      TEXT NOT NULL,
           bundle_version TEXT NOT NULL,
           bundle_json    TEXT NOT NULL,
           stage          TEXT NOT NULL DEFAULT 'production',
           created_at     TEXT NOT NULL,
           created_by     TEXT REFERENCES actors(actor_id),
           is_active      INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY (bundle_id, bundle_version)
         )",

        # --- instances ------------------------------------------------------
        # Per-object-type tables are created from the bundle (see schema.R).
        # This index is the type-agnostic handle on them: it makes 'find this
        # instance' a single lookup instead of a scan across every type table,
        # which the amendment, provenance and staleness paths all need.
        "CREATE TABLE IF NOT EXISTS instance_index (
           instance_id TEXT PRIMARY KEY,
           type_id     TEXT NOT NULL,
           table_name  TEXT NOT NULL,
           document_id TEXT REFERENCES documents(document_id),
           created_at  TEXT NOT NULL
         )",
        "CREATE INDEX IF NOT EXISTS idx_instance_index_doc ON instance_index (document_id)",
        "CREATE INDEX IF NOT EXISTS idx_instance_index_type ON instance_index (type_id)",

        "CREATE TABLE IF NOT EXISTS edges (
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
         )",
        "CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_instance_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_to ON edges (to_instance_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_doc ON edges (document_id)",

        "CREATE TABLE IF NOT EXISTS provenance (
           provenance_id TEXT PRIMARY KEY,
           instance_id   TEXT NOT NULL,
           document_id   TEXT REFERENCES documents(document_id),
           source_label  TEXT,
           page_no       INTEGER,
           excerpt       TEXT,
           confidence    REAL,
           created_at    TEXT NOT NULL
         )",
        "CREATE INDEX IF NOT EXISTS idx_provenance_instance ON provenance (instance_id)",

        # --- interpretation --------------------------------------------------
        "CREATE TABLE IF NOT EXISTS concept_evaluations (
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
         )",
        "CREATE INDEX IF NOT EXISTS idx_evals_doc ON concept_evaluations (target_document_id)",

        # Lineage: which instances an evaluation actually read. Without this,
        # 'stale' could only ever be set by hand; with it, amending an instance
        # marks every evaluation that depended on it.
        "CREATE TABLE IF NOT EXISTS concept_evaluation_dependencies (
           evaluation_id TEXT NOT NULL REFERENCES concept_evaluations(evaluation_id),
           instance_id   TEXT NOT NULL,
           PRIMARY KEY (evaluation_id, instance_id)
         )",
        "CREATE INDEX IF NOT EXISTS idx_eval_deps_instance
           ON concept_evaluation_dependencies (instance_id)",

        # --- audit -----------------------------------------------------------
        # seq, not the id, defines audit order. Two changes inside one
        # transaction share a timestamp to the second, and ordering the audit
        # trail by a random identifier would put them in an arbitrary order --
        # which is precisely the question an audit trail exists to answer.
        "CREATE TABLE IF NOT EXISTS edit_history (
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
         )",
        "CREATE INDEX IF NOT EXISTS idx_edit_history_row ON edit_history (table_name, row_id)",
        "CREATE INDEX IF NOT EXISTS idx_edit_history_doc ON edit_history (document_id)",

        "CREATE TABLE IF NOT EXISTS schema_amendments (
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
         )",
        "CREATE INDEX IF NOT EXISTS idx_schema_amendments_status
           ON schema_amendments (status)",

        "CREATE TABLE IF NOT EXISTS extraction_runs (
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
         )",

        # Every cloud call is recorded. The opt-in is only meaningful if it is
        # auditable after the fact -- who sent what, of which document, when.
        "CREATE TABLE IF NOT EXISTS llm_calls (
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
         )",
        "CREATE INDEX IF NOT EXISTS idx_llm_calls_doc ON llm_calls (document_id)",

        "CREATE TABLE IF NOT EXISTS org_settings (
           key        TEXT PRIMARY KEY,
           value      TEXT,
           updated_by TEXT REFERENCES actors(actor_id),
           updated_at TEXT
         )"
      )
    ),

    list(
      version = 2L,
      name    = "provenance_source",
      statements = c(
        # provenance already held the confidence the machine assigned. It needs
        # the tier too: amending an instance overwrites `source` on the
        # instance row to 'human' (correctly -- it is ground truth now), which
        # means the instance table can no longer say which tier originally
        # produced it. Without this column, extraction quality cannot be
        # attributed to local or cloud once anyone starts correcting things.
        "ALTER TABLE provenance ADD COLUMN source TEXT"
      )
    )
  )
}

#' Apply pending migrations
#' @param con A writable connection.
#' @return Invisibly, the versions applied.
#' @export
orph_migrate <- function(con) {
  assert_writable(con)
  DBI::dbExecute(con, "CREATE TABLE IF NOT EXISTS schema_migrations (
     version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
  applied <- DBI::dbGetQuery(con, "SELECT version FROM schema_migrations")$version
  pending <- Filter(function(m) !(m$version %in% applied), orph_migrations())
  for (m in pending) {
    with_tx(con, {
      for (s in m$statements) DBI::dbExecute(con, s)
      DBI::dbExecute(con,
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
        params = list(m$version, m$name, orph_now()))
    })
  }
  invisible(vapply(pending, function(m) m$version, integer(1)))
}

# ---------------------------------------------------------------------------
# Small helpers over DBI
# ---------------------------------------------------------------------------

#' @keywords internal
db_insert <- function(con, table, values) {
  assert_writable(con)
  cols <- names(values)
  sql <- sprintf("INSERT INTO %s (%s) VALUES (%s)",
                 DBI::dbQuoteIdentifier(con, table),
                 paste(vapply(cols, function(c) as.character(DBI::dbQuoteIdentifier(con, c)),
                              character(1)), collapse = ", "),
                 paste(rep("?", length(cols)), collapse = ", "))
  DBI::dbExecute(con, sql, params = unname(lapply(values, nullable)))
  invisible(TRUE)
}

#' @keywords internal
nullable <- function(x) if (is.null(x) || length(x) == 0) NA else x

#' @keywords internal
db_get_one <- function(con, sql, params = list()) {
  df <- db_query(con, sql, params)
  if (nrow(df) == 0) NULL else as.list(df[1, , drop = FALSE])
}

#' @keywords internal
db_query <- function(con, sql, params = list()) {
  # RSQLite errors on an empty params list rather than ignoring it, so the
  # parameterless case has to be dispatched separately.
  if (length(params) == 0) DBI::dbGetQuery(con, sql)
  else DBI::dbGetQuery(con, sql, params = params)
}

#' Checkpoint the write-ahead log
#'
#' Folds committed WAL pages back into the main database file.
#'
#' This is not housekeeping -- it is load-bearing for the read path. With WAL
#' enabled, committed data lives in the `-wal` sidecar until a checkpoint. A
#' reader that opens the database with SQLite's `immutable=1` flag (which is
#' what `datasette --immutable` does) skips the WAL entirely and therefore sees
#' the database as of the last checkpoint -- on a freshly written store, that
#' means it sees nothing at all. Checkpointing after each write keeps every
#' reader current and stops the WAL growing without bound.
#'
#' @param con A writable connection.
#' @param mode `"PASSIVE"` never blocks; `"TRUNCATE"` also resets the WAL file
#'   and is worth paying for after a batch of writes.
#' @return Invisibly, the checkpoint result row.
#' @export
orph_checkpoint <- function(con, mode = c("PASSIVE", "TRUNCATE", "FULL", "RESTART")) {
  assert_writable(con)
  mode <- match.arg(mode)
  invisible(tryCatch(
    DBI::dbGetQuery(con, sprintf("PRAGMA wal_checkpoint(%s)", mode)),
    error = function(e) NULL))
}

#' Read an org-level setting
#' @param con A connection.
#' @param key Setting key.
#' @param default Value returned when the key is unset.
#' @return The stored string, or `default`.
#' @export
orph_setting <- function(con, key, default = NULL) {
  row <- db_get_one(con, "SELECT value FROM org_settings WHERE key = ?", list(key))
  if (is.null(row) || is.na(row$value)) default else row$value
}

#' Write an org-level setting
#' @param con A writable connection.
#' @param key Setting key.
#' @param value Setting value.
#' @param actor_id Actor making the change.
#' @export
orph_set_setting <- function(con, key, value, actor_id = NULL) {
  assert_writable(con)
  DBI::dbExecute(con,
    "INSERT INTO org_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value,
       updated_by = excluded.updated_by, updated_at = excluded.updated_at",
    params = list(key, as.character(value), nullable(actor_id), orph_now()))
  invisible(TRUE)
}
