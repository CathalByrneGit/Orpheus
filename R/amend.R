# ---------------------------------------------------------------------------
# Pipeline steps 6 and 8: human review of extraction and of analysis.
#
# Nothing is destructively overwritten. A correction inserts into edit_history
# and updates the row's status and amended_* fields, so the pair "the model
# said X, a person changed it to Y, here is who and when" survives -- which is
# both the audit story and the only way to measure extraction accuracy later.
# ---------------------------------------------------------------------------

#' @keywords internal
locate_instance <- function(con, instance_id) {
  row <- db_get_one(con,
    "SELECT instance_id, type_id, table_name, document_id FROM instance_index WHERE instance_id = ?",
    list(instance_id))
  if (is.null(row)) cli::cli_abort("No instance {.val {instance_id}}.")
  row
}

#' @keywords internal
read_instance_row <- function(con, table_name, instance_id) {
  db_get_one(con, sprintf("SELECT * FROM %s WHERE instance_id = ?",
                          DBI::dbQuoteIdentifier(con, table_name)), list(instance_id))
}

#' Confirm an instance
#'
#' The reviewer agrees with the extracted values as they stand.
#'
#' @param con A writable connection.
#' @param instance_id Instance identifier.
#' @param actor_id Actor confirming.
#' @return Invisibly, the instance id.
#' @export
orph_confirm_instance <- function(con, instance_id, actor_id) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  loc <- locate_instance(con, instance_id)
  before <- read_instance_row(con, loc$table_name, instance_id)

  with_tx(con, {
    DBI::dbExecute(con, sprintf(
      "UPDATE %s SET status = 'confirmed', amended_by = ?, amended_at = ? WHERE instance_id = ?",
      DBI::dbQuoteIdentifier(con, loc$table_name)),
      params = list(actor_id, orph_now(), instance_id))
    record_edit(con, loc$table_name, instance_id, loc$document_id, "confirm",
                previous = list(status = before$status), new = list(status = "confirmed"),
                actor_id = actor_id)
  })
  invisible(instance_id)
}

#' Amend an instance
#'
#' @param con A writable connection.
#' @param instance_id Instance identifier.
#' @param changes Named list of property values to set.
#' @param actor_id Actor making the change.
#' @param note Optional free text recorded with the change.
#' @return Invisibly, the instance id.
#' @export
orph_amend_instance <- function(con, instance_id, changes, actor_id, note = NULL) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  if (!is.list(changes) || length(changes) == 0) {
    cli::cli_abort("{.arg changes} must be a non-empty named list.")
  }
  loc    <- locate_instance(con, instance_id)
  bundle <- orph_active_bundle(con)
  ot     <- orph_object_type(bundle, loc$type_id)
  declared <- orph_property_ids(ot)

  unknown <- setdiff(names(changes), setdiff(declared, RESERVED_PROPS))
  if (length(unknown) > 0) {
    cli::cli_abort(c(
      "{.val {unknown}} {?is/are} not {?a/} declared propert{?y/ies} of {.val {loc$type_id}}.",
      i = "Propose a schema amendment instead -- adding a property changes the
           bundle, which is a separate review from correcting one row."
    ))
  }

  before <- read_instance_row(con, loc$table_name, instance_id)
  previous <- before[names(changes)]

  # A human correction is ground truth, so it takes the top rubric level and
  # source `human`. Leaving the model's confidence in place would mean a
  # corrected row still read as a machine guess.
  set_cols <- c(names(changes), "status", "source", "confidence", "amended_by", "amended_at")
  set_vals <- c(unname(lapply(changes, function(v) if (is.null(v)) NA else v)),
                list("amended", "human", unname(ORPH_CONFIDENCE[["explicit"]]),
                     actor_id, orph_now()))
  if ("name" %in% names(changes) && "naive_key" %in% declared) {
    set_cols <- c(set_cols, "naive_key")
    set_vals <- c(set_vals, list(orph_naive_key(changes$name)))
  }

  with_tx(con, {
    DBI::dbExecute(con, sprintf("UPDATE %s SET %s WHERE instance_id = ?",
      DBI::dbQuoteIdentifier(con, loc$table_name),
      paste(sprintf("%s = ?", DBI::dbQuoteIdentifier(con, set_cols)), collapse = ", ")),
      params = c(set_vals, list(instance_id)))
    record_edit(con, loc$table_name, instance_id, loc$document_id, "amend",
                previous = previous, new = changes, actor_id = actor_id, note = note)
    mark_dependent_evaluations_stale(con, instance_id,
      sprintf("Instance %s was amended.", instance_id))
  })
  invisible(instance_id)
}

#' Reject an instance
#'
#' The row is excluded from downstream use but never deleted -- a rejected
#' extraction is evidence about extraction quality.
#'
#' @param con A writable connection.
#' @param instance_id Instance identifier.
#' @param actor_id Actor rejecting.
#' @param note Why it was rejected.
#' @return Invisibly, the instance id.
#' @export
orph_reject_instance <- function(con, instance_id, actor_id, note = NULL) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  loc <- locate_instance(con, instance_id)
  before <- read_instance_row(con, loc$table_name, instance_id)

  with_tx(con, {
    DBI::dbExecute(con, sprintf(
      "UPDATE %s SET status = 'rejected', amended_by = ?, amended_at = ? WHERE instance_id = ?",
      DBI::dbQuoteIdentifier(con, loc$table_name)),
      params = list(actor_id, orph_now(), instance_id))
    record_edit(con, loc$table_name, instance_id, loc$document_id, "reject",
                previous = list(status = before$status), new = list(status = "rejected"),
                actor_id = actor_id, note = note)
    mark_dependent_evaluations_stale(con, instance_id,
      sprintf("Instance %s was rejected.", instance_id))
  })
  invisible(instance_id)
}

#' Set the review status of an edge
#'
#' @param con A writable connection.
#' @param edge_id Edge identifier.
#' @param status One of `confirmed`, `amended`, `rejected`.
#' @param actor_id Actor making the change.
#' @param link_type_id Optionally correct the link type (implies `amended`).
#' @param note Optional free text.
#' @return Invisibly, the edge id.
#' @export
orph_review_edge <- function(con, edge_id, status = c("confirmed", "rejected", "amended"),
                             actor_id, link_type_id = NULL, note = NULL) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  status <- match.arg(status)
  before <- db_get_one(con, "SELECT * FROM edges WHERE edge_id = ?", list(edge_id))
  if (is.null(before)) cli::cli_abort("No edge {.val {edge_id}}.")

  if (!is.null(link_type_id)) {
    bundle <- orph_active_bundle(con)
    if (is.null(orph_link_type(bundle, link_type_id))) {
      cli::cli_abort("{.val {link_type_id}} is not a link type in the active bundle.")
    }
    status <- "amended"
  }

  with_tx(con, {
    if (is.null(link_type_id)) {
      DBI::dbExecute(con,
        "UPDATE edges SET status = ?, amended_by = ?, amended_at = ? WHERE edge_id = ?",
        params = list(status, actor_id, orph_now(), edge_id))
    } else {
      DBI::dbExecute(con,
        "UPDATE edges SET status = ?, link_type_id = ?, source = 'human', confidence = ?,
                          amended_by = ?, amended_at = ? WHERE edge_id = ?",
        params = list(status, link_type_id, unname(ORPH_CONFIDENCE[["explicit"]]),
                      actor_id, orph_now(), edge_id))
    }
    record_edit(con, "edges", edge_id, before$document_id, status,
                previous = list(status = before$status, link_type_id = before$link_type_id),
                new = list(status = status, link_type_id = link_type_id %||% before$link_type_id),
                actor_id = actor_id, note = note)
  })
  invisible(edge_id)
}

# ---------------------------------------------------------------------------
# Document-level review state
# ---------------------------------------------------------------------------

#' Mark a document reviewed or unreviewed
#'
#' The open question in the architecture was whether review should be
#' per-instance only or also document-level. Both are implemented, because they
#' answer different questions: per-instance status says whether *this fact* has
#' been checked, and the document flag says whether *anyone has been through
#' the whole thing* -- which is what a reviewer scanning a queue needs.
#'
#' Marking a document reviewed does not confirm its instances. Conflating the
#' two would let one click silently promote every unchecked machine guess in
#' the document to confirmed.
#'
#' @param con A writable connection.
#' @param document_id Document identifier.
#' @param actor_id Actor marking it.
#' @param reviewed `TRUE` to mark reviewed, `FALSE` to reopen.
#' @return A list with the new state and how many instances remain unconfirmed.
#' @export
orph_mark_document_reviewed <- function(con, document_id, actor_id, reviewed = TRUE) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  doc <- orph_get_document(con, document_id)
  if (is.null(doc)) cli::cli_abort("No document {.val {document_id}}.")

  new_status <- if (isTRUE(reviewed)) "reviewed" else "unreviewed"
  with_tx(con, {
    DBI::dbExecute(con,
      "UPDATE documents SET review_status = ?, reviewed_by = ?, reviewed_at = ? WHERE document_id = ?",
      params = list(new_status, if (isTRUE(reviewed)) actor_id else NA_character_,
                    if (isTRUE(reviewed)) orph_now() else NA_character_, document_id))
    record_edit(con, "documents", document_id, document_id, "review_status",
                previous = list(review_status = doc$review_status),
                new = list(review_status = new_status), actor_id = actor_id)
  })

  outstanding <- orph_review_progress(con, document_id)
  list(document_id = document_id, review_status = new_status,
       unconfirmed_instances = outstanding$unconfirmed,
       note = if (isTRUE(reviewed) && outstanding$unconfirmed > 0)
         sprintf("Marked reviewed with %d instance(s) still unconfirmed.", outstanding$unconfirmed)
         else NULL)
}

#' Per-document review progress
#'
#' @param con A connection.
#' @param document_id Document identifier.
#' @return A list of counts by status.
#' @export
orph_review_progress <- function(con, document_id) {
  idx <- db_query(con,
    "SELECT instance_id, table_name FROM instance_index WHERE document_id = ?",
    list(document_id))
  counts <- c(unconfirmed = 0L, confirmed = 0L, amended = 0L, rejected = 0L)
  for (tbl in unique(idx$table_name)) {
    df <- db_query(con, sprintf(
      "SELECT status, COUNT(*) AS n FROM %s WHERE document_id = ? GROUP BY status",
      DBI::dbQuoteIdentifier(con, tbl)), list(document_id))
    for (i in seq_len(nrow(df))) {
      s <- df$status[[i]]
      if (s %in% names(counts)) counts[[s]] <- counts[[s]] + as.integer(df$n[[i]])
    }
  }
  edges <- db_query(con,
    "SELECT status, COUNT(*) AS n FROM edges WHERE document_id = ? GROUP BY status",
    list(document_id))
  as.list(c(counts, total = sum(counts),
            edges_total = sum(as.integer(edges$n %||% 0L))))
}

# ---------------------------------------------------------------------------
# Schema amendment candidates
# ---------------------------------------------------------------------------

#' Record a schema amendment candidate
#'
#' Repeat sightings increment a counter rather than queueing duplicates: a
#' property appearing in forty contracts is one decision, and its frequency is
#' the strongest argument for accepting it.
#'
#' @keywords internal
record_schema_amendment <- function(con, document_id, amendment_type, type_id, property_id,
                                    observed_value = "", inferred_type = "string",
                                    rationale = "", actor_id = NULL) {
  assert_writable(con)
  existing <- db_get_one(con,
    "SELECT amendment_id, occurrences FROM schema_amendments
     WHERE status = 'pending' AND amendment_type = ?
       AND COALESCE(type_id,'') = COALESCE(?,'') AND COALESCE(property_id,'') = COALESCE(?,'')",
    list(amendment_type, nullable(type_id), nullable(property_id)))

  if (!is.null(existing)) {
    DBI::dbExecute(con, "UPDATE schema_amendments SET occurrences = occurrences + 1 WHERE amendment_id = ?",
                   params = list(existing$amendment_id))
    return(invisible(existing$amendment_id))
  }

  id <- orph_id("samend")
  db_insert(con, "schema_amendments", list(
    amendment_id = id, document_id = nullable(document_id), amendment_type = amendment_type,
    type_id = nullable(type_id), property_id = nullable(property_id),
    observed_value = substr(observed_value %||% "", 1, 2000),
    inferred_type = inferred_type %||% "string", rationale = rationale %||% "",
    occurrences = 1L, status = "pending", proposed_at = orph_now()))
  record_edit(con, "schema_amendments", id, document_id, "schema_amendment_proposed",
              previous = NULL,
              new = list(amendment_type = amendment_type, type_id = type_id,
                         property_id = property_id), actor_id = actor_id)
  invisible(id)
}

#' The schema amendment review queue
#'
#' Separate from instance review, because accepting one changes the bundle
#' itself rather than a single row.
#'
#' @param con A connection.
#' @param status Filter by status, or `NULL` for all.
#' @return A data frame, most frequently seen first.
#' @export
orph_schema_amendments <- function(con, status = "pending") {
  if (is.null(status)) {
    db_query(con, "SELECT * FROM schema_amendments ORDER BY occurrences DESC, proposed_at")
  } else {
    db_query(con, "SELECT * FROM schema_amendments WHERE status = ?
                   ORDER BY occurrences DESC, proposed_at", list(status))
  }
}

#' Accept or reject a schema amendment candidate
#'
#' Accepting a `new_property` amendment adds the property to the active bundle,
#' registers the bundle at a new patch version and applies the resulting column
#' to the instance table. `new_type` and `new_link_type` candidates are not
#' auto-applied: a new object or link type needs properties, keys and a place
#' in the model that only a person can decide, so accepting one records the
#' decision and leaves the bundle edit to a deliberate registration.
#'
#' @param con A writable connection.
#' @param amendment_id Amendment identifier.
#' @param decision `"accepted"` or `"rejected"`.
#' @param actor_id Actor deciding.
#' @param note Optional free text.
#' @return A list describing what changed.
#' @export
orph_review_schema_amendment <- function(con, amendment_id,
                                         decision = c("accepted", "rejected"),
                                         actor_id, note = NULL) {
  assert_writable(con)
  assert_string(actor_id, "actor_id")
  decision <- match.arg(decision)
  am <- db_get_one(con, "SELECT * FROM schema_amendments WHERE amendment_id = ?",
                   list(amendment_id))
  if (is.null(am)) cli::cli_abort("No schema amendment {.val {amendment_id}}.")
  if (!identical(am$status, "pending")) {
    cli::cli_abort("Amendment {.val {amendment_id}} was already {am$status}.")
  }

  applied <- FALSE; new_version <- NA_character_
  with_tx(con, {
    DBI::dbExecute(con,
      "UPDATE schema_amendments SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
       WHERE amendment_id = ?",
      params = list(decision, actor_id, orph_now(), nullable(note), amendment_id))

    if (decision == "accepted" && identical(am$amendment_type, "new_property")) {
      bundle <- orph_active_bundle(con)
      idx <- which(vapply(bundle$object_types, function(ot) identical(ot$id, am$type_id), logical(1)))
      if (length(idx) == 1) {
        existing <- orph_property_ids(bundle$object_types[[idx]])
        if (!(am$property_id %in% existing)) {
          bundle$object_types[[idx]]$properties <- c(
            bundle$object_types[[idx]]$properties,
            list(list(id = am$property_id, type = am$inferred_type %||% "string",
                      nullable = TRUE,
                      description = paste("Accepted schema amendment:", am$rationale %||% ""),
                      source = list(column = am$property_id))))
          new_version <- bump_patch(bundle$version)
          bundle$version <- new_version
          orph_register_bundle(con, bundle, actor_id = actor_id, activate = TRUE)
          applied <- TRUE
        }
      }
    }

    record_edit(con, "schema_amendments", amendment_id, am$document_id,
                paste0("schema_amendment_", decision),
                previous = list(status = "pending"),
                new = list(status = decision, applied = applied,
                           bundle_version = new_version),
                actor_id = actor_id, note = note)
  })

  list(amendment_id = amendment_id, decision = decision, applied_to_bundle = applied,
       bundle_version = new_version,
       note = if (decision == "accepted" && !applied)
         "Recorded. This amendment type is not applied automatically -- register an updated bundle."
         else NULL)
}

#' @keywords internal
bump_patch <- function(version) {
  parts <- as.integer(strsplit(as.character(version), ".", fixed = TRUE)[[1]])
  if (length(parts) < 3 || any(is.na(parts))) return(paste0(version, ".1"))
  paste(parts[[1]], parts[[2]], parts[[3]] + 1L, sep = ".")
}

# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

#' Mark evaluations that depended on an instance as stale
#'
#' The point of recording evaluation dependencies is that this can be automatic:
#' an amended instance makes every interpretation built on it visibly out of
#' date, rather than quietly wrong.
#'
#' @keywords internal
mark_dependent_evaluations_stale <- function(con, instance_id, reason) {
  assert_writable(con)
  n <- DBI::dbExecute(con,
    "UPDATE concept_evaluations SET stale = 1, stale_reason = ?
     WHERE stale = 0 AND evaluation_id IN (
       SELECT evaluation_id FROM concept_evaluation_dependencies WHERE instance_id = ?)",
    params = list(reason, instance_id))
  invisible(n)
}
