# ---------------------------------------------------------------------------
# The audit trail.
#
# SQLite has no storage-level time travel, so `edit_history` carries that job
# at the application level. That is a weaker guarantee than a snapshotting
# store would give -- a code path that forgets to log is a hole a storage-layer
# guarantee would not have -- so every mutating function in this package routes
# through record_edit(), and nothing writes to an instance table directly.
# ---------------------------------------------------------------------------

#' Append to the edit history
#'
#' Never called for its side effect alone: it is invoked inside the same
#' transaction as the change it describes, so a change and its audit row
#' commit or roll back together.
#'
#' @param con A writable connection.
#' @param table_name Table the change applies to.
#' @param row_id Primary key of the changed row.
#' @param document_id Document the row belongs to, for document-scoped audit views.
#' @param action What happened: `ingest`, `extract`, `confirm`, `amend`,
#'   `reject`, `evaluate`, `schema_amendment`, ...
#' @param previous Value before the change (list or `NULL`).
#' @param new Value after the change (list or `NULL`).
#' @param actor_id Actor responsible.
#' @param note Optional free text.
#' @return Invisibly, the history row id.
#' @keywords internal
record_edit <- function(con, table_name, row_id, document_id, action,
                        previous = NULL, new = NULL, actor_id = NULL, note = NULL) {
  assert_writable(con)
  id <- orph_id("edit")
  db_insert(con, "edit_history", list(
    id             = id,
    table_name     = table_name,
    row_id         = row_id,
    document_id    = nullable(document_id),
    action         = action,
    previous_value = to_json(previous),
    new_value      = to_json(new),
    edited_by      = nullable(actor_id),
    edited_at      = orph_now(),
    note           = nullable(note)
  ))
  invisible(id)
}

#' Read the edit history for a row
#'
#' @param con A connection.
#' @param table_name Table name.
#' @param row_id Row identifier.
#' @return A data frame, oldest first.
#' @export
orph_row_history <- function(con, table_name, row_id) {
  db_query(con,
    "SELECT seq, id, action, previous_value, new_value, edited_by, edited_at, note
     FROM edit_history WHERE table_name = ? AND row_id = ? ORDER BY seq",
    list(table_name, row_id))
}

#' Read the edit history for a document
#'
#' Everything that has happened to a document and to every instance extracted
#' from it, in one place -- the view an auditor asks for.
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @param limit Maximum rows.
#' @return A data frame, newest first.
#' @export
orph_document_history <- function(con, document_id, limit = 500L) {
  db_query(con,
    "SELECT seq, id, table_name, row_id, action, previous_value, new_value,
            edited_by, edited_at, note
     FROM edit_history WHERE document_id = ? ORDER BY seq DESC LIMIT ?",
    list(document_id, as.integer(limit)))
}

#' Record an LLM call
#'
#' Cloud processing is opt-in, and an opt-in nobody can audit afterwards is a
#' formality. Every call records who triggered it, against which document, how
#' much text went out, and a digest of the payload -- enough to answer "what
#' left the building" without storing the contract text a second time.
#'
#' @param con A writable connection.
#' @param tier `"local"` or `"cloud"`.
#' @param purpose What the call was for.
#' @param document_id Document the call concerned.
#' @param actor_id Actor who triggered it.
#' @param provider,model Provider and model identifiers.
#' @param payload Text sent, used for the size and digest only.
#' @param excerpt_only Whether only an excerpt was sent rather than the whole document.
#' @param error Error message, if the call failed.
#' @return Invisibly, the call id.
#' @keywords internal
record_llm_call <- function(con, tier, purpose, document_id = NULL, actor_id = NULL,
                            provider = NULL, model = NULL, payload = "",
                            excerpt_only = FALSE, error = NULL) {
  assert_writable(con)
  id <- orph_id("llm")
  db_insert(con, "llm_calls", list(
    call_id        = id,
    document_id    = nullable(document_id),
    actor_id       = nullable(actor_id),
    tier           = tier,
    provider       = nullable(provider),
    model          = nullable(model),
    purpose        = purpose,
    prompt_chars   = nchar(payload %||% ""),
    excerpt_only   = as.integer(isTRUE(excerpt_only)),
    payload_digest = digest::digest(payload %||% "", algo = "sha256"),
    created_at     = orph_now(),
    error          = nullable(error)
  ))
  invisible(id)
}

#' Read the cloud-processing audit log
#'
#' @param con A connection.
#' @param document_id Restrict to one document, or `NULL` for all.
#' @param tier Restrict to `"cloud"` or `"local"`, or `NULL` for both.
#' @param limit Maximum rows.
#' @return A data frame, newest first.
#' @export
orph_llm_audit <- function(con, document_id = NULL, tier = NULL, limit = 200L) {
  where <- character(); params <- list()
  if (!is.null(document_id)) { where <- c(where, "document_id = ?"); params <- c(params, document_id) }
  if (!is.null(tier))        { where <- c(where, "tier = ?");        params <- c(params, tier) }
  # Ordered by seq, not created_at: several calls in one request share a
  # timestamp to the second, and an audit log that reports them in an arbitrary
  # order cannot answer what happened first.
  sql <- paste0("SELECT * FROM llm_calls",
                if (length(where)) paste0(" WHERE ", paste(where, collapse = " AND ")) else "",
                " ORDER BY seq DESC LIMIT ?")
  db_query(con, sql, c(params, list(as.integer(limit))))
}
